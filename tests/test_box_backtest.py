"""박스권 매매 세트 검증기: 박스 판단 · 사고팔기 순서(손절 먼저) (가짜 데이터)"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import box_backtest as bx  # noqa: E402


def bars(rows):
    """[(o, h, l, c)] → 15분봉 dict"""
    return {'base': None, 'o': [r[0] for r in rows], 'h': [r[1] for r in rows], 'l': [r[2] for r in rows], 'c': [r[3] for r in rows]}


def wave_hours(n, lo=100.0, hi=106.0, period=8):
    """lo~hi 사이를 오르내리는 1시간봉 (h, l, c)"""
    H, L, C = [], [], []
    for j in range(n):
        up = (j // (period // 2)) % 2 == 0
        c = hi - 0.5 if up else lo + 0.5
        H.append(hi if up else lo + 1.5)
        L.append(hi - 1.5 if up else lo)
        C.append(c)
    return H, L, C


def test_box_found_in_range_and_not_in_trend():
    H, L, C = wave_hours(60)
    boxes = bx.box_series(H, L, C, 24, 3, 8)
    assert boxes[30] == (100.0, 106.0)
    # 한쪽으로 계속 오르면 박스가 아님
    Ht = [100 + j for j in range(60)]
    Lt = [99 + j for j in range(60)]
    Ct = [99.5 + j for j in range(60)]
    assert all(b is None for b in bx.box_series(Ht, Lt, Ct, 24, 3, 8)[24:])
    # 폭이 기준보다 넓으면 박스가 아님
    assert bx.box_series(H, L, C, 24, 10, 20)[30] is None


def test_buy_low_sell_high():
    # 박스 100~110, 사는 구간 20% = 102 이하, 파는 구간 = 108 이상, 손절 = 100 × 0.98 = 98
    rows = [(105, 105.5, 104.5, 105)] * 4 + [(104, 104, 101.5, 102), (102, 105, 102, 104.5), (105, 109, 104.8, 108.5), (108, 108, 107, 107)]
    tr = bx.run(bars(rows), [None, (100.0, 110.0)], 0.2, 2, 'touch', 48)
    assert len(tr) >= 1
    start, end, ret, why, fill, target, stop = tr[0]
    assert (start, why, fill, target) == (4, 'target', 102.0, 108.0)
    assert end == 6
    assert ret == bx.net_return(102.0, 108.0)


def test_stop_first_when_both_hit_in_one_bar():
    rows = [(105, 105.5, 104.5, 105)] * 4 + [(104, 104, 101.5, 102), (102, 109, 97, 100)]
    tr = bx.run(bars(rows), [None, (100.0, 110.0)], 0.2, 2, 'touch', 48)
    assert tr[0][3] == 'stop' and tr[0][1] == 5 and tr[0][2] == bx.net_return(102.0, 98.0)


def test_gap_below_stop_sells_at_open():
    rows = [(105, 105.5, 104.5, 105)] * 4 + [(104, 104, 101.5, 102), (95, 96, 94, 95)]
    tr = bx.run(bars(rows), [None, (100.0, 110.0)], 0.2, 2, 'touch', 48)
    assert tr[0][3] == 'stop' and tr[0][2] == bx.net_return(102.0, 95.0)


def test_time_limit_and_bounce_entry():
    # 튀어 오를 때 사기: 사는 구간에 닿은 뒤 종가가 오르면 다음 봉 시작 값에 삼 → 1시간(4봉) 지나면 정리
    rows = [(105, 105.5, 104.5, 105)] * 4 + [(103, 103, 101.5, 101.8), (101.8, 103, 101.6, 102.6)] + [(103, 104, 102.5, 103.5)] * 8
    tr = bx.run(bars(rows), [None, (100.0, 110.0), (100.0, 110.0), (100.0, 110.0)], 0.2, 2, 'bounce', 1)
    start, end, ret, why, fill, *_ = tr[0]
    assert (start, fill, why, end) == (6, 103, 'time', 10)


def test_load_bars_fills_gaps_and_hourly(tmp_path):
    rows = [{'t': '2026-07-01T00:00:00', 'o': 10, 'h': 11, 'l': 9, 'c': 10.5, 'v': 1},
            {'t': '2026-07-01T00:14:00', 'o': 10.5, 'h': 12, 'l': 10, 'c': 11, 'v': 1},
            {'t': '2026-07-01T00:50:00', 'o': 11, 'h': 11, 'l': 8, 'c': 9, 'v': 1},
            {'t': '2026-07-01T01:05:00', 'o': 9, 'h': 9, 'l': 9, 'c': 9, 'v': 1}]
    f = tmp_path / 'KRW-X_90d_1.json'
    f.write_text(json.dumps(rows), encoding='utf-8')
    b = bx.load_bars(f)
    assert len(b['c']) == 4                               # 끝난 시간(0시)까지만
    assert b['h'][0] == 12 and b['l'][0] == 9 and b['c'][0] == 11
    assert b['c'][1] == 11 and b['o'][2] == 11            # 거래 없던 15분은 직전 종가
    H, L, C = bx.hourly(b)
    assert (H, L, C) == ([12], [8], [9])


def test_stats_and_split():
    tr = [('2026-08-30T00:00', '2026-08-30T05:00', 0.02, 'target'), ('2026-09-02T00:00', '2026-09-02T03:00', -0.03, 'stop')]
    a, b = bx.split(tr)
    assert len(a) == 1 and len(b) == 1
    s = bx.stats(tr)
    assert s['n'] == 2 and s['win'] == 50 and abs(s['avg'] + 0.5) < 1e-9 and s['stop'] == 50


def test_rsi_hourly_uses_only_finished_hours_and_matches_bot_rsi():
    from bot_core.conditions import rsi_of
    import random
    r = random.Random(3)
    C = [100.0]
    for _ in range(60):
        C.append(C[-1] * (1 + r.uniform(-0.03, 0.03)))
    out = bx.rsi_hourly(C)
    assert all(v is None for v in out[:15])
    for j in range(15, len(C)):
        assert abs(out[j] - rsi_of(C[:j])) < 1e-9          # 시간 j에는 j 전까지 끝난 종가만


def test_load_15m_fills_gaps(tmp_path):
    rows = [{'t': '2026-07-01T00:00:00', 'o': 10, 'h': 11, 'l': 9, 'c': 10.5, 'v': 1},
            {'t': '2026-07-01T00:30:00', 'o': 10.5, 'h': 12, 'l': 10, 'c': 11, 'v': 1},
            {'t': '2026-07-01T01:00:00', 'o': 11, 'h': 11, 'l': 11, 'c': 11, 'v': 1}]
    f = tmp_path / 'KRW-X_15m_365d_1.json'
    f.write_text(json.dumps(rows), encoding='utf-8')
    b = bx.load_any(f)
    assert b['c'] == [10.5, 10.5, 11, 11] and b['o'][1] == 10.5


def test_trend_up_uses_finished_hours_only():
    C = [100.0] * (2 * 24) + [110.0] * 5 + [90.0] * 5
    up = bx.trend_up(C, days=2)
    assert up[:48] == [False] * 48                         # 48시간 전에는 자료 모자람
    assert up[48] is False                                 # 47까지 평균 100, 마지막 100 → 위가 아님
    assert up[49] is True                                  # 48(110)이 평균보다 위
    assert up[-1] is False                                 # 90으로 내려오면 아님
