"""과하게 떨어진 코인의 되돌림 검증기 (과거 일봉으로 계산만 함, 실제 주문 없음)

사건: 코인이 최근 w일 동안 X% 넘게 떨어졌는데 (선택) 같은 기간 비트코인은 −3%보다 덜 떨어짐
      → 그날 종가에 사서 H일 뒤 종가에 판다고 치고, 수수료·불리한 체결(사고팔 때 각 0.15%)을 뺌
      같은 코인은 판 뒤에야 다음 사건으로 셈
비교: 같은 해 거래 많은 코인 아무 날에나 사서 H일 들고 있기의 평균 (그냥 시장 흐름)
검증: 해마다 그 전 자료로 기준(w, X, 비트코인 조건, H)을 고르고 → 그 해에 그대로 대 봄

데이터: tools/trend_backtest.py 가 받아 둔 업비트 일봉 (data/trend_cache)
실행:  venv\\Scripts\\python.exe tools\\reversal_backtest.py [--days 2000]
결과:  콘솔 표 + data/reversal_backtest_result.json

과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.
"""
import argparse
import itertools
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import trend_backtest as tb  # noqa: E402

OUT = ROOT / 'data' / 'reversal_backtest_result.json'
GRID = {
    'win': [1, 2, 3],               # 며칠 동안 떨어졌나
    'drop': [10, 15, 20, 30],       # 몇 % 넘게
    'btc': [False, True],           # 같은 기간 비트코인은 −3%보다 덜 떨어짐
    'hold': [1, 3, 7],              # 며칠 들고 있나
}
BTC_OK = -3.0


def events(P, dates, p):
    """규칙 p의 사건 목록 [(날짜 번호, 코인, 순수익률%)]"""
    close, val30, age = P['close'], P['val30'], P['age']
    btc = close['KRW-BTC']
    out = []
    n = len(dates)
    cost = 2 * tb.COST * 100
    for m, c in close.items():
        if m == 'KRW-BTC':
            continue
        busy = -1
        for i in range(p['win'], n - p['hold']):
            if i <= busy or not c[i] or not c[i - p['win']] or not c[i + p['hold']]:
                continue
            if age[m][i] < tb.MIN_AGE or val30[m][i] < tb.MIN_VALUE:
                continue
            chg = (c[i] / c[i - p['win']] - 1) * 100
            if chg > -p['drop']:
                continue
            if p['btc'] and (btc[i] / btc[i - p['win']] - 1) * 100 <= BTC_OK:
                continue
            out.append((i, m, (c[i + p['hold']] / c[i] - 1) * 100 - cost))
            busy = i + p['hold']
    return out


def market_avg(P, a, b, hold):
    """날짜 a~b에 거래 많은 코인 아무 날에나 사서 hold일 뒤 판 평균(%) — 비용 뺌"""
    v = []
    for m, c in P['close'].items():
        for i in range(a, min(b, len(c) - 1 - hold) + 1, 3):
            if c[i] and c[i + hold] and P['age'][m][i] >= tb.MIN_AGE and P['val30'][m][i] >= tb.MIN_VALUE:
                v.append((c[i + hold] / c[i] - 1) * 100)
    return (sum(v) / len(v) - 2 * tb.COST * 100) if v else None


def stat(ev):
    if not ev:
        return {'n': 0}
    r = [x[2] for x in ev]
    sd = statistics.pstdev(r) if len(r) > 1 else 0
    return {'n': len(r), 'avg': sum(r) / len(r), 'median': statistics.median(r), 'win': sum(x > 0 for x in r) / len(r) * 100,
            'worst': min(r), 'best': max(r), 't': (sum(r) / len(r)) / sd * len(r) ** 0.5 if sd else 0}


def combos():
    keys = list(GRID)
    return [dict(zip(keys, v)) for v in itertools.product(*GRID.values())]


def key_of(p):
    return f"w{p['win']}_drop{p['drop']}_{'btcOK' if p['btc'] else 'any'}_H{p['hold']}"


def main():
    ap = argparse.ArgumentParser(description='과하게 떨어진 코인의 되돌림 검증기 (과거 일봉, 주문 없음)')
    ap.add_argument('--days', type=int, default=2000)
    args = ap.parse_args()
    t0 = time.time()
    from tools.spike_backtest import Pacer
    series, dates = tb.load_all(Pacer(0.12), args.days, offline=True)
    P = dict(zip(('close', 'val30', 'age'), tb.prepare(series, dates)))
    print(f'코인 {len(P["close"])}종 · {dates[0]} ~ {dates[-1]} · 규칙 {len(combos())}개', flush=True)
    all_ev = {key_of(p): events(P, dates, p) for p in combos()}
    year_of = lambda i: dates[i][:4]
    years = sorted({d[:4] for d in dates})[2:]          # 앞 2년은 고르는 데만
    picks, oos = [], []
    for y in years:
        a = next(i for i, d in enumerate(dates) if d.startswith(y))
        b = max(i for i, d in enumerate(dates) if d.startswith(y))
        scored = []
        for p in combos():
            tr = [e for e in all_ev[key_of(p)] if e[0] < a]
            s = stat(tr)
            if s['n'] >= 30:
                scored.append((s['t'], p))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1]
        te = [e for e in all_ev[key_of(best)] if a <= e[0] <= b]
        st = stat(te)
        mk = market_avg(P, a, b, best['hold'])
        pos = sum(stat([e for e in all_ev[key_of(p)] if a <= e[0] <= b]).get('avg', -1e9) > 0 for p in combos())
        picks.append({'year': y, 'rule': key_of(best), **st, 'market': mk, 'rules_pos': pos})
        oos += te
        if st['n']:
            print(f"{y}: 고른 규칙 {key_of(best)} → {st['n']}번 · 한 번 평균 {st['avg']:+.2f}% (중간 {st['median']:+.2f}%, 이김 {st['win']:.0f}%, "
                  f"최악 {st['worst']:+.1f}%) · 그냥 시장 {mk:+.2f}% · 그 해 플러스 규칙 {pos}/{len(combos())}", flush=True)
        else:
            print(f'{y}: 고른 규칙 {key_of(best)} → 사건 없음', flush=True)
    s = stat(oos)
    print(f"\n── 걸어가며 고른 규칙을 모두 모으면: {s['n']}번 · 한 번 평균 {s['avg']:+.2f}% · 중간 {s['median']:+.2f}% · 이김 {s['win']:.0f}% · "
          f"최악 {s['worst']:+.1f}% · 평균÷흩어짐×√n = {s['t']:.2f}")
    # 전체 기간 규칙별 (참고)
    table = sorted(((key_of(p), stat(all_ev[key_of(p)])) for p in combos()), key=lambda x: x[1].get('t', 0), reverse=True)
    print('\n── 전체 기간 규칙별 상위 8 (참고: 전체를 보고 고르면 좋아 보이는 게 당연) ──')
    for k, st in table[:8]:
        print(f"  {k:<24} {st['n']:>5}번 평균 {st['avg']:+6.2f}% 중간 {st['median']:+6.2f}% 이김 {st['win']:4.0f}% 최악 {st['worst']:+7.1f}%")
    OUT.write_text(json.dumps({'meta': {'made': datetime.now().isoformat(timespec='seconds'), 'from': dates[0], 'to': dates[-1], 'grid': GRID},
                               'years': picks, 'oos': s, 'table': [{'rule': k, **v} for k, v in table]}, ensure_ascii=False), encoding='utf-8')
    print(f'\n저장: {OUT.relative_to(ROOT)} · 걸린 시간 {time.time() - t0:.0f}초')


if __name__ == '__main__':
    main()
