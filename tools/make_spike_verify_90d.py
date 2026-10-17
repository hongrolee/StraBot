"""'순간 급등 · 일부 팔고 다시 사기' 과거 90일 결과 파일 만들기 → static/data/spike_verify_90d.json

봇과 같은 계산(bot_core/spike_verify.py)을 받아 둔 1분봉(data/spike_cache/*_90d_*.json,
tools/spike_backtest.py 로 받은 것)에 돌려, 봇 화면의 '검증 보기'와 검증 결과 창이 쓰는 숫자·표·캔들을 만든다.
화면의 숫자는 모두 이 파일에서 나온다 (손으로 고치지 말 것).

실행: venv\\Scripts\\python.exe tools\\make_spike_verify_90d.py
"""
import collections
import gc
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot_core import spike_verify as sv  # noqa: E402
from bot_core.spike_rebuy import DEFAULTS  # noqa: E402

SPLIT = '2026-09-01'          # 이 날 전(7~8월)으로 기준을 정했고, 이후(9월)에 그대로 대 봄


def r6(x):
    return float(f'{x:.6g}') if isinstance(x, (int, float)) else x


def main():
    files = sorted(glob.glob(str(ROOT / 'data' / 'spike_cache' / '*_90d_*.json')))
    if not files:
        sys.exit('data/spike_cache 에 90일 1분봉이 없어요. tools/spike_backtest.py --days 90 --coins 50 으로 먼저 받아 주세요.')
    events, nofilter, rand = [], [], []
    first_t, last_t = None, None
    for f in files:
        m = os.path.basename(f).split('_')[0]
        bars = json.load(open(f, encoding='utf-8'))
        first_t = min(first_t or bars[0]['t'], bars[0]['t'])
        last_t = max(last_t or bars[-1]['t'], bars[-1]['t'])
        r = sv.simulate(bars)
        for e in r['events']:
            if e.get('coin_change') is None:
                continue
            ch = sv.candles_around(bars, e)
            events.append({'market': m, 'signal_t': e['signal_t'], 'sell_t': e['sell_t'], 'sell_price': r6(e['sell_price']),
                           'peak': r6(e['peak']), 'mult': round(e['mult'], 2), 'rise': round(e['rise'], 2),
                           'rise_1h': round(e['rise_1h'], 2), 'how': e['how'], 'coin_change': round(e['coin_change'], 3),
                           'buys': [{'t': b['t'], 'price': r6(b['price']), 'why': b['why'], 'parts': b.get('parts', 1)} for b in e['buys']],
                           'minutes': ch['minutes'],
                           'candles': [[c['t'], r6(c['o']), r6(c['h']), r6(c['l']), r6(c['c'])] for c in ch['candles']]})
        nofilter += [e['coin_change'] for e in sv.simulate(bars, {'rise_1h': 0})['events'] if e.get('coin_change') is not None]
        rand += sv.random_baseline(bars)
        del bars
        gc.collect()
        print(m, flush=True)
    events.sort(key=lambda e: e['signal_t'])
    g = [e['coin_change'] for e in events]
    portion = DEFAULTS['portion'] / 100
    weeks = collections.defaultdict(float)
    months = collections.defaultdict(list)
    cum, curve = 0.0, []
    for e in events:
        d = datetime.fromisoformat(e['signal_t'])
        weeks[d.isocalendar()[:2]] += portion * e['coin_change']
        months[e['signal_t'][:7]].append(e['coin_change'])
        cum += portion * e['coin_change']
        curve.append({'t': e['signal_t'], 'cum': round(cum, 3)})
    train = [e['coin_change'] for e in events if e['signal_t'] < SPLIT]
    test = [e['coin_change'] for e in events if e['signal_t'] >= SPLIT]
    avg = lambda v: sum(v) / len(v) if v else 0.0
    out = {
        'meta': {'from': first_t[:10], 'to': last_t[:10], 'coins': len(files), 'fee_pct': sv.FEE * 100, 'slip_pct': sv.SLIP * 100,
                 'rule': {k: DEFAULTS[k] for k in ('mult', 'rise', 'rise_1h', 'pullback', 'portion', 'dips', 'max_wait_h')},
                 'made': datetime.now().isoformat(timespec='seconds')},
        'stats': {**sv.summarize(events), 'weeks': len(weeks), 'weeks_neg': sum(v < 0 for v in weeks.values())},
        'split': {'train': {'n': len(train), 'more': sum(x > 0 for x in train), 'avg': avg(train)},
                  'test': {'n': len(test), 'more': sum(x > 0 for x in test), 'avg': avg(test)}},
        'compare': {'random': avg(rand), 'random_n': len(rand), 'nofilter': avg(nofilter), 'nofilter_n': len(nofilter),
                    'rule': avg(g)},
        'months': [{'month': k, 'n': len(v), 'avg': avg(v), 'more': sum(x > 0 for x in v)} for k, v in sorted(months.items())],
        'curve': curve,
        'events': events,
    }
    path = ROOT / 'static' / 'data' / 'spike_verify_90d.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({k: out[k] for k in ('meta', 'stats', 'split', 'compare', 'months')}, ensure_ascii=False, indent=1))
    print('저장:', path, f'{path.stat().st_size / 1024:.0f}KB')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
