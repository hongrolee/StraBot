"""박스권 매매 세트 검증기 (과거 1분봉으로 계산만 함, 실제 주문 없음)

세트: 박스(최근 L시간 동안 좁은 폭에서 위·아래를 오르내림)를 찾으면
      - 박스 아래쪽 구간에서 사고 (바로 사기 / 아래에서 한 번 튀어 오를 때 사기)
      - 박스 위쪽 구간(또는 가운데)에 닿으면 팜
      - (선택) 비트코인이 최근 24시간 BTC_DROP% 넘게 떨어지는 중이면 새로 사지 않음
      - 박스 아래 선보다 s% 더 내려가면 손절, 최대 보유 H시간이 지나면 정리
      - 판 뒤 박스가 살아 있으면 다시 반복
신호는 1시간봉(끝난 봉만)으로, 사고팔기는 15분봉으로 계산. 같은 15분봉에 손절·목표가 함께 닿으면 손절 먼저(보수적).
수수료 0.05%와 불리한 체결 0.1%를 사고팔 때 모두 뺌.

데이터: --source 1m  → data/spike_cache/*_90d_*.json (순간 급등 검증 때 받은 90일 1분봉, 업비트 요청 없음)
       --source 15m → 업비트 15분봉을 --days 만큼 받아 data/box_cache/ 에 저장 (다시 받지 않음)
실행:  venv\\Scripts\\python.exe tools\\box_backtest.py --source 15m --days 365 [--coins 50] [--workers 4]
       (15m은 뒤 --test-days 일을 '대 보는 기간'으로, 그 앞을 '고르는 기간'으로 씀)
결과:  콘솔 표 + data/box_backtest_result.json

과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.
"""
import argparse
import itertools
import json
import math
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / 'data' / 'spike_cache'
CACHE15 = ROOT / 'data' / 'box_cache'
RSI_MAX = 35          # 'rsi' 사기: 박스 아래 구간 + 1시간봉 RSI(14)가 이 값 이하일 때만
TREND_DAYS = 50       # --trend: 지금 가격이 최근 50일 평균 가격 위일 때만 새로 삼 (오르는 흐름 안의 박스만)
OUT = ROOT / 'data' / 'box_backtest_result.json'
FEE = 0.0005          # 업비트 수수료 (사고팔 때 각각)
SLIP = 0.001          # 원하는 값보다 불리하게 체결되는 정도 (각각)
BAR = 15              # 사고팔기 계산 단위(분)
PER_HOUR = 60 // BAR
TOUCH_ZONE = 0.2      # 박스 위·아래 '찍음'으로 보는 구간 (폭의 20%)
SPLIT = '2026-09-01'  # 1m(90일): 이 날 전으로 규칙을 고르고 이 날부터 대 봄 (15m은 --test-days로 정함)
BTC_DROP = 2.0        # 시장 조건: 비트코인 24시간 등락이 -2%보다 나쁘면 새로 사지 않음

GRID = {
    'lookback': [24, 48, 72, 168],             # 박스를 보는 기간(시간)
    'width': [(3, 8), (4, 12), (5, 15), (8, 20)],  # 박스 폭 % (최소, 최대)
    'zone': [0.2, 0.3],                        # 사는 구간 = 아래에서 폭의 z, 파는 구간 = 위에서 폭의 z
    'stop': [1, 2, 3],                         # 박스 아래 선보다 몇 % 더 내려가면 손절
    'entry': ['touch', 'bounce', 'rsi'],       # 바로 사기 / 튀어 오를 때 사기 / RSI 낮을 때만 바로 사기
    'hold': [96],                              # 최대 보유(시간) — 1차에서 48시간은 늘 96시간보다 못했음
    'exit': ['top', 'mid'],                    # 파는 곳: 위쪽 구간 / 박스 가운데
    'market': [False, True],                   # 비트코인이 떨어지는 중이면 안 사기
}


