"""순간 급등 올라타기 검증기 (과거 1분봉으로 계산만 함, 실제 주문 없음)

규칙: 1분 거래대금이 평소(직전 60분 평균)의 N배 이상 + 그 1분에 X% 이상 오르면
      다음 1분 시작 가격에 사고, 아래 중 먼저 오는 것에 팜
        - 최고가에서 트레일링 % 내려옴 / 손절 % / 목표 % / 보유 시간 초과
수수료(0.05%씩)와 미끄러짐(0.2%씩)을 사고팔 때 모두 뺌.

실행:  venv\\Scripts\\python.exe tools\\spike_backtest.py [--days 30] [--coins 30]
결과:  콘솔 표 + data/spike_backtest.csv  (받은 1분봉은 data/spike_cache/ 에 저장해 다시 받지 않음)

과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.
"""
import argparse
import collections
import csv
import itertools
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEE = 0.0005          # 업비트 수수료 (사고팔 때 각각)
SLIP = 0.002          # 급등 중 원하는 값보다 불리하게 체결되는 정도 (각각)
AVG_WINDOW = 60       # '평소' = 직전 60개 1분봉 평균 거래대금
MIN_VALUE = 10_000_000  # 1분 거래대금 1천만원 미만은 무시 (거래가 거의 없는 코인의 튐)

GRID = {
    'mult': [3, 5, 10],          # 평소 대비 몇 배
    'rise': [1, 2, 3],           # 그 1분에 몇 % 상승
    'trail': [1.5, 3, 5],        # 최고가에서 몇 % 내려오면 팜
    'hold': [30, 60],            # 최대 보유 시간(분)
}
STOP = 3.0            # 손절 %
TARGET = 15.0         # 목표 %


# ── 계산 ──
_signal_cache = {}


def signal_bars(bars, mult, rise, min_value=MIN_VALUE):
    """사는 신호가 난 봉 번호들 (평소 거래대금은 누적합으로 한 번에 계산, 같은 자료·기준이면 다시 쓰기)"""
    key = (id(bars), len(bars), mult, rise, min_value)
    if key in _signal_cache:
        return _signal_cache[key]
    prefix = [0.0]
    for b in bars:
        prefix.append(prefix[-1] + b['v'])
    out = []
    for i in range(AVG_WINDOW, len(bars)):
        b = bars[i]
        avg = (prefix[i] - prefix[i - AVG_WINDOW]) / AVG_WINDOW
        if avg > 0 and b['v'] >= min_value and b['v'] >= avg * mult and b['o'] and (b['c'] / b['o'] - 1) * 100 >= rise:
            out.append(i)
    _signal_cache[key] = out
    return out


def simulate(bars, mult, rise, trail, hold, stop=STOP, target=TARGET, fee=FEE, slip=SLIP, min_value=MIN_VALUE):
    """bars: 오래된 순 [{'o','h','l','c','v'}]. 반환: 거래 목록 [{'i','entry','exit','pct','why','mins'}]"""
    trades = []
    n = len(bars)
    signals = signal_bars(bars, mult, rise, min_value)
    s = 0
    i = AVG_WINDOW
    while True:
        # 판 뒤(i) 이후의 첫 신호
        while s < len(signals) and signals[s] < i:
            s += 1
        if s >= len(signals) or signals[s] >= n - 1:
            break
        i = signals[s]
        # 봉이 끝난 뒤 알아채고 다음 봉 시작 가격에 삼
        entry = bars[i + 1]['o'] * (1 + slip)
        peak = entry
        exit_price = why = None
        j = i + 1
        while j < n:
            x = bars[j]
            stop_at = entry * (1 - stop / 100)
            trail_at = peak * (1 - trail / 100)
            floor = max(stop_at, trail_at)
            # 한 봉 안에서 순서를 모르면 불리한 쪽(먼저 내려감)으로 봄
            if x['l'] <= floor:
                exit_price = min(floor, x['o'])
                why = '손절' if stop_at >= trail_at else '트레일링'
                break
            if x['h'] >= entry * (1 + target / 100):
                exit_price = max(entry * (1 + target / 100), x['o'])
                why = '목표'
                break
            peak = max(peak, x['h'])
            if j - i >= hold:
                exit_price = x['c']
                why = '시간'
                break
            j += 1
        if exit_price is None:
            break                       # 자료 끝: 끝나지 않은 거래는 뺌
        sell = exit_price * (1 - slip)
        pct = (sell * (1 - fee)) / (entry * (1 + fee)) * 100 - 100
        trades.append({'i': i, 'entry': entry, 'exit': sell, 'pct': pct, 'why': why, 'mins': j - i})
        i = j + 1                       # 판 다음부터 다시 봄
    return trades


