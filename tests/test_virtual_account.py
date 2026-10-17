"""가상 계좌 (시뮬레이션 모드): 업비트와 같은 모양, 수수료·불리한 체결, 거절, 저장·초기화"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import virtual_account as va  # noqa: E402
from bot_core.exchange_wrapper import ExchangeWrapper  # noqa: E402

PRICES = {'KRW-A': 100.0, 'KRW-B': 2000.0}


def acct(tmp_path, krw=1_000_000):
    return va.VirtualAccount(tmp_path / 'sim_account.json', lambda m: PRICES.get(m), initial_krw=krw)


def bal(a):
    return {b['currency']: (float(b['balance']), float(b['avg_buy_price'])) for b in a.get_balances()}


def test_buy_sell_fee_slip_and_avg(tmp_path):
    a = acct(tmp_path)
    o = a.buy_market_order('KRW-A', 100_000)
    fill = 100 * (1 + va.SLIP)
    assert o['side'] == 'bid' and float(o['avg_price']) == pytest.approx(fill)
    b = bal(a)
    assert b['KRW'][0] == pytest.approx(1_000_000 - 100_000 * (1 + va.FEE))       # 금액 + 수수료
    assert b['A'][0] == pytest.approx(100_000 / fill) and b['A'][1] == pytest.approx(fill)
    PRICES['KRW-A'] = 120.0
    a.buy_market_order('KRW-A', 100_000)                                             # 평균 단가 = 두 번 산 금액 ÷ 개수
    q1, q2 = 100_000 / fill, 100_000 / (120 * (1 + va.SLIP))
    assert bal(a)['A'][1] == pytest.approx(200_000 / (q1 + q2))
    s = a.sell_market_order('KRW-A', q1 + q2)
    gross = (q1 + q2) * 120 * (1 - va.SLIP)
    assert float(s['paid_fee']) == pytest.approx(gross * va.FEE)
    assert 'A' not in bal(a)                                                         # 다 팔면 목록에서 빠짐
    assert bal(a)['KRW'][0] == pytest.approx(1_000_000 - 200_000 * (1 + va.FEE) + gross * (1 - va.FEE))
    PRICES['KRW-A'] = 100.0


def test_rejects(tmp_path):
    a = acct(tmp_path, krw=50_000)
    assert 'error' in a.buy_market_order('KRW-A', 4_000)                             # 5,000원 미만
    assert 'error' in a.buy_market_order('KRW-A', 50_000)                            # 수수료까지 모자람
    assert 'error' in a.buy_market_order('KRW-X', 10_000)                            # 가격 모름
    assert 'error' in a.sell_market_order('KRW-A', 1)                                # 없는 코인
    a.buy_market_order('KRW-A', 10_000)
    assert 'error' in a.sell_market_order('KRW-A', 1000)                             # 가진 것보다 많이
    assert 'error' in a.sell_market_order('KRW-A', 10)                               # 약 1,000원 → 최소 금액 미만


def test_saved_and_reloaded_and_reset_keeps_old(tmp_path):
    a = acct(tmp_path)
    o = a.buy_market_order('KRW-B', 200_000)
    b = acct(tmp_path, krw=5)                                                        # 다시 열면 파일 그대로 (초기자본 무시)
    assert bal(b) == bal(a) and b.get_order(o['uuid'])['uuid'] == o['uuid']
    assert b.closed_orders()[0]['uuid'] == o['uuid']
    keep = b.reset(3_000_000)
    assert keep.exists() and json.loads(keep.read_text(encoding='utf-8'))['orders']
    assert bal(b) == {'KRW': (3_000_000, 0.0)} and b.closed_orders() == []
    s = b.summary()
    assert s['initial_krw'] == 3_000_000 and s['total'] == 3_000_000 and s['profit'] == 0


def test_exchange_wrapper_uses_virtual_account_like_upbit(tmp_path):
    a = acct(tmp_path)
    ex = ExchangeWrapper(account=a)
    assert ex.test_connection()[0]
    ok, msg, order = ex.buy_market_order('KRW-A', 50_000)
    assert ok and order['side'] == 'bid'
    pos = ex.get_all_positions()
    assert set(pos) == {'A'} and pos['A'][0] > 0
    assert ex.get_krw_balance() == pytest.approx(1_000_000 - 50_000 * (1 + va.FEE))
    ok, _, _ = ex.sell_market_order('KRW-A', pos['A'][0])
    assert ok and ex.get_all_positions() == {}
    ok, msg, _ = ex.buy_market_order('KRW-A', 10_000_000)
    assert not ok and '모자랍' in msg
