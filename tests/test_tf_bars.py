"""조건 시간 봉(4시간·1시간): 봉을 일봉 기간 시세와 같은 이름의 칸으로, 진행 중인 봉 빼고, 캐시 (업비트 요청 없음)"""
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui.web_dashboard import WebDashboard  # noqa: E402


def stub():
    calls = []

    def get_candles(unit, market, count, to=None):
        calls.append((unit, market, count))
        # 업비트처럼 최신순: [0] 은 진행 중인 봉
        return [{'trade_price': 200.0 - i, 'high_price': 210.0 - i, 'candle_acc_trade_price': 1e6 + i} for i in range(count)]

    s = types.SimpleNamespace(_get_candles=get_candles, _tf_cache={'4h': {}, '1h': {}},
                              _tf_locks={'4h': threading.Lock(), '1h': threading.Lock()}, TF_UNIT=WebDashboard.TF_UNIT,
                              _long_fields=WebDashboard._long_fields)
    return s, calls


def test_tf_bars_shape_and_cache():
    s, calls = stub()
    got = WebDashboard._fetch_tf_bars(s, ['KRW-A', 'KRW-B'], '4h')
    a = got['KRW-A']
    assert len(a['day_closes']) == 30 and a['day_closes'][-1] == 199.0 and a['day_closes'][0] == 170.0   # 오래된 순, 진행 중 봉(200) 뺌
    assert a['day_highs'][-1] == 209.0 and len(a['day_values']) == 30
    # 긴 기간(120봉) 칸: 같은 요청으로 받아 오래된 순 · 끝은 같은 봉
    assert len(a['day_closes_long']) == 120 and a['day_closes_long'][-1] == 199.0 and a['day_closes_long'][0] == 80.0
    assert len(a['day_lows_long']) == 120 and len(a['day_lows']) == 30
    assert {c[0] for c in calls} == {'minutes/240'}
    WebDashboard._fetch_tf_bars(s, ['KRW-A', 'KRW-B'], '4h')      # 캐시 → 다시 요청 안 함
    assert len(calls) == 2
    WebDashboard._fetch_tf_bars(s, ['KRW-A'], '1h')
    assert calls[-1][0] == 'minutes/60'
