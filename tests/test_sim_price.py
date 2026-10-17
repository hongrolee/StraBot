"""가상 계좌 가격: 코인이 수백 개여도 업비트 요청은 100개당 하나, 최소 간격·429 쉬기 (업비트 요청 없음)"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gui.web_dashboard as wdm  # noqa: E402
from gui.web_dashboard import WebDashboard  # noqa: E402

CODES = [f'KRW-C{i:03d}' for i in range(250)]


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def dash():
    from utils.upbit_pace import UpbitPacer
    d = types.SimpleNamespace(_price_cache={}, _pacer=UpbitPacer(sleep=lambda s: None), _get_krw_markets=lambda: [{'market': c} for c in CODES],
                              PRICE_TTL=5, PRICE_MIN_GAP=3, PRICE_BACKOFF=15)
    d._refresh_prices = lambda: WebDashboard._refresh_prices(d)
    return d


def test_many_coins_one_batch(monkeypatch):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params['markets'].count(',') + 1)
        return Resp(200, [{'market': m, 'trade_price': 100.0} for m in params['markets'].split(',')])
    import requests
    monkeypatch.setattr(requests, 'get', get)
    d = dash()
    prices = [WebDashboard._sim_price(d, c) for c in CODES]
    assert all(p == 100.0 for p in prices)
    assert calls == [100, 100, 50]                     # 250개 → 요청 3번 (코인마다 따로 아님)
    assert WebDashboard._sim_price(d, 'KRW-NOPE') is None and len(calls) == 3    # 최소 간격 안: 다시 안 받음


def test_rate_limit_backs_off_and_keeps_old_price(monkeypatch):
    calls = []
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: (calls.append(1), Resp(429, {}))[1])
    d = dash()
    d._price_cache['KRW-C000'] = (0, 123.0)            # 오래된 시세
    assert WebDashboard._sim_price(d, 'KRW-C000') == 123.0
    assert WebDashboard._sim_price(d, 'KRW-C000') == 123.0
    assert len(calls) == 1 and d._price_next_fetch > wdm.time.time() + 10       # 429 → 15초 쉼
