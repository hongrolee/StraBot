"""순간 급등 세트 되돌려 보기: 과거 급등 54번의 1분봉을 '실제 봇 코드'(BotEngine.spike_tick)에 흘려 넣어 확인

- 급등마다 새 시뮬레이션 봇 (가상 100만 원, 급등 5분 전에 그 코인을 90만 원어치 들고 있음)
- 급등 5분 전 ~ 24시간 30분 뒤까지 1분씩: 실시간 모드와 같은 모양의 입력(1분 거래대금·직전 60분 평균·시가·종가·1시간 전 종가)
- 1분 안에서는 가격만 시가 → 저가 → 고가 → 종가를 잘게 나눠 차례로 넣음 (봇은 5초마다 그때 가격을 봄 · 검증도 저가를 먼저 봄)
- 봇이 남긴 거래 기록으로: 감지했나 · 판 가격 · 다시 산 가격 · 판 몫의 코인이 늘었나 → 검증 결과(static/data/spike_verify_90d.json)와 비교

데이터: data/spike_cache/*_90d_*.json (1분봉, 업비트 요청 없음). 결과: 콘솔 표 + data/spike_replay_result.json
실행:  venv\\Scripts\\python.exe tools\\spike_replay.py
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bot_core.bot_engine as be  # noqa: E402
from bot_core.bot_engine import BotEngine  # noqa: E402
from bot_core.simulation_engine import SimulationEngine  # noqa: E402

CACHE = ROOT / 'data' / 'spike_cache'
VERIFY = ROOT / 'static' / 'data' / 'spike_verify_90d.json'
OUT = ROOT / 'data' / 'spike_replay_result.json'
BEFORE, AFTER = 5, 24 * 60 + 30          # 급등 몇 분 전부터 몇 분 뒤까지
AVG = 60
STEPS = 8                                 # 1분 안 가격 구간마다 나눌 점 수


def minute_path(b):
    """1분봉 안의 가격 흐름 (시가 → 저가 → 고가 → 종가, 구간마다 STEPS 개)"""
    pts, legs = [], ((b['o'], b['l']), (b['l'], b['h']), (b['h'], b['c']))
    for a, z in legs:
        pts += [a + (z - a) * k / STEPS for k in range(1, STEPS + 1)]
    return [b['o']] + pts


class _NullLogger:
    def log_trade(self, *a, **k):
        pass


def load_bars(market):
    f = next(iter(sorted(CACHE.glob(f'{market}_90d_*.json'))), None)
    return json.loads(f.read_text(encoding='utf-8')) if f else None


def epoch(t):
    return datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()


def replay_one(ev, bars, rule, workdir):
    """급등 하나 → 봇이 한 일"""
    sym = ev['market']
    idx = {b['t']: i for i, b in enumerate(bars)}
    i0 = idx.get(ev['signal_t'])
    if i0 is None or i0 < AVG + BEFORE:
        return {'error': '1분봉 없음'}
    config = {'mode': 'simulation', 'risk_config': {'max_positions': 5},
              'trade_rules': {'buy': [], 'sell': [{'id': 'spike_pump', 'params': {k: rule[k] for k in ('mult', 'rise', 'rise_1h')},
                                                   'sell_mode': 'partial', 'portion': rule['portion']}], 'exclude': []},
              'simulation_config': {'initial_balance': 1_000_000}}
    e = BotEngine(config, data_dir=workdir)
    e.trade_logger = _NullLogger()
    e.simulation_engine = SimulationEngine(1_000_000)
    start = i0 - BEFORE
    e.simulation_engine.buy(sym, bars[start]['c'], 900_000 / bars[start]['c'], condition='되돌려 보기')
    qty0 = e._positions()[sym]['qty']
    vals = [b['v'] or 0 for b in bars]
    detected_t = None
    for i in range(start, min(len(bars), i0 + AFTER)):
        b = bars[i]
        path = minute_path(b)
        t_end = epoch(b['t']) + 60
        for k, p in enumerate(path[:-1]):            # 1분 안: 가격만 (급등 판단은 1분이 끝났을 때)
            e.spike_tick({'ready': True, 'minute': f'p{i}', 'coins': {}, 'prices': {sym: p}}, now=t_end - 60 + 60 * k / len(path))
        snap = {'ready': True, 'minute': i,
                'coins': {sym: {'value': vals[i], 'avg60': sum(vals[i - AVG:i]) / AVG, 'open': b['o'], 'close': b['c'],
                                'close_1h': bars[i - AVG]['c']}},
                'prices': {sym: b['c']}}
        before = sym in e.spike.state
        e.spike_tick(snap, now=t_end)
        if not before and sym in e.spike.state and detected_t is None:
            detected_t = b['t']
        if detected_t and sym not in e.spike.state:
            break                                    # 한 바퀴 끝 (다시 다 삼)
    items = json.loads((Path(workdir) / 'trades.json').read_text(encoding='utf-8')) if (Path(workdir) / 'trades.json').exists() else []
    sells = [t for t in items if t['side'] == 'sell']
    rebuys = [t for t in items if t['side'] == 'buy']
    sold_qty = sum(t['qty'] for t in sells)
    got_qty = sum(t['qty'] for t in rebuys)
    return {'detected_t': detected_t, 'sold': bool(sells), 'sell_price': sells[0]['price'] if sells else None,
            'rebuys': [round(t['price'], 8) for t in rebuys], 'done': bool(sells) and sym not in e.spike.state,
            'coin_change': (got_qty / sold_qty - 1) * 100 if sold_qty and sym not in e.spike.state else None,
            'qty_change': (e._positions()[sym]['qty'] / qty0 - 1) * 100 if sym in e._positions() else None}


def main():
    be.time.sleep = lambda s: None
    d = json.loads(VERIFY.read_text(encoding='utf-8'))
    rule, events = d['meta']['rule'], d['events']
    print(f'급등 {len(events)}번 · 규칙 {rule}', flush=True)
    t0 = time.time()
    cache, rows = {}, []
    for k, ev in enumerate(events, 1):
        m = ev['market']
        if m not in cache:
            cache[m] = load_bars(m)
        if not cache[m]:
            rows.append({'market': m, 'signal_t': ev['signal_t'], 'error': '1분봉 파일 없음'})
            continue
        with tempfile.TemporaryDirectory() as tmp:
            r = replay_one(ev, cache[m], rule, tmp)
        rows.append({'market': m, 'signal_t': ev['signal_t'], 'verify_sell': ev['sell_price'], 'verify_change': ev['coin_change'],
                     'verify_buys': [b['price'] for b in ev['buys']], **r})
        if len(cache) > 6:                           # 메모리: 오래된 코인 1분봉은 버림
            cache.pop(next(iter(cache)))
        print(f'  [{k}/{len(events)}] {m:<14} {ev["signal_t"]}  감지 {"O" if r.get("detected_t") else "X"}  '
              f'판 가격 {r.get("sell_price") or "-"} (검증 {ev["sell_price"]})  '
              f'판 몫 코인 {"%+.2f%%" % r["coin_change"] if r.get("coin_change") is not None else "-"} (검증 {ev["coin_change"]:+.2f}%)', flush=True)

    ok = [r for r in rows if not r.get('error')]
    det = [r for r in ok if r['detected_t']]
    same_t = [r for r in det if r['detected_t'] == r['signal_t']]
    sold = [r for r in ok if r['sold']]
    done = [r for r in ok if r['done'] and r['coin_change'] is not None]
    same_sell = [r for r in sold if abs(r['sell_price'] / r['verify_sell'] - 1) < 0.005]
    more = sum(r['coin_change'] > 0 for r in done)
    avg = sum(r['coin_change'] for r in done) / len(done) if done else 0
    vavg = sum(r['verify_change'] for r in done) / len(done) if done else 0
    print(f"\n── 되돌려 본 결과 ──\n  급등 감지 {len(det)}/{len(ok)} (같은 1분 {len(same_t)}) · 일부 판 것 {len(sold)} · 다시 사서 끝난 것 {len(done)}\n"
          f"  판 가격이 검증과 0.5% 안 {len(same_sell)}/{len(sold)}\n"
          f"  판 몫의 코인이 늘어난 경우 {more}/{len(done)} · 평균 {avg:+.2f}% (검증 평균 {vavg:+.2f}%)")
    OUT.write_text(json.dumps({'meta': {'made': datetime.now().isoformat(timespec='seconds'), 'rule': rule},
                               'summary': {'events': len(ok), 'detected': len(det), 'same_minute': len(same_t), 'sold': len(sold),
                                           'done': len(done), 'same_sell': len(same_sell), 'more': more, 'avg': avg, 'verify_avg': vavg},
                               'rows': rows}, ensure_ascii=False), encoding='utf-8')
    print(f'저장: {OUT.relative_to(ROOT)} · 걸린 시간 {time.time() - t0:.0f}초')


if __name__ == '__main__':
    main()
