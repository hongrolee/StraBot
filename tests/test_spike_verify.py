"""'순간 급등 · 일부 팔고 다시 사기' 검증 계산 (bot_core/spike_verify.py) — 가짜 1분봉"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import spike_verify as sv  # noqa: E402


def bar(t, o, h, l, c, v=2e7):
    return {'t': f'2026-09-01T{t // 60:02d}:{t % 60:02d}:00', 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


def series(after):
    """60분 동안 100 → 110(1시간 +10% 이상 되도록 천천히 오름), 그다음 급등 1분(+6%, 거래 10배), 그 뒤 after"""
    bars = []
    for k in range(61):
        p = 100 + k * 0.18
        bars.append(bar(k, p, p, p, p))
    bars.append(bar(61, 110.8, 117.5, 110.8, 117.5, 2e8))
    t = 62
    for o, h, l, c in after:
        bars.append(bar(t, o, h, l, c))
        t += 1
    return bars


def test_spike_sell_and_ladder_rebuy_like_bot():
    after = [(117.5, 120, 117.5, 120), (120, 120, 116, 116)] + [(116, 116, 100, 101)] + [(101, 101, 101, 101)] * 30
    r = sv.simulate(series(after), fee=0, slip=0)
    e, = r['events']
    assert e['sell_price'] == pytest.approx(120 * 0.97)                     # 최고가 120에서 3% 꺾인 값
    assert [b['why'] for b in e['buys']] == ['dip', 'dip', 'dip']           # −3·−5·−8% 모두 닿음
    assert e['coin_change'] > 0 and r['rule_end'] > r['hold_end']           # 코인이 늘고, 들고 있을 때보다 가치도 큼


def test_timeout_rebuy_when_price_runs_away():
    after = [(117.5, 120, 117.5, 120), (120, 120, 116, 116)] + [(130, 130, 130, 130)] * (24 * 60 + 5)
    e, = sv.simulate(series(after), fee=0, slip=0)['events']
    assert e['how'] == 'timeout' and e['buys'][-1]['parts'] == 3 and e['coin_change'] < 0


def test_no_event_without_hour_rise():
    flat = [bar(k, 100, 100, 100, 100) for k in range(61)] + [bar(61, 100, 107, 100, 107, 2e8)] + [bar(62 + k, 107, 107, 107, 107) for k in range(10)]
    assert sv.simulate(flat)['events'] == []                                # 직전 1시간 +10%가 안 되면 급등 아님


def test_candles_around_groups_long_windows():
    bars = series([(117.5, 120, 117.5, 120), (120, 120, 116, 116)] + [(130, 130, 130, 130)] * 2000)
    e = sv.simulate(bars)['events'][0]
    ch = sv.candles_around(bars, e)
    assert ch['minutes'] > 1 and len(ch['candles']) <= 240
