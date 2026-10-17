"""한 주문이 여러 번에 나눠 체결된 거래 내역은 한 줄로 (수량 · 금액 · 수수료는 합, 단가는 평균)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core.trade_store import TradeStore  # noqa: E402


def fill(t, oid, vol, price, kind='매수', cur='XRP'):
    return {'time': t, 'order_id': oid, 'order_time': '2026-10-02T19:35:00+09:00', 'currency': cur, 'market': 'KRW', 'kind': kind,
            'volume': vol, 'price': price, 'amount': vol * price, 'fee': vol * price * 0.0005, 'settle': vol * price * 1.0005}


def test_split_fills_become_one_row(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    st.add_fills([fill('2026-10-02T19:35:01+09:00', 'A', 214.0, 2087),
                  fill('2026-10-02T19:35:02+09:00', 'A', 421.0, 2088),
                  fill('2026-10-02T17:04:00+09:00', 'B', 428.0, 2074, kind='매도')])
    rows, total, nxt = st.fills_page()
    assert total == 2 and len(rows) == 2 and nxt is None
    a = next(r for r in rows if r['order_id'] == 'A')
    assert a['fills'] == 2 and abs(a['volume'] - 635.0) < 1e-9
    assert abs(a['amount'] - (214 * 2087 + 421 * 2088)) < 1e-6
    assert abs(a['price'] - a['amount'] / a['volume']) < 1e-9          # 평균 단가
    assert a['time'] == '2026-10-02T19:35:02+09:00'                     # 마지막 체결 시각
    b = next(r for r in rows if r['order_id'] == 'B')
    assert b['fills'] == 1
    assert len(st.fills_all(currencies=['XRP'])) == 2


def test_paging_counts_orders(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    st.add_fills([fill(f'2026-10-0{1 + i // 3}T1{i % 3}:00:0{i % 2}+09:00', f'O{i // 2}', 1.0 + i, 100) for i in range(6)])
    rows, total, nxt = st.fills_page(limit=2)
    assert total == 3 and len(rows) == 2 and nxt
    rest, _, nxt2 = st.fills_page(limit=2, before=nxt)
    assert len(rest) == 1 and nxt2 is None
    assert {r['order_id'] for r in rows + rest} == {'O0', 'O1', 'O2'}


def test_old_rows_without_order_id_are_not_counted_twice(tmp_path):
    st = TradeStore(tmp_path / 't.db')
    st.add_fills([fill('2026-09-24T05:56:57+09:00', 'X', 258.0, 2038)])
    old = fill('2026-09-24T05:56:57+09:00', None, 258.0, 2038)          # 옛 기록: 같은 체결인데 주문번호 없음
    st.add_fills([old, fill('2026-09-20T10:00:00+09:00', None, 10.0, 2000)])   # 짝이 없는 옛 줄은 그대로 셈
    rows, total, _ = st.fills_page()
    assert total == 2 and sorted(r['volume'] for r in rows) == [10.0, 258.0]
    s = st.realized_summary()
    assert abs(s['fees'] - (258 * 2038 + 10 * 2000) * 0.0005) < 1e-6


def test_fills_filter_by_bot(tmp_path):
    """거래 내역 '봇' 태그: 그 봇이 사고판 줄만 · 'none' = 봇 기록에 없는 직접 주문"""
    st = TradeStore(tmp_path / 't.db')
    st.add_fills([fill('2026-10-02T19:35:01+09:00', 'A', 10.0, 2000),                      # 봇 1 (주문번호로 짝)
                  fill('2026-10-02T18:00:00+09:00', 'B', 5.0, 2100, kind='매도'),          # 봇 2 (주문번호 없이 시각으로 짝)
                  fill('2026-10-02T12:00:00+09:00', 'C', 3.0, 1900)])                      # 직접 주문
    st.add_bot_trade('b1', '봇 1', {'time': '2026-10-02T19:35:00+09:00', 'symbol': 'KRW-XRP', 'side': 'buy', 'price': 2000,
                                     'qty': 10.0, 'total': 20000, 'order_id': 'A'})
    st.add_bot_trade('b2', '봇 2', {'time': '2026-10-02T18:01:00+09:00', 'symbol': 'KRW-XRP', 'side': 'sell', 'price': 2100,
                                     'qty': 5.0, 'total': 10500})
    ids = lambda bot: sorted(r['order_id'] for r in st.fills_page(bot=bot)[0])
    assert ids('b1') == ['A'] and ids('b2') == ['B'] and ids('none') == ['C'] and ids(None) == ['A', 'B', 'C']
    assert st.fills_page(bot='b1')[1] == 1 and [r['order_id'] for r in st.fills_all(bot='none')] == ['C']
