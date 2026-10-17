"""1시간봉 받기: 여러 봇이 한꺼번에 불러도 같은 코인을 한 번만 · 오래된 값은 바로 쓰고 뒤에서 한 번만 새로 (2026-10-15)"""
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui.web_dashboard as wdm  # noqa: E402

W = wdm.WebDashboard


def fake(calls, delay=0.02):
    lock = threading.Lock()

    def get_candles(unit, market, count, to=None, priority=False):
        with lock:
            calls.append(market)
        time.sleep(delay)
        return [{'candle_date_time_utc': '2026-10-15T00:00:00', 'trade_price': 1.0, 'low_price': 1.0}]
    return types.SimpleNamespace(_hourly_cache={}, _get_candles=get_candles)


def test_many_bots_at_once_fetch_each_coin_once():
    calls = []
    me = fake(calls)
    markets = [f'KRW-C{i}' for i in range(20)]
    outs = []
    ts = [threading.Thread(target=lambda: outs.append(W._fetch_hourly(me, markets))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(calls) == sorted(markets)                   # 8번 불렀지만 코인마다 한 번만
    assert all(len(o) == 20 for o in outs)


def test_stale_cache_answers_now_and_refreshes_once_in_background():
    calls = []
    me = fake(calls, delay=0.05)
    markets = [f'KRW-C{i}' for i in range(10)]
    old = time.time() - 700
    me._hourly_cache.update({m: (old, {'t': [], 'c': [9.0], 'l': [9.0]}) for m in markets})
    t0 = time.time()
    for _ in range(5):
        out = W._fetch_hourly(me, markets)
        assert out['KRW-C0']['c'] == [9.0]                     # 기다리지 않고 옛 값
    assert time.time() - t0 < 0.3
    for _ in range(100):                                      # 뒤에서 새로 받기가 끝날 때까지
        if len(calls) >= 10 and not me._hourly_bg_lock.locked():
            break
        time.sleep(0.05)
    assert sorted(calls) == sorted(markets)                   # 다섯 번 불렀지만 새로 받기는 한 번
    assert W._fetch_hourly(me, markets)['KRW-C0']['c'] == [1.0]
