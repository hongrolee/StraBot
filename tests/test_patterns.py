"""2026-10 패턴 조건: 손으로 만든 봉으로 '맞는 모양 / 안 맞는 모양' 확인

봉은 (고가, 저가, 종가[, 거래대금]) — 시가는 직전 봉 종가로 봄 (conditions._bars)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import conditions as cd  # noqa: E402


def data(bars):
    return {'day_highs_long': [b[0] for b in bars], 'day_lows_long': [b[1] for b in bars],
            'day_closes_long': [b[2] for b in bars], 'day_values_long': [b[3] if len(b) > 3 else 1e9 for b in bars]}


def coin(price):
    return {'currency': 'X', 'korean_name': 'x', 'current_price': price, 'today_low': price, 'trade_value_24h': 1e9}


def run(pid, bars, price, **params):
    pr = cd.PRESETS[pid]
    return pr['test'](coin(price), {**pr['params'], **params}, data(bars))


def flat(n=25, p=100.0):
    """평소: 0.5씩 오르내림 (평균 몸통 0.5)"""
    return [(p + 1, p - 1, p + (0.5 if i % 2 else 0)) for i in range(n)]


def line(closes, wick=0.5):
    return [(c + wick, c - wick, c) for c in closes]


def falling():
    return flat() + line([98, 96, 94, 92, 90])


def rising():
    return flat() + line([102, 104, 106, 108, 110])


def test_hammer():
    assert run('hammer', falling() + [(90.7, 86, 90.5)], 91)
    assert run('hammer', falling() + [(90.7, 89.8, 90.5)], 91) is None       # 아래꼬리 짧음
    assert run('hammer', flat() + [(100.7, 96, 100.5)], 101) is None          # 앞서 안 내림


def test_shooting_star():
    assert run('shooting_star', rising() + [(114, 109.8, 109.5)], 109)
    assert run('shooting_star', rising() + [(110.3, 109.2, 109.5)], 109) is None


def test_engulf():
    up = falling() + line([93])                       # 90 → 93 양봉이 앞 음봉(92 → 90) 몸통을 덮음
    assert run('bull_engulf', up, 93)
    assert run('bull_engulf', falling() + line([91.5]), 92) is None
    down = rising() + line([107])                     # 110 → 107 음봉이 앞 양봉(108 → 110) 몸통을 덮음
    assert run('bear_engulf', down, 107)
    assert run('bear_engulf', rising() + line([108.5]), 108) is None


def test_stars():
    base = flat()                                     # 마지막 종가 100.5
    assert run('morning_star', base + line([96, 96.2, 98.5]), 99)
    assert run('morning_star', base + line([96, 96.2, 97.5]), 98) is None     # 절반 못 되돌림
    assert run('morning_star', base + line([96, 98, 98.5]), 99) is None       # 가운데 봉이 큼
    assert run('evening_star', base + line([105, 104.8, 102]), 102)
    assert run('evening_star', base + line([105, 104.8, 103.5]), 103) is None


def test_three_soldiers_and_crows():
    base = flat()
    assert run('three_white', base + [(101.6, 100, 101.5), (102.7, 101, 102.5), (103.7, 102, 103.5)], 104)
    assert run('three_white', base + [(101.6, 100, 101.5), (104, 101, 102.5), (103.7, 102, 103.5)], 104) is None  # 위꼬리 김
    assert run('three_black', base + [(100.5, 99.4, 99.5), (99.5, 98.4, 98.5), (98.5, 97.4, 97.5)], 97)
    assert run('three_black', base + [(100.5, 99.4, 99.5), (99.5, 98.4, 98.5), (98.5, 97.4, 98.6)], 97) is None   # 마지막이 양봉


def zigzag(points, step=1.0):
    """꼭짓점 사이를 step씩 직선으로 잇는 종가"""
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        k = max(1, round(abs(b - a) / step))
        out += [a + (b - a) * j / k for j in range(1, k + 1)]
    return out


def test_double_bottom_and_top():
    w = line(zigzag([120, 100, 110, 100.5, 108]), wick=1)        # 바닥 99 · 99.5, 목선(사이 고가) 111
    assert run('double_bottom', w, 112)
    assert run('double_bottom', w, 110) is None                   # 아직 목선 아래
    m = line(zigzag([80, 100, 90, 99.5, 92]), wick=1)            # 천정 101 · 100.5, 목선(사이 저가) 89
    assert run('double_top', m, 88)
    assert run('double_top', m, 90) is None


def test_triangle_break():
    t = line(zigzag([90, 110, 96, 106, 100, 103, 101.5], step=0.5), wick=0.5)
    assert run('triangle_break', t, 108, days=60)
    assert run('triangle_break', t, 102, days=60) is None


def test_bull_flag():
    pole = line([104, 108, 112, 116, 120])                        # 100 → 120.5 (고가)
    rest = line([118.5, 117.5, 118, 117])                          # 쉼 구간 고가 119
    assert run('bull_flag', flat() + pole + rest, 121)
    assert run('bull_flag', flat() + pole + rest, 118.5) is None   # 아직 안 넘음
    assert run('bull_flag', flat() + line([101, 102, 103, 104, 105, 104.5, 104, 104.5, 104]), 106) is None    # 급등 아님


def test_cup_handle():
    cup = line(zigzag([110, 120, 95, 119.5, 115, 116], step=1.5), wick=0.5)   # 왼쪽 120.5 · 바닥 94.5 · 오른쪽 120 · 손잡이 114.5
    assert run('cup_handle', cup, 122)
    assert run('cup_handle', cup, 119) is None
    deep = line(zigzag([110, 120, 100, 119.5, 101, 102], step=1.5), wick=0.5)  # 손잡이가 컵 절반 아래로
    assert run('cup_handle', deep, 122) is None


def test_rsi_divergence():
    # 크게 내려 첫 저점(RSI 아주 낮음) → 반등 → 짧게 조금 더 낮은 두 번째 저점(RSI 덜 낮음) → 반등
    up = line([*zigzag([100, 130], step=1), *zigzag([130, 100], step=3), *zigzag([100, 108], step=3), *zigzag([108, 98], step=3), 99, 100, 101], wick=0.3)
    hit = run('rsi_div_up', up, 101, days=60, level=50, within=5)
    assert hit and hit['s_rsi_gap'] > 0
    # 두 번째 저점까지 오래 꾸준히 내리면 RSI 도 더 낮음 → 다이버전스 아님
    steady = line([*zigzag([100, 130], step=1), *zigzag([130, 100], step=3), *zigzag([100, 108], step=1), *zigzag([108, 98], step=0.5), 99, 100, 101], wick=0.3)
    assert run('rsi_div_up', steady, 101, days=60, level=50, within=5) is None


def test_value_breakout():
    base = [(101, 99, 100, 1e9)] * 25
    assert run('value_breakout', base + [(106, 100, 105, 3e9)], 106)
    assert run('value_breakout', base + [(106, 100, 105, 1.5e9)], 106) is None   # 거래 평소의 1.5배
    assert run('value_breakout', base + [(106, 100, 105, 3e9)], 100.5) is None   # 지금은 다시 아래


def test_rsi_series_matches_rsi_of():
    closes = [100 + (i * 7 % 11) - i * 0.3 for i in range(60)]
    s = cd.rsi_series(closes)
    for i in (14, 20, 59):
        assert s[i] == cd.rsi_of(closes[:i + 1])
    assert s[13] is None


def test_too_little_data_is_none():
    for pid in cd.PATTERN_IDS:
        assert run(pid, flat(2), 100) is None


# ── 2차 패턴 (상승 · 하락 · 반전) ──
def zz(points, step=1.0, wick=0.5):
    return line(zigzag(points, step), wick=wick)


def mirror(bars, axis=200.0):
    """위아래 뒤집기 (상승 모양 → 하락 모양)"""
    return [(axis - lo, axis - hi, axis - c) for hi, lo, c, *_ in bars]


def test_asc_and_desc_triangle():
    t = zz([90, 110, 96, 110, 102, 109.5, 105, 108])
    assert run('asc_triangle', t, 112)
    assert run('asc_triangle', t, 109) is None
    assert run('desc_triangle', mirror(t), 200 - 112)
    assert run('desc_triangle', mirror(t), 200 - 109) is None


def test_triangle_down():
    t = zz([90, 110, 96, 106, 100, 103, 101.5], step=0.5)
    assert run('triangle_down', t, 96, days=60)
    assert run('triangle_down', t, 102, days=60) is None


def test_pennant_and_bear_flag():
    pole = line([104, 108, 112, 116, 120])
    pen = [(120, 116, 118), (119.5, 116.5, 117.5), (119, 117, 118), (118.5, 117.3, 117.8), (118.3, 117.5, 118)]
    assert run('pennant_up', flat() + pole + pen, 121)
    assert run('pennant_up', flat() + pole + pen, 118.2) is None
    widen = [(118.5, 117.5, 118), (119, 117.2, 118), (119.5, 116.5, 117.5), (120, 116, 118)]
    assert run('pennant_up', flat() + pole + widen, 121) is None      # 좁아지지 않음
    down = mirror(flat() + pole + pen)
    assert run('bear_pennant', down, 200 - 121)
    assert run('bear_flag', down, 200 - 121)
    assert run('bear_flag', down, 200 - 118.2) is None


def test_measured_move():
    m = zz([100, 125, 112, 120], step=1)            # A 99.5 → B 125.5 (+26%) → C 111.5 (54% 되돌림)
    assert run('measured_up', m, 127)
    assert run('measured_up', m, 124) is None
    assert run('measured_down', mirror(m), 200 - 127)


def test_scallop():
    s = flat(20, 100) + zz([100, 110, 96, 108], step=1)      # 왼쪽 꼭대기 110.5 → 바닥 95.5 (−13.6%)
    assert run('asc_scallop', s, 112)
    assert run('asc_scallop', s, 109) is None
    assert run('desc_scallop', mirror(s), 200 - 112)


def test_three_valleys_and_peaks():
    v = zz([110, 100, 108, 103, 110, 105, 109])        # 바닥 99.5 < 102.5 < 104.5, 사이 꼭대기 110.5
    assert run('three_rising_valleys', v, 112)
    assert run('three_rising_valleys', v, 110) is None
    assert run('three_desc_peaks', mirror(v), 200 - 112)


def test_inv_cup_handle():
    cup = zz([110, 100, 125, 100.5, 105, 104], step=1.5)
    assert run('inv_cup_handle', cup, 98)
    assert run('inv_cup_handle', cup, 101) is None


def test_head_and_shoulders():
    hs = zz([95, 110, 100, 120, 100.5, 110.5, 104])     # 어깨 110.5 · 111 · 머리 120.5, 목선 99.5~100
    assert run('hs_top', hs, 98, days=90)
    assert run('hs_top', hs, 101, days=90) is None
    assert run('hs_bottom', mirror(hs), 200 - 98, days=90)
    flat_head = zz([95, 110, 100, 111, 100.5, 110.5, 104])   # 머리가 어깨와 비슷
    assert run('hs_top', flat_head, 98, days=90) is None


def test_rectangle():
    up = line([80 + i for i in range(21)])                          # 80 → 100 (+25%)
    box = [(101, 95, 98 + (i % 2) * 2) for i in range(20)]          # 95~101 박스 (6%)
    tail = line([97, 98, 97])
    assert run('rect_top', flat(25, 80) + up + box + tail, 93)
    assert run('rect_top', flat(25, 80) + up + box + tail, 96) is None
    assert run('rect_bottom', mirror(flat(25, 80) + up + box + tail), 200 - 93)


def test_diamond():
    d = zz([90, 100, 96, 104, 92, 108, 88, 106, 94, 102, 97, 100, 98.5], step=1)
    assert run('diamond_top', zz([80, 90]) + d, 95.5, days=119)
    assert run('diamond_top', zz([80, 90]) + d, 99, days=119) is None
    assert run('diamond_bottom', mirror(zz([80, 90]) + d), 200 - 95.5, days=119)            # 뒤집으면 내려오다 생긴 다이아몬드
    assert run('diamond_bottom', zz([80, 90]) + d, 104, days=119) is None                    # 오르다 생긴 모양은 바닥 아님
