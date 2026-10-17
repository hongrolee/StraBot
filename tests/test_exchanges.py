"""빗썸 계좌 (bot_core/exchanges.py): 업비트와 같은 함수 모양 · JWT · 주문 본문 · 거절 사유 · 거래소 정보"""
import hashlib
from urllib.parse import urlencode

import jwt
import pytest

SECRET = 'bithumb-test-secret-0123456789abcdef'

from bot_core.exchanges import (BithumbAccount, BithumbRequestError, api_keys, exchange_of, fee_rate, is_live)
from bot_core.exchange_wrapper import ExchangeWrapper


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.ok = status < 400
        self.text = str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class FakeSession:
    """빗썸 서버 흉내: 보낸 요청을 남기고 정해 둔 응답을 돌려줌"""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _go(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'params': params, 'json': json, 'headers': headers})
        path = url.replace('https://api.bithumb.com', '')
        r = self.routes.get((method, path))
        return r(params or json) if callable(r) else (r or Resp({'error': {'name': 'not_found'}}, 404))

    def get(self, url, **kw):
        return self._go('GET', url, **kw)

    def post(self, url, **kw):
        return self._go('POST', url, **kw)

    def delete(self, url, **kw):
        return self._go('DELETE', url, **kw)


def token_of(call):
    return jwt.decode(call['headers']['Authorization'].split(' ', 1)[1], SECRET, algorithms=['HS256'])


def test_balances_and_free_balance():
    s = FakeSession({('GET', '/v1/accounts'): Resp([
        {'currency': 'KRW', 'balance': '120000.5', 'locked': '1000', 'avg_buy_price': '0'},
        {'currency': 'BTC', 'balance': '0.01', 'locked': '0.002', 'avg_buy_price': '100000000'}])})
    a = BithumbAccount('ACC', SECRET, session=s)
    assert a.get_balance('KRW') == pytest.approx(120000.5)
    assert a.get_balance('KRW-BTC') == pytest.approx(0.01)            # 주문 가능 수량 (묶인 것 뺌)
    t = token_of(s.calls[0])
    assert t['access_key'] == 'ACC' and t['nonce'] and t['timestamp'] > 0 and 'query_hash' not in t
    # ExchangeWrapper 가 업비트처럼 씀: 보유 = 주문 가능 + 묶인 것
    ex = ExchangeWrapper(account=a)
    assert ex.get_all_positions() == {'BTC': (pytest.approx(0.012), 100000000.0)}


def test_market_orders_body_and_query_hash():
    def order(body):
        return Resp({'uuid': 'u1', 'side': body['side'], 'ord_type': body['ord_type'], 'state': 'wait', 'market': body['market']})
    s = FakeSession({('POST', '/v1/orders'): order})
    a = BithumbAccount('ACC', SECRET, session=s)
    r = a.buy_market_order('KRW-XRP', 10000)
    assert r['uuid'] == 'u1'
    body = s.calls[0]['json']
    assert body == {'market': 'KRW-XRP', 'side': 'bid', 'price': '10000', 'ord_type': 'price'}
    assert token_of(s.calls[0])['query_hash'] == hashlib.sha512(urlencode(body).encode()).hexdigest()
    a.sell_market_order('KRW-XRP', 1.23456789)
    assert s.calls[1]['json'] == {'market': 'KRW-XRP', 'side': 'ask', 'volume': '1.23456789', 'ord_type': 'market'}


def test_rejection_becomes_readable_error_and_wrapper_returns_false():
    s = FakeSession({('POST', '/v1/orders'): Resp({'error': {'name': 'insufficient_funds_bid', 'message': 'x'}}, 400)})
    a = BithumbAccount('ACC', SECRET, session=s)
    with pytest.raises(BithumbRequestError) as e:
        a.buy_market_order('KRW-XRP', 10000)
    assert e.value.status == 400 and e.value.name == 'insufficient_funds_bid' and '빗썸 거절' in str(e.value)
    ok, msg, order = ExchangeWrapper(account=a).buy_market_order('KRW-XRP', 10000)
    assert not ok and order is None and '원화가 모자랍니다' in msg


def test_order_lookup_and_cancel():
    s = FakeSession({('GET', '/v1/order'): Resp({'uuid': 'u9', 'state': 'done', 'executed_volume': '2'}),
                     ('DELETE', '/v1/order'): Resp({'uuid': 'u9', 'state': 'cancel'}),
                     ('GET', '/v1/orders'): Resp([{'uuid': 'a'}, {'uuid': 'b'}])})
    a = BithumbAccount('ACC', SECRET, session=s)
    assert a.get_order('u9')['executed_volume'] == '2'
    assert s.calls[-1]['params'] == {'uuid': 'u9'}
    assert len(a.get_order('KRW-BTC')) == 2 and s.calls[-1]['params']['state'] == 'wait'
    assert a.cancel_order('u9')['state'] == 'cancel'


