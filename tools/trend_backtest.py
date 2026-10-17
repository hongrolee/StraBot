"""추세 따라가기 + 시장 날씨 필터 검증기 (과거 일봉으로 계산만 함, 실제 주문 없음)

규칙: H일마다
      - 시장 날씨: 비트코인 종가가 최근 R일 평균 위면 '맑음'(투자), 아니면 '비'(전부 현금)
      - 맑으면 최근 M일 동안 가장 많이 오른 코인 N개를 같은 금액씩 들고 감 (거래가 적은 코인은 뺌)
      - 바뀐 만큼 수수료 0.05% + 불리한 체결 0.1%를 뺌
검증: 해마다 '그 전까지의 자료'로 가장 나은 규칙을 고르고 → 그 해에 그대로 대 봄 (걸어가며 확인)
비교: 비트코인 그냥 들고 있기 / 비트코인 + 날씨 필터만 / 거래 많은 코인 전부 똑같이 들고 있기

데이터: 업비트 원화 마켓 일봉 (data/trend_cache/ 에 저장, 다시 받지 않음)
주의: 업비트에서 이미 상장 폐지된 코인은 과거 자료를 받을 수 없어 결과가 실제보다 좋게 나올 수 있음
실행:  venv\\Scripts\\python.exe tools\\trend_backtest.py [--days 2000]
결과:  콘솔 표 + data/trend_backtest_result.json

과거 계산 결과이며 앞으로의 수익을 뜻하지 않아요.
"""
import argparse
import itertools
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / 'data' / 'trend_cache'
OUT = ROOT / 'data' / 'trend_backtest_result.json'
FEE = 0.0005
SLIP = 0.001
COST = FEE + SLIP                 # 바꾼 금액 비율마다
MIN_VALUE = 500_000_000           # 최근 30일 하루 평균 거래대금 5억 원 미만은 뺌
MIN_AGE = 60                      # 상장 60일이 안 된 코인은 뺌
STABLE = {'KRW-USDT', 'KRW-USDC', 'KRW-USD1', 'KRW-USDS', 'KRW-DAI', 'KRW-PYUSD', 'KRW-RLUSD', 'KRW-USDE'}

GRID = {
    'regime': [0, 50, 100, 200],      # 비트코인 평균선 기간 (0 = 날씨 필터 없음)
    'mom': [7, 14, 28, 56],           # 최근 며칠 상승률로 고를지
    'top': [3, 5, 10],                # 몇 개 들고 갈지
    'hold': [3, 7, 14],               # 며칠마다 바꿀지
}


# ── 데이터 ──
def fetch_days(pacer, market, days):
    """업비트 일봉 days일치 → [{'d': 'YYYY-MM-DD'(KST), 'o','c','v'}] 오래된 순 (같은 날 받은 게 있으면 그대로)"""
    sys.path.insert(0, str(ROOT))
    from tools.spike_backtest import get
    CACHE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    path = CACHE / f'{market}_1d_{days}d_{today}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    rows, to = {}, None
    while len(rows) < days:
        params = {'market': market, 'count': 200}
        if to:
            params['to'] = to
        chunk = get(pacer, 'https://api.upbit.com/v1/candles/days', params)
        if not chunk:
            break
        for c in chunk:
            rows[c['candle_date_time_kst'][:10]] = {'d': c['candle_date_time_kst'][:10], 'o': c['opening_price'],
                                                    'c': c['trade_price'], 'v': c['candle_acc_trade_price']}
        if len(chunk) < 200:
            break
        to = chunk[-1]['candle_date_time_utc'] + 'Z'
    out = [rows[k] for k in sorted(rows)][-days:]
    path.write_text(json.dumps(out), encoding='utf-8')
    return out


def load_all(pacer, days, offline=False):
    """{market: {날짜: (종가, 거래대금)}} + 날짜 목록(비트코인 기준)"""
    if offline:
        latest = {}
        for f in sorted(CACHE.glob(f'KRW-*_1d_{days}d_*.json')):
            latest[f.name.split('_')[0]] = f
        data = {m: json.loads(f.read_text(encoding='utf-8')) for m, f in latest.items()}
    else:
        sys.path.insert(0, str(ROOT))
        from tools.spike_backtest import get
        markets = [m['market'] for m in get(pacer, 'https://api.upbit.com/v1/market/all', {'isDetails': 'false'})
                   if m['market'].startswith('KRW-') and m['market'] not in STABLE]
        data, t0 = {}, time.time()
        for i, m in enumerate(markets, 1):
            data[m] = fetch_days(pacer, m, days)
            if i % 20 == 0 or i == len(markets):
                el = time.time() - t0
                print(f'  일봉 받는 중 {i}/{len(markets)} · 경과 {el:.0f}초 · 남은 약 {el / i * (len(markets) - i):.0f}초', flush=True)
    series = {m: {r['d']: (r['c'], r['v']) for r in rows} for m, rows in data.items() if rows}
    dates = sorted(series['KRW-BTC'])
    return series, dates


