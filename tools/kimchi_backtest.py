"""김치 프리미엄 검증기 (과거 일봉으로 계산만 함, 실제 주문 없음)

김치 프리미엄 = 업비트 비트코인 원화 가격 ÷ (바이낸스 비트코인 달러 가격 × 원/달러 환율) − 1
  - 업비트 일봉(한국 날짜 d)과 바이낸스 일봉(세계 표준시 날짜 d)은 같은 24시간 (한국 오전 9시 ~ 다음 날 오전 9시)
  - 환율은 유럽중앙은행 기준(frankfurter.app, 주말·휴일은 직전 값)

확인하는 것
  1. 프리미엄이 높을 때(지난 1년 중 상위 몇 %) 그 뒤 비트코인 수익이 낮았나 — 1·7·30일 뒤
  2. 규칙: 프리미엄이 높으면 비트코인을 팔고 현금, 낮아지면 다시 삼 (+ 비트코인 200일 평균선 조건)
     해마다 그 전 자료로 기준을 고르고 → 그 해에 그대로 대 봄. 비교: 비트코인 들고 있기 / 200일 평균선만

데이터: 업비트 일봉(data/trend_cache, 없으면 받음) · 바이낸스 BTCUSDT 일봉 · 환율 → data/kimchi_cache/
실행:  venv\\Scripts\\python.exe tools\\kimchi_backtest.py [--days 2000]
결과:  콘솔 표 + data/kimchi_backtest_result.json

과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.
"""
import argparse
import bisect
import itertools
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import trend_backtest as tb  # noqa: E402

CACHE = ROOT / 'data' / 'kimchi_cache'
OUT = ROOT / 'data' / 'kimchi_backtest_result.json'
COST = tb.COST
RANK_DAYS = 365                     # 프리미엄 순위는 지난 1년 안에서만 (앞날 자료를 쓰지 않게)
GRID = {
    'high': [0.7, 0.8, 0.9, 0.95],  # 지난 1년 중 이 순위 이상이면 팜
    'back': [0.5, 0.7],             # 이 순위 밑으로 내려오면 다시 삼
    'ma': [0, 200],                 # 비트코인 평균선 조건 (0 = 없음)
}


def _get_json(url, params=None, tries=4):
    import requests
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 + k)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(1 + k)


def binance_daily(days):
    """바이낸스 BTCUSDT 일봉 종가 {날짜(UTC): 종가}"""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"binance_btcusdt_{days}d_{datetime.now(timezone.utc):%Y%m%d}.json"
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    start = int((datetime.now(timezone.utc) - timedelta(days=days + 2)).timestamp() * 1000)
    out = {}
    for host in ('https://api.binance.com', 'https://data-api.binance.vision'):
        try:
            t = start
            while True:
                rows = _get_json(f'{host}/api/v3/klines', {'symbol': 'BTCUSDT', 'interval': '1d', 'startTime': t, 'limit': 1000})
                if not rows:
                    break
                for r in rows:
                    out[datetime.fromtimestamp(r[0] / 1000, timezone.utc).strftime('%Y-%m-%d')] = float(r[4])
                if len(rows) < 1000:
                    break
                t = rows[-1][0] + 86400000
                time.sleep(0.2)
            break
        except Exception as e:
            print(f'  {host} 실패: {e}', flush=True)
            out = {}
    path.write_text(json.dumps(out), encoding='utf-8')
    return out


def usdkrw(first, last):
    """원/달러 환율 {날짜: 값} (유럽중앙은행 기준, 영업일만)"""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f'usdkrw_{first}_{last}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    d = _get_json(f'https://api.frankfurter.app/{first}..{last}', {'from': 'USD', 'to': 'KRW'})
    out = {k: v['KRW'] for k, v in d.get('rates', {}).items()}
    path.write_text(json.dumps(out), encoding='utf-8')
    return out