# ── 데이터 ──
def load_bars(path):
    """1분봉 → 빈 칸 없는 15분봉 {base, o, h, l, c} (첫 봉을 정시로 맞춤, 거래 없던 칸은 직전 종가)"""
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not raw:
        return None
    first = datetime.fromisoformat(raw[0]['t'])
    base = first.replace(minute=0, second=0, microsecond=0)
    agg = {}
    for b in raw:
        k = int((datetime.fromisoformat(b['t']) - base).total_seconds() // 60) // BAR
        a = agg.get(k)
        if a is None:
            agg[k] = [b['o'], b['h'], b['l'], b['c']]
        else:
            a[1] = max(a[1], b['h'])
            a[2] = min(a[2], b['l'])
            a[3] = b['c']
    n = max(agg) + 1
    n -= n % PER_HOUR                       # 끝난 시간까지만
    o, h, l, c = [], [], [], []
    prev = raw[0]['o']
    for k in range(n):
        a = agg.get(k)
        if a is None:
            a = [prev, prev, prev, prev]
        o.append(a[0]); h.append(a[1]); l.append(a[2]); c.append(a[3])
        prev = a[3]
    return {'base': base, 'o': o, 'h': h, 'l': l, 'c': c}


def load_15m(path):
    """받아 둔 15분봉 [{t,o,h,l,c}] → 빈 칸 없는 15분봉 dict (load_bars와 같은 모양)"""
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not raw:
        return None
    base = datetime.fromisoformat(raw[0]['t']).replace(minute=0, second=0, microsecond=0)
    agg = {int((datetime.fromisoformat(r['t']) - base).total_seconds() // 60) // BAR: r for r in raw}
    n = max(agg) + 1
    n -= n % PER_HOUR
    o, h, l, c = [], [], [], []
    prev = raw[0]['o']
    for k in range(n):
        r = agg.get(k)
        a = (r['o'], r['h'], r['l'], r['c']) if r else (prev, prev, prev, prev)
        o.append(a[0]); h.append(a[1]); l.append(a[2]); c.append(a[3])
        prev = a[3]
    return {'base': base, 'o': o, 'h': h, 'l': l, 'c': c}


def load_any(path):
    return load_15m(path) if '_15m_' in Path(path).name else load_bars(path)


def fetch_15m(pacer, market, days, on_page=None):
    """업비트 15분봉을 days일치 받아 data/box_cache/ 에 저장 (같은 날 받은 게 있으면 그대로)"""
    from datetime import timezone
    sys.path.insert(0, str(ROOT))
    from tools.spike_backtest import get
    CACHE15.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    path = CACHE15 / f"{market}_15m_{days}d_{end:%Y%m%d}.json"
    if path.exists():
        return path
    start = end - timedelta(days=days)
    rows, to = {}, end
    while to > start:
        chunk = get(pacer, 'https://api.upbit.com/v1/candles/minutes/15',
                    {'market': market, 'count': 200, 'to': to.strftime('%Y-%m-%dT%H:%M:%SZ')})
        if on_page:
            on_page()
        if not chunk:
            break
        for c in chunk:
            rows[c['candle_date_time_utc']] = {'t': c['candle_date_time_utc'], 'o': c['opening_price'], 'h': c['high_price'],
                                               'l': c['low_price'], 'c': c['trade_price'], 'v': c['candle_acc_trade_price']}
        oldest = datetime.fromisoformat(chunk[-1]['candle_date_time_utc']).replace(tzinfo=timezone.utc)
        if oldest >= to:
            break
        to = oldest
    cut = start.strftime('%Y-%m-%dT%H:%M:%S')
    path.write_text(json.dumps([rows[k] for k in sorted(rows) if k >= cut]), encoding='utf-8')
    return path


def rsi_hourly(C, n=14):
    """시간 j에 쓸 RSI(14): j 전까지 끝난 1시간 종가로 (와일더 방식, 모자라면 None)"""
    out = [None] * len(C)
    gain = loss = 0.0
    for i in range(1, len(C)):
        ch = C[i] - C[i - 1]
        if i <= n:
            gain += max(ch, 0)
            loss += max(-ch, 0)
            if i == n:
                gain /= n
                loss /= n
        else:
            gain = (gain * (n - 1) + max(ch, 0)) / n
            loss = (loss * (n - 1) + max(-ch, 0)) / n
        if i >= n and i + 1 < len(C):
            out[i + 1] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def trend_up(C, days=TREND_DAYS):
    """시간 j에 쓸 '오르는 흐름': j 전 마지막 종가가 그 전 days일(×24시간) 평균보다 높음 (자료가 모자라면 False)"""
    n = days * 24
    out = [False] * len(C)
    run_sum = 0.0
    for i, c in enumerate(C):
        run_sum += c
        if i >= n:
            run_sum -= C[i - n]
        if i + 1 >= n and i + 1 < len(C):
            out[i + 1] = c > run_sum / n
    return out


def hourly(b):
    """15분봉 → 1시간봉 (h, l, c)"""
    n = len(b['c']) // PER_HOUR
    H = [max(b['h'][j * PER_HOUR:(j + 1) * PER_HOUR]) for j in range(n)]
    L = [min(b['l'][j * PER_HOUR:(j + 1) * PER_HOUR]) for j in range(n)]
    C = [b['c'][(j + 1) * PER_HOUR - 1] for j in range(n)]
    return H, L, C


def _visits(flags):
    """연속된 True 묶음 수 (같은 구간에 머문 몇 시간은 한 번으로)"""
    k, prev = 0, False
    for f in flags:
        if f and not prev:
            k += 1
        prev = f
    return k


def box_series(H, L, C, lookback, wmin, wmax, strict=True):
    """시간 j의 박스 (그 전 lookback시간의 끝난 봉으로만) → (아래 선, 위 선) 또는 None

    strict: 폭이 wmin~wmax% + 위·아래를 각각 2번 이상 찍음 + 처음과 끝 종가 차이가 폭의 절반 이하(한쪽으로 흐르지 않음)
    strict=False: 기간 최고·최저만 (박스 확인 없이 비교용)
    """
    out = [None] * len(H)
    for j in range(lookback, len(H)):
        hs, ls = H[j - lookback:j], L[j - lookback:j]
        hi, lo = max(hs), min(ls)
        rng = hi - lo
        if lo <= 0 or rng <= 0:
            continue
        if strict:
            width = (hi / lo - 1) * 100
            if not (wmin <= width <= wmax):
                continue
            if _visits([x >= hi - TOUCH_ZONE * rng for x in hs]) < 2 or _visits([x <= lo + TOUCH_ZONE * rng for x in ls]) < 2:
                continue
            if abs(C[j - 1] - C[j - lookback]) > 0.5 * rng:
                continue
        out[j] = (lo, hi)
    return out


def net_return(buy, sell, fee=FEE, slip=SLIP):
    return sell * (1 - slip) * (1 - fee) / (buy * (1 + slip) * (1 + fee)) - 1


# ── 매매 ──
def _exit_step(b, m, stop, target):
    """보유 중 m번째 15분봉: 손절 먼저, 그다음 목표 → (가격, 이유) 또는 None"""
    o, h, l = b['o'][m], b['h'][m], b['l'][m]
    if o <= stop:
        return o, 'stop'            # 시작부터 손절선 아래 (갭)
    if l <= stop:
        return stop, 'stop'
    if o >= target:
        return o, 'target'
    if h >= target:
        return target, 'target'
    return None


def run(b, boxes, zone, stop_pct, entry, hold_h, fee=FEE, slip=SLIP, exit_at='top', allow=None):
    """박스 세트를 처음부터 끝까지 돌림 → 거래 목록 [(산 봉, 판 봉, 수익률, 이유, 산 값, 목표, 손절)]

    exit_at: 'top' = 위쪽 구간(위에서 폭의 zone)에서 팜, 'mid' = 박스 가운데에서 팜
    allow: 시간마다 새로 사도 되는지 (None = 늘 됨) — 시장 조건
    """
    o, h, l, c = b['o'], b['h'], b['l'], b['c']
    n = len(c)
    trades = []
    k, wait_until = PER_HOUR, 0
    while k < n:
        j = k // PER_HOUR
        box = boxes[j] if j < len(boxes) else None
        if box is None or k < wait_until or (allow is not None and not allow[j]):
            k += 1
            continue
        lo, hi = box
        rng = hi - lo
        target = lo + 0.5 * rng if exit_at == 'mid' else hi - zone * rng
        buy_level, stop = lo + zone * rng, lo * (1 - stop_pct / 100)
        fill, start = None, None
        if entry == 'touch':
            if l[k] <= buy_level and o[k] > stop:
                fill, start = min(o[k], buy_level), k
        elif k + 1 < n and min(l[k - 1], l[k]) <= buy_level and c[k] > c[k - 1] and stop < c[k] <= lo + 0.5 * rng and o[k + 1] > stop:
            fill, start = o[k + 1], k + 1
        if fill is None:
            k += 1
            continue
        # 산 봉 안에서도 손절선에 닿으면 손절 (목표는 순서를 알 수 없어 다음 봉부터)
        if l[start] <= stop:
            px, why, end = stop, 'stop', start
        else:
            px, why, end = None, None, None
            for m in range(start + 1, n):
                hit = _exit_step(b, m, stop, target)
                if hit:
                    px, why, end = hit[0], hit[1], m
                    break
                if m - start >= hold_h * PER_HOUR:
                    px, why, end = c[m], 'time', m
                    break
            if px is None:
                px, why, end = c[n - 1], 'end', n - 1
        trades.append((start, end, net_return(fill, px, fee, slip), why, fill, target, stop))
        # 손절 뒤에는 다음 시간(박스를 새로 계산)부터, 그 밖에는 다음 봉부터
        wait_until = (end // PER_HOUR + 1) * PER_HOUR if why == 'stop' else end + 1
        k = max(end + 1, wait_until)
    return trades


def run_fixed(b, starts, tp, sl, hold_h, fee=FEE, slip=SLIP):
    """정해진 봉에 사서 +tp / -sl / 보유 시간으로 파는 비교용 (아무 때나 사기)"""
    o, c, n = b['o'], b['c'], len(b['c'])
    out, busy = [], -1
    for s in sorted(starts):
        if s <= busy or s >= n:
            continue
        fill = o[s]
        stop, target = fill * (1 - sl), fill * (1 + tp)
        px, why, end = None, None, None
        for m in range(s, n):
            hit = _exit_step(b, m, stop, target) if m > s else (None if b['l'][m] > stop else (stop, 'stop'))
            if hit:
                px, why, end = hit[0], hit[1], m
                break
            if m - s >= hold_h * PER_HOUR:
                px, why, end = c[m], 'time', m
                break
        if px is None:
            px, why, end = c[n - 1], 'end', n - 1
        out.append((s, end, net_return(fill, px, fee, slip), why, fill, target, stop))
        busy = end
    return out


# ── 코인 하나: 모든 조합 ──
def combos():
    keys = list(GRID)
    return [dict(zip(keys, v)) for v in itertools.product(*GRID.values())]


def combo_key(p):
    return (f"L{p['lookback']}_W{p['width'][0]}-{p['width'][1]}_z{p['zone']}_s{p['stop']}_{p['entry']}_H{p['hold']}"
            f"_{p['exit']}{'_btc' if p['market'] else ''}")


_btc = {}


def btc_closes():
    """비트코인 1시간 종가 {그 시간 시작 시각: 종가} (프로세스마다 한 번만 읽음)"""
    if not _btc:
        f = next(iter(sorted(CACHE15.glob('KRW-BTC_15m_*.json'), reverse=True)), None) \
            or next(iter(sorted(CACHE.glob('KRW-BTC_90d_*.json'))), None)
        if f:
            b = load_any(f)
            _, _, C = hourly(b)
            _btc.update({b['base'] + timedelta(hours=j): c for j, c in enumerate(C)})
    return _btc


def market_allow(base, hours):
    """시간 j에 새로 사도 되는지: 끝난 1시간봉 기준 비트코인 24시간 등락이 -BTC_DROP%보다 나쁘지 않음 (자료가 없으면 허용)"""
    btc = btc_closes()
    out = []
    for j in range(hours):
        t = base + timedelta(hours=j)
        now, before = btc.get(t - timedelta(hours=1)), btc.get(t - timedelta(hours=25))
        out.append(not (now and before) or (now / before - 1) * 100 > -BTC_DROP)
    return out


def coin_job(path, split=None, trend=False):
    split = split or SPLIT
    b = load_any(path)
    if not b or len(b['c']) < 7 * 24 * PER_HOUR:        # 1주일도 안 되는 코인은 뺌
        return None
    H, L, C = hourly(b)
    boxes = {(lb, w): box_series(H, L, C, lb, *w) for lb in GRID['lookback'] for w in GRID['width']}
    mkt = market_allow(b['base'], len(H))
    rsi = rsi_hourly(C)
    low_rsi = [r is not None and r <= RSI_MAX for r in rsi]
    up = trend_up(C) if trend else [True] * len(C)
    allows = {(m, e): [(not m or a) and (e != 'rsi' or r) and u for a, r, u in zip(mkt, low_rsi, up)]
              for m in GRID['market'] for e in GRID['entry']}
    t = lambda k: (b['base'] + timedelta(minutes=BAR * k)).strftime('%Y-%m-%dT%H:%M')
    res = {}
    for p in combos():
        allow = allows[(p['market'], p['entry'])] if (p['market'] or p['entry'] == 'rsi' or trend) else None
        tr = run(b, boxes[(p['lookback'], p['width'])], p['zone'], p['stop'], 'touch' if p['entry'] == 'rsi' else p['entry'],
                 p['hold'], exit_at=p['exit'], allow=allow)
        res[combo_key(p)] = [(t(s), t(e), r, why) for s, e, r, why, *_ in tr]
    market = Path(path).name.split('_')[0]
    if len(b['c']) and b['base'] > datetime.fromisoformat(split) - timedelta(days=60):
        return None                                  # 고르는 기간 자료가 두 달도 안 되는 새 코인은 뺌
    first, last = b['c'][0], b['c'][-1]
    split_k = max(0, int((datetime.fromisoformat(split) - b['base']).total_seconds() // 60) // BAR)
    hold = {'all': last / first - 1,
            'train': b['c'][min(split_k, len(b['c']) - 1)] / first - 1,
            'test': last / b['c'][min(split_k, len(b['c']) - 1)] - 1}
    return {'market': market, 'trades': res, 'hold': hold, 'boxed_hours': {f'L{lb}_W{w[0]}-{w[1]}': sum(x is not None for x in v)
                                                                          for (lb, w), v in boxes.items()}, 'hours': len(H)}


def baseline_job(path, p, tp, sl, n_per_coin_seed, trend=False):
    """고른 규칙과 비교: 박스 확인 없이(기간 최고·최저만) / 아무 때나 사기(같은 거래 수, 같은 목표·손절 폭)"""
    b = load_any(path)
    H, L, C = hourly(b)
    t = lambda k: (b['base'] + timedelta(minutes=BAR * k)).strftime('%Y-%m-%dT%H:%M')
    loose = box_series(H, L, C, p['lookback'], 0, 1e9, strict=False)
    mkt = market_allow(b['base'], len(H))
    rsi = rsi_hourly(C)
    up = trend_up(C) if trend else [True] * len(C)
    allow = [(not p['market'] or a) and (p['entry'] != 'rsi' or (r is not None and r <= RSI_MAX)) and u for a, r, u in zip(mkt, rsi, up)]
    nobox = run(b, loose, p['zone'], p['stop'], 'touch' if p['entry'] == 'rsi' else p['entry'], p['hold'], exit_at=p['exit'], allow=allow)
    n_rand, seed = n_per_coin_seed
    rnd = random.Random(seed)
    starts = [rnd.randrange(p['lookback'] * PER_HOUR, len(b['c']) - 1) for _ in range(max(n_rand, 1) * 3)]
    rand = run_fixed(b, starts, tp, sl, p['hold'])[:max(n_rand, 0)]
    fmt = lambda tr: [(t(s), t(e), r, why) for s, e, r, why, *_ in tr]
    return {'market': Path(path).name.split('_')[0], 'nobox': fmt(nobox), 'random': fmt(rand)}


# ── 요약 ──
def stats(trades):
    """trades: [(산 때, 판 때, 수익률, 이유)]"""
    if not trades:
        return {'n': 0}
    rs = [x[2] * 100 for x in trades]
    weeks = {}
    for s, e, r, why in trades:
        wk = datetime.fromisoformat(e).strftime('%G-W%V')
        weeks[wk] = weeks.get(wk, 0) + r * 100
    sd = statistics.pstdev(rs) if len(rs) > 1 else 0
    return {'n': len(rs), 'win': sum(r > 0 for r in rs) / len(rs) * 100, 'avg': sum(rs) / len(rs), 'sum': sum(rs),
            'median': statistics.median(rs), 'worst': min(rs), 'best': max(rs),
            'stop': sum(x[3] == 'stop' for x in trades) / len(rs) * 100,
            'target': sum(x[3] == 'target' for x in trades) / len(rs) * 100,
            'weeks': len(weeks), 'weeks_neg': sum(v < 0 for v in weeks.values()),
            'score': (sum(rs) / len(rs)) / sd * math.sqrt(len(rs)) if sd else 0.0}


def split(trades):
    return [x for x in trades if x[0] < SPLIT], [x for x in trades if x[0] >= SPLIT]


def fmt_row(name, s):
    if not s.get('n'):
        return f"  {name:<34} 거래 없음"
    return (f"  {name:<34} {s['n']:>5}번  이김 {s['win']:5.1f}%  평균 {s['avg']:+6.2f}%  중간 {s['median']:+6.2f}%  "
            f"최악 {s['worst']:+7.2f}%  손절 {s['stop']:4.1f}%  손해 본 주 {s['weeks_neg']}/{s['weeks']}")


def main():
    ap = argparse.ArgumentParser(description='박스권 매매 세트 검증기 (과거 1분봉, 주문 없음)')
    ap.add_argument('--coins', type=int, default=50)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--source', choices=['1m', '15m'], default='1m')
    ap.add_argument('--days', type=int, default=365, help='15m: 받을 기간(일)')
    ap.add_argument('--test-days', type=int, default=120, help='15m: 뒤 몇 일을 대 보는 기간으로')
    ap.add_argument('--gap', type=float, default=0.12, help='요청 사이 간격(초)')
    ap.add_argument('--offline', action='store_true', help='15m: 받아 둔 15분봉(data/box_cache)만 씀, 업비트 요청 없음')
    ap.add_argument('--trend', action='store_true', help=f'최근 {TREND_DAYS}일 평균 가격 위일 때만 새로 삼')
    args = ap.parse_args()
    global SPLIT
    t0 = time.time()
    if args.source == '15m' and args.offline:
        latest = {}
        for f in sorted(CACHE15.glob(f'KRW-*_15m_{args.days}d_*.json')):
            latest[f.name.split('_')[0]] = f              # 같은 코인은 가장 최근에 받은 것
        files = sorted(latest.values())[:args.coins + 1]
        if not files:
            sys.exit('data/box_cache 에 받아 둔 15분봉이 없어요 (--offline 없이 먼저 받아 주세요)')
        made = max(datetime.strptime(f.stem.split('_')[-1], '%Y%m%d') for f in files)
        SPLIT = (made - timedelta(days=args.test_days)).strftime('%Y-%m-%d')
    elif args.source == '15m':
        sys.path.insert(0, str(ROOT))
        from tools.spike_backtest import Pacer, top_markets
        pacer = Pacer(args.gap)
        markets = top_markets(pacer, args.coins)
        if 'KRW-BTC' not in markets:
            markets.append('KRW-BTC')                 # 시장 조건용
        per = args.days * 96 // 200 + 1
        done = {'pages': 0}
        total = per * len(markets)
        print(f'업비트 15분봉 {args.days}일 받기: 코인 {len(markets)}종 · 요청 약 {total}번', flush=True)

        def tick():
            done['pages'] += 1
            if done['pages'] % 50 == 0:
                el = time.time() - t0
                print(f'  받는 중 {done["pages"]}/{total} · 경과 {el:.0f}초 · 남은 약 {el / done["pages"] * (total - done["pages"]):.0f}초', flush=True)
        files = []
        for i, m in enumerate(markets, 1):
            f = fetch_15m(pacer, m, args.days, tick)
            files.append(f)
        files = [f for f in files if Path(f).name.split('_')[0] != 'KRW-BTC' or 'KRW-BTC' in markets[:args.coins]]
        SPLIT = (datetime.now() - timedelta(days=args.test_days)).strftime('%Y-%m-%d')
    else:
        files = sorted(CACHE.glob('KRW-*_90d_*.json'))[:args.coins]
        if not files:
            sys.exit('data/spike_cache 에 90일 1분봉이 없어요 (tools/spike_backtest.py 로 먼저 받아 주세요)')
    print(f'코인 {len(files)}종 · 조합 {len(combos())}개 · {SPLIT} 전으로 고르고 그 뒤에 대 봄'
          f"{f' · 최근 {TREND_DAYS}일 평균 위일 때만' if args.trend else ''}", flush=True)
    coins = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(coin_job, str(f), SPLIT, args.trend): f for f in files}
        for i, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            if r:
                coins.append(r)
            el = time.time() - t0
            print(f'[{i}/{len(files)}] {Path(futs[fu]).name.split("_")[0]:<14} 경과 {el:5.0f}초 · 남은 약 {el / i * (len(files) - i):5.0f}초', flush=True)

    # 조합마다 고른 기간 성적 → 가장 나은 규칙 (거래 30번 이상, 점수 = 평균 ÷ 흩어짐 × √거래 수)
    table = []
    for key in coins[0]['trades']:
        allt = sorted(x for c in coins for x in c['trades'][key])
        tr, te = split(allt)
        table.append({'key': key, 'train': stats(tr), 'test': stats(te), 'all': stats(allt)})
    ok = [r for r in table if r['train'].get('n', 0) >= 30]
    ok.sort(key=lambda r: r['train']['score'], reverse=True)
    both = [r for r in ok if r['train']['avg'] > 0 and r['test'].get('n', 0) >= 30 and r['test']['avg'] > 0]
    print(f"\n규칙 {len(ok)}개 중 고르는 기간 플러스 {sum(r['train']['avg'] > 0 for r in ok)}개 · "
          f"대 보는 기간 플러스 {sum(r['test'].get('avg', 0) > 0 for r in ok)}개 · 둘 다 플러스 {len(both)}개")
    print('\n── 고른 기간 성적 상위 10개 규칙 → 대 본 기간 ──')
    for r in ok[:10]:
        print(r['key'])
        print(fmt_row('   고른 기간', r['train']))
        print(fmt_row('   대 본 기간', r['test']))
    best = ok[0] if ok else None
    if not best:
        sys.exit('거래가 충분한 규칙이 없어요')

    # 가장 나은 규칙과 비교 대상
    p = next(x for x in combos() if combo_key(x) == best['key'])
    best_by_coin = {c['market']: c['trades'][best['key']] for c in coins}
    # 아무 때나 사기: 같은 목표·손절 폭. 박스에서는 폭의 (1-2z)만큼 오르면 목표, 폭의 z + 아래 선의 s% 내려가면 손절 → 폭 가운데값으로 근사
    wmid = sum(p['width']) / 2 / 100
    tp = wmid * ((0.5 - p['zone']) if p['exit'] == 'mid' else (1 - 2 * p['zone']))
    sl = wmid * p['zone'] + p['stop'] / 100
    base = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(baseline_job, str(f), p, tp, sl, (len(best_by_coin.get(Path(f).name.split('_')[0], [])), i), args.trend)
                for i, f in enumerate(files)]
        for fu in as_completed(futs):
            base.append(fu.result())
    nobox = sorted(x for r in base for x in r['nobox'])
    rand = sorted(x for r in base for x in r['random'])
    allb = sorted(x for v in best_by_coin.values() for x in v)
    hold_all = [c['hold'] for c in coins]

    print(f"\n── 가장 나은 규칙: {best['key']} ──")
    for name, tr in [('이 규칙', allb), ('박스 확인 없이 (기간 최고·최저만)', nobox), (f'아무 때나 사기 (+{tp*100:.1f}% / -{sl*100:.1f}%)', rand)]:
        a, b_ = split(tr)
        print(name)
        print(fmt_row('   고른 기간', stats(a)))
        print(fmt_row('   대 본 기간', stats(b_)))
    print(f"  그냥 들고 있기 (코인 평균)  고른 기간 {statistics.mean(h['train'] for h in hold_all) * 100:+.1f}%  "
          f"대 본 기간 {statistics.mean(h['test'] for h in hold_all) * 100:+.1f}%")

    # 분기별: 이 규칙 vs 비트코인 등락 (시장이 오를 때·내릴 때 어땠는지)
    btc = btc_closes()
    print('\n── 분기별 (이 규칙 · 비트코인) ──')
    quarters = {}
    for x in allb:
        d = datetime.fromisoformat(x[0])
        quarters.setdefault(f'{d.year}-Q{(d.month - 1) // 3 + 1}', []).append(x)
    qrows = {}
    for q, v in sorted(quarters.items()):
        y, n = int(q[:4]), int(q[-1])
        a, z = datetime(y, 3 * n - 2, 1), datetime(y + (n == 4), (3 * n) % 12 + 1, 1)
        ks = sorted(k for k in btc if a <= k < z)
        bchg = (btc[ks[-1]] / btc[ks[0]] - 1) * 100 if ks else None
        qrows[q] = {**stats(v), 'btc': bchg}
        print(fmt_row(f"{q} (BTC {bchg:+.0f}%)" if bchg is not None else q, stats(v)))

    per_coin = sorted(((m, stats(v)) for m, v in best_by_coin.items() if v), key=lambda x: x[1]['sum'], reverse=True)
    print('\n── 코인별 (이 규칙, 전체 기간) ──')
    for m, s in per_coin[:5] + [('...', {'n': 0})] + per_coin[-5:]:
        print(fmt_row(m, s) if s.get('n') else '  ...')

    OUT.write_text(json.dumps({
        'meta': {'made': datetime.now().isoformat(timespec='seconds'), 'coins': len(coins), 'split': SPLIT, 'fee': FEE, 'slip': SLIP,
                 'source': args.source, 'days': args.days, 'trend': args.trend,
                 'grid': {k: v for k, v in GRID.items()}, 'best': best['key'], 'params': p, 'random_tp_sl': [tp, sl]},
        'top': ok[:20],
        'best': {'all': stats(allb), 'train': stats(split(allb)[0]), 'test': stats(split(allb)[1])},
        'quarters': qrows,
        'breadth': {'rules': len(ok), 'train_pos': sum(r['train']['avg'] > 0 for r in ok),
                    'test_pos': sum(r['test'].get('avg', 0) > 0 for r in ok), 'both_pos': len(both)},
        'compare': {'nobox': {'train': stats(split(nobox)[0]), 'test': stats(split(nobox)[1])},
                    'random': {'train': stats(split(rand)[0]), 'test': stats(split(rand)[1])},
                    'hold': {'train': statistics.mean(h['train'] for h in hold_all) * 100, 'test': statistics.mean(h['test'] for h in hold_all) * 100}},
        'per_coin': [{'market': m, **s} for m, s in per_coin],
        'trades': [{'market': m, 'buy_t': s, 'sell_t': e, 'ret': r * 100, 'why': why} for m, v in best_by_coin.items() for s, e, r, why in v],
    }, ensure_ascii=False, default=list), encoding='utf-8')
    print(f'\n저장: {OUT.relative_to(ROOT)} · 걸린 시간 {time.time() - t0:.0f}초')


if __name__ == '__main__':
    main()