# ── 계산 ──
def prepare(series, dates):
    """날짜 순서 배열로: close[m][i] (없으면 None), value30[m][i] (최근 30일 평균 거래대금), age[m][i]"""
    idx = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    close, val30, age = {}, {}, {}
    for m, s in series.items():
        c = [None] * n
        v = [0.0] * n
        for d, (px, vol) in s.items():
            if d in idx:
                c[idx[d]] = px
                v[idx[d]] = vol
        first = next((i for i, x in enumerate(c) if x is not None), None)
        if first is None:
            continue
        acc, a30, ag = 0.0, [0.0] * n, [0] * n
        for i in range(n):
            acc += v[i] - (v[i - 30] if i >= 30 else 0)
            a30[i] = acc / min(i + 1, 30)
            ag[i] = i - first + 1 if i >= first else 0
        close[m], val30[m], age[m] = c, a30, ag
    return close, val30, age


def sma_list(xs, n):
    out, acc = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        acc += x
        if i >= n:
            acc -= xs[i - n]
        if i + 1 >= n:
            out[i] = acc / n
    return out


def simulate(P, p, start, end):
    """날짜 번호 start~end(포함)를 규칙 p로 굴림 → 하루 수익률 목록, 투자한 날 비율, 거래(바꾼) 횟수"""
    close, val30, age, btc_ma = P['close'], P['val30'], P['age'], P['btc_ma']
    btc = close['KRW-BTC']
    hold, rets, invested, turns = {}, [], 0, 0
    for i in range(start, end + 1):
        # 오늘 수익: 어제 정한 보유를 오늘 종가까지
        r = 0.0
        if hold:
            for m, w in hold.items():
                c0, c1 = close[m][i - 1], close[m][i]
                r += w * ((c1 / c0 - 1) if c0 and c1 else 0.0)
            invested += 1
        # 바꾸는 날: 오늘 종가로 판단, 오늘 종가에 바꿈 (24시간 시장이라 다음 날 시작 가격 ≈ 오늘 종가)
        if (i - start) % p['hold'] == 0:
            new = {}
            ma = btc_ma[p['regime']][i] if p['regime'] else None
            sunny = p['regime'] == 0 or (ma is not None and btc[i] > ma)
            if sunny:
                cands = []
                for m, c in close.items():
                    j = i - p['mom']
                    if j < 0 or not c[i] or not c[j] or age[m][i] < MIN_AGE or val30[m][i] < MIN_VALUE:
                        continue
                    cands.append((c[i] / c[j] - 1, m))
                cands.sort(reverse=True)
                pick = [m for _, m in cands[:p['top']]]
                new = {m: 1 / len(pick) for m in pick} if pick else {}
            change = sum(abs(new.get(m, 0) - hold.get(m, 0)) for m in set(new) | set(hold))
            if change > 1e-9:
                turns += 1
            r -= change * COST
            hold = new
        rets.append(r)
    return rets, invested / max(1, end - start + 1), turns


def perf(rets):
    """하루 수익률 → 합친 수익률, 연 환산, 최대 낙폭, 샤프(연), 플러스인 달 비율"""
    if not rets:
        return {}
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / n) if n > 1 else 0.0
    return {'total': (eq - 1) * 100, 'cagr': (eq ** (365 / n) - 1) * 100 if eq > 0 else -100.0,
            'mdd': mdd * 100, 'sharpe': mean / sd * math.sqrt(365) if sd else 0.0, 'days': n}


def month_rows(rets, dates):
    out = {}
    for r, d in zip(rets, dates):
        out.setdefault(d[:7], []).append(r)
    return {k: (math.prod(1 + x for x in v) - 1) * 100 for k, v in out.items()}


def combos():
    keys = list(GRID)
    return [dict(zip(keys, v)) for v in itertools.product(*GRID.values())]


def key_of(p):
    return f"R{p['regime']}_M{p['mom']}_N{p['top']}_H{p['hold']}"


