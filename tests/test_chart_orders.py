"""차트에서 걸어 두는 주문: 종류 정하기 · 호가 단위 · 시뮬레이션 지정가(묶기 · 취소 · 체결) · 감시 주문 · 화면 API"""
import json

import pytest

from bot_core import chart_orders as co
from bot_core.virtual_account import VirtualAccount


def test_decide_kind_by_side_and_price():
    assert co.decide_kind('bid', 90, 100) == 'limit' and co.decide_kind('bid', 110, 100) == 'stop'
    assert co.decide_kind('ask', 110, 100) == 'limit' and co.decide_kind('ask', 90, 100) == 'stop'


def test_snap_price_to_tick():
    assert co.snap_price(12_347, 'bid', 10) == 12_340 and co.snap_price(12_341, 'ask', 10) == 12_350
    assert co.snap_price(1.2345, 'bid', 0.001) == 1.234 and co.snap_price(1.2341, 'ask', 0.001) == 1.235
    assert co.snap_price(12_340, 'ask', 10) == 12_340                      # 이미 맞으면 그대로
    assert co.table_tick(50_000_000) == 1000 and co.table_tick(15_000) == 10 and co.table_tick(0.5) == 0.001


@pytest.fixture
def sim(tmp_path):
    prices = {'KRW-BTC': 100_000.0}
    fills = []
    acct = VirtualAccount(tmp_path / 'sim.json', lambda m: prices.get(m), initial_krw=1_000_000, on_order=fills.append)
    acct.prices, acct.fills = prices, fills
    return acct


def test_sim_limit_locks_cancels_and_fills_at_limit(sim):
    o = sim.place_limit('KRW-BTC', 'bid', 90_000, krw=100_000)
    assert 'error' not in o
    assert sim.get_balance('KRW') == pytest.approx(1_000_000 - 100_000 * 1.0005)      # 수수료까지 묶임
    assert sim.get_balances()[0]['locked'] == str(100_000 * 1.0005)
    assert sim.buy_market_order('KRW-BTC', 950_000).get('error')                      # 묶인 돈은 못 씀
    assert sim.cancel_limit(o['id'])['state'] == 'cancel' and sim.get_balance('KRW') == 1_000_000
    o = sim.place_limit('KRW-BTC', 'bid', 90_000, krw=90_000)
    done = sim.fill_limit(o['id'])
    assert done['avg_price'] == '90000.0' and done['ord_type'] == 'limit' and sim.fills[-1]['ord_type'] == 'limit'
    assert sim.get_balance('KRW-BTC') == pytest.approx(1.0) and sim.state['krw'] == pytest.approx(1_000_000 - 90_000 * 1.0005)
    # 매도 지정가: 수량이 묶이고, 체결되면 그 가격으로 받음
    s = sim.place_limit('KRW-BTC', 'ask', 120_000, qty=1.0)
    assert sim.get_balance('KRW-BTC') == pytest.approx(0) and sim.sell_market_order('KRW-BTC', 0.5).get('error')
    assert sim.place_limit('KRW-BTC', 'ask', 120_000, qty=0.1).get('error')            # 더 걸 수량 없음
    sim.fill_limit(s['id'])
    assert 'BTC' not in sim.state['coins'] and sim.pending_orders() == []
    assert sim.state['krw'] == pytest.approx(1_000_000 - 90_000 * 1.0005 + 120_000 * 0.9995)


class FakeAccount:
    def __init__(self, krw=1_000_000, coins=None):
        self.krw, self.coins, self.orders = krw, dict(coins or {}), []

    def get_balance(self, t='KRW'):
        cur = t.split('-')[-1]
        return self.krw if cur == 'KRW' else self.coins.get(cur, 0.0)

    def buy_market_order(self, m, krw):
        self.orders.append(('buy', m, krw))
        return {'uuid': 'b'}

    def sell_market_order(self, m, q):
        self.orders.append(('sell', m, q))
        return {'uuid': 's'}