# ── 가진 코인이 치솟을 때 일부 팔기: 그냥 들고 있을 때와 비교 ──
SELL_GRID = {
    'mult': [3, 5, 10],          # 평소 대비 몇 배
    'rise': [2, 3, 5],           # 그 1분에 몇 % 상승
    'pullback': [0, 2, 3],       # 0 = 알아챈 즉시 팜, 2·3 = 치솟은 뒤 최고가에서 몇 % 꺾이면 팜 (최대 30분 기다림)
}
HORIZONS = [60, 360, 1440]      # 판 뒤 1시간·6시간·24시간 뒤 가격과 비교
COOLDOWN = 60                   # 한 번 판 뒤 60분은 같은 급등으로 봄 (다시 안 팜)
PULLBACK_WAIT = 30


def spike_sells(bars, mult, rise, pullback, fee=FEE, slip=SLIP, min_value=MIN_VALUE, horizons=HORIZONS):
    """급등 때 판 경우들. 반환 [{'i','sell','vs': {분: 들고 있을 때보다 나은 %}}]
    vs[h] > 0 이면 팔아 둔 돈이 h분 뒤 코인 값어치보다 많음 (파는 게 나았음)"""
    out = []
    n = len(bars)
    last = -10 ** 9
    for i in signal_bars(bars, mult, rise, min_value):
        if i - last < COOLDOWN or i + 1 >= n:
            continue
        if pullback:
            # 다음 봉부터 최고가를 따라가다 pullback% 꺾이면 팜 (30분 안에 안 꺾이면 그때 종가)
            peak = bars[i]['c']
            j, price = i + 1, None
            while j < n and j - i <= PULLBACK_WAIT:
                x = bars[j]
                if x['l'] <= peak * (1 - pullback / 100):
                    price = min(peak * (1 - pullback / 100), x['o'])
                    break
                peak = max(peak, x['h'])
                j += 1
            if price is None:
                if j >= n:
                    break
                price = bars[j]['c']
        else:
            j, price = i + 1, bars[i + 1]['o']
        sold = price * (1 - slip) * (1 - fee)
        vs = {h: (sold / bars[j + h]['c'] - 1) * 100 for h in horizons if j + h < n}
        if len(vs) < len(horizons):
            break                        # 자료 끝: 비교할 수 없는 경우는 뺌
        out.append({'i': i, 'sell_i': j, 'sell': sold, 'vs': vs})
        last = i
    return out


def summarize_sells(events, h):
    """h분 뒤 기준: 파는 게 나았던 비율·평균·중간값·상위 3번 뺀 평균"""
    v = sorted((e['vs'][h] for e in events), reverse=True)
    if not v:
        return {'n': 0, 'better': 0.0, 'avg': 0.0, 'median': 0.0, 'avg_wo_top3': 0.0}
    rest = v[3:] or [0.0]
    return {'n': len(v), 'better': sum(x > 0 for x in v) / len(v) * 100, 'avg': sum(v) / len(v),
            'median': v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2,
            'avg_wo_top3': sum(rest) / len(rest)}


# ── 급등 때 팔고 다시 사기: 코인 개수가 늘었나 ──
REBUY_RULES = [('time', h) for h in (60, 120, 240, 360, 720)] + [('dip', d) for d in (3, 5, 8)] + [('bounce', r) for r in (2, 3)]
REBUY_MAX_WAIT = 1440           # 가격·반등 기준이 24시간 안에 안 오면 그때 가격에 다시 삼
SELL_SETS = [dict(mult=3, rise=3, pullback=0), dict(mult=5, rise=5, pullback=3), dict(mult=3, rise=2, pullback=0)]


def rebuy(bars, sell_i, sold_price, rule, param, fee=FEE, slip=SLIP, max_wait=REBUY_MAX_WAIT):
    """sell_i 봉에서 sold_price(수수료 전)에 판 뒤 다시 사기. 반환 (다시 산 봉, 코인 개수 변화 %, 기다린 분, 시간초과?)
    코인 개수 변화 = 판 돈으로 다시 산 코인 수 / 판 코인 수 − 1"""
    n = len(bars)
    got = sold_price * (1 - slip) * (1 - fee)
    if rule == 'time':
        j = sell_i + param
        if j >= n:
            return None
        pay = bars[j]['c'] * (1 + slip) * (1 + fee)
        return j, (got / pay - 1) * 100, param, False
    low = sold_price
    for j in range(sell_i + 1, min(n, sell_i + max_wait + 1)):
        x = bars[j]
        if rule == 'dip':
            level = sold_price * (1 - param / 100)
            if x['l'] <= level:                      # 예약 주문: 미끄러짐 없이 그 값(더 낮게 열리면 그 값)에 삼
                pay = min(level, x['o']) * (1 + fee)
                return j, (got / pay - 1) * 100, j - sell_i, False
        else:                                        # bounce: 판 뒤 최저가에서 param% 오르면 삼
            if x['h'] >= low * (1 + param / 100) and j > sell_i + 1:
                pay = max(low * (1 + param / 100), x['o']) * (1 + slip) * (1 + fee)
                return j, (got / pay - 1) * 100, j - sell_i, False
            low = min(low, x['l'])
    j = sell_i + max_wait
    if j >= n:
        return None
    pay = bars[j]['c'] * (1 + slip) * (1 + fee)
    return j, (got / pay - 1) * 100, max_wait, True


