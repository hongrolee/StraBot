"""
안전장치(RiskManager) 계산 테스트
- 하루 손실 한도(%)·예전 원 단위 한도, 1회 최대 주문 금액, 주문 간격, 트레일링, 설정 바꾸기

봇이 코인마다 손절·익절·트레일링을 거는 동작은 tests/test_condition_trader.py에서 확인
실행: venv/Scripts/python.exe -m pytest tests/test_risk_safety.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_core.risk_manager as rm  # noqa: E402
from bot_core.risk_manager import RiskManager  # noqa: E402


def test_daily_loss_limit_percent():
    r = RiskManager({'daily_loss_limit_percent': 10, 'max_positions': 5})
    ok, _ = r.can_open_position(0, 1_000_000)     # 오늘 시작 자금 100만 원 → 한도 10만 원
    assert ok
    r.update_daily_loss(-60_000)
    assert r.can_open_position(0, 940_000)[0]
    r.update_daily_loss(-40_001)
    ok, msg = r.can_open_position(0, 900_000)
    assert not ok and '손실 한도' in msg


def test_legacy_krw_limit_still_works():
    r = RiskManager({'daily_loss_limit': 50_000, 'max_positions': 5})
    r.update_daily_loss(-50_000)
    assert not r.can_open_position(0, 1_000_000)[0]


def test_max_order_krw_caps_size():
    r = RiskManager({'position_size_percent': 10, 'max_order_krw': 200_000})
    size, qty = r.calculate_position_size(10_000_000, 1000)
    assert size == 200_000 and qty == 200


def test_max_positions():
    r = RiskManager({'max_positions': 2})
    assert r.can_open_position(1, 1_000_000)[0]
    ok, msg = r.can_open_position(2, 1_000_000)
    assert not ok and '최대' in msg


def test_order_interval(monkeypatch):
    r = RiskManager({})
    r.record_order('KRW-A')
    assert r.order_too_soon('KRW-A')[0]
    assert not r.order_too_soon('KRW-B')[0]          # 코인마다 따로
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    assert not r.order_too_soon('KRW-A')[0]


def test_trailing_not_in_loss_zone_and_fires_after_peak():
    r = RiskManager({'trailing_stop_percent': 3})
    for p in (99, 97, 95):                             # 손실 구간은 손절이 담당
        assert not r.check_trailing_stop('KRW-A', 100, p)[0]
    for p in (105, 110, 107.5):
        assert not r.check_trailing_stop('KRW-B', 100, p)[0]
    assert r.check_trailing_stop('KRW-B', 100, 106.6)[0]


def test_update_config_keeps_state():
    r = RiskManager({'stop_loss_percent': 5, 'daily_loss_limit_percent': 10})
    r.note_balance(1_000_000)
    r.update_daily_loss(-30_000)
    r.record_order('KRW-A')
    r.update_config({'stop_loss_percent': 2, 'daily_loss_limit_percent': 10})
    assert r.stop_loss_percent == 2
    assert r.daily_loss == -30_000 and r.order_too_soon('KRW-A')[0]


def test_all_in_one_coin_skips_30pct_cap():
    """최대 보유 1개 · 비율 100% = 가진 돈 전부 (평균선 위에서만 들고 있기 봇) — 손해 본 뒤 줄어든 돈으로도 다시 삼"""
    r = RiskManager({'position_size_percent': 100, 'max_positions': 1})
    size, _ = r.calculate_position_size(10_000_000, 1000)
    assert 9_900_000 < size < 10_000_000
    size, _ = r.calculate_position_size(8_000_000, 1000)
    assert 7_900_000 < size < 8_000_000
    two = RiskManager({'position_size_percent': 100, 'max_positions': 2})
    assert two.calculate_position_size(10_000_000, 1000)[0] == 3_000_000     # 여러 코인이면 전처럼 30%까지



def test_daily_loss_limit_zero_means_off():
    """하루 손실 한도 0% = 안 씀 (전에는 0원 한도라 처음부터 새로 사기를 막았음)"""
    from bot_core.risk_manager import RiskManager
    rm = RiskManager({'daily_loss_limit_percent': 0, 'max_positions': 1, 'position_size_percent': 100})
    ok, why = rm.can_open_position(0, 1_000_000)
    assert ok, why
    rm2 = RiskManager({'daily_loss_limit_percent': 5, 'max_positions': 1, 'position_size_percent': 100})
    rm2.note_balance(1_000_000)
    rm2.update_daily_loss(-60_000)
    assert not rm2.can_open_position(0, 940_000)[0]          # 5% 는 그대로 지킴
