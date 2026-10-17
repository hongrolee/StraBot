"""순간 급등 올라타기 검증기: 사는 시점·파는 이유·수수료 계산 (가짜 1분봉, 요청 없음)"""
import pytest

from tools.spike_backtest import AVG_WINDOW, fill_gaps, simulate, summarize


def flat(n, price=100.0, value=2e7):
    return [{'o': price, 'h': price, 'l': price, 'c': price, 'v': value} for _ in range(n)]


def spike_then(after):
    """평소 60분(거래대금 2천만) → 급등 1분(거래대금 10배, +3%) → after 봉들"""
    bars = flat(AVG_WINDOW)
    bars.append({'o': 100.0, 'h': 103.0, 'l': 100.0, 'c': 103.0, 'v': 2e8})
    return bars + after


def bar(o, h, l, c, v=5e7):
    return {'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


def test_buys_next_open_and_trails_out():
    bars = spike_then([bar(103, 106, 103, 106), bar(106, 110, 106, 110), bar(110, 110, 104, 105)])
    t, = simulate(bars, mult=5, rise=2, trail=3, hold=60, slip=0, fee=0)
    assert t['i'] == AVG_WINDOW and t['entry'] == 103
    assert t['why'] == '트레일링'
    assert t['exit'] == pytest.approx(110 * 0.97)      # 최고가 110에서 3% 내려온 값


def test_no_signal_when_rise_or_volume_small():
    bars = spike_then([bar(103, 104, 102, 103)] * 5)
    assert simulate(bars, mult=20, rise=2, trail=3, hold=60) == []   # 10배라 20배 기준 미달
    assert simulate(bars, mult=5, rise=5, trail=3, hold=60) == []    # +3%라 5% 기준 미달


def test_stop_loss_and_worse_side_first():
    # 한 봉에서 목표와 손절이 둘 다 닿으면 불리한 손절로 봄
    bars = spike_then([bar(100, 120, 95, 100)])
    t, = simulate(bars, mult=5, rise=2, trail=10, hold=60, stop=3, target=15, slip=0, fee=0)
    assert t['why'] == '손절' and t['exit'] == pytest.approx(97)


def test_time_exit_and_fees():
    bars = spike_then([bar(100, 100.5, 99.9, 100.2)] * 40)
    t, = simulate(bars, mult=5, rise=2, trail=5, hold=30, fee=0.0005, slip=0.002)
    assert t['why'] == '시간' and t['mins'] == 30
    entry, sell = 100 * 1.002, 100.2 * 0.998
    assert t['pct'] == pytest.approx((sell * 0.9995) / (entry * 1.0005) * 100 - 100)


def test_summary_and_gap_fill():
    s = summarize([{'pct': 2.0, 'mins': 10}, {'pct': -1.0, 'mins': 20}])
    assert s['trades'] == 2 and s['win'] == 50 and s['avg'] == pytest.approx(0.5) and s['worst'] == -1.0
    bars = fill_gaps([{'t': '2026-09-01T00:00:00', 'o': 1, 'h': 1, 'l': 1, 'c': 1, 'v': 5},
                      {'t': '2026-09-01T00:03:00', 'o': 2, 'h': 2, 'l': 2, 'c': 2, 'v': 5}])
    assert [b['v'] for b in bars] == [5, 0.0, 0.0, 5]


# ── 가진 코인이 치솟을 때 일부 팔기 ──
from tools.spike_backtest import spike_sells, summarize_sells  # noqa: E402


def test_sell_now_compares_with_later_price():
    # 급등 뒤 가격이 90으로 내려감 → 103에 판 것이 나음
    bars = spike_then([bar(103, 103, 103, 103)] + [bar(90, 90, 90, 90)] * 1500)
    e, = spike_sells(bars, mult=5, rise=2, pullback=0, fee=0, slip=0)
    assert e['sell'] == 103 and e['sell_i'] == AVG_WINDOW + 1
    assert e['vs'][60] == pytest.approx((103 / 90 - 1) * 100)


def test_sell_on_pullback_and_cooldown():
    # 110까지 더 오른 뒤 3% 꺾이면 팜, 60분 안의 두 번째 급등은 같은 급등으로 봄
    after = [bar(103, 110, 103, 110), bar(110, 110, 105, 106)] + [bar(106, 106, 106, 106)] * 1500
    after[10] = {'o': 106, 'h': 110, 'l': 106, 'c': 110, 'v': 5e8}
    bars = spike_then(after)
    ev = spike_sells(bars, mult=5, rise=2, pullback=3, fee=0, slip=0)
    assert len(ev) == 1 and ev[0]['sell'] == pytest.approx(110 * 0.97)
    s = summarize_sells(ev, 60)
    assert s['n'] == 1 and s['better'] == 100


# ── 팔고 다시 사기 ──
from tools.spike_backtest import rebuy, round_trips  # noqa: E402


def test_rebuy_rules():
    bars = flat(10, 100) + [bar(100, 100, 94, 95)] + [bar(95, 99, 95, 98)] + flat(1500, 98)
    # 판 가격 100, 수수료·미끄러짐 0
    j, gain, waited, timeout = rebuy(bars, 5, 100, 'dip', 5, fee=0, slip=0)
    assert j == 10 and gain == pytest.approx(100 / 95 * 100 - 100) and not timeout
    j, gain, _, _ = rebuy(bars, 5, 100, 'bounce', 3, fee=0, slip=0)
    assert j == 11 and gain == pytest.approx(100 / (94 * 1.03) * 100 - 100)   # 최저 94에서 3% 반등
    j, gain, waited, _ = rebuy(bars, 5, 100, 'time', 60, fee=0, slip=0)
    assert waited == 60 and gain == pytest.approx(100 / 98 * 100 - 100)
    # 24시간 안에 -8% 안 오면 그때 가격에 삼
    _, _, waited, timeout = rebuy(bars, 5, 100, 'dip', 8, fee=0, slip=0)
    assert timeout and waited == 1440


def test_round_trips_skip_sells_while_waiting():
    bars = flat(3000, 100)
    trips = round_trips(bars, [(100, 100), (150, 100), (400, 100)], 'time', 120)
    assert [t['sell_i'] for t in trips] == [100, 400]     # 150은 다시 사기 전이라 건너뜀


# ── 1분봉 받기: 예전에 받은 기간은 다시 안 받음 (가짜 응답, 실제 요청 없음) ──
import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import tools.spike_backtest as sb  # noqa: E402


def test_minute_bars_reuses_cached_span(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    fmt = lambda d: d.strftime('%Y-%m-%dT%H:%M:%S')
    # 예전에 받은 1일치: 2일 전 ~ 1일 전
    old = [{'t': fmt(now - timedelta(days=2) + timedelta(minutes=k)), 'o': 1, 'h': 1, 'l': 1, 'c': 1, 'v': 1} for k in range(1440)]
    (tmp_path / 'KRW-X_1d_20000101.json').write_text(json.dumps(old))
    asked = []

    def fake_get(pacer, url, params):
        to = datetime.strptime(params['to'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        asked.append(to)
        return [{'candle_date_time_utc': fmt(to - timedelta(minutes=k + 1)), 'opening_price': 2, 'high_price': 2,
                 'low_price': 2, 'trade_price': 2, 'candle_acc_trade_price': 2} for k in range(200)]

    monkeypatch.setattr(sb, 'get', fake_get)
    bars = sb.minute_bars(None, 'KRW-X', 3, tmp_path)
    # 받은 요청: 최근 1일(1440분 → 8번) + 과거 1일(8번), 예전 기간(가운데 1일)은 안 받음
    assert len(asked) == 16
    assert all(not (now - timedelta(days=2) < t <= now - timedelta(days=1)) for t in asked)
    assert len(bars) == 3 * 1440
    # 가운데 1일은 예전 자료 그대로 (경계의 한 번 요청분만 새 자료로 덮음: 예전 마지막 봉은 진행 중이었을 수 있음)
    assert 1440 - 200 <= sum(b['v'] == 1 for b in bars) <= 1440


def test_compact_bars_same_results():
    """압축한 봉으로 계산해도 결과가 같음"""
    from tools.spike_backtest import CompactBars
    bars = spike_then([bar(103, 106, 103, 106), bar(106, 110, 106, 110), bar(110, 110, 104, 105)] + flat(1500, 105))
    for k, b in enumerate(bars):
        b['t'] = (datetime(2026, 9, 1) + timedelta(minutes=k)).strftime('%Y-%m-%dT%H:%M:%S')
    cb = CompactBars(bars)
    assert len(cb) == len(bars) and cb[5]['t'] == bars[5]['t'] and cb[61]['v'] == bars[61]['v']
    assert simulate(cb, mult=5, rise=2, trail=3, hold=60) == simulate(bars, mult=5, rise=2, trail=3, hold=60)
    assert spike_sells(cb, mult=5, rise=2, pullback=3) == spike_sells(bars, mult=5, rise=2, pullback=3)


# ── 꼬리 줄이기: 나눠 다시 사기 · 되사기 손절선 ──
from tools.spike_backtest import rebuy_plan, tail_summary  # noqa: E402


def test_rebuy_plan_single_matches_old_rebuy():
    bars = flat(10, 100) + [bar(100, 100, 94, 95)] + flat(1500, 98)
    old = rebuy(bars, 5, 100, 'dip', 5)
    new = rebuy_plan(bars, 5, 100, [5], None)
    assert new['j'] == old[0] and new['gain'] == pytest.approx(old[1]) and new['how'] == 'dip'


def test_rebuy_plan_cap_stops_runaway():
    # 판 뒤 계속 오름: 손절선 +8%에서 바로 다시 삼 (24시간 기다리지 않음)
    bars = flat(10, 100) + [bar(100, 104, 100, 104), bar(104, 109, 104, 109)] + flat(1500, 130)
    r = rebuy_plan(bars, 5, 100, [5], 8, fee=0, slip=0)
    assert r['how'] == 'cap' and r['j'] == 11 and r['gain'] == pytest.approx(100 / 108 * 100 - 100)
    no_cap = rebuy_plan(bars, 5, 100, [5], None, fee=0, slip=0)
    assert no_cap['how'] == 'timeout' and no_cap['gain'] == pytest.approx(100 / 130 * 100 - 100)


def test_rebuy_plan_ladder_mixed():
    # −3%만 닿고 그 뒤 +8% 손절선: 1/3은 97에, 2/3은 108에 삼
    bars = flat(10, 100) + [bar(100, 100, 96.5, 97), bar(97, 108.5, 97, 108)] + flat(1500, 108)
    r = rebuy_plan(bars, 5, 100, [3, 5, 8], 8, fee=0, slip=0)
    assert r['how'] == 'mixed'
    assert r['gain'] == pytest.approx(((100 / 3) / 97 + (200 / 3) / 108 - 1) * 100)


def test_tail_summary_weeks():
    trips = [{'gain': 5.0, 't': '2026-09-01T00:00:00'}, {'gain': -20.0, 't': '2026-09-02T00:00:00'},
             {'gain': 4.0, 't': '2026-09-10T00:00:00'}]
    s = tail_summary(trips, portion=0.3)
    assert s['worst'] == -20 and s['weeks'] == 2 and s['weeks_neg'] == 1 and s['worst_week'] == pytest.approx(-4.5)
