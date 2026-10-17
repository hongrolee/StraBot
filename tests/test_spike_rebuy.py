"""
급등 때 일부 팔고 다시 사기 (봇이 산 코인만) — 가짜 체결 요약·가짜 거래소, 실제 주문·요청 없음
실행: venv/Scripts/python.exe -m pytest tests/test_spike_rebuy.py -q
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_core.bot_engine as be  # noqa: E402
import bot_core.spike_rebuy as sr  # noqa: E402
from bot_core.bot_engine import BotEngine  # noqa: E402
from bot_core.simulation_engine import SimulationEngine  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_condition_trader import FakeExchange, NullLogger  # noqa: E402

ON = {'enabled': True}
T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(be.time, 'sleep', lambda s: None)


def spike_snap(minute=1, price=None, sym='KRW-A', value=1e9, avg=1e8, o=100.0, c=106.0, ago=95.0, ready=True):
    """방금 끝난 1분: 거래대금 평소의 10배 · 1분 +6% · 1시간 +11.6%"""
    return {'ready': ready, 'minute': minute,
            'coins': {sym: {'value': value, 'avg60': avg, 'open': o, 'close': c, 'close_1h': ago}},
            'prices': {sym: price if price is not None else c}}


def prices(p, sym='KRW-A', minute=2):
    return {'ready': True, 'minute': minute, 'coins': {}, 'prices': {sym: p}}


def sim_engine(tmp_path, rules=None):
    config = {'mode': 'simulation', 'risk_config': {'max_positions': 5},
              'trade_rules': {'buy': {'id': 'my_up'}, 'spike_rebuy': {**ON, **(rules or {})}},
              'simulation_config': {'initial_balance': 1_000_000}}
    e = BotEngine(config, data_dir=tmp_path)
    e.trade_logger = NullLogger()
    e.simulation_engine = SimulationEngine(1_000_000)
    e.simulation_engine.buy('KRW-A', 100.0, 1000.0, condition='테스트')   # 봇이 산 코인 1,000개
    return e


def qty(e, sym='KRW-A'):
    return e._positions()[sym]['qty']


# ── 판단 ──
def test_is_spike_needs_all_three():
    s = sr.settings({'spike_rebuy': ON})
    c = spike_snap()['coins']['KRW-A']
    assert sr.is_spike(c, s)
    assert not sr.is_spike({**c, 'value': 4e8}, s)             # 평소의 4배
    assert not sr.is_spike({**c, 'close': 104.0}, s)           # 1분 +4%
    assert not sr.is_spike({**c, 'close_1h': 100.0}, s)        # 1시간 +6% (아직 덜 부풀어 오름)
    assert not sr.is_spike({**c, 'value': 5e6, 'avg60': 1e5}, s)   # 1분 1천만 원 미만


def test_settings_clamp_and_defaults():
    s = sr.settings({'spike_rebuy': {'enabled': 1, 'portion': 500, 'dips': ['x']}})
    assert s['enabled'] and s['portion'] == 100 and s['dips'] == [3, 5, 8] and s['rise_1h'] == 10


# ── 봇 (시뮬레이션) ──
def test_full_cycle_sell_on_pullback_then_rebuy_in_parts(tmp_path):
    e = sim_engine(tmp_path)
    e.spike_tick(spike_snap(), now=T0)                           # 급등 감지 → 꺾이기를 기다림
    assert e.spike.state['KRW-A']['phase'] == 'selling' and qty(e) == 1000
    e.spike_tick(prices(110.0), now=T0 + 10)                     # 더 오름 (최고 110)
    e.spike_tick(prices(106.5), now=T0 + 20)                     # 110에서 3% 꺾임 → 30% 팜
    assert qty(e) == pytest.approx(700)
    st = e.spike.state['KRW-A']
    assert st['phase'] == 'waiting' and len(st['left']) == 3
    sold = st['sold_price']
    e.spike_tick(prices(sold * 0.965), now=T0 + 60)              # −3.5% → 첫 몫만 삼
    assert len(e.spike.state['KRW-A']['left']) == 2
    e.spike_tick(prices(sold * 0.91), now=T0 + 120)              # −9% → 남은 두 몫 삼 → 끝
    assert 'KRW-A' not in e.spike.state
    done = e.spike.history[0]
    assert done['how'] == 'done' and done['coin_change'] > 0     # 싸게 다시 사서 코인이 늘어남
    assert qty(e) > 1000                                         # 판 300개보다 더 많이 다시 삼
    assert any('급등 때 30% 팜' in d['message'] for d in e.decisions)


def test_timeout_rebuys_rest_and_cooldown(tmp_path):
    e = sim_engine(tmp_path)
    e.spike_tick(spike_snap(), now=T0)
    e.spike_tick(prices(102.0), now=T0 + 10)                     # 106에서 3% 넘게 꺾임 → 팜
    sold = e.spike.state['KRW-A']['sold_price']
    e.spike_tick(prices(sold * 1.2), now=T0 + 25 * 3600)         # 24시간 지남 → 비싸도 남은 몫 삼
    assert 'KRW-A' not in e.spike.state
    assert e.spike.history[0]['coin_change'] < 0                 # 비싸게 다시 사서 코인이 줄어듦
    # 끝난 코인은 60분 동안 다시 보지 않음
    e.spike_tick(spike_snap(minute=99), now=time.time() + 10)
    assert 'KRW-A' not in e.spike.state


def test_no_sell_after_30min_without_pullback_sells_anyway(tmp_path):
    e = sim_engine(tmp_path)
    e.spike_tick(spike_snap(), now=T0)
    e.spike_tick(prices(107.0), now=T0 + 31 * 60)                # 30분 동안 안 꺾임 → 그때 팜
    assert e.spike.state['KRW-A']['phase'] == 'waiting'


def test_only_bot_positions_and_disabled(tmp_path):
    e = sim_engine(tmp_path, rules={'enabled': False})
    e.spike_tick(spike_snap(), now=T0)
    assert e.spike.state == {}                                   # 꺼져 있으면 감지하지 않음
    e2 = sim_engine(tmp_path / 'b')
    e2.spike_tick(spike_snap(sym='KRW-ZZZ'), now=T0)             # 봇이 안 산 코인 → 무시
    assert e2.spike.state == {}


def test_not_ready_flow_does_nothing(tmp_path):
    e = sim_engine(tmp_path)
    e.spike_tick(spike_snap(ready=False), now=T0)                # 켠 뒤 61분이 안 됨
    assert e.spike.state == {}


def test_position_closed_elsewhere_cancels_rebuy(tmp_path):
    e = sim_engine(tmp_path)
    e.spike_tick(spike_snap(), now=T0)
    e.spike_tick(prices(102.0), now=T0 + 10)
    e.simulation_engine.sell('KRW-A', 90.0, qty(e))              # 손절 등으로 봇이 나머지를 다 팜
    e.spike_tick(prices(80.0), now=T0 + 60)
    assert 'KRW-A' not in e.spike.state and 'KRW-A' not in e._positions()   # 다시 사지 않음
    assert e.spike.history[0]['how'] == 'position_closed'


def test_state_survives_restart(tmp_path):
    e = sim_engine(tmp_path)
    e.spike_tick(spike_snap(), now=T0)
    e.spike_tick(prices(102.0), now=T0 + 10)
    again = sr.SpikeRebuy(tmp_path / 'spike_state.json')        # 봇을 껐다 켬
    assert again.state['KRW-A']['phase'] == 'waiting'


# ── 봇 (실전 장부, 가짜 거래소) ──
def test_live_ledger_partial_sell_and_rebuy(tmp_path):
    config = {'mode': 'live', 'risk_config': {}, 'trade_rules': {'buy': {'id': 'my_up'}, 'spike_rebuy': ON}}
    e = BotEngine(config, data_dir=tmp_path)
    e.trade_logger = NullLogger()
    e.exchange = FakeExchange(krw=0, holdings={'A': [1500.0, 100.0]})    # 사용자 코인 500 + 봇 코인 1000
    e.ledger = {'KRW-A': {'qty': 1000.0, 'entry_price': 100.0, 'opened_at': '', 'condition': 't'}}
    e.exchange.prices = {'KRW-A': 106.0}
    e.spike_tick(spike_snap(), now=T0)
    e.exchange.prices = {'KRW-A': 102.0}
    e.spike_tick(prices(102.0), now=T0 + 10)
    assert e.ledger['KRW-A']['qty'] == pytest.approx(700)       # 봇 코인의 30%만 (사용자 코인은 그대로)
    assert e.exchange.orders[-1][0] == 'sell' and e.exchange.orders[-1][2] == pytest.approx(300)
    e.exchange.prices = {'KRW-A': 90.0}
    e.spike_tick(prices(90.0), now=T0 + 60)                      # 세 가격 모두 아래 → 한 번에 다시 삼
    assert 'KRW-A' not in e.spike.state
    assert e.ledger['KRW-A']['qty'] > 1000 and e.ledger['KRW-A']['entry_price'] < 100


# ── 매도 칸의 '순간 급등' 조건으로 켜기 (파는 방식) ──
def sell_rule(mode='partial', portion=30):
    return {'id': 'spike_pump', 'params': {'mult': 5, 'rise': 5, 'rise_1h': 10}, 'sell_mode': mode, 'portion': portion}


def test_settings_from_sell_rule_and_legacy():
    s = sr.settings({'sell': [sell_rule(portion=40)]})
    assert s['enabled'] and s['portion'] == 40 and s['mult'] == 5
    assert not sr.settings({'sell': [sell_rule('all')]})['enabled']          # 전부 팔기 = 일반 매도 조건
    assert sr.settings({'spike_rebuy': {'enabled': True}})['enabled']       # 예전 설정도 읽음
    assert sr.is_partial_rule({'id': 'spike_pump'})                         # 파는 방식 없으면 = 일부 팔고 다시 사기


def test_partial_rule_in_sell_zone_runs_spike_cycle(tmp_path):
    config = {'mode': 'simulation', 'risk_config': {}, 'simulation_config': {'initial_balance': 1_000_000},
              'trade_rules': {'buy': {'id': 'my_up'}, 'sell': [sell_rule()]}}
    e = BotEngine(config, data_dir=tmp_path)
    e.trade_logger = NullLogger()
    e.simulation_engine = SimulationEngine(1_000_000)
    e.simulation_engine.buy('KRW-A', 100.0, 1000.0)
    e.spike_tick(spike_snap(), now=T0)
    e.spike_tick(prices(102.0), now=T0 + 10)
    assert qty(e) == pytest.approx(700)                                    # 30%만 팜


def test_sell_all_mode_sells_everything_via_condition(tmp_path, monkeypatch):
    import bot_core.risk_manager as rm
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    config = {'mode': 'simulation', 'risk_config': {'take_profit_percent': 50}, 'simulation_config': {'initial_balance': 1_000_000},
              'trade_rules': {'buy': {'id': 'my_up'}, 'sell': [sell_rule('all')]}}
    e = BotEngine(config, data_dir=tmp_path)
    e.trade_logger = NullLogger()
    e.simulation_engine = SimulationEngine(1_000_000)
    e.simulation_engine.buy('KRW-A', 100.0, 1000.0)
    coin = {'currency': 'A', 'korean_name': 'A', 'current_price': 106.0, 'change_rate': 6, 'prev_close': 100, 'today_low': 99,
            'trade_value_24h': 1e9, 'spike_value': 1e9, 'spike_avg': 1e8, 'spike_open': 100.0, 'spike_close': 106.0, 'spike_close_1h': 95.0}
    data = {'coins': [coin], 'lows': {'KRW-A': {'day_closes': [100] * 30, 'day_highs': [100] * 30, 'day_values': [1e9] * 30}},
            'hourly': {}, 'flow_ready': True, 'custom': []}
    e.tick({**data, 'spike_ready': False})                                  # 켠 뒤 1시간 전: 판단 안 함
    assert 'KRW-A' in e.simulation_engine.positions
    e.tick({**data, 'spike_ready': True})
    assert 'KRW-A' not in e.simulation_engine.positions                    # 전부 팜
    e.spike_tick(spike_snap(), now=T0)
    assert e.spike.state == {}                                             # 급등 모듈은 꺼져 있음


# ── 급등 지켜보기 (화면용): 가장 가까운 코인 · 켠 뒤 가장 가까웠던 순간 ──
def test_watch_list_ranks_closest_and_counts_met():
    s = sr.settings({'sell': [sell_rule()]})
    snap = {'ready': True, 'minute': 5, 'coins': {
        'KRW-A': {'value': 2e7, 'avg60': 1e7, 'open': 100.0, 'close': 101.0, 'close_1h': 94.0},   # 1시간 +7.4% · 1분 +1% · 2배
        'KRW-B': {'value': 6e7, 'avg60': 1e7, 'open': 100.0, 'close': 106.0, 'close_1h': 105.0},  # 1분 +6% ✓ · 거래 6배 ✓
        'KRW-C': {'value': 5e6, 'avg60': 1e5, 'open': 100.0, 'close': 100.0, 'close_1h': 100.0},  # 50배지만 1천만 원 미만 → 안 셈
        'KRW-D': {'value': 1e7, 'avg60': 1e7, 'open': 100.0, 'close': 100.0, 'close_1h': 100.0}}}
    w = sr.watch_list(snap, {'KRW-A', 'KRW-B', 'KRW-C', 'KRW-D'}, s)
    assert [x['symbol'] for x in w] == ['KRW-B', 'KRW-A', 'KRW-C']
    assert w[0]['met'] == 2 and w[2]['met'] == 0
    assert sr.watch_list(snap, {'KRW-Z'}, s) == []                         # 가진 코인만


def test_engine_keeps_watch_and_best_moment(tmp_path):
    config = {'mode': 'simulation', 'risk_config': {}, 'simulation_config': {'initial_balance': 1_000_000},
              'trade_rules': {'buy': {'id': 'my_up'}, 'sell': [sell_rule()]}}
    e = BotEngine(config, data_dir=tmp_path)
    e.trade_logger = NullLogger()
    e.simulation_engine = SimulationEngine(1_000_000)
    e.simulation_engine.buy('KRW-A', 100.0, 1000.0)
    e.spike_tick(spike_snap(ready=False), now=T0)
    assert e.spike_watch == {'ready': False}
    e.spike_tick(spike_snap(minute=1, value=3e7, avg=1e7, o=100.0, c=101.0, ago=97.0), now=T0)   # 1시간 +4% · 1분 +1% · 3배
    e.spike_tick(spike_snap(minute=2, value=1e7, avg=1e7, o=100.0, c=100.0, ago=100.0), now=T0 + 60)
    w = e.spike_watch
    assert w['ready'] and w['items'][0]['symbol'] == 'KRW-A' and w['items'][0]['mult'] == pytest.approx(1)
    assert w['best']['mult'] == pytest.approx(3) and w['best']['time']              # 켠 뒤 가장 가까웠던 1분은 기억
    e.is_running = True
    assert e.get_status()['spike']['watch']['best']['symbol'] == 'KRW-A'
