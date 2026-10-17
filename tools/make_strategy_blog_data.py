"""StraHub 블로그 7편(추세 따라가기 · 김치 프리미엄 · 되돌림)에 넣을 숫자 → data/strategy_blog_data.json

trend / kimchi / reversal 결과 파일을 읽고, 되돌림은 '같은 날 몰림'을 다시 계산함 (받아 둔 일봉만, 업비트 요청 없음).
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import reversal_backtest as rb, trend_backtest as tb  # noqa: E402
from tools.spike_backtest import Pacer  # noqa: E402

CRASH_RULE = {'win': 2, 'drop': 20, 'btc': False, 'hold': 3}


def main():
    d = ROOT / 'data'
    tr = json.loads((d / 'trend_backtest_result.json').read_text(encoding='utf-8'))
    km = json.loads((d / 'kimchi_backtest_result.json').read_text(encoding='utf-8'))
    rv = json.loads((d / 'reversal_backtest_result.json').read_text(encoding='utf-8'))

    eq = tr['equity']
    step = 3                                                    # 결과 파일은 3일 간격 → 9일 간격으로
    A1 = {'from': tr['meta']['from'], 'to': tr['meta']['to'], 'coins': tr['meta']['coins'], 'rules': 144,
          'years': [(y['year'], y['rule'], round(y['total'], 1), round(y['mdd'], 1), round(y['btc'], 1), y['rules_beat_btc'])
                    for y in tr['years']],
          'compare': {k: (round(v['total'], 1), round(v['cagr'], 1), round(v['mdd'], 1), round(v['sharpe'], 2)) for k, v in tr['compare'].items()},
          'eq': {'dates': eq['dates'][::step], 'oos': eq['oos'][::step], 'btc': eq['btc'][::step], 'weather': eq['weather'][::step]}}

    pr = km['premium']
    B = {'from': km['meta']['from'], 'to': km['meta']['to'],
         'premium': {k: round(pr[k], 2) for k in ('median', 'p10', 'p90', 'min', 'max')},
         'series': {'dates': pr['series']['dates'][::3], 'kp': pr['series']['kp'][::3]},
         'forward': [(r['bin'], round(r['7'][0], 2), r['7'][1], round(r['30'][0], 2), r['30'][1]) for r in km['forward']],
         'years': [(y['year'], y['rule'], round(y['total'], 1), round(y['mdd'], 1), round(y['btc'], 1), y['beat'], y['rules']) for y in km['years']],
         'compare': {k: (round(v['total'], 1), round(v['cagr'], 1), round(v['mdd'], 1), round(v['sharpe'], 2)) for k, v in km['compare'].items()}}

    series, dates = tb.load_all(Pacer(0.1), 2000, offline=True)
    P = dict(zip(('close', 'val30', 'age'), tb.prepare(series, dates)))
    ev = rb.events(P, dates, CRASH_RULE)
    by = defaultdict(list)
    for i, m, r in ev:
        by[dates[i]].append(r)
    days = sorted(((sum(v), len(v), k) for k, v in by.items()), reverse=True)
    davg = [sum(v) / len(v) for v in by.values()]
    top5 = {k for _, _, k in days[:5]}
    rest = [r for i, m, r in ev if dates[i] not in top5]
    per_year = defaultdict(list)
    for i, m, r in ev:
        per_year[dates[i][:4]].append(r)
    A2 = {'rules': 72, 'oos': {k: round(v, 2) if isinstance(v, float) else v for k, v in rv['oos'].items()},
          'years': [(y['year'], y['rule'], y.get('n', 0), round(y.get('avg', 0), 2), round(y.get('win', 0)), round(y.get('worst', 0), 1),
                     round(y['market'], 2) if y.get('market') is not None else None, y['rules_pos']) for y in rv['years']],
          'crash': {'n': len(ev), 'avg': round(sum(r for *_, r in ev) / len(ev), 2), 'days': len(by),
                    'top': [(k, n, round(s), round(s / n, 1)) for s, n, k in days[:5]],
                    'day_avg': round(sum(davg) / len(davg), 2), 'day_pos': round(sum(x > 0 for x in davg) / len(davg) * 100),
                    'wo_top5_n': len(rest), 'wo_top5': round(sum(rest) / len(rest), 2),
                    'per_year': {y: (len(v), round(sum(v) / len(v), 2)) for y, v in sorted(per_year.items())}}}
    out = d / 'strategy_blog_data.json'
    out.write_text(json.dumps({'A1': A1, 'B': B, 'A2': A2}, ensure_ascii=False), encoding='utf-8')
    print('저장', out, len(A1['eq']['dates']), len(B['series']['dates']))
    print(json.dumps(A2['crash'], ensure_ascii=False))


if __name__ == '__main__':
    main()
