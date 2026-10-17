"""한 번에 살 금액의 기준: 계좌 총자산(직접 가진 코인 포함)이 아니라 봇이 산 금액 + 쓸 수 있는 원화 (운용 한도까지)"""
from bot_core.bot_engine import BotEngine


class _Ex:
    def __init__(self, krw):
        self.krw = krw

    def get_krw_balance(self):
        return self.krw


def _engine(budget, krw, ledger):
    e = BotEngine.__new__(BotEngine)
    e.simulation_engine = None
    e.exchange = _Ex(krw)
    e.ledger = ledger
    e._budget_now = lambda: budget
    return e


def test_uses_cash_not_account_total():
    # 계좌 540만원(직접 가진 코인 450만원 포함) · 원화 50만원 · 봇은 아직 안 삼 → 기준 50만원
    assert _engine(5_400_000, 500_000, {})._size_base() == 500_000


def test_counts_what_bot_already_bought():
    # 봇이 30만원어치 샀고 원화 20만원 남음 → 기준 50만원 (다시 켜도 작아지지 않음)
    ledger = {'KRW-BTC': {'entry_price': 100_000, 'qty': 3}}
    assert _engine(5_400_000, 200_000, ledger)._size_base() == 500_000


def test_capped_by_budget():
    assert _engine(300_000, 500_000, {})._size_base() == 300_000


def test_zero_budget_falls_back_to_cash():
    assert _engine(0, 70_000, {})._size_base() == 70_000