def upbit_btc(days):
    latest = sorted(tb.CACHE.glob(f'KRW-BTC_1d_{days}d_*.json'))
    if latest:
        rows = json.loads(latest[-1].read_text(encoding='utf-8'))
    else:
        from tools.spike_backtest import Pacer
        rows = tb.fetch_days(Pacer(0.12), 'KRW-BTC', days)
    return {r['d']: r['c'] for r in rows}


def premium_series(up, bn, fx):
    """날짜 · 업비트 종가 · 프리미엄(%) — 세 자료가 다 있는 날만 (환율은 직전 영업일 값)"""
    fx_days = sorted(fx)
    dates, px, kp = [], [], []
    for d in sorted(up):
        if d not in bn:
            continue
        k = bisect.bisect_right(fx_days, d) - 1
        if k < 0 or (datetime.fromisoformat(d) - datetime.fromisoformat(fx_days[k])).days > 5:
            continue
        dates.append(d)
        px.append(up[d])
        kp.append((up[d] / (bn[d] * fx[fx_days[k]]) - 1) * 100)
    return dates, px, kp


def rank_series(kp, window=RANK_DAYS):
    """날짜 i의 프리미엄이 지난 window일(i 포함) 중 몇 번째인지 0~1 (자료가 반 이상 있어야)"""
    out = [None] * len(kp)
    for i in range(len(kp)):
        w = kp[max(0, i - window + 1):i + 1]
        if len(w) >= window // 2:
            out[i] = sum(x < kp[i] for x in w) / (len(w) - 1 or 1)
    return out


def forward_table(px, rank, horizons=(1, 7, 30)):
    """순위 구간별 그 뒤 비트코인 수익률 평균(%)과 횟수"""
    bins = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.9), (0.9, 1.01)]
    out = []
    for lo, hi in bins:
        row = {'bin': f'{int(lo * 100)}~{min(int(hi * 100), 100)}%'}
        for h in horizons:
            v = [(px[i + h] / px[i] - 1) * 100 for i in range(len(px) - h) if rank[i] is not None and lo <= rank[i] < hi]
            row[h] = (sum(v) / len(v), len(v)) if v else (None, 0)
        out.append(row)
    return out


def simulate(px, rank, ma_line, p, a, b):
    """날짜 a~b: 오늘 종가로 판단해 오늘 종가에 사고팖 → 하루 수익률 목록"""
    rets, held = [], False
    for i in range(a, b + 1):
        r = (px[i] / px[i - 1] - 1) if held else 0.0
        rk = rank[i]
        want = held
        if rk is not None:
            if held and rk >= p['high']:
                want = False
            elif not held and rk <= p['back']:
                want = True
        if p['ma'] and (ma_line[i] is None or px[i] <= ma_line[i]):
            want = False
        if want != held:
            r -= COST
            held = want
        rets.append(r)
    return rets


def combos():
    keys = list(GRID)
    return [dict(zip(keys, v)) for v in itertools.product(*GRID.values()) if v[1] < v[0]]


def key_of(p):
    return f"high{p['high']}_back{p['back']}_ma{p['ma']}"


