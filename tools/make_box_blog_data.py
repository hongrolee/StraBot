"""StraHub 블로그 6편(박스권 매매 검증)에 넣을 숫자·사례 뽑기 → data/box_blog_data.json

1·2차(90일 1분봉)는 그때 규칙 목록으로 다시 계산해 '두 기간 모두 플러스' 개수를 셈.
3·4차(1년 15분봉)는 --result3 / --result4 결과 파일에서 읽음. 사례 그래프는 4차 규칙으로 실제 거래 하나씩.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import box_backtest as bx  # noqa: E402

ROUND_GRIDS = {
    1: {'lookback': [24, 48, 72], 'width': [(3, 8), (4, 12), (5, 15)], 'zone': [0.2, 0.3], 'stop': [1, 2, 3],
        'entry': ['touch', 'bounce'], 'hold': [48, 96], 'exit': ['top'], 'market': [False]},
    2: {'lookback': [24, 48, 72], 'width': [(3, 8), (4, 12), (5, 15), (8, 20)], 'zone': [0.2, 0.3], 'stop': [1, 2, 3],
        'entry': ['touch', 'bounce'], 'hold': [96], 'exit': ['top', 'mid'], 'market': [False, True]},
}
CASE_RULE = {'lookback': 24, 'width': (8, 20), 'zone': 0.3, 'stop': 3, 'entry': 'touch', 'hold': 96, 'exit': 'mid'}


def round_90d(grid):
    """90일 1분봉, 9/1 전으로 고르고 9/1부터 대 봄 (한 프로세스에서 순서대로)"""
    bx.GRID.clear()
    bx.GRID.update(grid)
    bx.SPLIT = '2026-09-01'
    files = sorted(bx.CACHE.glob('KRW-*_90d_*.json'))
    trades, holds = {}, []
    for i, f in enumerate(files, 1):
        r = bx.coin_job(str(f), '2026-09-01')
        if not r:
            continue
        holds.append(r['hold'])
        for k, v in r['trades'].items():
            trades.setdefault(k, []).extend(v)
        print(f'  {i}/{len(files)}', end='\r', flush=True)
    return summarize(trades, holds)


def summarize(trades, holds):
    rows = []
    for k, v in trades.items():
        a, b = bx.split(sorted(v))
        rows.append({'key': k, 'train': bx.stats(a), 'test': bx.stats(b)})
    ok = [r for r in rows if r['train'].get('n', 0) >= 30]
    ok.sort(key=lambda r: r['train']['score'], reverse=True)
    both = [r for r in ok if r['train']['avg'] > 0 and r['test'].get('n', 0) >= 30 and r['test']['avg'] > 0]
    best = ok[0]
    return {'rules': len(ok), 'train_pos': sum(r['train']['avg'] > 0 for r in ok),
            'test_pos': sum(r['test'].get('avg', 0) > 0 for r in ok), 'both_pos': len(both),
            'best': best, 'hold_train': sum(h['train'] for h in holds) / len(holds) * 100,
            'hold_test': sum(h['test'] for h in holds) / len(holds) * 100}


def from_result(path):
    d = json.loads(Path(path).read_text(encoding='utf-8'))
    top = d['top'][0]
    wins = [t['ret'] for t in d['trades'] if t['ret'] > 0]
    losses = [t['ret'] for t in d['trades'] if t['ret'] <= 0]
    return {'rules': d['breadth']['rules'], 'train_pos': d['breadth']['train_pos'], 'test_pos': d['breadth']['test_pos'],
            'both_pos': d['breadth']['both_pos'], 'best': top, 'hold_train': d['compare']['hold']['train'],
            'hold_test': d['compare']['hold']['test'], 'nobox': d['compare']['nobox'], 'random': d['compare']['random'],
            'quarters': d.get('quarters'), 'split': d['meta']['split'],
            'win_avg': sum(wins) / len(wins) if wins else 0, 'loss_avg': sum(losses) / len(losses) if losses else 0,
            'stop_share': top['train'].get('stop')}


def cases(split):
    """4차 방식(50일 평균 위 · 박스 24시간 · 폭 8~20% · 아래 30%에서 사고 가운데에서 팜 · 아래 선 −3% 손절)으로
    대 본 기간의 실제 거래 중 '목표에 닿은 것'과 '손절된 것'을 하나씩 → 1시간 종가 흐름 + 박스 선"""
    files = sorted(bx.CACHE15.glob('KRW-*_15m_365d_*.json'))
    out = {}
    p = CASE_RULE
    for f in files:
        m = f.name.split('_')[0]
        if m in ('KRW-BTC',):
            continue
        b = bx.load_15m(f)
        H, L, C = bx.hourly(b)
        boxes = bx.box_series(H, L, C, p['lookback'], *p['width'])
        up = bx.trend_up(C)
        tr = bx.run(b, boxes, p['zone'], p['stop'], p['entry'], p['hold'], exit_at=p['exit'], allow=up)
        for s, e, r, why, fill, target, stop in tr:
            t = b['base'] + timedelta(minutes=bx.BAR * s)
            if t.strftime('%Y-%m-%d') < split:
                continue
            kind = 'good' if why == 'target' and r > 0.02 else 'bad' if why == 'stop' else None
            if not kind or kind in out:
                continue
            j = s // bx.PER_HOUR
            lo, hi = boxes[j]
            a, z = max(0, j - p['lookback'] - 6), min(len(C) - 1, e // bx.PER_HOUR + 18)
            out[kind] = {'m': m.replace('KRW-', ''), 'buy_t': t.isoformat(), 'sell_t': (b['base'] + timedelta(minutes=bx.BAR * e)).isoformat(),
                         'lo': lo, 'hi': hi, 'buy': fill, 'target': target, 'stop': stop, 'ret': r * 100,
                         'why': why,
                         'box_from': j - p['lookback'] - a, 'buy_k': j - a, 'sell_k': e // bx.PER_HOUR - a,
                         'start': (b['base'] + timedelta(hours=a)).isoformat(),
                         'pts': [round(x, 8) for x in C[a:z + 1]]}
        if len(out) == 2:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--result3', required=True)
    ap.add_argument('--result4', required=True)
    args = ap.parse_args()
    r3, r4 = from_result(args.result3), from_result(args.result4)
    import copy
    base_grid = copy.deepcopy(bx.GRID)
    rounds = {}
    for n, g in ROUND_GRIDS.items():
        print(f'{n}차 다시 계산', flush=True)
        rounds[n] = round_90d(g)
    bx.GRID.clear()
    bx.GRID.update(base_grid)
    rounds[3], rounds[4] = r3, r4
    data = {'rounds': rounds, 'cases': cases(r4['split'])}
    out = ROOT / 'data' / 'box_blog_data.json'
    out.write_text(json.dumps(data, ensure_ascii=False, default=list), encoding='utf-8')
    for n, r in rounds.items():
        print(n, r['rules'], r['train_pos'], r['test_pos'], r['both_pos'], r['best']['key'],
              '%.2f / %.2f' % (r['best']['train'].get('avg', 0), r['best']['test'].get('avg', 0)))
    for k, c in data['cases'].items():
        print(k, c['m'], c['buy_t'], c['why'], '%.2f' % c['ret'])
    print('저장', out)


if __name__ == '__main__':
    main()
