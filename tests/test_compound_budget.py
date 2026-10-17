"""운용 한도: 금액이면 시작 금액 + 판 손익, 비율이면 계좌 총자산 × 비율 — 둘 다 번 만큼 다시 투자(복리). 0 = 제한 없음"""
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core.bot_engine import BotEngine, add_realized, realized_total  # noqa: E402


def engine(tmp_path, budget, krw=10_000_000, ledger=None, pct=0, coins=None, prices=None):
    e = types.SimpleNamespace(data_dir=tmp_path, ledger=ledger or {}, last_prices=prices or {},
                              exchange=types.SimpleNamespace(get_krw_balance=lambda: krw, get_all_positions=lambda: coins or {}),
                              _get_config=lambda: {'budget': budget, 'budget_pct': pct})
    for name in ('_budget', '_budget_pct', '_has_limit', '_account_total', '_realized_total', '_budget_now', '_live_budget_left'):
        setattr(e, name, types.MethodType(getattr(BotEngine, name), e))
    return e


def test_realized_from_trades_then_file(tmp_path):
    (tmp_path / 'trades.json').write_text(json.dumps([{'side': 'buy'}, {'side': 'sell', 'profit': 300}, {'side': 'sell', 'profit': -100}]))
    assert realized_total(tmp_path) == 200
    add_realized(tmp_path, 50, json.loads((tmp_path / 'trades.json').read_text()))
    assert realized_total(tmp_path) == 250                     # 처음엔 기록 합계 200 + 이번 50
    add_realized(tmp_path, 10, [])
    assert realized_total(tmp_path) == 260                     # 그다음부터는 누적 파일


def test_budget_grows_with_profit(tmp_path):
    (tmp_path / 'realized.json').write_text(json.dumps({'total': 1_000_000}))
    e = engine(tmp_path, 10_000_000, krw=20_000_000, ledger={'KRW-A': {'entry_price': 100, 'qty': 50_000}})   # 500만 원어치 보유
    assert e._budget_now() == 11_000_000
    assert e._live_budget_left() == 6_000_000                 # 1,100만 − 500만


def test_budget_shrinks_with_loss_and_krw_caps(tmp_path):
    (tmp_path / 'realized.json').write_text(json.dumps({'total': -2_000_000}))
    e = engine(tmp_path, 10_000_000, krw=5_000_000)
    assert e._budget_now() == 8_000_000
    assert e._live_budget_left() == 5_000_000                 # 계좌 원화보다 많이는 못 씀


def test_zero_means_no_new_buys(tmp_path):
    e = engine(tmp_path, 0, krw=3_000_000)                    # 0원 · 0% = 새로 사지 않음 (예전: 한도 없음)
    assert e._budget_now() == 0 and e._live_budget_left() == 0


def test_old_zero_budget_bots_move_to_remaining_pct(tmp_path):
    from bot_core.bot_manager import BotManager
    m = BotManager.__new__(BotManager)
    bots = [{'id': 'a', 'name': 'A', 'budget_pct': 30}, {'id': 'b', 'name': 'B', 'budget': 0}, {'id': 'c', 'name': 'C', 'budget': 500}]
    m.config = {'bots': bots}
    m._save = lambda c: None
    m._migrate_zero_budget()
    assert bots[1]['budget_pct'] == 70 and not bots[2].get('budget_pct')
    bots[1]['budget_pct'] = 0
    m._migrate_zero_budget()                                  # 한 번만 (그 뒤 0은 '새로 사지 않음')
    assert bots[1]['budget_pct'] == 0


def test_pct_of_account_total(tmp_path):
    # 원화 600만 + 코인 A 100개 × 지금 4만 원 = 1,000만 → 50% = 500만, 이미 200만어치 들고 있음
    e = engine(tmp_path, 0, krw=6_000_000, pct=50, coins={'A': (100, 30_000)}, prices={'KRW-A': 40_000},
               ledger={'KRW-A': {'entry_price': 20_000, 'qty': 100}})
    assert e._account_total() == 10_000_000
    assert e._budget_now() == 5_000_000
    assert e._live_budget_left() == 3_000_000


def test_pct_sum_limited_to_100(tmp_path):
    from bot_core.bot_manager import BotManager
    m = BotManager.__new__(BotManager)
    import threading
    m._lock = threading.RLock()
    bots = [{'id': 'a', 'name': 'A', 'budget_pct': 60}, {'id': 'b', 'name': 'B'}]
    m.bots = lambda: bots
    m.get = lambda i: next((b for b in bots if b['id'] == i), None)
    m.running_modes = lambda i: []
    m._save = lambda c: None
    m.config = {}
    mode = ['sim']
    m._get_mode = lambda: mode[0]
    ok, msg, _ = m.update('b', {'budget_pct': 50})
    assert not ok and '40%' in msg
    ok, _, b = m.update('b', {'budget_pct': 40})
    assert ok and m.pct_of(b) == 40
    # 모드마다 따로: 업비트 모드에서 바꿔도 시뮬레이션 비율은 그대로
    mode[0] = 'upbit'
    assert m.update('a', {'budget_pct': 10})[0] and m.update('b', {'budget_pct': 90})[0]
    assert m.pct_of(bots[0], 'upbit') == 10 and m.pct_of(bots[0], 'sim') == 60
    assert m.pct_of(bots[1], 'upbit') == 90 and m.pct_of(bots[1], 'sim') == 40


def test_amount_bots_move_to_pct_once(tmp_path):
    from bot_core.bot_manager import BotManager
    m = BotManager.__new__(BotManager)
    bots = [{'id': 'a', 'name': 'A', 'budget_pct': 50}, {'id': 'b', 'name': 'B', 'budget': 2_000_000}, {'id': 'c', 'name': 'C', 'budget': 9_000_000}]
    m.config = {'bots': bots}
    m._save = lambda c: None
    m.account_snapshot = lambda prices=None: {'total': 10_000_000}
    m.data_dir = lambda i, mode=None: tmp_path / i
    m._migrate_pct_only()
    assert bots[1]['budget_pct'] == 20 and bots[1]['budget'] == 0          # 200만 / 1,000만
    assert bots[2]['budget_pct'] == 30                                       # 90% 지만 남은 30%까지
    bots[1]['budget_pct'] = 5
    m._migrate_pct_only()                                                   # 한 번만
    assert bots[1]['budget_pct'] == 5