def make_watcher(tmp_path, sim, accounts, blocked=None):
    stores = {m: co.ChartOrderStore(tmp_path / f'co_{m}.json') for m in ('upbit', 'sim')}
    notes = []
    w = co.ChartOrderWatcher(stores, sim, lambda m: sim.prices.get(m), lambda m: accounts[m],
                             lambda: blocked, notify=lambda msg, lv: notes.append((lv, msg)))
    return w, stores, notes


def test_watch_orders_fire_when_price_crosses(tmp_path, sim):
    up = FakeAccount(coins={'BTC': 0.3})
    w, stores, notes = make_watcher(tmp_path, sim, {'upbit': up, 'sim': sim})
    buy = stores['upbit'].add('KRW-BTC', 'bid', 105_000, krw=50_000)              # 뚫으면 사기 (dir 없는 예전 것 = 위)
    sell = stores['upbit'].add('KRW-BTC', 'ask', 95_000, qty=0.5)                 # 떨어지면 팔기 (가진 0.3만)
    w.check()
    assert up.orders == [] and len(stores['upbit'].waiting()) == 2
    sim.prices['KRW-BTC'] = 106_000
    w.check()
    assert up.orders == [('buy', 'KRW-BTC', 50_000)] and stores['upbit'].recent()[0]['state'] == 'done'
    sim.prices['KRW-BTC'] = 94_000
    w.check()
    assert up.orders[-1] == ('sell', 'KRW-BTC', 0.3) and stores['upbit'].waiting() == []
    # 다시 켜도(파일에서 읽어도) 끝난 주문은 다시 실행하지 않음
    again = co.ChartOrderStore(tmp_path / 'co_upbit.json')
    assert again.waiting() == [] and {o['id'] for o in again.recent()} == {buy['id'], sell['id']}
    assert all(lv == 'success' for lv, _ in notes)


def test_watch_buy_refused_when_live_blocked_and_sim_limit_fills(tmp_path, sim):
    up = FakeAccount()
    w, stores, notes = make_watcher(tmp_path, sim, {'upbit': up, 'sim': sim}, blocked='계정 권한이 필요합니다.')
    stores['upbit'].add('KRW-BTC', 'bid', 101_000, krw=50_000)
    lim = sim.place_limit('KRW-BTC', 'bid', 99_000, krw=99_000)
    sim.prices['KRW-BTC'] = 102_000
    w.check()
    failed = stores['upbit'].recent()[0]
    assert up.orders == [] and failed['state'] == 'failed' and '권한' in failed['note'] and notes[-1][0] == 'error'
    assert sim.pending_orders()[0]['id'] == lim['id']                               # 아직 안 내려옴
    sim.prices['KRW-BTC'] = 98_500
    w.check()
    assert sim.pending_orders() == [] and sim.get_balance('KRW-BTC') == pytest.approx(1.0)
    assert sim.fills[-1]['avg_price'] == '99000.0'                                  # 지정 가격 그대로


# ── 화면 API ──
@pytest.fixture
def dash(tmp_path, monkeypatch):
    import pyupbit
    from gui.web_dashboard import WebDashboard
    cfg = tmp_path / 'config' / 'bot_config.json'
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({'app_mode': 'upbit', 'bots': []}), encoding='utf-8')
    d = WebDashboard(str(cfg), port=0, user_data_dir=tmp_path)
    d.fake = FakeAccount(coins={'BTC': 2.0})
    monkeypatch.setattr(d, '_account', lambda mode=None: d.sim_account if (mode or d.app_mode()) == 'sim' else d.fake)
    monkeypatch.setattr(d, '_sim_price', lambda m: 100_000.0)
    monkeypatch.setattr(pyupbit, 'get_tickers', lambda fiat='KRW', **k: ['KRW-BTC'])
    monkeypatch.setattr(co, 'tick_size', lambda m, p: 100)
    placed, cancelled = [], []
    monkeypatch.setattr(co, 'upbit_place_limit', lambda a, m, s, p, v: placed.append((m, s, p, v)) or {
        'uuid': 'u-1', 'market': m, 'side': s, 'ord_type': 'limit', 'price': str(p), 'volume': str(v), 'remaining_volume': str(v)})
    monkeypatch.setattr(co, 'upbit_open_orders', lambda a, m=None: [{
        'uuid': 'u-app', 'market': 'KRW-BTC', 'side': 'ask', 'ord_type': 'limit', 'price': '130000', 'volume': '0.5',
        'remaining_volume': '0.5', 'executed_volume': '0'}])
    monkeypatch.setattr(co, 'upbit_cancel', lambda a, u: cancelled.append(u) or {'uuid': u, 'state': 'cancel', 'executed_volume': '0.2'})
    d.placed, d.cancelled = placed, cancelled
    d.client = d.app.test_client()
    return d


