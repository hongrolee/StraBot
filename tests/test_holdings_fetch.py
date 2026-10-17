"""보유 현황 계산: 원화 마켓 전체 시세 한 번(_krw_tickers)으로 현재가 · 거래대금 · 등락률, 마켓에 없으면 상장폐지 (업비트 요청 없음)"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui.web_dashboard import WebDashboard  # noqa: E402


def stub():
    calls = {'tickers': 0}
    balances = [
        {'currency': 'KRW', 'balance': '1000000', 'locked': '0'},
        {'currency': 'BTC', 'balance': '0.01', 'locked': '0', 'avg_buy_price': '100000000'},
        {'currency': 'OLD', 'balance': '5', 'locked': '0', 'avg_buy_price': '1000'},       # 원화 마켓에 없음 → 상장폐지
    ]

    def tickers():
        calls['tickers'] += 1
        return {'KRW-BTC': {'trade_price': 110000000.0, 'acc_trade_price_24h': 5e11, 'signed_change_rate': 0.02,
                            'low_price': 105000000.0, 'prev_closing_price': 107000000.0}}

    s = types.SimpleNamespace(
        _account=lambda mode=None: types.SimpleNamespace(get_balances=lambda: balances),
        _get_coin_korean_names=lambda: {'BTC': '비트코인'},
        _krw_tickers=tickers,
        _get_krw_markets=lambda: [{'market': 'KRW-BTC', 'korean_name': '비트코인'}],
        _get_market_events=lambda: {'warning': set(), 'caution': {}},
        _get_upbit_notices=lambda: {'by_symbol': {}, 'global': []},
        app_mode=lambda: 'upbit',
    )
    s.feed = lambda ex='upbit': s                       # 업비트 시세 묶음 = 자기 자신
    return s, calls


def test_holdings_from_shared_tickers():
    s, calls = stub()
    h = WebDashboard._fetch_holdings(s)
    assert calls['tickers'] == 1
    btc = next(a for a in h['assets'] if a['currency'] == 'BTC')
    assert btc['current_price'] == 110000000.0 and btc['trade_value_24h'] == 5e11
    assert abs(btc['change_rate'] - 2.0) < 1e-9 and btc['prev_close'] == 107000000.0
    assert not btc['is_delisted']
    old = next(a for a in h['assets'] if a['currency'] == 'OLD')
    assert old['is_delisted']
    assert h['krw_balance'] == 1000000


def test_buy_times_first_buy_of_current_holding(tmp_path):
    """매수일: 다 판 뒤 다시 산 때부터 · 봇 기록과 거래 내역이 같은 주문이면 한 번만 · 자투리는 다 판 것으로"""
    from bot_core.trade_store import TradeStore
    st = TradeStore(tmp_path / 't.db')
    f = lambda t, kind, vol, oid: {'time': t, 'currency': 'AAA', 'kind': kind, 'volume': vol, 'price': 1000.0, 'order_id': oid}  # noqa: E731
    st.add_fills([f('2026-10-01T10:00:00+09:00', '매수', 10, 'o1'), f('2026-10-02T10:00:00+09:00', '매도', 9.9995, 'o2'),   # 0.5원어치 남음 = 다 판 것
                  f('2026-10-05T10:00:00+09:00', '매수', 5, 'o3'), f('2026-10-06T10:00:00+09:00', '매수', 5, 'o4')])
    st.add_bot_trade('b1', '봇', {'time': '2026-10-06T10:00:01+09:00', 'symbol': 'KRW-AAA', 'side': 'buy', 'price': 1000, 'qty': 5,
                                  'total': 5000, 'fee': 2.5, 'order_id': 'o4'})                       # 같은 주문 (한 번만)
    st.add_bot_trade('b1', '봇', {'time': '2026-10-07T10:00:00+09:00', 'symbol': 'KRW-AAA', 'side': 'buy', 'price': 1000, 'qty': 1,
                                  'total': 1000, 'fee': 0.5, 'order_id': 'o5'})                       # 아직 거래 내역에 없는 봇 매수
    bt = st.buy_times(['AAA', 'NONE'])
    from bot_core.trade_store import to_ts
    assert bt['AAA']['first'] == to_ts('2026-10-05T10:00:00+09:00')
    assert bt['AAA']['last'] == to_ts('2026-10-07T10:00:00+09:00') and bt['AAA']['n'] == 3
    assert 'NONE' not in bt
    assert st.buy_times(['AAA'], since_ts=to_ts('2026-10-06T00:00:00+09:00'))['AAA']['first'] == to_ts('2026-10-06T10:00:00+09:00')   # 계좌를 새로 만든 뒤부터
