"""하루 매매 전략 한 쌍 (bot_core/strategy_tc.py, 2026-10-22) — 손으로 그린 봉으로 판정 확인
실행: venv/Scripts/python.exe -m pytest tests/test_strategy_tc.py -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import backtest as bt  # noqa: E402
from bot_core import conditions as cd  # noqa: E402
from bot_core import signal_stats  # noqa: E402
from bot_core import strategy_tc as tc  # noqa: E402

P = {'_tf': '5m', 'htf': '15m', 'k_htf': 1, 'k_ltf': 1, 'zpad': 0, 'max_wait': 6, 'stop_atr': 1.0, 'trend': 1}

# 위 봉(15분): 천천히 오르다 → 고점 105 → 저점 100 → 종가 106 으로 그 고점을 뚫음 → 구역 = 그 고점 봉의 몸통 위 104 ~ 꼬리 105, 그 뒤 최고가 109
H_PRE = [(90 + i * 0.5 + 0.3, 90 + i * 0.5 - 0.3, 90 + i * 0.5) for i in range(12)]     # (고, 저, 종) 흔들림 점 없음
H_MAIN = [(101, 99, 100), (103, 100, 102), (105, 102, 104), (104, 101, 103), (102, 100, 101), (104, 101, 103),
          (107, 103, 106), (109, 105, 108), (108, 105, 107)]
# 거래 봉(5분): 구역 위에서 머물다 → 첫 바닥 104.3 → 반등 → 첫 바닥 몸통(104.6) 아래 104.5 까지 다시 → 양봉 105.3 (마지막 봉)
L_PRE = [(107.5, 106.5, 107.0)] * 15
L_MAIN = [(106.6, 105.2, 105.5), (105.6, 104.3, 104.6), (105.6, 104.5, 105.4), (106.0, 105.2, 105.8), (105.9, 104.5, 104.8), (105.5, 104.6, 105.3)]


def bars(rows):
    return {'day_highs_long': [r[0] for r in rows], 'day_lows_long': [r[1] for r in rows], 'day_closes_long': [r[2] for r in rows]}


def data(extra_l=()):
    return {**bars(L_PRE + L_MAIN + list(extra_l)), 'htf': bars(H_PRE + H_MAIN)}


def coin(price, **kw):
    return {'currency': 'X', 'korean_name': 'x', 'current_price': price, 'today_low': price, 'trade_value_24h': 1e9, **kw}


def test_htf_zone_and_target():
    hs = tc.htf_state(tc._arrays(bars(H_PRE + H_MAIN)), 1)
    assert hs['up'] and hs['zone'] == (104.0, 105.0) and hs['tgt'] == 109.0


def test_buy_on_double_bottom_green_candle():
    hit = tc.buy_test(coin(105.4), P, data())
    assert hit and hit['s_target'] == 109.0 and hit['s_zone_low'] == 104.0 and hit['s_stop'] < 104.3
    assert tc.buy_test(coin(105.4), P, data([(105.6, 105.1, 105.2)])) is None    # 양봉 뒤 봉이 더 끝나면 신호 지남
    red_last = dict(data())
    red_last.update(bars(L_PRE + L_MAIN[:-1] + [(105.5, 104.6, 104.7)]))           # 마지막 봉이 음봉이면 아직
    assert tc.buy_test(coin(104.8), P, red_last) is None
    broke = dict(data())
    broke.update(bars(L_PRE + L_MAIN[:4] + [(105.9, 103.9, 104.0), (104.5, 103.8, 104.4)]))   # 첫 바닥 아래로 마감 → 쌍바닥 실패
    assert tc.buy_test(coin(104.5), P, broke) is None


def test_trend_and_zone_used_once():
    down = dict(data())
    down['htf'] = bars(H_PRE + H_MAIN + [(107, 99, 99.5)])                          # 위 봉이 확정 저점(100) 아래로 마감 → 하락
    assert tc.buy_test(coin(105.4), P, down) is None
    assert tc.buy_test(coin(105.4), dict(P, trend=0), down) is not None
    now = 1_780_000_000
    assert tc.buy_test(coin(105.4, bot_last_trade_ts=now - 600, bot_now_ts=now), P, data()) is None   # 구역이 생긴 뒤 이미 사고팖
    assert tc.buy_test(coin(105.4, bot_last_trade_ts=now - 86400, bot_now_ts=now), P, data()) is not None


def test_backtest_appends_current_bar():
    d = data()
    d.update(bars(L_PRE + L_MAIN[:-1]))                                              # 백테스트: 지금 봉은 끝난 봉 목록에 없음
    assert tc.buy_test(coin(105.3, bar_open=104.8, bar_high=105.5, bar_low=104.6), P, d) is not None


def test_sell_rebuilds_plan():
    t0 = 1_780_000_000
    hit = tc.buy_test(coin(105.4), P, data())
    stop, tgt = hit['s_stop'], hit['s_target']
    later = data([(105.8, 105.2, 105.6), (106.0, 105.4, 105.9)])                     # 산 뒤 끝난 봉 둘
    c = dict(bot_entry=105.4, bot_opened_ts=t0, bot_now_ts=t0 + 600)
    assert tc.sell_test(coin(106.0, **c), P, later) is None                          # 사이
    s = tc.sell_test(coin(stop - 0.01, **c), P, later)
    assert s and s['s_why'].startswith('쌍바닥') and abs(s['s_stop'] - stop) < 1e-9
    t = tc.sell_test(coin(tgt + 0.1, **c), P, later)
    assert t and t['s_why'].startswith('직전 고점') and t['s_target'] == tgt
    # 백테스트: 봉 안 저가가 손절에 닿으면 손절 값에
    f = tc.sell_test(coin(105.0, bar_open=105.5, bar_high=105.6, bar_low=stop - 0.5, **c), P, later)
    assert f and abs(f['s_fill'] - stop) < 1e-9


def test_registered_and_backtest_loads_upper_tf():
    for cid in ('tc_buy', 'tc_sell'):
        c = cd.resolve(cid, [])
        assert c and c['params']['tf'] == '5m' and c['params']['htf'] == '15m'
    assert {'tc_buy', 'tc_sell'} <= cd.TF_AWARE and {'tc_buy', 'tc_sell'} <= signal_stats.SKIP
    p = bt.plan({'trade_rules': {'buy': [{'id': 'tc_buy', 'params': {'tf': '5m', 'htf': '15m'}}], 'sell': []}}, [])
    assert {'5m', '15m'} <= p['tfs']


def test_engine_attaches_upper_bars(tmp_path):
    from bot_core.bot_engine import BotEngine
    rules = {'buy': [{'id': 'tc_buy', 'params': {'tf': '5m', 'htf': '15m'}}], 'sell': [], 'exclude': []}
    e = BotEngine({'mode': 'simulation', 'trade_rules': rules}, data_dir=tmp_path / 's')
    d = {'coins': [{'currency': 'X'}], 'lows_tf': {'5m': {'KRW-X': {'day_highs': [1]}}, '15m': {'KRW-X': {'day_highs': [2]}}}}
    got = e._lows_for(rules['buy'][0], d, 't')
    assert got['KRW-X']['htf'] == {'day_highs': [2]} and got['KRW-X']['day_highs'] == [1]
    assert e._lows_for(rules['buy'][0], {'coins': d['coins'], 'lows_tf': {'5m': d['lows_tf']['5m']}}, 't') is None   # 위 봉 아직 없음