def test_api_everything_is_a_reservation(dash):
    """업비트 모드여도 업비트에는 걸지 않음: 아래 = 내려오면, 위 = 오르면 (원화 · 코인이 묶이지 않음)"""
    c = dash.client
    r = c.post('/api/chart-orders', json={'market': 'BTC', 'side': 'bid', 'price': 90_050, 'preview': True}).get_json()
    assert r['dir'] == 'down' and r['price'] == 90_000 and r['krw_free'] == 1_000_000      # 호가 단위로 내림
    r = c.post('/api/chart-orders', json={'market': 'BTC', 'side': 'bid', 'price': 90_050, 'krw': 45_000}).get_json()
    assert r['success'] and r['order']['source'] == 'watch' and r['order']['dir'] == 'down' and dash.placed == []
    r = c.post('/api/chart-orders', json={'market': 'BTC', 'side': 'ask', 'price': 120_000, 'qty': 1}).get_json()
    assert r['success'] and r['order']['dir'] == 'up' and '예약' in r['message'] and dash.placed == []
    assert c.post('/api/chart-orders', json={'market': 'BTC', 'side': 'ask', 'price': 120_000, 'qty': 5}).status_code == 400
    assert c.post('/api/chart-orders', json={'market': 'BTC', 'side': 'bid', 'price': 90_000, 'krw': 3000}).status_code == 400
    lst = c.get('/api/chart-orders?market=KRW-BTC').get_json()
    assert [o['source'] for o in lst['orders']].count('watch') == 2 and lst['orders'][0]['id'] == 'u-app'   # 업비트 앱에서 건 것도 보임
    r = c.delete('/api/chart-orders/u-app?source=upbit').get_json()
    assert r['success'] and dash.cancelled == ['u-app'] and '0.2개가 이미 체결' in r['message']
    wid = next(o['id'] for o in lst['orders'] if o['source'] == 'watch')
    assert c.delete(f'/api/chart-orders/{wid}?source=watch').get_json()['success']
    assert c.delete(f'/api/chart-orders/{wid}?source=watch').status_code == 404


def test_api_old_sim_limit_still_cancellable(dash):
    dash.config['app_mode'] = 'sim'
    old = dash.sim_account.place_limit('KRW-BTC', 'bid', 80_000, krw=100_000)      # 예전에 건 시뮬레이션 지정가
    lst = dash.client.get('/api/chart-orders?market=BTC').get_json()
    assert [o['source'] for o in lst['orders']] == ['sim'] and lst['mode'] == 'sim'
    assert dash.client.delete(f"/api/chart-orders/{old['id']}?source=upbit").status_code == 400     # 모드가 다른 출처는 안 됨
    assert dash.client.delete(f"/api/chart-orders/{old['id']}?source=sim").get_json()['success']
    assert dash.sim_account.pending_orders() == []


