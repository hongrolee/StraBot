"""멈춘 봇 장부를 실제 계좌와 맞춤: 멈춘 동안 직접 판 코인"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_manager import PRICES, engine, make  # noqa: E402

RULES = {'buy': [{'id': 'my_up'}], 'sell': [], 'exclude': []}


def test_stopped_bot_drops_coins_sold_by_hand(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': RULES})
    assert m.start(a['id'], 'sim')[0]
    e = engine(m, a['id'], 'sim')
    PRICES['KRW-A'] = 100.0
    PRICES['KRW-B'] = 100.0
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 50_000, 't', 't', quiet=True)
    assert e._order_buy('KRW-B', {'current_price': 100.0, 'korean_name': 'B'}, 50_000, 't', 't', quiet=True)
    assert m.stop(a['id'], 'sim')[0]
    acct = m.acct
    acct.sell_market_order('KRW-A', acct.get_balance('A'))           # 멈춘 동안 직접 다 팖
    acct.sell_market_order('KRW-B', acct.get_balance('B') / 2)       # B 는 반만
    assert m.reconcile_stopped(a['id'], 'sim', now=1000.0) == 0       # 한 번 본 것으로는 바꾸지 않음
    assert m.reconcile_stopped(a['id'], 'sim', now=1010.0) == 2
    led = json.loads((m.data_dir(a['id'], 'sim') / 'bot_positions.json').read_text(encoding='utf-8'))
    assert 'KRW-A' not in led and abs(led['KRW-B']['qty'] - acct.get_balance('B')) < 1e-9
    assert 'KRW-A' in json.loads((m.data_dir(a['id'], 'sim') / 'manual_sold.json').read_text(encoding='utf-8'))
    st = m.detail(a['id'], 'sim')['status'] if hasattr(m, 'detail') else None
    if st is not None:
        assert [p['symbol'] for p in st['positions']] == ['KRW-B']


def test_running_bot_is_left_to_engine(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': RULES})
    assert m.start(a['id'], 'sim')[0]
    assert m.reconcile_stopped(a['id'], 'sim') == 0