def main():
    ap = argparse.ArgumentParser(description='추세 따라가기 + 시장 날씨 필터 검증기 (과거 일봉, 주문 없음)')
    ap.add_argument('--days', type=int, default=2000)
    ap.add_argument('--gap', type=float, default=0.12)
    ap.add_argument('--offline', action='store_true', help='받아 둔 일봉만 씀')
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from tools.spike_backtest import Pacer
    t0 = time.time()
    series, dates = load_all(Pacer(args.gap), args.days, args.offline)
    P = dict(zip(('close', 'val30', 'age'), prepare(series, dates)))
    btc = P['close']['KRW-BTC']
    P['btc_ma'] = {r: sma_list(btc, r) for r in GRID['regime'] if r}
    print(f'코인 {len(P["close"])}종 · {dates[0]} ~ {dates[-1]} ({len(dates)}일) · 규칙 {len(combos())}개', flush=True)

    warm = max(max(GRID['regime']), max(GRID['mom'])) + 1
    years = sorted({d[:4] for d in dates})
    test_years = [y for y in years if dates.index(next(d for d in dates if d.startswith(y))) > warm + 365]
    idx_of = lambda y: (dates.index(next(d for d in dates if d.startswith(y))),
                        max(i for i, d in enumerate(dates) if d.startswith(y)))
    oos, oos_dates, picks = [], [], []
    for y in test_years:
        a, b = idx_of(y)
        # 그 전까지 자료로 고르기 (샤프가 가장 높은 규칙)
        scored = []
        for p in combos():
            rets, inv, _ = simulate(P, p, warm, a - 1)
            scored.append((perf(rets)['sharpe'], p))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1]
        rets, inv, turns = simulate(P, best, a, b)
        # 같은 해에 모든 규칙 (몇 개가 비트코인보다 나았나)
        btc_y = perf([(btc[i] / btc[i - 1] - 1) for i in range(a, b + 1)])
        all_y = [perf(simulate(P, p, a, b)[0])['total'] for p in combos()]
        beat = sum(t > btc_y['total'] for t in all_y)
        pf = perf(rets)
        picks.append({'year': y, 'rule': key_of(best), 'train_sharpe': scored[0][0], **pf, 'invested': inv * 100, 'turns': turns,
                      'btc': btc_y['total'], 'rules_beat_btc': beat, 'rules': len(all_y)})
        oos += rets
        oos_dates += dates[a:b + 1]
        print(f"{y}: 고른 규칙 {key_of(best)} → 그 해 {pf['total']:+.1f}% (최대 낙폭 {pf['mdd']:.1f}%, 투자한 날 {inv * 100:.0f}%) · "
              f"비트코인 {btc_y['total']:+.1f}% · 규칙 {beat}/{len(all_y)}개가 비트코인보다 나음", flush=True)

    # 비교 (같은 기간)
    s, e = dates.index(oos_dates[0]), dates.index(oos_dates[-1])
    btc_rets = [btc[i] / btc[i - 1] - 1 for i in range(s, e + 1)]
    ma200 = P['btc_ma'][200]                         # 비트코인 + 날씨(200일)만: 어제 평균선 위면 오늘 들고 있음
    weather = [((btc[i] / btc[i - 1] - 1) if ma200[i - 1] and btc[i - 1] > ma200[i - 1] else 0.0) for i in range(s, e + 1)]
    ew = []
    for i in range(s, e + 1):
        ok = [m for m, c in P['close'].items() if c[i] and c[i - 1] and P['age'][m][i - 1] >= MIN_AGE and P['val30'][m][i - 1] >= MIN_VALUE]
        ew.append(sum(P['close'][m][i] / P['close'][m][i - 1] - 1 for m in ok) / len(ok) if ok else 0.0)
    rows = {'걸어가며 고른 규칙': perf(oos), '비트코인 들고 있기': perf(btc_rets),
            '비트코인 + 날씨(200일)만': perf(weather), '거래 많은 코인 전부 똑같이': perf(ew)}
    print(f"\n── {oos_dates[0]} ~ {oos_dates[-1]} (규칙은 해마다 그 전 자료로만 고름) ──")
    for k, v in rows.items():
        print(f"  {k:<22} 합 {v['total']:+8.1f}%  연 {v['cagr']:+6.1f}%  최대 낙폭 {v['mdd']:6.1f}%  샤프 {v['sharpe']:4.2f}")

    OUT.write_text(json.dumps({
        'meta': {'made': datetime.now().isoformat(timespec='seconds'), 'from': dates[0], 'to': dates[-1], 'coins': len(P['close']),
                 'grid': GRID, 'cost': COST, 'min_value': MIN_VALUE, 'min_age': MIN_AGE},
        'years': picks, 'compare': rows,
        'months': {'oos': month_rows(oos, oos_dates), 'btc': month_rows(btc_rets, oos_dates)},
        'equity': {'dates': oos_dates[::3], 'oos': _eq(oos)[::3], 'btc': _eq(btc_rets)[::3], 'weather': _eq(weather)[::3]},
    }, ensure_ascii=False), encoding='utf-8')
    print(f'\n저장: {OUT.relative_to(ROOT)} · 걸린 시간 {time.time() - t0:.0f}초')


def _eq(rets):
    out, e = [], 1.0
    for r in rets:
        e *= 1 + r
        out.append(round(e, 5))
    return out


if __name__ == '__main__':
    main()