def test_resume_after_gap_holds_and_asks(tmp_path, sim):
    """꺼져 있던 동안 지나간 예약은 실행하지 않고 멈춤 → 지금 실행 · 다시 지켜보기 · 취소"""
    up = FakeAccount(coins={'BTC': 1.0})
    stores = {m: co.ChartOrderStore(tmp_path / f'co_{m}.json') for m in ('upbit', 'sim')}
    beat = tmp_path / 'beat.txt'
    notes = []
    mk = lambda: co.ChartOrderWatcher(stores, sim, lambda m: sim.prices.get(m), lambda m: up, lambda: None,
                                      notify=lambda msg, lv: notes.append((lv, msg)), beat_path=beat)
    w = mk()
    sell = stores['upbit'].add('KRW-BTC', 'ask', 95_000, qty=1.0, dir='down')
    buy = stores['upbit'].add('KRW-BTC', 'bid', 120_000, krw=50_000, dir='up')
    w.check(t=1000)                                                    # 처음 (기록 없음) → 아무것도 안 지남
    assert beat.read_text() == '1000'
    sim.prices['KRW-BTC'] = 90_000                                     # 꺼져 있던 동안 내려감
    w2 = mk()
    w2.check(t=1000 + 3600)
    assert up.orders == [] and [o['state'] for o in stores['upbit'].active()] == ['hold', 'wait']
    assert '꺼져 있던' in stores['upbit'].get(sell['id'])['note'] and notes[-1][0] == 'warning'
    r = w2.run_now('upbit', sell['id'])                                # 지금 실행
    assert r['ok'] and up.orders == [('sell', 'KRW-BTC', 1.0)] and stores['upbit'].get(sell['id'])['state'] == 'done'
    # 다시 지켜보기: 지금 가격 기준으로 방향을 다시 정함
    sim.prices['KRW-BTC'] = 130_000
    w3 = mk()
    w3.check(t=1000 + 7200)
    assert stores['upbit'].get(buy['id'])['state'] == 'hold'
    assert stores['upbit'].rewatch(buy['id'], 130_000)['dir'] == 'down'
    w3.check(t=1000 + 7202)
    assert stores['upbit'].get(buy['id'])['state'] == 'wait' and len(up.orders) == 1    # 아직 120,000원 위
    sim.prices['KRW-BTC'] = 119_900
    w3.check(t=1000 + 7204)
    assert up.orders[-1] == ('buy', 'KRW-BTC', 50_000)


def test_buy_reservation_holds_on_big_jump_but_sell_does_not(tmp_path, sim):
    up = FakeAccount(coins={'BTC': 1.0})
    w, stores, notes = make_watcher(tmp_path, sim, {'upbit': up, 'sim': sim})
    buy = stores['upbit'].add('KRW-BTC', 'bid', 101_000, krw=50_000, dir='up')
    sell = stores['upbit'].add('KRW-BTC', 'ask', 99_000, qty=1.0, dir='down')
    sim.prices['KRW-BTC'] = 110_000                                    # 매수 예약가보다 8.9% 위에서 처음 확인
    w.check()
    assert stores['upbit'].get(buy['id'])['state'] == 'hold' and up.orders == []
    sim.prices['KRW-BTC'] = 90_000                                     # 매도 예약가보다 9% 아래 — 매도는 멈추지 않음
    w.check()
    assert up.orders == [('sell', 'KRW-BTC', 1.0)] and stores['upbit'].get(sell['id'])['state'] == 'done'


def test_api_resolve_hold(dash):
    store = dash.chart_orders['upbit']
    o = store.add('KRW-BTC', 'ask', 95_000, qty=1.0, dir='down')
    store.hold(o['id'], '테스트', 90_000)
    c = dash.client
    assert c.post(f"/api/chart-orders/{o['id']}/resolve", json={'action': 'rewatch'}).get_json()['success']
    assert store.get(o['id'])['state'] == 'wait' and store.get(o['id'])['dir'] == 'down'     # 지금 100,000원 → 다시 내려오면
    store.hold(o['id'], '테스트', 90_000)
    r = c.post(f"/api/chart-orders/{o['id']}/resolve", json={'action': 'run'}).get_json()
    assert r['success'] and dash.fake.orders == [('sell', 'KRW-BTC', 1.0)]
    o2 = store.add('KRW-BTC', 'bid', 120_000, krw=50_000, dir='up')
    store.hold(o2['id'], '테스트', 130_000)
    assert c.post(f"/api/chart-orders/{o2['id']}/resolve", json={'action': 'cancel'}).get_json()['success']
    assert store.get(o2['id'])['state'] == 'cancel'
    assert c.post(f"/api/chart-orders/{o2['id']}/resolve", json={'action': 'run'}).status_code == 404
