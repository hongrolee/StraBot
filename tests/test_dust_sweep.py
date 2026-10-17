"""작은 코인 정리: 5,000원 미만 코인을 5,300원어치 더 사서 합친 뒤 전부 팖 (web_dashboard._dust_sweep · _dust_list)"""
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui.web_dashboard as wdm  # noqa: E402

W = wdm.WebDashboard


class FakeAccount:
    """업비트처럼: 5,000원 미만 주문 거절 · 시장가 매수는 바로 잔고에"""
    def __init__(self, krw, coins, prices):
        self.krw, self.coins, self.prices, self.orders = krw, dict(coins), prices, []

    def get_balances(self):
        return [{'currency': 'KRW', 'balance': str(self.krw)}] + [{'currency': c, 'balance': str(q)} for c, q in self.coins.items()]

    def buy_market_order(self, sym, krw):
        if krw < 5000 or krw * 1.0005 > self.krw:
            return {'error': {'message': '주문 불가'}}
        cur = sym.split('-')[1]
        self.krw -= krw * 1.0005
        self.coins[cur] = self.coins.get(cur, 0) + krw / self.prices[sym]
        self.orders.append(('buy', sym, krw))
        return {'uuid': 'b'}

    def sell_market_order(self, sym, qty):
        cur = sym.split('-')[1]
        if qty * self.prices[sym] < 5000:
            return {'error': {'message': '최소 주문금액 미만'}}
        self.coins[cur] -= qty
        self.krw += qty * self.prices[sym] * 0.9995
        self.orders.append(('sell', sym, qty))
        return {'uuid': 's'}


def fake_self(acct, prices):
    return types.SimpleNamespace(
        _account=lambda mode=None: acct, _fee=lambda mode=None: 0.0005, app_mode=lambda: 'upbit',
        _prices_of=lambda syms, mode=None: {s: prices[s] for s in syms if s in prices},
        _krw_market_set=lambda mode=None: set(prices), _get_coin_korean_names=lambda: {'KRW-A': '에이'},
        _sell_lock=threading.Lock(), _selling=set(), DUST_MIN_KRW=W.DUST_MIN_KRW, DUST_TOPUP_KRW=W.DUST_TOPUP_KRW)


def test_dust_list_only_small_tradable_coins(monkeypatch):
    prices = {'KRW-A': 100.0, 'KRW-B': 1000.0}
    acct = FakeAccount(100_000, {'A': 30, 'B': 10, 'C': 5}, prices)          # A 3,000원 · B 10,000원 · C 원화 마켓 없음
    out = W._dust_list(fake_self(acct, prices))
    assert [c['currency'] for c in out['coins']] == ['A'] and out['coins'][0]['name'] == '에이'


def test_sweep_tops_up_then_sells_everything(monkeypatch):
    monkeypatch.setattr(wdm.time, 'sleep', lambda s: None)
    prices = {'KRW-A': 100.0, 'KRW-B': 1000.0}
    acct = FakeAccount(20_000, {'A': 30, 'B': 3}, prices)                    # A 3,000원 · B 3,000원
    job = {'mode': 'upbit', 'total': 2, 'done': 0, 'items': []}
    W._dust_sweep(fake_self(acct, prices), job, ['A', 'B'])
    assert all(i['ok'] for i in job['items']) and job['done'] == 2
    assert acct.coins['A'] < 1e-9 and acct.coins['B'] < 1e-9
    assert [o[0] for o in acct.orders] == ['buy', 'sell', 'buy', 'sell']


def test_sweep_stops_coin_when_not_enough_krw(monkeypatch):
    monkeypatch.setattr(wdm.time, 'sleep', lambda s: None)
    prices = {'KRW-A': 100.0}
    acct = FakeAccount(1_000, {'A': 30}, prices)
    job = {'mode': 'upbit', 'total': 1, 'done': 0, 'items': []}
    W._dust_sweep(fake_self(acct, prices), job, ['A'])
    assert not job['items'][0]['ok'] and '원화가 모자랍니다' in job['items'][0]['message'] and acct.orders == []
