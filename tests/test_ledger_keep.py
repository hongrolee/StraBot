"""봇 장부가 산 코인을 잃지 않는지: 5,000원어치 산 코인이 값이 조금 내려도 장부에 남고, 같은 코인을 더 사면 수량이 더해짐"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_manager import PRICES, engine, make  # noqa: E402

RULES = {'buy': [{'id': 'my_up'}], 'sell': [], 'exclude': []}


def _bot(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': RULES})
    assert m.start(a['id'], 'sim')[0]
    return m, engine(m, a['id'], 'sim')


def test_min_order_coin_stays_in_ledger_when_price_dips(tmp_path):
    m, e = _bot(tmp_path)
    PRICES['KRW-A'] = 100.0
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 5_000, 't', 't', quiet=True)
    e.last_prices['KRW-A'] = 99.0                                  # 산 직후 값이 조금 내려 4,9xx원
    e._sync_positions()
    assert 'KRW-A' in e.ledger                                      # 전에는 여기서 장부에서 빠져 계좌에만 남았음


def test_user_sold_most_then_small_rest_stays_with_bot(tmp_path):
    m, e = _bot(tmp_path)
    PRICES['KRW-A'] = 100.0
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 50_000, 't', 't', quiet=True)
    qty = e.ledger['KRW-A']['qty']
    m.acct.sell_market_order('KRW-A', qty * 0.95)                  # 사용자가 직접 95% 팔아 2,5xx원 남음
    e.last_prices['KRW-A'] = 100.0
    e._sync_positions()
    assert 'KRW-A' in e.ledger                                      # 한 번 본 것으로는 줄이지 않음
    e._shrink_seen['KRW-A'] -= 10                                   # 10초 뒤 다시 확인해도 줄어 있음
    e._sync_positions()
    assert abs(e.ledger['KRW-A']['qty'] - qty * 0.05) < 1e-9          # 남은 2,5xx원어치는 봇이 계속 맡음 (2026-10-15)
    e.exchange.get_all_positions = lambda: {}                        # 나머지도 계좌에서 다 팔리면
    e._sync_positions()                                              # 한 번 봄
    e._shrink_seen = {k: v - 10 for k, v in e._shrink_seen.items()}
    e._sync_positions()
    assert 'KRW-A' not in e.ledger                                  # 다 팔렸을 때만 뺌


def test_other_bot_selling_same_coin_does_not_wipe_mine(tmp_path):
    """다른 봇이 같은 코인을 팔 때: 계좌에서는 빠졌는데 그 봇 장부는 아직인 틈에 장부를 맞춰도 내 몫은 그대로"""
    m, e = _bot(tmp_path)
    PRICES['KRW-A'] = 100.0
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 20_000, 't', 't', quiet=True)
    mine = e.ledger['KRW-A']['qty']
    m.acct.buy_market_order('KRW-A', 200_000)                       # 다른 봇 몫
    other = m.acct.get_balance('A') - mine
    e._get_others_qty = lambda: {'KRW-A': other}                     # 다른 봇 장부에는 아직 남아 있음
    m.acct.sell_market_order('KRW-A', other)                         # 다른 봇이 팔아 계좌에서는 빠짐
    e.last_prices['KRW-A'] = 100.0
    e._sync_positions()                                              # 틈에 맞춤
    e._get_others_qty = lambda: {}                                   # 곧 다른 봇 장부도 지워짐
    e._shrink_seen = {k: v - 10 for k, v in getattr(e, '_shrink_seen', {}).items()}
    e._sync_positions()
    assert abs(e.ledger['KRW-A']['qty'] - mine) < 1e-9


def test_buying_same_coin_again_adds_up(tmp_path):
    m, e = _bot(tmp_path)
    PRICES['KRW-A'] = 100.0
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 20_000, 't', 't', quiet=True)
    q1 = e.ledger['KRW-A']['qty']
    PRICES['KRW-A'] = 200.0
    assert e._order_buy('KRW-A', {'current_price': 200.0, 'korean_name': 'A'}, 20_000, 't', 't', quiet=True)
    pos = e.ledger['KRW-A']
    assert pos['qty'] > q1 * 1.4                                    # 덮어쓰지 않고 더함
    assert 130 < pos['entry_price'] < 137                           # 평균 매수가 ≈ 133원


def test_small_position_sells_by_topping_up_first(tmp_path):
    """5,000원 미만이라 그대로는 못 파는 코인: 5,300원어치 더 사서 합친 뒤 한 번에 다 팖 (2026-10-15)"""
    m, e = _bot(tmp_path)
    PRICES['KRW-A'] = 100.0
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 5_000, 't', 't', quiet=True)
    PRICES['KRW-A'] = 90.0
    e._sell('KRW-A', 90.0, '손절', risk_exit=True)                  # 4,5xx원
    assert 'KRW-A' not in e.ledger and m.acct.get_balance('A') < 1e-9
    import json
    log = json.loads((e.data_dir / 'trades.json').read_text(encoding='utf-8'))
    sides = [t.get('side') for t in (log if isinstance(log, list) else log.get('trades', []))][-3:]
    assert sides == ['buy', 'buy', 'sell']                           # 처음 산 것 · 더 산 것 · 다 판 것


def test_small_position_waits_when_not_enough_krw(tmp_path):
    m, e = _bot(tmp_path)
    PRICES['KRW-A'] = 100.0
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 5_000, 't', 't', quiet=True)
    e.exchange.get_krw_balance = lambda: 1_000                       # 원화가 모자람
    e._sell('KRW-A', 90.0, '손절', risk_exit=True)
    assert 'KRW-A' in e.ledger                                      # 장부에 두고 기다림
