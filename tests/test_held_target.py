"""매매대상 '지금 보유한 코인' (held_coins)"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_core.conditions as cd  # noqa: E402
from bot_core.bot_engine import BotEngine  # noqa: E402

COINS = [{'currency': c, 'current_price': p, 'trade_value_24h': 1e9, 'warning': False, 'notices': []}
         for c, p in (('BTC', 100_000_000), ('ETH', 5_000_000), ('XRP', 1_000), ('DOGE', 300))]


def universe(positions, targets):
    eng = BotEngine.__new__(BotEngine)
    eng.exchange = types.SimpleNamespace(get_all_positions=lambda: positions)
    eng._custom = lambda data: {}
    eng.mode, eng.account_mode = 'simulation', 'sim'
    return [c['currency'] for c in eng._universe({'target': targets}, COINS, {})]


def test_preset_exists_and_tests_held_flag():
    p = cd.PRESETS['held_coins']
    assert p['title'] == '지금 보유한 코인'
    assert p['test']({'held': True}, {}, {}) == {} and p['test']({}, {}, {}) is None


def test_engine_keeps_only_held_coins_even_small_ones():
    pos = {'BTC': (0.001, 90_000_000), 'XRP': (3, 900), 'DOGE': (100, 250), 'ETH': (0, 0)}   # XRP 3,000원어치도 보유
    assert universe(pos, [{'id': 'held_coins', 'params': {}}]) == ['BTC', 'XRP', 'DOGE']


def test_balance_error_means_nothing_held():
    assert universe(None, [{'id': 'held_coins', 'params': {}}]) == []


def test_combines_with_other_targets_as_and():
    pos = {'BTC': (0.001, 0), 'DOGE': (100, 0)}
    got = universe(pos, [{'id': 'held_coins', 'params': {}}, {'id': 'price_range', 'params': {'min_price': 0, 'max_price': 1000}}])
    assert got == ['DOGE']
