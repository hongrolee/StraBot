"""맡긴 코인: 손절 · 익절 기준 = 맡긴 순간 가격 (2026-10-08, 원래 평균가로 맡겨 40개가 바로 손절로 팔린 일 뒤)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core.bot_engine import BotEngine, add_realized, adopted_legacy_total, realized_total  # noqa: E402
from bot_core.risk_manager import RiskManager  # noqa: E402


class FakeEngine:
    _rebase_adopted = BotEngine._rebase_adopted

    def __init__(self, ledger, prices):
        self.ledger, self.last_prices, self.saved, self.notes = ledger, prices, 0, []

    def _decide(self, level, msg):
        self.notes.append(msg)

    def _save_ledger(self):
        self.saved += 1


def test_old_adoption_rebased_to_now_price():
    e = FakeEngine({'KRW-A': {'qty': 10, 'entry_price': 100.0, 'adopted': True},           # 예전 방식 (원래 평균가)
                    'KRW-B': {'qty': 5, 'entry_price': 50.0, 'adopted': True, 'basis': 'adopt_price'},
                    'KRW-C': {'qty': 1, 'entry_price': 10.0}},                              # 봇이 산 코인
                   {'KRW-A': 30.0, 'KRW-B': 40.0, 'KRW-C': 5.0})
    e._rebase_adopted()
    assert e.ledger['KRW-A']['entry_price'] == 30.0 and e.ledger['KRW-A']['orig_entry'] == 100.0
    assert e.ledger['KRW-B']['entry_price'] == 50.0                                        # 이미 새 방식이면 그대로
    assert e.ledger['KRW-C']['entry_price'] == 10.0                                        # 봇이 산 코인은 건드리지 않음
    assert e.saved == 1
    # 다시 불러도 바뀌지 않음
    e._rebase_adopted()
    assert e.saved == 1
    # 지금 가격 기준이면 −70% 였던 코인도 손절(20%)에 바로 걸리지 않음
    rm = RiskManager({'stop_loss_percent': 20})
    assert rm.check_stop_loss(e.ledger['KRW-A']['entry_price'], 30.0)[0] is False
    assert rm.check_stop_loss(100.0, 30.0)[0] is True


def test_realized_keeps_adopted_legacy(tmp_path):
    (tmp_path / 'realized.json').write_text('{"total": -100.0, "adopted_legacy": -5000.0}', encoding='utf-8')
    add_realized(tmp_path, 30.0, [])
    assert realized_total(tmp_path) == -70.0
    assert adopted_legacy_total(tmp_path) == -5000.0                                        # 판 손익을 더해도 따로 둔 몫은 그대로


def test_daily_loss_base_is_whole_bot_money():
    """남은 원화가 적어도 하루 손실 한도는 봇이 굴리는 돈 전체 기준"""
    rm = RiskManager({'daily_loss_limit_percent': 10, 'max_positions': 50})
    rm.note_balance(1_000_000)            # 봇 전체 (산 코인 + 원화)
    rm.daily_loss = -20_000
    ok, _ = rm.can_open_position(3, 150_000)    # 남은 원화 15만 원 — 예전엔 한도 1.5만 원으로 막혔음
    assert ok
    rm.daily_loss = -120_000
    assert rm.can_open_position(3, 150_000)[0] is False


def test_cleanup_list_marks_over_stop(tmp_path, monkeypatch):
    """정리 후보: 원래 평균가 대비 손절선을 넘은 맡긴 코인만 over (자동으로 팔지 않음)"""
    from bot_core import bot_manager as bm
    mgr = bm.BotManager.__new__(bm.BotManager)
    mgr.engines = {}
    monkeypatch.setattr(mgr, 'mode', lambda m=None: 'upbit', raising=False)
    monkeypatch.setattr(mgr, 'get', lambda bid: {'id': bid}, raising=False)
    monkeypatch.setattr(mgr, 'risk_of', lambda bot, m=None: {'stop_loss_percent': 20}, raising=False)
    monkeypatch.setattr(mgr, '_ledger_of', lambda bid, m: {
        'KRW-A': {'qty': 10, 'entry_price': 30.0, 'orig_entry': 100.0, 'adopted': True},     # −70%
        'KRW-B': {'qty': 10, 'entry_price': 95.0, 'orig_entry': 100.0, 'adopted': True},     # −5%
        'KRW-C': {'qty': 10, 'entry_price': 50.0}}, raising=False)                            # 봇이 산 코인
    d = mgr.adopt_cleanup_list('x', {'KRW-A': 30.0, 'KRW-B': 95.0, 'KRW-C': 20.0})
    by = {r['currency']: r for r in d['coins']}
    assert set(by) == {'A', 'B'}
    assert by['A']['over'] and not by['B']['over']
    assert round(by['A']['loss']) == -700
