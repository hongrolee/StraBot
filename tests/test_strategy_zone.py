"""수요 구역 되돌림 조건 한 쌍 (bot_core/strategy_zone.py, 2026-10-18) — 손으로 그린 봉으로 판정 확인
실행: venv/Scripts/python.exe -m pytest tests/test_strategy_zone.py -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import conditions as cd  # noqa: E402
from bot_core import signal_stats  # noqa: E402
from bot_core import strategy_zone as sz  # noqa: E402


def bars(rows):
    """rows: [(high, low, close), ...] 끝난 봉 (오래된 순)"""
    return {'day_highs_long': [r[0] for r in rows], 'day_lows_long': [r[1] for r in rows], 'day_closes_long': [r[2] for r in rows]}


def coin(price, **kw):
    return {'currency': 'X', 'korean_name': 'x', 'current_price': price, 'today_low': price, 'trade_value_24h': 1e9, **kw}


# 조용한 20봉(100 근처) → 치솟는 봉(100 → 105, 직전 고가 101 위에서 마감) → 그 뒤 최고가 110 까지 오르고 → 지금 되돌아옴
QUIET = [(101.0, 99.5, 100.0)] * 20          # 폭 1.5% ≤ 3%
IMPULSE = [(105.5, 100.0, 105.0)]            # +5%, 직전 고가(101) 위
AFTER = [(108.0, 104.0, 107.0), (110.0, 106.0, 109.0), (109.0, 103.0, 104.0)]


def test_zone_found_and_buy_on_retest():
    d = bars(QUIET + IMPULSE + AFTER)
    z = sz.zones(sz._bars(d), {})
    assert len(z) == 1 and z[0]['low'] == 99.5 and z[0]['high'] == 101.0 and z[0]['peak'] == 110.0 and z[0]['ago'] == 3
    hit = sz.buy_test(coin(101.2), {'band': 0.5}, d)      # 구역 윗선 +0.5% 안 → 되돌아옴
    assert hit and hit['s_target'] == 110.0 and hit['s_zone_low'] == 99.5
    assert abs(hit['s_rr'] - (110 - 101.2) / (101.2 - 99.5 * 0.999)) < 1e-9
    assert sz.buy_test(coin(101.2), {}, d) is None       # 윗선 여유 기본 0 (2026-10-22): 윗선보다 비싸면 안 삼
    assert sz.buy_test(coin(100.8), {}, d) is not None
    assert 's_fill' not in hit                            # 실제 봇(봉 안 시가 · 저가 없음)은 지금 가격 그대로
    assert sz.buy_test(coin(103.0), {}, d) is None       # 아직 구역 위
    assert sz.buy_test(coin(99.0), {}, d) is None        # 구역 아래로 뚫림


def test_rr_filter_and_dead_zone():
    d = bars(QUIET + IMPULSE + AFTER)
    assert sz.buy_test(coin(101.2), {'rr': 20}, d) is None            # 손익비 모자람
    dead = bars(QUIET + IMPULSE + AFTER + [(104.0, 98.0, 99.0)])      # 종가가 구역 아래 → 구역 죽음
    assert sz.zones(sz._bars(dead), {}) == [] and sz.buy_test(coin(100.5), {}, dead) is None
    noisy = bars([(103.0, 97.0, 100.0)] * 20 + IMPULSE + AFTER)       # 직전 봉들이 조용하지 않음(폭 6%)
    assert sz.zones(sz._bars(noisy), {}) == []


def test_sell_when_zone_breaks():
    d = bars(QUIET + IMPULSE + AFTER)
    assert sz.sell_test(coin(98.0), {}, d) is None                      # 산 값 없음
    assert sz.sell_test(coin(100.0, bot_entry=101.0), {}, d) is None    # 아직 구역 안
    hit = sz.sell_test(coin(98.5, bot_entry=101.0), {}, d)              # 영상처럼 구역 아래로 내려가는 순간 손절 (봉 마감 안 기다림)
    assert hit and hit['s_why'].startswith('구역') and hit['s_zone_low'] == 99.5
    assert sz.sell_test(coin(99.45, bot_entry=101.0), {}, d) is None    # 아랫선 −0.1%(99.40) 위면 아직
    broken = bars(QUIET + IMPULSE + AFTER + [(104.0, 97.0, 98.0)])      # 끝난 봉 종가 98 < 아랫선 99.5
    hit = sz.sell_test(coin(98.5, bot_entry=101.0), {}, broken)
    assert hit and hit['s_why'].startswith('구역') and hit['s_gain'] < 0
    assert sz.buy_test(coin(100.5), {}, broken) is None                 # 깨진 구역은 다시 사지 않음 (되풀이 방지)
    assert sz.sell_test(coin(98.5, bot_entry=120.0), {}, broken) is None  # 구역 밖에서 산 코인은 모름


def test_sell_at_target_peak():
    d = bars(QUIET + IMPULSE + AFTER)
    hit = sz.sell_test(coin(110.5, bot_entry=101.0), {}, d)             # 치솟은 뒤 최고가 110 이상 → 익절
    assert hit and hit['s_why'].startswith('목표') and hit['s_target'] == 110.0 and hit['s_gain'] > 0
    assert sz.sell_test(coin(109.0, bot_entry=101.0), {}, d) is None    # 아직 목표 아래
    # 산 뒤에 끝난 봉이 더 높은 고가(115)를 만들어도 목표는 '살 때의 최고가' 110 그대로 (산 지 2봉 = 7200초, 1시간봉)
    d2 = bars(QUIET + IMPULSE + AFTER + [(115.0, 108.0, 112.0), (113.0, 109.0, 110.5)])
    c = coin(110.6, bot_entry=101.0, bot_opened_ts=1000.0, bot_now_ts=1000.0 + 7200)
    hit2 = sz.sell_test(c, {'_tf': '1h'}, d2)
    assert hit2 and hit2['s_why'].startswith('목표') and hit2['s_target'] == 110.0
    c3 = coin(110.6, bot_entry=101.0)                                    # 산 시각을 모르면 끝난 봉 전부로 → 목표 115
    assert sz.sell_test(c3, {}, d2) is None


def stairs(cycles=5, up=True):
    """계단: 6봉 오르고 4봉 눌리기를 되풀이 (눌림 저점이 앞 계단보다 높음) → 상승 추세, 거울로 뒤집으면 하락 추세"""
    rows = []
    for c in range(cycles):
        b = 100 + 4 * c
        for i in range(6):
            rows.append((b + i + 1.5, b + i, b + i + 1))
        for lo in (b + 4, b + 3, b + 2.5, b + 2):
            rows.append((lo + 1.5, lo, lo + 0.5))
    if not up:
        rows = [(300 - l, 300 - h, 300 - c) for h, l, c in rows]
    return rows


def test_trend_filter_and_trend_break():
    up, down = stairs(), stairs(up=False)
    assert sz.trend(sz._bars(bars(up))) == 'up'
    assert sz.trend(sz._bars(bars(down))) == 'down'
    assert sz.trend(sz._bars(bars(QUIET[:12]))) is None                 # 너무 짧음
    def shift(rows, to):
        """계단의 첫 종가를 to 에 맞춰 평행 이동 (값이 이어지게)"""
        off = rows[0][2] - to
        return [(h - off, l - off, c - off) for h, l, c in rows]

    # 내려오는 계단 뒤에 되돌아온 자리: trend=1 이면 안 삼(하락 추세), trend=0 이면 삼
    d = bars(shift(down, 130.0)[:30] + [(101.0, 99.5, 100.0)] * 20 + IMPULSE + AFTER)
    assert sz.buy_test(coin(100.8), {'trend': 0}, d) is not None
    if sz.trend(sz._bars(d)) != 'up':
        assert sz.buy_test(coin(100.8), {'trend': 1}, d) is None
    # 들고 있는데 추세가 꺾이면(구역 뒤에 내려오는 계단) trend=1 에서만 판다
    dd = bars(QUIET + IMPULSE + AFTER + shift(down, 104.0))
    assert sz.trend(sz._bars(dd)) == 'down'
    hit = sz.sell_test(coin(103.0, bot_entry=101.0), {'trend': 1}, dd)
    assert hit and hit['s_why'] == '추세 꺾임'
    r = sz.sell_test(coin(103.0, bot_entry=101.0), {'trend': 0}, dd)       # trend=0: 추세로는 안 팔고, 계단이 구역 아래로 마감했으면 구역 깨짐만
    assert r is None or r['s_why'].startswith('구역')


def test_presets_registered_with_hourly_default():
    for cid in ('zone_buy', 'zone_sell'):
        c = cd.resolve(cid, [])
        assert c and c['params']['tf'] == '1h' and c['params']['band'] == 0
    assert cd.PRESETS['zone_buy']['params']['trend'] == 1                # 영상 1단계: 상승 추세에서만 (2026-10-22)
    assert cd.PRESETS['zone_sell']['params']['trend'] == 0               # 추세 꺾임 팔기는 영상에 없음
    assert 'zone_buy' in cd.TF_AWARE
    assert cd.PRESETS['zone_buy']['params']['entry'] == 0 and cd.PRESETS['zone_buy']['params']['depth'] == 0.2
    assert cd.PRESETS['zone_buy']['test'] is sz.buy_test and cd.PRESETS['zone_sell']['test'] is sz.sell_test
    assert 'zone_sell' in cd.TF_AWARE                                   # 팔기는 봉 길이로 '산 뒤 봉 수' 를 셈
    assert {'zone_buy', 'zone_sell'} <= signal_stats.SKIP               # 봇 규칙 전용 — 일봉 신호 통계에서 뺌


# ── 사는 때 (entry, 2026-10-22): 0 닿는 순간 · 1 윗선보다 depth% 더 내려오면 · 2 닿은 봉이 끝난 뒤 ──
TOUCH = [(104.0, 100.5, 102.0)]               # 방금 끝난 봉이 구역(99.5~101)에 닿고 구역 위(102)에서 마감


def test_entry_deeper_needs_extra_drop():
    d = bars(QUIET + IMPULSE + AFTER)
    p = {'entry': 1, 'depth': 0.2}                                     # 101 × 0.998 = 100.798 까지 내려와야
    assert sz.buy_test(coin(100.9), p, d) is None
    assert sz.buy_test(coin(100.7), p, d) is not None
    assert sz.buy_test(coin(100.7), {'entry': 1, 'depth': 2.0}, d) is None   # 구역(1.5%)보다 깊으면 못 삼


def test_entry_after_bar_close_live():
    p = {'entry': 2}
    assert sz.buy_test(coin(100.8), p, bars(QUIET + IMPULSE + AFTER)) is None   # 지금 가격은 구역 안이지만 끝난 봉은 안 닿음
    d = bars(QUIET + IMPULSE + AFTER + TOUCH)
    hit = sz.buy_test(coin(102.0), p, d)                                 # 닿은 봉이 끝난 뒤 그다음 가격(윗선 위)에 삼
    assert hit and abs(hit['s_rr'] - (110 - 102) / (102 - 99.5 * 0.999)) < 1e-9
    assert sz.buy_test(coin(102.0), {}, d) is None                      # '닿는 순간'이면 윗선 위라 안 삼
    broke = bars(QUIET + IMPULSE + AFTER + [(104.0, 98.0, 99.0)])       # 닿은 봉이 구역 아래로 마감 → 구역 죽음
    assert sz.buy_test(coin(100.0), p, broke) is None


def test_backtest_intrabar_fill_price():
    d = bars(QUIET + IMPULSE + AFTER)
    c = coin(102.0, bar_open=104.0, bar_low=100.6)                      # 백테스트: 지금 봉이 104 에서 열려 100.6 까지 내렸다 102 로 마감
    assert sz.buy_test(c, {}, d)['s_fill'] == 101.0                     # 닿는 순간 = 윗선 값에 산 것으로
    assert abs(sz.buy_test(c, {'entry': 1}, d)['s_fill'] - 101 * 0.998) < 1e-9
    assert sz.buy_test(c, {'entry': 2}, d)['s_fill'] == 102.0           # 봉이 끝난 뒤 = 그 봉 종가
    assert sz.buy_test(coin(102.0, bar_open=104.0, bar_low=100.9), {'entry': 1}, d) is None   # 100.798 까지는 안 내려옴
    assert sz.buy_test(coin(102.0, bar_open=104.0, bar_low=101.3), {}, d) is None             # 윗선에 안 닿음
    assert sz.buy_test(coin(100.2, bar_open=100.5, bar_low=100.1), {}, d)['s_fill'] == 100.5  # 이미 구역 안에서 열림 → 시가


def test_sell_finds_zone_when_bought_above_top():
    d = bars(QUIET + IMPULSE + AFTER + TOUCH)
    hit = sz.sell_test(coin(110.5, bot_entry=102.0), {}, d)            # '봉이 끝난 뒤' 윗선(101) 위 102 에 산 코인
    assert hit and hit['s_why'].startswith('목표') and hit['s_target'] == 110.0
    broken = bars(QUIET + IMPULSE + AFTER + TOUCH + [(103.0, 97.0, 98.0)])
    hit = sz.sell_test(coin(98.5, bot_entry=102.0), {}, broken)
    assert hit and hit['s_why'].startswith('구역') and hit['s_zone_low'] == 99.5


# ── 영상과 같게 (2026-10-22): 구역은 한 번만 · 같은 봉 안 되사기 막기 · 손절 / 익절은 닿는 순간 ──
def test_zone_used_once():
    d = bars(QUIET + IMPULSE + AFTER + [(104.0, 100.8, 103.0)])        # 끝난 봉이 이미 윗선(101)까지 왔었음 = 구역을 씀
    assert sz.buy_test(coin(100.9), {}, d) is None
    assert sz.buy_test(coin(100.9), {}, bars(QUIET + IMPULSE + AFTER)) is not None   # 처음 되돌아오면 삼
    deep = bars(QUIET + IMPULSE + AFTER + [(104.0, 100.9, 103.0)])     # '더 내려오면'(100.798)까지는 안 왔었음 → 아직 안 씀
    assert sz.buy_test(coin(100.7), {'entry': 1}, deep) is not None


def test_no_rebuy_in_same_bar():
    d = bars(QUIET + IMPULSE + AFTER)
    t = 1_780_000_000 // 3600 * 3600 + 600                               # 어떤 1시간봉의 10분
    p = {'_tf': '1h'}
    assert sz.buy_test(coin(100.8, bot_last_trade_ts=t, bot_now_ts=t + 1200), p, d) is None    # 같은 봉에서 이미 사고팖
    assert sz.buy_test(coin(100.8, bot_last_trade_ts=t, bot_now_ts=t + 3600), p, d) is not None  # 다음 봉이면 다시 봄
    assert sz.buy_test(coin(100.8, bot_last_trade_ts=t, bot_now_ts=t + 1200, bar_open=102.0, bar_low=100.7), p, d) is not None   # 백테스트는 봉마다 한 번이라 안 봄


def test_sell_inside_bar_for_backtest():
    d = bars(QUIET + IMPULSE + AFTER)
    stop = sz.sell_test(coin(104.0, bot_entry=101.0, bar_open=103.0, bar_low=99.0, bar_high=106.0), {}, d)
    assert stop['s_why'].startswith('구역') and abs(stop['s_fill'] - 99.5 * 0.999) < 1e-9     # 봉 안에서 아랫선 −0.1% 에 손절
    gap = sz.sell_test(coin(98.0, bot_entry=101.0, bar_open=98.5, bar_low=97.0, bar_high=99.0), {}, d)
    assert gap['s_fill'] == 98.5                                         # 이미 아래서 열리면 시가
    tp = sz.sell_test(coin(108.0, bot_entry=101.0, bar_open=103.0, bar_low=102.0, bar_high=111.0), {}, d)
    assert tp['s_why'].startswith('목표') and tp['s_fill'] == 110.0      # 봉 안에서 최고가(110) 에 익절
    both = sz.sell_test(coin(105.0, bot_entry=101.0, bar_open=103.0, bar_low=99.0, bar_high=111.0), {}, d)
    assert both['s_why'].startswith('구역')                              # 한 봉에 둘 다면 손절 먼저 (검증과 같은 불리한 순서)
    assert sz.sell_test(coin(104.0, bot_entry=101.0, bar_open=103.0, bar_low=100.0, bar_high=109.0), {}, d) is None


def test_engine_gives_last_trade_time(tmp_path):
    import json
    from datetime import datetime
    from bot_core.bot_engine import BotEngine
    rules = {'buy': [{'id': 'zone_buy', 'params': {'tf': '1h'}}], 'sell': [], 'exclude': []}
    e = BotEngine({'mode': 'simulation', 'trade_rules': rules}, data_dir=tmp_path / 's')
    e.data_dir.mkdir(parents=True, exist_ok=True)
    (e.data_dir / 'trades.json').write_text(json.dumps([
        {'time': '2026-10-22T10:05:00', 'symbol': 'KRW-X', 'side': 'buy'},
        {'time': '2026-10-22T10:20:00', 'symbol': 'KRW-X', 'side': 'sell'},
        {'time': '2026-10-21T09:00:00', 'symbol': 'KRW-Y', 'side': 'buy'}]), encoding='utf-8')
    out = {c['currency']: c.get('bot_last_trade_ts') for c in e._with_bot_state(rules, [{'currency': 'X'}, {'currency': 'Y'}, {'currency': 'Z'}])}
    assert out['X'] == datetime.fromisoformat('2026-10-22T10:20:00').timestamp()
    assert out['Y'] == datetime.fromisoformat('2026-10-21T09:00:00').timestamp() and out['Z'] is None
    assert 'bot_last_trade_ts' not in e._with_bot_state({'buy': [], 'sell': []}, [{'currency': 'X'}])[0]
