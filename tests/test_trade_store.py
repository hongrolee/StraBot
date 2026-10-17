"""
거래 기록 저장소 (bot_core/trade_store.py)
- 체결 쪽 나누기(최신순 · 다음 쪽 · 전체 수) · 같은 체결 두 번 안 들어감 · 종류 · 코인 거르기
- 봇 기록: 매수평균가 · 초기화 이후만 · 거래 수 · 승률, 거래 내역 줄과 짝짓기(주문번호 / 3분 안)
- 오래된 기록 정리 · 예전 JSON 옮기기(한 번만) · 가상 계좌 주문이 저장소에 쌓이고 JSON 은 최근 것만
- 봇이 사고팔면 저장소에 전부 남음 (trades.json 은 최근 것만)
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_core import virtual_account as va  # noqa: E402
from bot_core.trade_store import TradeStore, migrate_legacy, sim_order_row  # noqa: E402


def iso(sec_ago: int) -> str:
    return (datetime.now().astimezone() - timedelta(seconds=sec_ago)).isoformat(timespec='seconds')


def fill(i, cur='BTC', kind='매수', ago=None):
    t = iso(ago if ago is not None else 1000 - i)
    return {'time': t, 'order_time': t, 'order_id': f'o{i}', 'currency': cur, 'market': 'KRW', 'kind': kind,
            'volume': 1.0, 'price': 100.0 + i, 'amount': 100.0 + i, 'fee': 0.05, 'settle': 101 + i}


def test_fills_page_newest_first_with_cursor_and_total(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    assert st.add_fills(fill(i) for i in range(25)) == 25
    rows, total, nxt = st.fills_page(limit=10)
    assert total == 25 and len(rows) == 10 and nxt
    assert rows[0]['order_id'] == 'o24' and rows[-1]['order_id'] == 'o15'          # 최신순
    rows2, _, nxt2 = st.fills_page(limit=10, before=nxt)
    assert rows2[0]['order_id'] == 'o14'
    rows3, _, nxt3 = st.fills_page(limit=10, before=nxt2)
    assert len(rows3) == 5 and nxt3 is None                                       # 끝


def test_same_fill_is_not_added_twice(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    st.add_fills([fill(1), fill(2)])
    assert st.add_fills([fill(1), fill(2), fill(3)]) == 1
    assert st.count_fills() == 3


def test_filter_by_kind_coin_and_period(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    st.add_fills([fill(1, 'BTC', '매수'), fill(2, 'ETH', '매도'), fill(3, 'XRP', '매수'), fill(4, 'BTC', '매도', ago=10 * 86400)])
    assert st.fills_page(kind='매도')[1] == 2
    assert {r['currency'] for r in st.fills_page(currencies=['BTC', 'ETH'])[0]} == {'BTC', 'ETH'}
    assert st.fills_page(currencies=[], q='xr')[1] == 1                            # 심볼 일부로
    assert st.fills_page(currencies=[])[1] == 0                                    # 맞는 코인이 없으면 0
    assert st.fills_page(since_ts=int(time.time()) - 86400)[1] == 3                # 기간


def test_bot_trades_avg_cost_counts_and_reset(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    b = lambda ago, side, qty, total, profit=None: {'time': (datetime.now() - timedelta(seconds=ago)).isoformat(timespec='seconds'),
                                                    'symbol': 'KRW-X', 'side': side, 'price': total / qty, 'qty': qty,
                                                    'fee': 0, 'total': total, 'profit': profit, 'reason': 'r'}
    st.add_bot_trade('a', '봇A', b(50, 'buy', 1, 100))
    assert st.add_bot_trade('a', '봇A', b(40, 'buy', 1, 200)) == 150                 # 산 뒤 평균
    st.add_bot_trade('a', '봇A', b(30, 'sell', 1, 160, profit=10))
    st.add_bot_trade('a', '봇A', b(20, 'sell', 1, 140, profit=-10))
    c = st.bot_counts('a')
    assert (c['n'], c['buys'], c['sells'], c['wins'], c['losses']) == (4, 2, 2, 1, 1)
    assert [t['side'] for t in st.bot_trades('a', newest_first=False)] == ['buy', 'buy', 'sell', 'sell']
    st.mark_reset('a')                                                             # 처음부터 다시: 그 전은 봇 화면에서 뺌
    assert st.bot_counts('a')['n'] == 0 and st.bot_trades('a') == []
    assert len(st.bot_trades('a', all_runs=True)) == 4                             # 기록은 남음


def test_reasons_by_order_id_or_nearby_time(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    now = datetime.now()
    st.add_bot_trade('a', '옛 이름', {'time': now.isoformat(timespec='seconds'), 'symbol': 'KRW-BTC', 'side': 'buy', 'price': 1,
                                     'qty': 1, 'fee': 0, 'total': 1, 'reason': '조건1', 'order_id': 'o1'})
    st.add_bot_trade('b', '봇B', {'time': (now - timedelta(seconds=60)).isoformat(timespec='seconds'), 'symbol': 'KRW-ETH',
                                  'side': 'sell', 'price': 1, 'qty': 1, 'fee': 0, 'total': 1, 'profit': 5, 'reason': '익절'})
    rows = st.reasons_for([fill(1, 'BTC'), {**fill(9, 'ETH', '매도'), 'order_id': 'zz', 'time': iso(0), 'order_time': iso(0)},
                           {**fill(7, 'XRP'), 'order_id': 'none'}], names={'a': '새 이름'})
    assert rows[0]['bot'] == '새 이름' and rows[0]['reason'] == '조건1'                  # 주문번호 · 지금 봇 이름
    assert rows[1]['bot'] == '봇B' and rows[1]['profit'] == 5                         # 3분 안 가장 가까운 것
    assert 'bot' not in rows[2]                                                    # 봇이 아닌 주문


def test_prune_removes_only_old(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    st.add_fills([fill(1, ago=400 * 86400), fill(2, ago=10)])
    st.add_bot_trade('a', 'A', {'time': (datetime.now() - timedelta(days=400)).isoformat(timespec='seconds'), 'symbol': 'KRW-X',
                                'side': 'buy', 'price': 1, 'qty': 1, 'fee': 0, 'total': 1})
    assert st.prune(365) == 2
    assert st.count_fills() == 1


def test_virtual_account_orders_go_to_store_and_json_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(va, 'MAX_ORDERS', 3)
    st = TradeStore(tmp_path / 't.db')
    acct = va.VirtualAccount(tmp_path / 'sim_account.json', lambda m: 100.0, initial_krw=1_000_000,
                             on_order=lambda o: st.add_fills([sim_order_row(o)]))
    for _ in range(5):
        acct.buy_market_order('KRW-X', 10_000)
    assert len(acct.closed_orders()) == 3                                          # JSON 은 최근 것만 (가볍게)
    assert st.count_fills() == 5                                                   # 저장소엔 전부
    assert st.fills_page()[0][0]['kind'] == '매수'


def test_migrate_legacy_once(tmp_path):
    data = tmp_path / 'data'
    bots = data / 'bots'
    (bots / 'b1' / 'sim').mkdir(parents=True)
    acct = va.VirtualAccount(data / 'sim_account.json', lambda m: 100.0, initial_krw=1_000_000)
    acct.buy_market_order('KRW-X', 10_000)
    t0 = datetime.now() - timedelta(minutes=5)
    items = [{'time': t0.isoformat(timespec='seconds'), 'symbol': 'KRW-X', 'side': 'buy', 'price': 100, 'qty': 1, 'fee': 0, 'total': 100, 'reason': 'r'},
             {'time': (t0 + timedelta(minutes=1)).isoformat(timespec='seconds'), 'symbol': 'KRW-X', 'side': 'buy', 'price': 300, 'qty': 1, 'fee': 0, 'total': 300, 'reason': 'r'}]
    (bots / 'b1' / 'sim' / 'trades.json').write_text(json.dumps(items), encoding='utf-8')
    old = bots / '_reset' / f"b1_{(t0 - timedelta(days=1)):%Y%m%d%H%M%S}"
    old.mkdir(parents=True)
    (old / 'trades.json').write_text(json.dumps([{**items[0], 'time': (t0 - timedelta(days=2)).isoformat(timespec='seconds')}]), encoding='utf-8')
    (data / 'trade_chunks_cache.json').write_text(json.dumps({'2026-09-01': [fill(1)]}), encoding='utf-8')
    stores = {'sim': TradeStore(data / 's.db'), 'upbit': TradeStore(data / 'u.db')}
    migrate_legacy(stores, data, bots, [{'id': 'b1', 'name': '봇1'}])
    assert stores['sim'].count_fills() == 1 and stores['upbit'].count_fills() == 1
    mine = stores['sim'].bot_trades('b1', newest_first=False)
    assert len(mine) == 2 and mine[1]['avg_cost'] == 200                            # 초기화 뒤 2건 · 매수평균가
    assert len(stores['sim'].bot_trades('b1', all_runs=True)) == 3                  # 초기화 전 기록도 남음
    assert not (data / 'trade_chunks_cache.json').exists()                          # 옮긴 캐시는 이름만 바꿈
    assert (data / 'trade_chunks_cache.json.migrated').exists()
    migrate_legacy(stores, data, bots, [{'id': 'b1', 'name': '봇1'}])               # 두 번째는 아무것도 안 함
    assert len(stores['sim'].bot_trades('b1', all_runs=True)) == 3


def test_bot_trades_are_kept_in_store_beyond_trades_json(tmp_path):
    from test_bot_manager import RULES, engine, make, tick, data
    m, cfg, _ = make(tmp_path)
    stores = {'sim': TradeStore(tmp_path / 'trades_sim.db')}
    m._get_trade_store = lambda mode: stores.get(mode)
    m.acct.on_order = lambda o: stores['sim'].add_fills([sim_order_row(o)])
    _, _, a = m.create({'name': 'A', 'budget': 10 ** 12, 'trade_rules': RULES})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    tick(e, data({'X': 0, 'Y': 0}))
    tick(e, data({'X': 6, 'Y': 6}))                                                # 두 코인 새로 맞음 → 삼
    got = stores['sim'].bot_trades(a['id'])
    assert {t['symbol'] for t in got} == {'KRW-X', 'KRW-Y'} and all(t.get('avg_cost') for t in got)
    assert stores['sim'].count_fills() == 2                                        # 거래 내역 줄도
    assert m.summary(a['id'], 'sim')['trades'] == 2
    assert [t['symbol'] for t in m.detail(a['id'], 'sim')['recent_trades']]          # 최근 거래도 저장소에서
    assert len(m.all_trades(a['id'], 'sim', symbol='KRW-X')) == 1


def test_realized_summary_matches_cash_like_upbit(tmp_path):
    """처음 자본 − 보유 원화 − 총 매수 = 수수료 − 판 손익 (매수평균가는 업비트처럼 수수료 미포함)"""
    st = TradeStore(tmp_path / 't.db')
    prices = {'KRW-X': 100.0}
    acct = va.VirtualAccount(tmp_path / 'sim_account.json', lambda m: prices[m], initial_krw=1_000_000,
                             on_order=lambda o: st.add_fills([sim_order_row(o)]))
    acct.buy_market_order('KRW-X', 100_000)
    acct.buy_market_order('KRW-X', 50_000)
    prices['KRW-X'] = 90.0
    acct.sell_market_order('KRW-X', acct.get_balance('X') / 2)
    s = st.realized_summary()
    cur = acct.state['coins']['X']
    total_buy = cur['avg'] * cur['qty']
    assert (s['buys'], s['sells'], s['partial']) == (2, 1, False)
    assert abs((1_000_000 - acct.get_balance('KRW') - total_buy) - (s['fees'] - s['realized'])) < 0.01



def test_board_holdings_sums_all_bots(tmp_path):
    """전체 봇 보드 맨 위: 봇 장부를 합쳐 보유 코인 · 총 매수 · 총 평가 · 손익 (같은 코인은 하나로, 시세 없는 코인 = 상폐)"""
    from test_bot_manager import make
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 10 ** 6})
    _, _, b = m.create({'name': 'B', 'budget': 10 ** 6})
    for bid, led in ((a['id'], {'KRW-X': {'qty': 2, 'entry_price': 100}, 'KRW-Y': {'qty': 1, 'entry_price': 50}}),
                     (b['id'], {'KRW-X': {'qty': 1, 'entry_price': 130}, 'KRW-Z': {'qty': 4, 'entry_price': 10}})):
        d = m.data_dir(bid, 'sim'); d.mkdir(parents=True)
        (d / 'bot_positions.json').write_text(json.dumps(led), encoding='utf-8')
    h = m.board_holdings(m.list('sim'), prices={'KRW-X': 120.0, 'KRW-Y': 40.0}, mode='sim')
    assert h['coins'] == 3 and (h['up'], h['down'], h['delisted']) == (1, 1, 1)      # X 수익 · Y 손실 · Z 상폐
    assert h['buy'] == 2 * 100 + 50 + 130 + 40 and h['value'] == 3 * 120 + 40
    assert abs(h['pnl'] - (400 - 420)) < 1e-9


def test_bot_today_and_recent_trades(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    now = datetime.now()
    t = lambda ago_h, side, profit=None, bid='a': st.add_bot_trade(bid, bid.upper(), {'time': (now - timedelta(hours=ago_h)).isoformat(timespec='seconds'),
                                                                           'symbol': 'KRW-X', 'side': side, 'price': 1, 'qty': 1, 'fee': 0, 'total': 1, 'profit': profit, 'reason': 'r'})
    t(30, 'sell', 100)                                                              # 어제
    t(1, 'buy'); t(0.5, 'sell', -20); t(0.2, 'buy', bid='b')
    midnight = int((now - timedelta(hours=1.5)).timestamp())                           # '오늘' 시작 = 1.5시간 전으로 둠
    d = st.bot_today('a', midnight)
    assert d['trades'] == 2 and d['realized'] == -20 and d['last_ts'] is not None
    feed = st.recent_bot_trades(10)
    assert [x['bot_id'] for x in feed][:2] == ['b', 'a'] and len(st.recent_bot_trades(2)) == 2
    assert st.recent_bot_trades(10, ['b'])[0]['bot_id'] == 'b'


def test_board_monitor_health(tmp_path):
    """상태 판정: 멈춤 · 응답 없음 · 손실 한도 · 정상"""
    from test_bot_manager import make, RULES
    m, cfg, _ = make(tmp_path)
    m._get_trade_store = lambda mode: TradeStore(tmp_path / 'm.db')
    _, _, a = m.create({'name': 'A', 'budget': 10 ** 6, 'trade_rules': RULES})
    _, _, b = m.create({'name': 'B', 'budget': 10 ** 6, 'trade_rules': RULES})
    mon = m.board_monitor(m.list('sim'), mode='sim')
    assert mon['bots'][a['id']]['level'] == 'off' and mon['alerts'] == 0
    m.start(a['id'], 'sim')
    e = m.engines[(a['id'], 'sim')]
    e.last_update_time = datetime.now() - timedelta(minutes=30)                      # 오래 확인이 없음
    mon = m.board_monitor(m.list('sim'), mode='sim')
    assert mon['bots'][a['id']]['level'] == 'error' and mon['alerts'] == 1
    e.last_update_time = datetime.now()
    e.risk_manager.daily_loss_limit_percent = None                                     # 한도 1,000원으로
    e.risk_manager.daily_loss_limit_krw = 1000.0
    e.risk_manager.update_config = lambda c: None
    e.risk_manager.reset_daily_loss = lambda: None
    e.risk_manager.daily_loss = -5000.0                                                # 손실 한도 넘음
    assert m.board_monitor(m.list('sim'), mode='sim')['bots'][a['id']]['label'] == '손실 한도'
    e.risk_manager.daily_loss = 0
    assert m.board_monitor(m.list('sim'), mode='sim')['bots'][a['id']]['level'] == 'ok'
