"""
하루 급등 · 꺾이면 일부 팔고 다시 사기 (봇이 산 코인만) — 가짜 1시간봉·가짜 가격, 실제 주문·요청 없음
실행: venv/Scripts/python.exe -m pytest tests/test_surge_rebuy.py -q
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_core.bot_engine as be  # noqa: E402
import bot_core.conditions as cd  # noqa: E402
import bot_core.spike_rebuy as sr  # noqa: E402
from bot_core.bot_engine import BotEngine  # noqa: E402
from bot_core.simulation_engine import SimulationEngine  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_condition_trader import NullLogger  # noqa: E402

T0 = 1_800_000_000.0
H = 3600


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(be.time, 'sleep', lambda s: None)


def hourly(now, first=100.0, n=24, step=0.0):
    """최근 n개 1시간봉 (오래된 순, 마지막이 진행 중). 첫 봉 종가 = first"""
    base = int(now // H) * H
    closes = [first * (1 + step) ** i for i in range(n)]
    return {'t': [base - (n - 1 - i) * H for i in range(n)], 'c': closes, 'l': [c * 0.99 for c in closes]}


def surge_rule(mode='partial', portion=30, **params):
    return {'id': 'surge_24h', 'params': {'rise_24h': 20, **params}, 'sell_mode': mode, 'portion': portion}


def spike_rule():
    return {'id': 'spike_pump', 'params': {'mult': 5, 'rise': 5, 'rise_1h': 10}, 'sell_mode': 'partial', 'portion': 30}


def sim_engine(tmp_path, sell=None, risk=None):
    config = {'mode': 'simulation', 'risk_config': risk or {'max_positions': 5},
              'trade_rules': {'buy': {'id': 'my_up'}, 'sell': sell if sell is not None else [surge_rule()]},
              'simulation_config': {'initial_balance': 1_000_000}}
    e = BotEngine(config, data_dir=tmp_path)
    e.trade_logger = NullLogger()
    e.simulation_engine = SimulationEngine(1_000_000)
    e.simulation_engine.buy('KRW-A', 100.0, 1000.0, condition='테스트')   # 봇이 산 코인 1,000개
    return e


def prices(p, sym='KRW-A'):
    return {'ready': True, 'minute': 1, 'coins': {}, 'prices': {sym: p}}


def qty(e, sym='KRW-A'):
    return e._positions()[sym]['qty']


def detect(e, price=125.0, now=T0, first=100.0):
    """지금 가격 price, 24시간 전 first 인 가짜 시장 데이터로 하루 급등 감지"""
    e.last_prices = {'KRW-A': price}
    return e.surge_detect({'hourly': {'KRW-A': hourly(now, first)}}, now=now)


def sell_on_pullback(e):
    detect(e)                                                    # 24시간 +25% → 지켜보기
    e.spike_tick(prices(130.0), now=T0 + H)                      # 더 오름 (최고 130)
    e.spike_tick(prices(119.0), now=T0 + 2 * H)                  # 130에서 8% 넘게 꺾임 → 30% 팜


# ── 조건 '하루 급등' (1시간봉) ──
def test_surge_condition_from_hourly():
    cond = cd.resolve('surge_24h', [])
    coins = [{'currency': c, 'korean_name': c, 'current_price': p} for c, p in (('A', 125.0), ('B', 115.0), ('C', 130.0), ('D', 150.0))]
    hr = {'KRW-A': hourly(T0, 100.0), 'KRW-B': hourly(T0, 100.0),
          'KRW-C': hourly(T0, 100.0, n=1),                          # 봉 하나뿐 (지금 봉) → 24시간 전 가격 없음
          'KRW-D': hourly(T0, 50.0, n=26)}                          # 맨 앞 봉은 24시간보다 오래됨 → 둘째 봉부터
    hr['KRW-D']['c'][1] = 120.0
    hits = cd.run_condition(cond, None, coins, {}, hr, now=T0)
    assert sorted(h['currency'] for h in hits) == ['A', 'D']
    a = next(h for h in hits if h['currency'] == 'A')
    assert a['s_close_24h'] == 100.0 and a['s_rise_24h'] == pytest.approx(25.0)
    d = next(h for h in hits if h['currency'] == 'D')
    assert d['s_close_24h'] == 120.0                                 # 24시간 안의 첫 봉
    loose = cd.run_condition(cond, {'rise_24h': 10}, coins, {}, hr, now=T0)
    assert [h['currency'] for h in loose][-1] == 'B'                 # 많이 오른 순 (B +15%가 마지막)
    assert cd.close_24h_of(None) is None


def test_is_surge_same_as_condition():
    s = sr.surge_settings({'sell': [surge_rule()]})
    assert sr.is_surge(121.0, 100.0, s)['rise_24h'] == pytest.approx(21.0)
    assert sr.is_surge(119.0, 100.0, s) is None
    assert sr.is_surge(150.0, None, s) is None


# ── 설정 ──
def test_surge_settings_defaults_and_clamp():
    s = sr.surge_settings({'sell': [surge_rule()]})
    assert s['enabled'] and s['portion'] == 30 and s['rise_24h'] == 20 and s['pullback'] == 8
    assert s['pullback_wait_h'] == 48 and s['max_wait_h'] == 72 and s['dips'] == [5, 10, 15] and s['timeout_sell'] is False
    bad = sr.surge_settings({'sell': [{**surge_rule(portion=500, rise_24h=0, pullback=99, max_wait_h=9999), 'dips': ['x']}]})
    assert bad['portion'] == 100 and bad['rise_24h'] == 1 and bad['pullback'] == 50 and bad['max_wait_h'] == 168
    assert bad['dips'] == [5, 10, 15]
    assert not sr.surge_settings({'sell': [surge_rule('all')]})['enabled']       # 전부 팔기 = 일반 매도 조건
    assert not sr.surge_settings({})['enabled']
    assert sr.is_partial_rule({'id': 'surge_24h'}) and sr.is_surge_rule({'id': 'surge_24h'})
    assert not sr.is_spike_rule({'id': 'surge_24h'})
    assert not sr.settings({'sell': [surge_rule()]})['enabled']                   # 순간 급등 설정과 섞이지 않음


# ── 봇 (시뮬레이션) ──
def test_detect_needs_rule_rise_and_bot_position(tmp_path):
    e = sim_engine(tmp_path)
    assert detect(e, price=115.0) == []                          # +15%: 아직
    assert detect(e, price=125.0)[0][0] == 'KRW-A'
    st = e.surge.state['KRW-A']
    assert st['phase'] == 'selling' and st['peak'] == 125.0 and st['info']['rise_24h'] == pytest.approx(25.0)
    assert (tmp_path / 'surge_state.json').exists()              # 상태는 봇 폴더(여기서는 임시 폴더)에 저장
    off = sim_engine(tmp_path / 'off', sell=[])
    assert detect(off) == [] and off.surge.state == {}           # 규칙이 없으면 보지 않음
    other = sim_engine(tmp_path / 'other')
    other.last_prices = {'KRW-Z': 200.0}
    assert other.surge_detect({'hourly': {'KRW-Z': hourly(T0)}}, now=T0) == []   # 봇이 안 산 코인은 무시


def test_sells_portion_on_pullback_then_rebuys_at_dips(tmp_path):
    e = sim_engine(tmp_path)
    sell_on_pullback(e)
    assert qty(e) == pytest.approx(700)
    st = e.surge.state['KRW-A']
    assert st['phase'] == 'waiting'
    sold = st['sold_price']
    assert st['left'] == pytest.approx([sold * 0.95, sold * 0.90, sold * 0.85])
    assert any('하루 급등 때 30% 팜' in d['message'] for d in e.decisions)
    e.spike_tick(prices(sold * 0.94), now=T0 + 3 * H)            # −6% → 첫 몫만 삼
    assert len(e.surge.state['KRW-A']['left']) == 2
    e.spike_tick(prices(sold * 0.84), now=T0 + 4 * H)            # −16% → 남은 두 몫 → 끝
    assert 'KRW-A' not in e.surge.state
    done = e.surge.history[0]
    assert done['how'] == 'done' and done['coin_change'] > 0 and qty(e) > 1000
    assert any('하루 급등 뒤 다시 삼' in d['message'] for d in e.decisions)


def test_trade_log_reasons(tmp_path):
    e = sim_engine(tmp_path)
    logged = []
    e._log_trade = lambda *a, **k: logged.append(a[7] if len(a) > 7 else k.get('reason'))
    sell_on_pullback(e)
    sold = e.surge.state['KRW-A']['sold_price']
    e.spike_tick(prices(sold * 0.8), now=T0 + 3 * H)
    import re
    assert re.fullmatch(r'하루 급등 때 일부 팔기 \(\d+(\.\d+)?%\)', logged[0]), logged      # 판 비율까지 (예: 30%)
    assert logged[1] == '하루 급등 뒤 다시 사기'


def test_no_pullback_in_48h_cancels_without_selling(tmp_path):
    e = sim_engine(tmp_path)
    detect(e)
    e.spike_tick(prices(126.0), now=T0 + 47 * H)                 # 아직 기다림
    assert e.surge.state['KRW-A']['phase'] == 'selling'
    e.spike_tick(prices(127.0), now=T0 + 49 * H)                 # 48시간 동안 8% 안 꺾임 → 팔지 않고 끝
    assert 'KRW-A' not in e.surge.state and qty(e) == 1000
    assert e.surge.history[0]['how'] == 'no_pullback'
    assert any('팔지 않았습니다' in d['message'] for d in e.decisions)


def test_forced_rebuy_after_max_wait(tmp_path):
    e = sim_engine(tmp_path)
    sell_on_pullback(e)
    sold = e.surge.state['KRW-A']['sold_price']
    e.spike_tick(prices(sold * 1.1), now=T0 + 2 * H + 71 * H)    # 72시간 전: 비싸면 기다림
    assert len(e.surge.state['KRW-A']['left']) == 3
    e.spike_tick(prices(sold * 1.1), now=T0 + 2 * H + 73 * H)    # 72시간 지남 → 비싸도 남은 몫 삼
    assert 'KRW-A' not in e.surge.state
    assert e.surge.history[0]['coin_change'] < 0                 # 비싸게 다시 사서 코인이 줄어듦


def test_cooldown_24h_after_finish(tmp_path):
    e = sim_engine(tmp_path)
    detect(e)
    e.spike_tick(prices(127.0), now=T0 + 49 * H)                 # 안 꺾여서 끝남 (T0+49h)
    assert detect(e, now=T0 + 50 * H) == []                      # 아직 +25%지만 24시간 동안은 다시 안 봄
    assert detect(e, now=T0 + 72 * H) == []
    assert detect(e, now=T0 + 74 * H)[0][0] == 'KRW-A'           # 24시간 지나면 다시


def test_state_survives_restart(tmp_path):
    e = sim_engine(tmp_path)
    sell_on_pullback(e)
    again = sr.SpikeRebuy(tmp_path / 'surge_state.json', cooldown_sec=sr.SURGE_COOLDOWN_SEC)
    assert again.state['KRW-A']['phase'] == 'waiting'


# ── 두 세트가 한 코인에서 겹치지 않음 ──
def spike_snap(minute=1, sym='KRW-A'):
    return {'ready': True, 'minute': minute,
            'coins': {sym: {'value': 1e9, 'avg60': 1e8, 'open': 100.0, 'close': 106.0, 'close_1h': 95.0}},
            'prices': {sym: 106.0}}


def test_surge_skips_coin_in_spike_machine(tmp_path):
    e = sim_engine(tmp_path, sell=[spike_rule(), surge_rule()])
    e.spike_tick(spike_snap(), now=T0)                           # 순간 급등 먼저
    assert 'KRW-A' in e.spike.state
    assert detect(e, now=T0 + 10) == [] and e.surge.state == {}


def test_spike_skips_coin_in_surge_machine(tmp_path):
    e = sim_engine(tmp_path, sell=[spike_rule(), surge_rule()])
    detect(e)                                                    # 하루 급등 먼저
    e.spike_tick(spike_snap(), now=T0 + 10)
    assert 'KRW-A' in e.surge.state and e.spike.state == {}


# ── tick: 일부 팔기 세트는 일반 매도 조건으로 다 팔지 않음 ──
def market(price, now):
    coin = {'currency': 'A', 'korean_name': 'A', 'current_price': price, 'change_rate': 25, 'prev_close': 100, 'today_low': 99,
            'trade_value_24h': 1e9}
    return {'coins': [coin], 'lows': {'KRW-A': {'day_closes': [100] * 30, 'day_highs': [100] * 30, 'day_values': [1e9] * 30}},
            'hourly': {'KRW-A': hourly(now, 100.0)}, 'flow_ready': True, 'custom': []}


def test_check_sells_ignores_partial_surge_rule_and_tick_detects(tmp_path, monkeypatch):
    import bot_core.risk_manager as rm
    monkeypatch.setattr(rm, 'MIN_ORDER_INTERVAL_SEC', 0)
    risk = {'take_profit_percent': 500, 'stop_loss_percent': 50}
    e = sim_engine(tmp_path, risk=risk)
    e.tick(market(125.0, time.time()))
    assert qty(e) == 1000                                        # 조건에 맞아도 전부 팔지 않음
    assert e.surge.state['KRW-A']['phase'] == 'selling'          # 대신 하루 급등 지켜보기 시작
    status = e.get_status()
    assert status['surge']['active'][0]['symbol'] == 'KRW-A' and status['surge']['settings']['enabled']

    whole = sim_engine(tmp_path / 'all', sell=[surge_rule('all')], risk=risk)
    whole.tick(market(125.0, time.time()))
    assert 'KRW-A' not in whole.simulation_engine.positions      # 전부 팔기면 일반 매도 조건처럼 다 팜
    assert whole.surge.state == {}


def test_position_closed_elsewhere_stops_surge(tmp_path):
    e = sim_engine(tmp_path)
    sell_on_pullback(e)
    e.simulation_engine.sell('KRW-A', 90.0, qty(e))              # 손절 등으로 봇이 나머지를 다 팜
    e.spike_tick(prices(80.0), now=T0 + 3 * H)
    assert 'KRW-A' not in e.surge.state and 'KRW-A' not in e._positions()
    assert e.surge.history[0]['how'] == 'position_closed'