def round_trips(bars, sells, rule, param):
    """sells: [(판 봉, 판 가격)] 오래된 순. 다시 사기 전에는 다음 팔기를 하지 않음"""
    out, free_at = [], -1
    for sell_i, price in sells:
        if sell_i <= free_at:
            continue
        r = rebuy(bars, sell_i, price, rule, param)
        if r is None:
            break
        j, gain, waited, timeout = r
        out.append({'sell_i': sell_i, 'gain': gain, 'waited': waited, 'timeout': timeout})
        free_at = j
    return out


def summarize_trips(trips):
    g = sorted((t['gain'] for t in trips), reverse=True)
    if not g:
        return {'n': 0, 'more': 0.0, 'avg': 0.0, 'median': 0.0, 'avg_wo_top3': 0.0, 'wait_h': 0.0, 'timeout': 0.0}
    rest = g[3:] or [0.0]
    return {'n': len(g), 'more': sum(x > 0 for x in g) / len(g) * 100, 'avg': sum(g) / len(g), 'median': g[len(g) // 2],
            'avg_wo_top3': sum(rest) / len(rest), 'wait_h': sum(t['waited'] for t in trips) / len(g) / 60,
            'timeout': sum(t['timeout'] for t in trips) / len(g) * 100}


def report_rebuys(data):
    """급등 때 팔고 다시 사기 비교 + 아무 때나(매시 정각) 팔고 같은 방법으로 다시 사기(비교용)"""
    rows = []
    for sp in SELL_SETS:
        # spike_sells의 'sell'은 fee=0, slip=0으로 부르면 수수료 전 판 가격
        sells = {m: [(e['sell_i'], e['sell']) for e in spike_sells(bars, **sp, fee=0, slip=0)] for m, bars in data.items()}
        for rule, param in REBUY_RULES:
            trips = [t for m, bars in data.items() for t in round_trips(bars, sells[m], rule, param)]
            rows.append({'sell': sp, 'rule': rule, 'param': param, **summarize_trips(trips)})
    for rule, param in REBUY_RULES:
        trips = []
        for bars in data.values():
            random_sells = [(i, bars[i]['o']) for i in range(AVG_WINDOW, len(bars), 60)]
            trips += round_trips(bars, random_sells, rule, param)
        rows.append({'sell': 'random', 'rule': rule, 'param': param, **summarize_trips(trips)})
    return rows


# ── 꼬리(가끔 크게 잃는 경우) 줄이기: 나눠 다시 사기 · 되사기 손절선 · 걸러내기 ──
SPLIT_AT = '2026-09-01'          # 이 날 전(7~8월)으로 규칙을 고르고, 이후(9월)에 그대로 대 봄 (UTC)
IMPROVE_SELL = dict(mult=5, rise=5, pullback=3)
PLANS = {                         # 이름: (다시 살 가격들 = 판 가격 대비 −%, 되사기 손절선 +% 또는 None)
    '지금 규칙 (−5%, 24시간)': ([5], None),
    '−5% · 손절선 +5%': ([5], 5),
    '−5% · 손절선 +8%': ([5], 8),
    '−5% · 손절선 +10%': ([5], 10),
    '−3% · 손절선 +8%': ([3], 8),
    '나눠 사기 −3/−5/−8%': ([3, 5, 8], None),
    '나눠 사기 −3/−5/−8% · 손절선 +8%': ([3, 5, 8], 8),
}


def rebuy_plan(bars, sell_i, sold_price, dips, cap=None, fee=FEE, slip=SLIP, max_wait=REBUY_MAX_WAIT):
    """판 몫을 len(dips)등분해 '판 가격 −d%'마다 하나씩 다시 삼 (예약 주문, 미끄러짐 없음).
    cap: 판 가격보다 +cap% 오르면 남은 몫을 그 자리에서 모두 삼 (한 봉에서 둘 다 닿으면 불리한 cap 쪽으로 봄).
    max_wait 분 안에 다 못 사면 남은 몫은 그때 가격에 삼.
    반환 {'j': 끝난 봉, 'gain': 코인 수 변화 %, 'waited': 분, 'how': 'dip'|'cap'|'timeout'|'mixed'} 또는 None(자료 끝)"""
    n = len(bars)
    got = sold_price * (1 - slip) * (1 - fee)           # 판 돈 (코인 1개를 팔았다고 봄)
    part = got / len(dips)
    left = sorted(dips)                                 # 가까운 가격부터
    coins, hows = 0.0, set()
    for j in range(sell_i + 1, min(n, sell_i + max_wait + 1)):
        x = bars[j]
        if cap is not None and x['h'] >= sold_price * (1 + cap / 100):
            pay = max(sold_price * (1 + cap / 100), x['o']) * (1 + slip) * (1 + fee)
            coins += part * len(left) / pay
            hows.add('cap')
            return {'j': j, 'gain': (coins - 1) * 100, 'waited': j - sell_i, 'how': 'cap' if len(hows) == 1 else 'mixed'}
        while left and x['l'] <= sold_price * (1 - left[0] / 100):
            level = sold_price * (1 - left.pop(0) / 100)
            coins += part / (min(level, x['o']) * (1 + fee))
            hows.add('dip')
        if not left:
            return {'j': j, 'gain': (coins - 1) * 100, 'waited': j - sell_i, 'how': 'dip'}
    j = sell_i + max_wait
    if j >= n:
        return None
    coins += part * len(left) / (bars[j]['c'] * (1 + slip) * (1 + fee))
    hows.add('timeout')
    return {'j': j, 'gain': (coins - 1) * 100, 'waited': max_wait, 'how': 'timeout' if len(hows) == 1 else 'mixed'}


def spike_features(bars, e, btc_close=None, coin_value=0.0):
    """급등 한 번의 특징 (걸러내기·손실 원인 분석용). 결과를 보기 전에 알 수 있는 값만."""
    i = e['i']
    b = bars[i]
    f = {'rise_1m': (b['c'] / b['o'] - 1) * 100 if b['o'] else 0.0,
         'rise_1h': (b['c'] / bars[i - 60]['c'] - 1) * 100 if i >= 60 and bars[i - 60]['c'] else 0.0,
         'hour_kst': (int(b['t'][11:13]) + 9) % 24,
         'coin_value': coin_value,
         'btc_1h': None}
    if btc_close is not None:
        t = b['t']
        t0 = (datetime.fromisoformat(t) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')
        if t in btc_close and t0 in btc_close and btc_close[t0]:
            f['btc_1h'] = (btc_close[t] / btc_close[t0] - 1) * 100
    return f


FILTERS = {                       # 이름: 건너뛸 급등 (True면 팔지 않음)
    '걸러내기 없음': lambda f: False,
    '1시간에 이미 +15% 넘게 오른 급등은 건너뜀': lambda f: f['rise_1h'] > 15,
    '1시간에 이미 +25% 넘게 오른 급등은 건너뜀': lambda f: f['rise_1h'] > 25,
    '거래 규모 하위 절반 코인은 건너뜀': lambda f: f['coin_small'],
    '비트코인도 1시간에 +1% 넘게 오를 때는 건너뜀': lambda f: f['btc_1h'] is not None and f['btc_1h'] > 1,
    # 손실 원인 분석에서 나온 반대 방향: 이미 크게 부풀어 오른 급등에서만 판다 (7~8월로 기준을 고르고 9월에 확인)
    '1시간 상승 +8% 미만 급등은 건너뜀': lambda f: f['rise_1h'] < 8,
    '1시간 상승 +10% 미만 급등은 건너뜀': lambda f: f['rise_1h'] < 10,
}


def improve_events(data):
    """코인별 급등(팔기) + 특징. 반환 {코인: [ {i, sell_i, sell, f} ]}"""
    btc = data.get('KRW-BTC')
    btc_close = {btc[k]['t']: btc[k]['c'] for k in range(len(btc))} if btc is not None else None
    values = {m: sum(b.cols['v']) / max(len(b) / 1440, 1) if hasattr(b, 'cols') else sum(x['v'] for x in b) / max(len(b) / 1440, 1)
              for m, b in data.items()}
    median_value = sorted(values.values())[len(values) // 2]
    out = {}
    for m, bars in data.items():
        evs = []
        for e in spike_sells(bars, **IMPROVE_SELL, fee=0, slip=0, horizons=[]):
            f = spike_features(bars, e, btc_close, values[m])
            f['coin_small'] = values[m] < median_value
            evs.append({**e, 'f': f, 't': bars[e['sell_i']]['t']})
        out[m] = evs
    return out


def plan_trips(bars, events, dips, cap, skip=lambda f: False):
    """다시 사기 전에는 다음 팔기를 하지 않음 (round_trips 와 같은 규칙)"""
    out, free_at = [], -1
    for e in events:
        if e['sell_i'] <= free_at or skip(e['f']):
            continue
        r = rebuy_plan(bars, e['sell_i'], e['sell'], dips, cap)
        if r is None:
            break
        out.append({**r, 't': e['t'], 'f': e['f']})
        free_at = r['j']
    return out


def tail_summary(trips, portion=0.3):
    """꾸준함 지표: 늘어난 비율·평균·최악·하위 10% 평균, 그리고 보유의 portion 만 판다고 쳤을 때 주별 결과"""
    g = sorted(t['gain'] for t in trips)
    if not g:
        return {'n': 0, 'more': 0.0, 'avg': 0.0, 'worst': 0.0, 'low10': 0.0, 'weeks': 0, 'weeks_neg': 0, 'worst_week': 0.0}
    k = max(1, len(g) // 10)
    weeks = collections.defaultdict(float)
    for t in trips:
        d = datetime.fromisoformat(t['t'])
        weeks[d.isocalendar()[:2]] += portion * t['gain']
    return {'n': len(g), 'more': sum(x > 0 for x in g) / len(g) * 100, 'avg': sum(g) / len(g), 'worst': g[0],
            'low10': sum(g[:k]) / k, 'weeks': len(weeks), 'weeks_neg': sum(v < 0 for v in weeks.values()),
            'worst_week': min(weeks.values())}


def report_improve(data):
    events = improve_events(data)
    rows = []
    for fname, skip in FILTERS.items():
        for pname, (dips, cap) in PLANS.items():
            trips = [t for m, bars in data.items() for t in plan_trips(bars, events[m], dips, cap, skip)]
            train = [t for t in trips if t['t'] < SPLIT_AT]
            test = [t for t in trips if t['t'] >= SPLIT_AT]
            rows.append({'filter': fname, 'plan': pname, 'all': tail_summary(trips),
                         'train': tail_summary(train), 'test': tail_summary(test),
                         'how': dict(collections.Counter(t['how'] for t in trips))})
    # 손실 원인: 지금 규칙에서 코인이 줄어든 경우 vs 늘어난 경우의 특징 평균
    base = [t for m, bars in data.items() for t in plan_trips(bars, events[m], [5], None)]
    prof = {}
    for key in ('rise_1m', 'rise_1h', 'hour_kst', 'btc_1h', 'coin_value'):
        for side, sel in (('줄어듦', [t for t in base if t['gain'] < 0]), ('늘어남', [t for t in base if t['gain'] >= 0])):
            vals = [t['f'][key] for t in sel if t['f'][key] is not None]
            prof[(key, side)] = (sum(vals) / len(vals) if vals else None, len(vals))
    small = {side: sum(t['f']['coin_small'] for t in sel) / max(len(sel), 1) * 100
             for side, sel in (('줄어듦', [t for t in base if t['gain'] < 0]), ('늘어남', [t for t in base if t['gain'] >= 0]))}
    return rows, prof, small


def print_improve(data):
    rows, prof, small = report_improve(data)
    print('\n[손실 원인] 지금 규칙에서 코인이 줄어든 경우 vs 늘어난 경우 (평균)')
    names = {'rise_1m': '급등 1분 상승 %', 'rise_1h': '급등까지 1시간 상승 %', 'hour_kst': '시각(한국)',
             'btc_1h': '비트코인 1시간 변화 %', 'coin_value': '코인 하루 거래대금(억)'}
    for key, name in names.items():
        a, na = prof[(key, '줄어듦')]
        b, nb = prof[(key, '늘어남')]
        sc = 1e8 if key == 'coin_value' else 1
        print(f"  {name:<20} 줄어듦 {a / sc if a is not None else float('nan'):>8.2f} ({na}번)   늘어남 {b / sc if b is not None else float('nan'):>8.2f} ({nb}번)")
    print(f"  거래 규모 하위 절반 코인 비율   줄어듦 {small['줄어듦']:.0f}%   늘어남 {small['늘어남']:.0f}%")

    print(f'\n[방법 비교] 전체 90일 · 7~8월(고르는 기간) · 9월(확인 기간). 주별 = 보유의 30%만 판다고 칠 때')
    print(f"{'걸러내기':<28} {'다시 사기':<26} | {'횟수':>4} {'늘어남':>6} {'평균':>7} {'최악':>7} {'하위10%':>8} {'손해 주':>7} {'최악 주':>7} | "
          f"{'7~8월 평균':>9} {'최악':>7} | {'9월 평균':>8} {'최악':>7}")
    for r in rows:
        a, tr, te = r['all'], r['train'], r['test']
        print(f"{r['filter'][:28]:<28} {r['plan'][:26]:<26} | {a['n']:>4} {a['more']:>5.0f}% {a['avg']:>+6.2f}% {a['worst']:>+6.1f}% "
              f"{a['low10']:>+7.1f}% {a['weeks_neg']:>3}/{a['weeks']:<3} {a['worst_week']:>+6.1f}% | "
              f"{tr['avg']:>+8.2f}% {tr['worst']:>+6.1f}% | {te['avg']:>+7.2f}% {te['worst']:>+6.1f}%")
    out = ROOT / 'data' / 'spike_improve.csv'
    with out.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['filter', 'plan'] + [f'{p}_{k}' for p in ('all', 'train', 'test') for k in rows[0]['all']] + ['how'])
        for r in rows:
            w.writerow([r['filter'], r['plan']] + [r[p][k] for p in ('all', 'train', 'test') for k in r['all']] + [json.dumps(r['how'], ensure_ascii=False)])
    print(f'\n저장: {out}')
    print('과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.')


def summarize(trades):
    if not trades:
        return {'trades': 0, 'win': 0.0, 'avg': 0.0, 'total': 0.0, 'worst': 0.0, 'best': 0.0, 'mins': 0.0}
    p = [t['pct'] for t in trades]
    return {'trades': len(p), 'win': sum(x > 0 for x in p) / len(p) * 100, 'avg': sum(p) / len(p),
            'total': sum(p), 'worst': min(p), 'best': max(p), 'mins': sum(t['mins'] for t in trades) / len(p)}


# ── 자료 받기 (업비트 공개 시세, 키 필요 없음) ──
class Pacer:
    """요청 시작 간격 유지 (업비트 시세 제한 초당 10회보다 여유 있게) — 여러 스레드가 함께 써도 간격 유지"""
    def __init__(self, gap=0.25):
        self.gap, self.next = gap, 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + self.gap
        if start > now:
            time.sleep(start - now)


_local = threading.local()


def get(pacer, url, params):
    import requests
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()          # 연결을 다시 써서 요청마다 새로 접속하지 않음
    for attempt in range(6):
        pacer.wait()
        r = _local.session.get(url, params=params, timeout=10)
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f'요청 제한이 풀리지 않아요: {url}')


def top_markets(pacer, count):
    markets = [m['market'] for m in get(pacer, 'https://api.upbit.com/v1/market/all', {'isDetails': 'true'})
               if m['market'].startswith('KRW-') and m.get('market_warning') != 'CAUTION']
    stable = {'KRW-USDT', 'KRW-USDC', 'KRW-USD1', 'KRW-USDS', 'KRW-DAI'}
    ticks = []
    for k in range(0, len(markets), 100):
        ticks += get(pacer, 'https://api.upbit.com/v1/ticker', {'markets': ','.join(markets[k:k + 100])})
    ticks = [t for t in ticks if t['market'] not in stable]
    ticks.sort(key=lambda t: t['acc_trade_price_24h'], reverse=True)
    return [t['market'] for t in ticks[:count]]


def pages_needed(market, days, cache_dir):
    """이 코인을 받는 데 필요한 요청 수 (이미 받은 기간은 빼고) — 남은 시간 계산용"""
    end = datetime.now(timezone.utc)
    if (cache_dir / f"{market}_{days}d_{end.strftime('%Y%m%d')}.json").exists():
        return 0
    have = _cached_span(market, cache_dir)
    minutes = days * 1440
    if have:
        minutes -= int((have[1] - have[0]).total_seconds() // 60)
    return max(1, minutes // 200 + 1)


def _cached_span(market, cache_dir):
    """예전에 받아 둔 같은 코인 1분봉 중 가장 긴 것: (처음 시각, 마지막 시각, 봉들) 없으면 None"""
    best = None
    for f in cache_dir.glob(f'{market}_*d_*.json'):
        bars = json.loads(f.read_text(encoding='utf-8'))
        if bars and (best is None or len(bars) > len(best)):
            best = bars
    if not best:
        return None
    t = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return t(best[0]['t']), t(best[-1]['t']), best


def minute_bars(pacer, market, days, cache_dir, on_page=None):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cache = cache_dir / f"{market}_{days}d_{end.strftime('%Y%m%d')}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding='utf-8'))
    start = end - timedelta(days=days)
    rows = {}

    def fetch(to, stop):
        """to부터 과거로 stop까지 받기"""
        while to > stop:
            chunk = get(pacer, 'https://api.upbit.com/v1/candles/minutes/1',
                        {'market': market, 'count': 200, 'to': to.strftime('%Y-%m-%dT%H:%M:%SZ')})
            if on_page:
                on_page()
            if not chunk:
                return
            for c in chunk:
                rows[c['candle_date_time_utc']] = {'t': c['candle_date_time_utc'], 'o': c['opening_price'], 'h': c['high_price'],
                                                   'l': c['low_price'], 'c': c['trade_price'], 'v': c['candle_acc_trade_price']}
            oldest = datetime.fromisoformat(chunk[-1]['candle_date_time_utc']).replace(tzinfo=timezone.utc)
            if oldest >= to:
                return
            to = oldest

    have = _cached_span(market, cache_dir)
    if have and have[0] > start:
        # 예전에 받은 기간은 그대로 쓰고, 그 뒤(최근)와 그 앞(과거)만 새로 받음
        first, last, old = have
        rows.update({b['t']: b for b in old})
        fetch(end, last)
        fetch(first, start)
    else:
        fetch(end, start)
    bars = [rows[k] for k in sorted(rows) if k >= start.strftime('%Y-%m-%dT%H:%M:%S')]
    bars = fill_gaps(bars)
    cache.write_text(json.dumps(bars), encoding='utf-8')
    return bars


class CompactBars:
    """1분봉을 숫자 배열로 압축 (코인 50개 × 90일을 dict로 두면 메모리가 모자람).
    bars[i]['o'] 처럼 dict와 같게 읽힘. 't'는 'YYYY-MM-DDTHH:MM:SS' 문자열로 돌려줌."""
    FIELDS = ('o', 'h', 'l', 'c', 'v')

    def __init__(self, bars):
        from array import array
        self.cols = {k: array('d', (b[k] for b in bars)) for k in self.FIELDS}
        base = datetime.fromisoformat(bars[0]['t']) if bars else None
        self.base = base
        self.mins = array('l', (int((datetime.fromisoformat(b['t']) - base).total_seconds() // 60) for b in bars))

    def __len__(self):
        return len(self.mins)

    def __iter__(self):
        return (self[i] for i in range(len(self)))

    def __getitem__(self, i):
        return _BarView(self, i)


class _BarView:
    __slots__ = ('bars', 'i')

    def __init__(self, bars, i):
        self.bars, self.i = bars, i

    def __getitem__(self, k):
        if k == 't':
            return (self.bars.base + timedelta(minutes=self.bars.mins[self.i])).strftime('%Y-%m-%dT%H:%M:%S')
        return self.bars.cols[k][self.i]


def fill_gaps(bars):
    """거래가 없던 1분은 업비트가 봉을 안 줌 → 거래대금 0, 가격은 직전 종가로 채움 ('평소'가 부풀지 않게)"""
    out = []
    for b in bars:
        if out:
            prev = datetime.fromisoformat(out[-1]['t'])
            cur = datetime.fromisoformat(b['t'])
            gap = int((cur - prev).total_seconds() // 60)
            for k in range(1, min(gap, 1440)):
                c = out[-1]['c']
                out.append({'t': (prev + timedelta(minutes=k)).isoformat(), 'o': c, 'h': c, 'l': c, 'c': c, 'v': 0.0})
        out.append(b)
    return out


def main():
    ap = argparse.ArgumentParser(description='순간 급등 올라타기 검증기 (과거 1분봉, 주문 없음)')
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--coins', type=int, default=30)
    ap.add_argument('--gap', type=float, default=0.15, help='요청 사이 간격(초). 업비트 제한은 초당 10회')
    ap.add_argument('--workers', type=int, default=3, help='동시에 받는 코인 수')
    ap.add_argument('--mode', choices=['buy', 'sell', 'rebuy', 'improve'], default='buy',
                    help='buy = 급등에 올라타기, sell = 가진 코인이 치솟을 때 일부 팔기 (들고 있을 때와 비교)')
    args = ap.parse_args()
    cache_dir = ROOT / 'data' / 'spike_cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    pacer = Pacer(args.gap)

    markets = top_markets(pacer, args.coins)
    need = {m: pages_needed(m, args.days, cache_dir) for m in markets}
    total = sum(need.values())
    print(f'거래대금 상위 {len(markets)}개 코인, 최근 {args.days}일 1분봉 · 새로 받을 요청 {total:,}번 '
          f'(예상 {total * args.gap / 60:.0f}분, 이미 받은 기간은 다시 안 받음)', flush=True)
    data = {}
    done = [0]
    began = time.monotonic()

    def progress(prefix):
        spent = time.monotonic() - began
        left = (total - done[0]) * (spent / done[0]) if done[0] else (total - done[0]) * args.gap
        pct = done[0] / total * 100 if total else 100
        print(f'{prefix} · 전체 {pct:.0f}% · 지난 {spent / 60:.0f}분 · 남은 약 {left / 60:.0f}분', flush=True)

    lock = threading.Lock()

    def on_page():
        with lock:
            done[0] += 1

    def one(m):
        bars = CompactBars(minute_bars(pacer, m, args.days, cache_dir, on_page=on_page))
        with lock:
            data[m] = bars
            progress(f'  [{len(data)}/{len(markets)}] {m}: {len(bars):,}개 봉')

    # 코인 3개를 동시에 받음 (요청 간격은 pacer가 전체로 지킴)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, markets))
    data = {m: data[m] for m in markets}
    print('받기 끝 · 계산 중…', flush=True)

    if args.mode == 'sell':
        return report_sells(data)
    if args.mode == 'rebuy':
        return print_rebuys(data)
    if args.mode == 'improve':
        return print_improve(data)

    rows = []
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(keys, combo))
        trades = []
        for m, bars in data.items():
            for t in simulate(bars, **p):
                trades.append({**t, 'market': m})
        s = summarize(trades)
        coins = len({t['market'] for t in trades})
        rows.append({**p, **s, 'coins': coins})

    rows.sort(key=lambda r: r['avg'], reverse=True)
    out = ROOT / 'data' / 'spike_backtest.csv'
    with out.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"\n손절 {STOP}% · 목표 {TARGET}% · 수수료 {FEE*100:.2f}%·미끄러짐 {SLIP*100:.1f}% (사고팔 때 각각)")
    print(f"{'평소대비':>6} {'1분상승':>6} {'트레일링':>7} {'보유':>4} | {'거래':>5} {'이긴비율':>7} {'평균':>7} {'합계':>8} {'최악':>7} {'평균보유':>7}")
    for r in rows:
        print(f"{r['mult']:>5}배 {r['rise']:>5}% {r['trail']:>6}% {r['hold']:>3}분 | {r['trades']:>5} {r['win']:>6.1f}% "
              f"{r['avg']:>+6.2f}% {r['total']:>+7.1f}% {r['worst']:>+6.1f}% {r['mins']:>5.1f}분")
    print(f'\n저장: {out}')
    print('과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.')


def print_rebuys(data):
    rows = report_rebuys(data)
    out = ROOT / 'data' / 'spike_rebuy_backtest.csv'
    with out.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows([{**r, 'sell': str(r['sell'])} for r in rows])
    name = {'time': lambda p: f'{p // 60}시간 뒤', 'dip': lambda p: f'판값 -{p}%', 'bounce': lambda p: f'바닥+{p}%'}
    print()
    print('+ = 팔고 다시 산 뒤 코인이 늘어남 (수수료·미끄러짐 뺌)')
    for key in [str(sp) for sp in SELL_SETS] + ['random']:
        print()
        print(f"[{'아무 때나 팔기 (비교용)' if key == 'random' else '급등 때 팔기 ' + key}]")
        print(f"{'다시 사기':>10} {'횟수':>5} {'늘어난비율':>8} {'평균':>7} {'중간값':>7} {'상위3뺀평균':>9} {'평균대기':>7} {'24시간초과':>8}")
        for r in rows:
            if str(r['sell']) != key:
                continue
            print(f"{name[r['rule']](r['param']):>10} {r['n']:>5} {r['more']:>7.0f}% {r['avg']:>+6.2f}% {r['median']:>+6.2f}% "
                  f"{r['avg_wo_top3']:>+8.2f}% {r['wait_h']:>5.1f}시간 {r['timeout']:>7.0f}%")
    print()
    print(f'저장: {out}')
    print('과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.')


def report_sells(data):
    rows = []
    keys = list(SELL_GRID)
    for combo in itertools.product(*SELL_GRID.values()):
        p = dict(zip(keys, combo))
        events = []
        for m, bars in data.items():
            events += [{**e, 'market': m} for e in spike_sells(bars, **p)]
        row = {**p, 'coins': len({e['market'] for e in events})}
        for h in HORIZONS:
            for k, v in summarize_sells(events, h).items():
                row[f'{k}_{h // 60}h'] = v
        rows.append(row)
    rows.sort(key=lambda r: r['median_6h'], reverse=True)
    out = ROOT / 'data' / 'spike_sell_backtest.csv'
    with out.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print()
    print(f"+ = 팔아 둔 돈이 그 시간 뒤 코인 값어치보다 많음 (수수료 {FEE*100:.2f}%·미끄러짐 {SLIP*100:.1f}% 뺌)")
    print(f"{'평소대비':>6} {'1분상승':>6} {'파는때':>8} {'횟수':>5} | " + ' | '.join(f'{h // 60:>2}시간 뒤: 나은비율 평균 중간값' for h in HORIZONS))
    for r in rows:
        when = '즉시' if not r['pullback'] else f"꺾이면{r['pullback']}%"
        cells = ' | '.join(f"{r[f'better_{h // 60}h']:>13.0f}% {r[f'avg_{h // 60}h']:>+5.2f}% {r[f'median_{h // 60}h']:>+5.2f}%" for h in HORIZONS)
        print(f"{r['mult']:>5}배 {r['rise']:>5}% {when:>8} {r[f'n_1h']:>5} | {cells}")
    print(f'\n저장: {out}')
    print('과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