def test_exchange_helpers():
    assert is_live('upbit') and is_live('bithumb') and not is_live('sim')
    assert exchange_of('sim') == 'upbit' and exchange_of('bithumb') == 'bithumb'
    assert fee_rate('upbit') == 0.0005
    assert fee_rate('bithumb', {'bithumb_fee_pct': 0.04}) == pytest.approx(0.0004)
    assert fee_rate('bithumb', {'bithumb_fee_pct': 'x'}) == 0.0025              # 잘못된 값이면 기본값
    cfg = {'api_key': 'U', 'api_secret': 'US', 'bithumb_api_key': 'B', 'bithumb_api_secret': 'BS'}
    assert api_keys('upbit', cfg) == ('U', 'US') and api_keys('bithumb', cfg) == ('B', 'BS')


def test_bithumb_feed_uses_bithumb_api_warning_and_kst_day(monkeypatch, tmp_path):
    """빗썸 시세 묶음: 빗썸 주소 · market_warning → 유의 · 기간 시세 '오늘'은 KST 0시 기준"""
    import types
    import requests
    from datetime import datetime, timezone
    from gui.exchange_feed import make_feed
    from gui.web_dashboard import WebDashboard
    owner = types.SimpleNamespace(user_data_dir=tmp_path, config={}, coin_facts=None, bots=None)
    f = make_feed(owner, 'bithumb')
    urls = []

    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        return Resp([{'market': 'KRW-AAA', 'korean_name': '에이', 'market_warning': 'CAUTION'},
                     {'market': 'KRW-BBB', 'korean_name': '비', 'market_warning': 'NONE'}, {'market': 'BTC-AAA'}])
    monkeypatch.setattr(requests, 'get', fake_get)
    assert f._get_market_events()['warning'] == {'KRW-AAA'}
    assert [m['market'] for m in f._get_krw_markets()] == ['KRW-AAA', 'KRW-BBB']
    assert all(u.startswith('https://api.bithumb.com/') for u in urls)
    # 오늘: 빗썸 = KST 날짜, 업비트 = UTC 날짜
    kst = (datetime.now(timezone.utc).timestamp() + 9 * 3600)
    assert f._period_day_key() == datetime.fromtimestamp(kst, timezone.utc).date().isoformat()
    up = types.SimpleNamespace(DAY_START_KST=9)
    assert WebDashboard._period_day_key(up) == datetime.now(timezone.utc).date().isoformat()


def test_volume_spike_follows_candle_unit():
    """거래대금 급증: 일봉 = 24시간 ÷ 하루 평균, 봉 단위(4시간 등) = 방금 끝난 봉 ÷ 그 앞 N봉 평균"""
    from bot_core import conditions as cd
    cond = cd.resolve('volume_spike', [])
    coin = {'currency': 'AAA', 'trade_value_24h': 1000, 'current_price': 1, 'change_rate': 0}
    day = {'KRW-AAA': {'day_values': [100] * 20}}
    bars = {'KRW-AAA': {'day_values': [100] * 20 + [400]}}        # 방금 끝난 4시간봉이 평소의 4배
    assert [c['currency'] for c in cd.run_condition(cond, {'days': 20, 'mult': 3}, [coin], day)] == ['AAA']      # 일봉: 1000 ÷ 100 = 10배
    hit = cd.run_condition(cond, {'days': 20, 'mult': 3, 'tf': '4h'}, [coin], bars)
    assert hit and abs(hit[0]['s_mult'] - 4) < 1e-9                  # 봉 단위: 24시간 거래대금이 아니라 방금 끝난 봉
    assert not cd.run_condition(cond, {'days': 20, 'mult': 5, 'tf': '4h'}, [coin], bars)


def test_ticker_drops_delisted_market_instead_of_failing(tmp_path, monkeypatch):
    """업비트는 묶음에 없는 마켓이 하나라도 있으면 통째로 404 — 마켓 목록을 새로 받아 다시 묻고, 그래도면 반씩 나눠 뺌"""
    import json as _json
    from gui.web_dashboard import WebDashboard
    cfg = tmp_path / 'config' / 'bot_config.json'
    cfg.parent.mkdir()
    cfg.write_text(_json.dumps({'app_mode': 'sim', 'bots': []}), encoding='utf-8')
    d = WebDashboard(str(cfg), port=0, user_data_dir=tmp_path)
    live = {'KRW-A', 'KRW-B', 'KRW-C'}
    stale = [{'market': m, 'korean_name': m} for m in ('KRW-A', 'KRW-B', 'KRW-GONE', 'KRW-C')]
    monkeypatch.setattr(d, '_get_krw_markets', lambda: stale)          # 목록을 새로 받아도 아직 남아 있는 경우까지

    class Resp:
        def __init__(self, codes):
            self.codes = codes
            self.status_code = 404 if any(c not in live for c in codes) else 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{'market': c, 'trade_price': 1.0} for c in self.codes]

    class S:
        def get(self, url, params=None, timeout=None):
            return Resp(params['markets'].split(','))
    d._ticker_session = S()
    assert set(d._krw_tickers()) == live
