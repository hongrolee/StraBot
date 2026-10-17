"""
조건 매매 봇 테스트 (실제 주문·업비트 요청 없음: 가짜 거래소·가짜 시장 데이터)

- 매수 조건에 '새로 들어온' 코인만 삼 (시작 때 이미 맞는 코인은 기준)
- 봇이 산 코인만 팜 (사용자가 직접 산 코인은 안 건드림), 코인마다 자기 가격으로 손절·익절·트레일링
- 매도 조건, 최대 보유 수, 한 번에 3개, 하루 손실 한도, 주문 간격, 제외·유의·스테이블 코인
실행: venv/Scripts/python.exe -m pytest tests/test_condition_trader.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_core.bot_engine as be  # noqa: E402
import bot_core.risk_manager as rm  # noqa: E402
from bot_core.bot_engine import BotEngine  # noqa: E402
from bot_core.simulation_engine import SimulationEngine  # noqa: E402

# 테스트용 내 조건: 오늘 +5% 이상 오르면 '매수 조건', -5% 이하면 '매도 조건'
UP = {'id': 'my_up', 'title': '오늘 5% 이상', 'match': 'all', 'sort': {'rule': 0, 'dir': 'desc'},
      'rules': [{'metric': 'change_rate', 'op': '>=', 'value': 5}]}
DOWN = {'id': 'my_down', 'title': '오늘 -5% 이하', 'match': 'all', 'sort': {'rule': 0, 'dir': 'asc'},
        'rules': [{'metric': 'change_rate', 'op': '<=', 'value': -5}]}


class FakeExchange:
    """업비트 대신: 코인별 보유 수량을 들고 있고 주문을 기록"""

    def __init__(self, krw=1_000_000.0, holdings=None):
        self.krw = krw
        self.holdings = dict(holdings or {})   # {'BTC': [qty, avg]}
        self.prices = {}
        self.orders = []

    def get_all_positions(self):
        return {k: (v[0], v[1]) for k, v in self.holdings.items()}

    def get_krw_balance(self):
        return self.krw

    def buy_market_order(self, symbol, krw):
        cur = symbol.replace('KRW-', '')
        price = self.prices[symbol]
        qty = krw * (1 - 0.0005) / price
        old = self.holdings.get(cur, [0.0, 0.0])
        self.holdings[cur] = [old[0] + qty, price]
        self.krw -= krw
        self.orders.append(('buy', symbol, round(krw)))
        return True, 'ok', {'uuid': 'fake'}

    def sell_market_order(self, symbol, qty):
        cur = symbol.replace('KRW-', '')
        self.holdings[cur][0] -= qty
        self.krw += qty * self.prices[symbol]
        self.orders.append(('sell', symbol, qty))
        return True, 'ok', {'uuid': 'fake'}


class NullLogger:
    def log_trade(self, *a, **k):
        pass


def market(changes, price=100.0, ready=True, extra=None):
    """가짜 시장 데이터: {'A': 전일대비%} → 코인 목록 + 일봉(다 받은 상태)"""
    coins, lows = [], {}
    for cur, ch in changes.items():
        p = price[cur] if isinstance(price, dict) else price
        coins.append({'currency': cur, 'korean_name': f'코인{cur}', 'current_price': p, 'change_rate': ch,
                      'prev_close': p / (1 + ch / 100), 'today_low': p * 0.9, 'trade_value_24h': 1e9,
                      **((extra or {}).get(cur, {}))})
        if ready:
            lows[f'KRW-{cur}'] = {'day_closes': [p] * 30, 'day_highs': [p] * 30, 'day_values': [1e9] * 30}
    return {'coins': coins, 'lows': lows, 'hourly': {}, 'flow_ready': True, 'custom': [UP, DOWN]}


def make_engine(tmp_path, mode='live', risk=None, rules=None, **ex):
    config = {'mode': mode, 'budget': 10 ** 12, 'risk_config': {'position_size_percent': 10, 'max_positions': 10, 'stop_loss_percent': 5,
                                            'take_profit_percent': 50, **(risk or {})},
              'trade_rules': {'buy': {'id': 'my_up'}, **(rules or {})},
              'simulation_config': {'initial_balance': 1_000_000}}
    engine = BotEngine(config, data_dir=tmp_path)
    engine.trade_logger = NullLogger()
    if mode == 'live':
        engine.exchange = FakeExchange(**ex)
    else:
        engine.simulation_engine = SimulationEngine(1_000_000)
    return engine


def tick(engine, data):
    if engine.exchange:
        engine.exchange.prices = {f"KRW-{c['currency']}": c['current_price'] for c in data['coins']}
    engine.tick(data)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(be.time, 'sleep', lambda s: None)


def bought(engine):
    return [o[1] for o in engine.exchange.orders if o[0] == 'buy']


def test_first_tick_only_sets_baseline_then_buys_new(tmp_path):
    e = make_engine(tmp_path)
    tick(e, market({'A': 7, 'B': 1}))          # A는 시작 때 이미 맞음 → 기준만
    assert e.exchange.orders == []
    tick(e, market({'A': 8, 'B': 6}))          # B가 새로 들어옴 → B만 삼
    assert bought(e) == ['KRW-B']
    assert 'KRW-B' in e.ledger and e.ledger['KRW-B']['condition'] == '오늘 5% 이상'


def test_max_three_per_tick_and_max_positions(tmp_path):
    e = make_engine(tmp_path)
    tick(e, market({c: 0 for c in 'ABCDE'}))
    tick(e, market({c: 6 for c in 'ABCDE'}))
    assert len(bought(e)) == 3                  # 한 번에 최대 3개
    e2 = make_engine(tmp_path / 'b', risk={'max_positions': 1})
    tick(e2, market({c: 0 for c in 'ABC'}))
    tick(e2, market({c: 6 for c in 'ABC'}))
    assert len(bought(e2)) == 1                 # 최대 보유 1개


def test_never_sells_user_coins_and_uses_each_coins_own_price(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    e = make_engine(tmp_path, holdings={'USERCOIN': [100.0, 100.0]})   # 사용자가 직접 산 코인
    tick(e, market({'B': 0, 'USERCOIN': 0}))
    tick(e, market({'B': 6, 'USERCOIN': 0}))
    assert bought(e) == ['KRW-B']
    # 사용자 코인 -50%, 봇 코인 B -6% (손절 5%) → B만 팜
    tick(e, market({'B': 0, 'USERCOIN': 0}, price={'B': 94.0, 'USERCOIN': 50.0}))
    sells = [o[1] for o in e.exchange.orders if o[0] == 'sell']
    assert sells == ['KRW-B']
    assert e.exchange.holdings['USERCOIN'][0] == 100.0
    assert 'KRW-B' not in e.ledger


def test_sells_only_bot_quantity_when_user_also_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    e = make_engine(tmp_path, holdings={'B': [500.0, 90.0]})           # 사용자가 이미 B 500개 보유
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    bot_qty = e.ledger['KRW-B']['qty']
    tick(e, market({'B': 0}, price=90.0))                             # 손절
    sell = [o for o in e.exchange.orders if o[0] == 'sell'][0]
    assert sell[2] == pytest.approx(bot_qty)                          # 봇이 산 만큼만
    assert e.exchange.holdings['B'][0] == pytest.approx(500.0)


def test_sell_condition(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    e = make_engine(tmp_path, rules={'sell': {'id': 'my_down'}})
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    tick(e, market({'B': -6}, price=99.0))     # 손절(-5%)은 아니지만 매도 조건에 맞음
    assert [o[0] for o in e.exchange.orders] == ['buy', 'sell']
    assert any('매도 조건' in d['message'] for d in e.decisions)


def test_sell_condition_waits_order_interval_but_stop_loss_does_not(tmp_path):
    e = make_engine(tmp_path, rules={'sell': {'id': 'my_down'}})
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    tick(e, market({'B': -6}, price=99.0))     # 방금 샀음 → 매도 조건 매도는 60초 기다림
    assert [o[0] for o in e.exchange.orders] == ['buy']
    tick(e, market({'B': -6}, price=90.0))     # 손절은 간격과 상관없이
    assert [o[0] for o in e.exchange.orders] == ['buy', 'sell']


def test_trailing_stop_per_coin(tmp_path):
    e = make_engine(tmp_path, risk={'trailing_stop_percent': 3})
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    entry = e.ledger['KRW-B']['entry_price']
    for p in (entry * 1.05, entry * 1.10, entry * 1.08):
        tick(e, market({'B': 0}, price=p))
    assert [o[0] for o in e.exchange.orders] == ['buy']
    tick(e, market({'B': 0}, price=entry * 1.066))   # 최고 1.10에서 -3.1%
    assert [o[0] for o in e.exchange.orders] == ['buy', 'sell']


def test_excluded_risky_and_stable_not_bought(tmp_path):
    e = make_engine(tmp_path, rules={'exclude': ['ex', 'KRW-EX2']})
    extra = {'WARN': {'warning': True}, 'DEL': {'notices': [{'type': 'delisting'}]}}
    names = ['EX', 'EX2', 'WARN', 'DEL', 'USDT', 'OK']
    tick(e, market({c: 0 for c in names}, extra=extra))
    tick(e, market({c: 6 for c in names}, extra=extra))
    assert bought(e) == ['KRW-OK']


def test_daily_loss_limit_blocks_buys(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    e = make_engine(tmp_path, risk={'daily_loss_limit_percent': 1})
    e.risk_manager.note_balance(1_000_000)
    e.risk_manager.update_daily_loss(-20_000)                  # 오늘 이미 -2%
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    assert bought(e) == []
    assert any('손실 한도' in d['message'] for d in e.decisions)


def test_user_selling_shrinks_ledger(tmp_path):
    e = make_engine(tmp_path)
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    e.exchange.holdings['B'][0] = 0.0                          # 사용자가 업비트에서 직접 팜
    tick(e, market({'B': 0}))
    assert 'KRW-B' in e.ledger                                 # 한 번 본 것으로는 줄이지 않음 (다른 봇 매도 틈 방지)
    e._shrink_seen = {k: v - 10 for k, v in e._shrink_seen.items()}
    tick(e, market({'B': 0}))                                  # 다음 확인에도 그대로면 장부에서 뺌
    assert 'KRW-B' not in e.ledger
    assert [o[0] for o in e.exchange.orders] == ['buy']        # 봇은 팔지 않음


def test_ledger_survives_restart(tmp_path):
    e = make_engine(tmp_path)
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    e2 = make_engine(tmp_path, holdings={'B': e.exchange.holdings['B']})
    e2._load_ledger()
    assert 'KRW-B' in e2.ledger


def test_waits_until_data_ready(tmp_path):
    e = make_engine(tmp_path)
    tick(e, market({'B': 0}, ready=False))
    tick(e, market({'B': 6}, ready=False))
    assert e.exchange.orders == [] and e.baselines == {}


def test_changing_buy_params_resets_baseline(tmp_path):
    e = make_engine(tmp_path)
    tick(e, market({'B': 3}))
    e.config['trade_rules']['buy']['params'] = {'r0': 2}       # 기준 숫자를 2%로 낮춤 → 기준 다시 잡음
    tick(e, market({'B': 3}))
    assert e.exchange.orders == []
    tick(e, market({'B': 3, 'C': 3}))
    assert bought(e) == ['KRW-C']


def test_simulation_mode_buy_sell_and_stats(tmp_path):
    e = make_engine(tmp_path, mode='simulation', risk={'stop_loss_percent': 5, 'take_profit_percent': 10})
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    assert 'KRW-B' in e.simulation_engine.positions
    tick(e, market({'B': 0}, price=120.0))                    # 익절
    stats = e.simulation_engine.get_statistics()
    assert stats['positions'] == {}
    assert stats['statistics']['win_rate'] == 100             # 판 거래 1건, 이익
    assert stats['total_profit_rate'] > 0
    status = e.get_status()
    assert status['decisions'][0]['kind'] == 'sell'
    assert (tmp_path / 'sim_state.json').exists()


def test_simulation_position_shows_profit(tmp_path):
    e = make_engine(tmp_path, mode='simulation')
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    tick(e, market({'B': 0}, price=103.0))
    pos = e.simulation_engine.get_statistics()['positions']['KRW-B']
    assert pos['profit'] > 0 and pos['profit_rate'] > 0


def test_no_buy_rule_cannot_start(tmp_path):
    e = BotEngine({'mode': 'simulation', 'trade_rules': {}}, data_dir=tmp_path)
    ok, msg = e.initialize()
    assert not ok and '매수 조건' in msg


# 조건 여러 개: 하나라도 새로 들어오면 삼 / 조건마다 따로 기준
BIG = {'id': 'my_big', 'title': '거래대금 3배', 'match': 'all', 'sort': {'rule': 0, 'dir': 'desc'},
       'rules': [{'metric': 'value_mult', 'op': '>=', 'value': 3, 'days': 20}]}


def test_multiple_buy_conditions_or(tmp_path):
    e = make_engine(tmp_path, rules={'buy': [{'id': 'my_up'}, {'id': 'my_big'}]})
    data = lambda ch, big: {**market(ch, extra={c: {'trade_value_24h': 5e9} for c in big}), 'custom': [UP, DOWN, BIG]}
    tick(e, data({'A': 0, 'B': 0, 'C': 0}, ['A']))            # A는 거래대금 조건에 이미 맞음 → 기준
    tick(e, data({'A': 0, 'B': 6, 'C': 0}, ['A', 'C']))       # B는 +5% 조건에, C는 거래대금 조건에 새로 들어옴
    assert sorted(bought(e)) == ['KRW-B', 'KRW-C']
    conds = {s: v['condition'] for s, v in e.ledger.items()}
    assert conds == {'KRW-B': '오늘 5% 이상', 'KRW-C': '거래대금 3배'}


def test_multiple_sell_conditions_and_old_single_format(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    # 예전 형식(매수 조건 하나 dict)도 그대로 동작
    e = make_engine(tmp_path, rules={'buy': {'id': 'my_up'}, 'sell': [{'id': 'my_big'}, {'id': 'my_down'}]})
    data = lambda ch, big=(): {**market(ch, extra={c: {'trade_value_24h': 5e9} for c in big}), 'custom': [UP, DOWN, BIG]}
    tick(e, data({'B': 0}))
    tick(e, data({'B': 6}))
    tick(e, data({'B': 0}, ['B']))                            # 두 번째가 아니라 첫 번째 매도 조건에 맞음
    assert [o[0] for o in e.exchange.orders] == ['buy', 'sell']
    assert any("'거래대금 3배'" in d['message'] for d in e.decisions)


def test_removed_condition_forgets_baseline(tmp_path):
    e = make_engine(tmp_path, rules={'buy': [{'id': 'my_up'}]})
    tick(e, market({'B': 6}))
    assert len(e.baselines) == 1
    e.config['trade_rules']['buy'] = [{'id': 'my_up', 'params': {'r0': 3}}]
    tick(e, market({'B': 6}))
    assert len(e.baselines) == 1 and e.exchange.orders == []  # 숫자가 바뀐 조건은 새 기준


def test_buy_all_conditions_and(tmp_path):
    """'그리고': 두 매수 조건에 동시에 맞게 된 코인만 삼"""
    e = make_engine(tmp_path, rules={'buy': [{'id': 'my_up'}, {'id': 'my_big'}], 'buy_match': 'all'})
    data = lambda ch, big: {**market(ch, extra={c: {'trade_value_24h': 5e9} for c in big}), 'custom': [UP, DOWN, BIG]}
    tick(e, data({'A': 6, 'B': 0, 'C': 0}, []))            # 기준: 둘 다 맞는 코인 없음
    tick(e, data({'A': 6, 'B': 6, 'C': 0}, ['C']))         # A·B는 +5%만, C는 거래대금만 → 안 삼
    assert bought(e) == []
    tick(e, data({'A': 6, 'B': 6, 'C': 0}, ['B']))         # B가 이제 둘 다 만족 → B만 삼
    assert bought(e) == ['KRW-B']
    assert e.ledger['KRW-B']['condition'] == '오늘 5% 이상 + 거래대금 3배'


def test_sell_all_conditions_and(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    e = make_engine(tmp_path, rules={'buy': [{'id': 'my_up'}], 'sell': [{'id': 'my_down'}, {'id': 'my_big'}], 'sell_match': 'all'})
    data = lambda ch, big=(): {**market(ch, extra={c: {'trade_value_24h': 5e9} for c in big}), 'custom': [UP, DOWN, BIG]}
    tick(e, data({'B': 0}))
    tick(e, data({'B': 6}))
    tick(e, data({'B': -6}))                                                # -5% 조건만 맞음 → 안 팜
    assert [o[0] for o in e.exchange.orders] == ['buy']
    tick(e, data({'B': -6}, ['B']))                                         # 둘 다 맞음 → 팜
    assert [o[0] for o in e.exchange.orders] == ['buy', 'sell']



def test_rule_uses_chosen_time_bars_and_waits_until_they_arrive(tmp_path):
    """조건 시간 봉: params.tf='4h' 이면 일봉(lows) 대신 lows_tf['4h'] 로 계산, 아직 없으면 그 조건은 건너뜀"""
    e = make_engine(tmp_path, rules={'buy': [{'id': 'streak_up', 'params': {'days': 3, 'tf': '4h'}}]})
    flat = [100.0] * 30
    rising = [100.0] * 26 + [100.0, 101.0, 102.0, 103.0]

    def data(b_bars, price_b=104.0):
        d = market({'A': 0, 'B': 0}, price={'A': 100.0, 'B': price_b})
        d['lows_tf'] = {'4h': {'KRW-A': {'day_closes': flat, 'day_highs': flat, 'day_values': [1e9] * 30},
                               'KRW-B': {'day_closes': b_bars, 'day_highs': b_bars, 'day_values': [1e9] * 30}}} if b_bars else {}
        return d

    tick(e, data(None))                         # 4시간봉을 아직 못 받음 → 건너뜀 (기준도 안 잡음)
    assert e.exchange.orders == [] and not e.baselines
    tick(e, data(flat))                         # 받음 → 기준 (아무것도 연속 상승 아님)
    assert e.exchange.orders == [] and e.baselines
    tick(e, data(rising))                       # B 가 4시간봉으로 연속 상승 → 삼 (일봉은 그대로 평평)
    assert bought(e) == ['KRW-B']


def test_trade_target_limits_buys(tmp_path):
    """매매대상: 코인 성격(target, '그리고' 기본 · '또는') · 직접 고른 코인(only) 안에서만 매수 조건을 봄"""
    extra = {'A': {'listed_days': 10, 'on_binance': False}, 'B': {'listed_days': 900, 'on_binance': True},
             'C': {'listed_days': 20, 'on_binance': True}}
    e = make_engine(tmp_path, rules={'target': [{'id': 'new_listing', 'params': {'days': 30}}, {'id': 'not_on_binance'}]})
    tick(e, market({'A': 1, 'B': 1, 'C': 1}, extra=extra))
    tick(e, market({'A': 7, 'B': 7, 'C': 7}, extra=extra))     # 셋 다 새로 맞았지만 '새로 상장 그리고 업비트에만' = A
    assert bought(e) == ['KRW-A']
    e2 = make_engine(tmp_path / 'b', rules={'target': [{'id': 'new_listing', 'params': {'days': 30}}, {'id': 'not_on_binance'}],
                                            'target_match': 'any'})
    tick(e2, market({'A': 1, 'B': 1, 'C': 1}, extra=extra))
    tick(e2, market({'A': 7, 'B': 7, 'C': 7}, extra=extra))
    assert sorted(bought(e2)) == ['KRW-A', 'KRW-C']               # '또는' = 새로 상장(A·C) + 업비트에만(A)
    e3 = make_engine(tmp_path / 'c', rules={'only': ['b', 'KRW-C'], 'exclude': ['C']})
    tick(e3, market({'A': 1, 'B': 1, 'C': 1}, extra=extra))
    tick(e3, market({'A': 7, 'B': 7, 'C': 7}, extra=extra))
    assert bought(e3) == ['KRW-B']                                 # 직접 고른 B · C 중 사지 않을 C 는 뺌
    # 왼쪽 목록의 '직접 고른 코인' (매매대상 조건): '또는'이면 고른 코인 + 새로 상장한 코인
    e4 = make_engine(tmp_path / 'd', rules={'target': [{'id': 'pick_coins', 'params': {'coins': ['B']}},
                                                       {'id': 'new_listing', 'params': {'days': 15}}], 'target_match': 'any'})
    tick(e4, market({'A': 1, 'B': 1, 'C': 1}, extra=extra))
    tick(e4, market({'A': 7, 'B': 7, 'C': 7}, extra=extra))
    assert sorted(bought(e4)) == ['KRW-A', 'KRW-B']


def test_last_slot_buys_with_what_is_left(tmp_path, monkeypatch):
    """20% × 5개: 수수료 때문에 5번째 살 돈이 조금 모자라도 마지막 자리는 남은 돈으로 삼 (2026-10-15)"""
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    e = make_engine(tmp_path, risk={'position_size_percent': 20, 'max_positions': 5, 'stop_loss_percent': 0})
    tick(e, market({c: 0 for c in 'ABCDE'}))
    tick(e, market({'A': 6, 'B': 6, 'C': 6, 'D': 0, 'E': 0}))
    tick(e, market({c: 6 for c in 'ABCDE'}))
    assert sorted(bought(e)) == [f'KRW-{c}' for c in 'ABCDE']
    last = [o[2] for o in e.exchange.orders if o[0] == 'buy'][-1]
    assert 150_000 < last < 200_000 and e.exchange.krw >= 0
    # 마지막 자리가 아니면 그대로 (돈이 모자라면 사지 않음)
    e2 = make_engine(tmp_path / 'b', risk={'position_size_percent': 20, 'max_positions': 10, 'stop_loss_percent': 0}, krw=100_000)
    assert e2._last_slot_fit(200_000, 100_000) is None


def test_start_wait_skips_start_buy_and_refill(tmp_path, monkeypatch):
    """'켤 때는 사지 않고 새로 맞게 될 때만' (start_wait): 코인 성격 조건도 켤 때 맞는 코인은 사지 않음 (2026-10-15)"""
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    e = make_engine(tmp_path, rules={'buy': [{'id': 'all_coins'}]})
    tick(e, market({'A': 0, 'B': 0}))
    assert sorted(bought(e)) == ['KRW-A', 'KRW-B']          # 기본: 켤 때 맞는 코인을 삼
    w = make_engine(tmp_path / 'w', rules={'buy': [{'id': 'all_coins'}], 'start_wait': True})
    tick(w, market({'A': 0, 'B': 0}))
    tick(w, market({'A': 0, 'B': 0}))
    assert bought(w) == []                                   # 켤 때 · 돈이 있어도 안 삼
    tick(w, market({'A': 0, 'B': 0, 'C': 0}))
    assert bought(w) == ['KRW-C']                            # 새로 맞게 된 코인만


def test_custom_held_metric_sees_account_holdings(tmp_path, monkeypatch):
    """내 조건의 '보유 = 예'는 계좌에 있는 코인에만 맞음 (전에는 매매대상이 '보유 코인'일 때만 held 가 붙어 늘 안 맞았음)"""
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    HELD_UP = {'id': 'my_held_up', 'title': '보유 + 5%', 'match': 'all', 'sort': {'rule': 0, 'dir': 'desc'},
               'rules': [{'metric': 'held', 'op': '==', 'value': 1}, {'metric': 'change_rate', 'op': '>=', 'value': 5}]}
    e = make_engine(tmp_path, rules={'buy': [{'id': 'my_held_up'}]}, holdings={'A': [1.0, 100.0]})
    d0, d1 = market({'A': 0, 'B': 0}), market({'A': 6, 'B': 6})
    d0['custom'].append(HELD_UP)
    d1['custom'].append(HELD_UP)
    tick(e, d0)
    tick(e, d1)
    assert bought(e) == ['KRW-A']                     # 계좌에 있는 A 만 (B 는 보유 아님)


# ── 2026-10-17 검토에서 찾은 틈: 잔고 조회 실패 · 하루 손실 되살리기 · 장부 원자적 저장 ──
def test_sell_waits_when_balance_lookup_fails(tmp_path):
    """손절 순간 잔고 조회가 실패하면 장부를 지우지 않고 다음 확인 때 다시 본다 (전에는 '직접 팔았음'으로 보고 장부만 지웠음)"""
    e = make_engine(tmp_path)
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    assert 'KRW-B' in e.ledger
    e.exchange.get_all_positions = lambda: None                # 업비트 429 등으로 조회 실패
    tick(e, market({'B': -8}, price=90.0))                                 # 손절(−5%) 걸리는 값
    assert 'KRW-B' in e.ledger and e.ledger['KRW-B']['qty'] > 0
    assert [o[0] for o in e.exchange.orders] == ['buy']
    assert any('잔고 조회에 실패' in d['message'] for d in e.decisions)
    del e.exchange.get_all_positions                           # 조회가 되면 그때 팖
    tick(e, market({'B': -8}, price=90.0))
    assert 'KRW-B' not in e.ledger and [o[0] for o in e.exchange.orders] == ['buy', 'sell']


def test_buy_skips_when_balance_lookup_fails(tmp_path):
    """사기 직전 잔고 조회가 실패하면 사지 않는다 (전에는 주문 전 수량을 0 으로 쳐 사용자 코인까지 봇 수량에 들어갔음)"""
    e = make_engine(tmp_path, holdings={'B': [1000.0, 100.0]})   # 사용자가 이미 B 1,000개
    tick(e, market({'B': 0}))
    e.exchange.get_all_positions = lambda: None
    tick(e, market({'B': 6}))
    assert bought(e) == [] and 'KRW-B' not in e.ledger
    assert any('매수 보류' in d['message'] and '잔고 조회' in d['message'] for d in e.decisions)
    del e.exchange.get_all_positions
    tick(e, market({'B': 6}))                                  # 이미 맞던 코인이라 기준에 들어가 있음 → 새로 들어와야 삼
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    assert bought(e) == ['KRW-B'] and e.ledger['KRW-B']['qty'] < 1000   # 봇 몫만


def test_daily_loss_restored_on_restart(tmp_path):
    """오늘 판 손실은 봇을 다시 켜도 하루 손실 한도에 남아 있다"""
    e = make_engine(tmp_path, risk={'daily_loss_limit_percent': 1})
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    tick(e, market({'B': -8}, price=90.0))                                 # 손절로 팜 → 오늘 손실
    assert e.risk_manager.daily_loss < 0
    lost = e.risk_manager.daily_loss
    e2 = make_engine(tmp_path, risk={'daily_loss_limit_percent': 1})
    assert e2.risk_manager.daily_loss == 0
    e2._restore_daily_loss()
    assert e2.risk_manager.daily_loss == pytest.approx(lost)


def test_ledger_and_trades_saved_atomically(tmp_path):
    e = make_engine(tmp_path)
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    assert not list(tmp_path.glob('*.tmp'))
    import json as _json
    assert 'KRW-B' in _json.loads((tmp_path / 'bot_positions.json').read_text(encoding='utf-8'))
    assert _json.loads((tmp_path / 'trades.json').read_text(encoding='utf-8'))[-1]['side'] == 'buy'


def test_baseline_kept_when_data_briefly_unready(tmp_path):
    """자료가 잠깐 덜 모여 한 번 건너뛰어도 매수 기준은 남아, 그 사이 새로 들어온 코인을 산다 (전에는 기준이 지워져 다시 '기준 잡기'만 했음)"""
    e = make_engine(tmp_path)
    tick(e, market({'C': 0}))                                   # 기준: 지금 맞는 코인 없음
    tick(e, market({'C': 0}, ready=False))                      # 4시간봉 등이 잠깐 덜 모임 → 이번 확인 건너뜀
    tick(e, market({'C': 6}))                                   # 새로 들어옴 → 사야 함
    assert bought(e) == ['KRW-C']


def test_spike_rebuy_gives_up_when_cash_short(tmp_path):
    """급등 뒤 되사기: 원화가 모자라면 접는다 (전에는 5초마다 영원히 '보류' 경고)"""
    e = make_engine(tmp_path)
    tick(e, market({'B': 0}))
    tick(e, market({'B': 6}))
    e.exchange.krw = 1000.0
    e.spike.state['KRW-B'] = {'phase': 'waiting', 'sold_price': 110.0, 'sold_qty': 10.0, 'krw': 30000.0, 'left': [100.0],
                              'part_krw': 30000.0, 'deadline': 10 ** 12, 'since': 0, 'bought_qty': 0.0, 'info': {}}
    e._spike_rebuy('KRW-B', 100.0, {'krw': 30000.0, 'n': 1, 'why': '시험'}, now=1.0)
    assert 'KRW-B' not in e.spike.state and e.spike.history[0]['how'] == 'rebuy_failed'
    assert any('다시 사기 접음' in d['message'] for d in e.decisions)
    assert [o[0] for o in e.exchange.orders] == ['buy']        # 되사기 주문은 안 나감


def test_adopted_coins_do_not_eat_buy_budget(tmp_path):
    """맡긴 코인은 팔기만 맡긴 것 → 새로 살 돈(운용 한도)에서 빼지 않음 (2026-10-17 이홍로 님 결정)"""
    e = make_engine(tmp_path, krw=5_000_000.0)
    e.ledger['KRW-A'] = {'qty': 1.0, 'entry_price': 10 ** 12, 'adopted': True, 'orig_entry': 10 ** 12}   # 한도만큼 맡김
    assert e._live_budget_left() == pytest.approx(5_000_000)
    e.ledger['KRW-B'] = {'qty': 1.0, 'entry_price': 10 ** 12 - 1_000_000}                                 # 봇이 산 코인은 한도를 차지
    assert e._live_budget_left() == pytest.approx(1_000_000)


def test_bot_selling_adopted_coin_books_pre_adopt_loss_separately(tmp_path):
    """맡긴 코인을 봇이 손절로 팔면, 맡기기 전 손실(원래 평균가 → 맡긴 때)은 '맡긴 코인 정리' 몫으로 남는다"""
    from bot_core.bot_engine import adopted_legacy_total
    e = make_engine(tmp_path, holdings={'B': [100.0, 100.0]})
    e.ledger['KRW-B'] = {'qty': 100.0, 'entry_price': 60.0, 'orig_entry': 100.0, 'adopted': True, 'basis': 'adopt_price', 'condition': '맡긴 코인'}
    tick(e, market({'B': -8}, price=55.0))                       # 맡긴 때 60 → 55: −8% 손절
    assert 'KRW-B' not in e.ledger
    assert adopted_legacy_total(tmp_path) == pytest.approx((60.0 - 100.0) * 100)
