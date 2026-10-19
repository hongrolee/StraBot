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
    hit = sz.buy_test(coin(101.2), {}, d)                 # 구역 윗선 +0.5% 안 → 되돌아옴
    assert hit and hit['s_target'] == 110.0 and hit['s_zone_low'] == 99.5
    assert abs(hit['s_rr'] - (110 - 101.2) / (101.2 - 99.5 * 0.999)) < 1e-9
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
    assert sz.sell_test(coin(98.5, bot_entry=101.0), {}, d) is None     # 지금 가격만 아랫선 아래 (봉은 안 끝남) → 아직 안 팜
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
    assert sz.buy_test(coin(101.2), {'trend': 0}, d) is not None
    if sz.trend(sz._bars(d)) != 'up':
        assert sz.buy_test(coin(101.2), {'trend': 1}, d) is None
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
        assert c and c['params']['tf'] == '1h' and c['params']['trend'] == 0
    assert cd.PRESETS['zone_buy']['test'] is sz.buy_test and cd.PRESETS['zone_sell']['test'] is sz.sell_test
    assert 'zone_sell' in cd.TF_AWARE                                   # 팔기는 봉 길이로 '산 뒤 봉 수' 를 셈
    assert {'zone_buy', 'zone_sell'} <= signal_stats.SKIP               # 봇 규칙 전용 — 일봉 신호 통계에서 뺌