def main():
    ap = argparse.ArgumentParser(description='김치 프리미엄 검증기 (과거 일봉, 주문 없음)')
    ap.add_argument('--days', type=int, default=2000)
    args = ap.parse_args()
    t0 = time.time()
    up = upbit_btc(args.days)
    print(f'업비트 비트코인 일봉 {len(up)}일', flush=True)
    bn = binance_daily(args.days)
    print(f'바이낸스 비트코인 일봉 {len(bn)}일', flush=True)
    first = min(up)
    fx = usdkrw((date.fromisoformat(first) - timedelta(days=10)).isoformat(), max(up))
    print(f'환율 {len(fx)}일', flush=True)
    dates, px, kp = premium_series(up, bn, fx)
    rank = rank_series(kp)
    ma200 = tb.sma_list(px, 200)
    srt = sorted(kp)
    q = lambda f: srt[int(f * (len(srt) - 1))]
    print(f'\n프리미엄 {dates[0]} ~ {dates[-1]} ({len(dates)}일): 중간 {q(0.5):+.2f}% · 아래 10% {q(0.1):+.2f}% · 위 10% {q(0.9):+.2f}% · '
          f'최저 {srt[0]:+.2f}% · 최고 {srt[-1]:+.2f}%')

    print('\n── 프리미엄 순위(지난 1년 중)별 그 뒤 비트코인 수익률 ──')
    ft = forward_table(px, rank)
    for r in ft:
        cells = '  '.join(f"{h}일 뒤 {r[h][0]:+6.2f}% ({r[h][1]}번)" if r[h][0] is not None else f'{h}일 뒤 -' for h in (1, 7, 30))
        print(f"  {r['bin']:<9} {cells}")

    # 걸어가며: 해마다 그 전 자료로 규칙 고르기
    years = sorted({d[:4] for d in dates})
    start_i = next(i for i, r in enumerate(rank) if r is not None) + 1
    picks, oos, oos_dates = [], [], []
    for y in years:
        a = next(i for i, d in enumerate(dates) if d.startswith(y))
        b = max(i for i, d in enumerate(dates) if d.startswith(y))
        if a - start_i < 365:
            continue
        scored = sorted(((tb.perf(simulate(px, rank, ma200, p, start_i, a - 1))['sharpe'], p) for p in combos()),
                        key=lambda x: x[0], reverse=True)
        best = scored[0][1]
        rets = simulate(px, rank, ma200, best, a, b)
        pf = tb.perf(rets)
        hold = tb.perf([px[i] / px[i - 1] - 1 for i in range(a, b + 1)])
        beat = sum(tb.perf(simulate(px, rank, ma200, p, a, b))['total'] > hold['total'] for p in combos())
        picks.append({'year': y, 'rule': key_of(best), **pf, 'btc': hold['total'], 'beat': beat, 'rules': len(combos())})
        oos += rets
        oos_dates += dates[a:b + 1]
        print(f"{y}: 고른 규칙 {key_of(best)} → 그 해 {pf['total']:+.1f}% (최대 낙폭 {pf['mdd']:.1f}%) · 비트코인 {hold['total']:+.1f}% · "
              f"규칙 {beat}/{len(combos())}개가 비트코인보다 나음", flush=True)

    s, e = dates.index(oos_dates[0]), dates.index(oos_dates[-1])
    btc = [px[i] / px[i - 1] - 1 for i in range(s, e + 1)]
    weather = [((px[i] / px[i - 1] - 1) if ma200[i - 1] and px[i - 1] > ma200[i - 1] else 0.0) for i in range(s, e + 1)]
    rows = {'걸어가며 고른 규칙': tb.perf(oos), '비트코인 들고 있기': tb.perf(btc), '비트코인 + 200일 평균선만': tb.perf(weather)}
    print(f"\n── {oos_dates[0]} ~ {oos_dates[-1]} ──")
    for k, v in rows.items():
        print(f"  {k:<20} 합 {v['total']:+8.1f}%  연 {v['cagr']:+6.1f}%  최대 낙폭 {v['mdd']:6.1f}%  샤프 {v['sharpe']:4.2f}")

    OUT.write_text(json.dumps({
        'meta': {'made': datetime.now().isoformat(timespec='seconds'), 'from': dates[0], 'to': dates[-1], 'grid': GRID, 'cost': COST},
        'premium': {'median': q(0.5), 'p10': q(0.1), 'p90': q(0.9), 'min': srt[0], 'max': srt[-1],
                    'series': {'dates': dates[::3], 'kp': [round(x, 3) for x in kp[::3]]}},
        'forward': ft, 'years': picks, 'compare': rows,
    }, ensure_ascii=False), encoding='utf-8')
    print(f'\n저장: {OUT.relative_to(ROOT)} · 걸린 시간 {time.time() - t0:.0f}초')


if __name__ == '__main__':
    main()
