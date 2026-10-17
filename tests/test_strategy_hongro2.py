"""Hongro 전략2 (StraHub Hongro's Strategy2 by aviva95) 를 옮긴 조건 한 쌍"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import conditions as cd  # noqa: E402
from bot_core import strategy_hongro2 as h2  # noqa: E402


def bars(closes, spread=0.01):
    return {'day_closes_long': closes, 'day_highs_long': [c * (1 + spread) for c in closes],
            'day_lows_long': [c * (1 - spread) for c in closes], 'day_values_long': [1e6] * len(closes)}


def test_presets_registered_with_4h_default():
    for cid in ('hongro2_buy', 'hongro2_sell'):
        c = cd.resolve(cid, [])
        assert c and c['params'] == {'tf': '4h'}


def test_sell_needs_entry_price_and_short_take_profit():
    flat = [100.0] * 115 + [100, 100, 100, 100, 108.5]      # 마지막 봉이 앞 5봉 최저(99)의 +7% 넘음
    d = bars(flat)
    assert h2.sell_test({}, {}, d) is None                   # 산 값 없으면 계산 안 함
    hit = h2.sell_test({'bot_entry': 100.0}, {}, d)
    assert hit and hit['s_why'].startswith('C')


def test_buy_A_when_momentum_turns_up_through_zero():
    down = [100 - i * 0.5 for i in range(100)]
    up = [down[-1] + (i + 1) * 1.2 for i in range(20)]
    seen = []
    closes = down + up
    for k in range(60, len(closes) + 1):
        r = h2.buy_test({}, {}, bars(closes[:k]))
        if r:
            seen.append(r['s_kind'])
    assert 'A' in seen                                       # 내리다 오르면 어느 봉에서 A 가 켜짐


def test_momentum_matches_original_formula():
    import random
    random.seed(1)
    c = [100.0]
    for _ in range(80):
        c.append(c[-1] * (1 + random.uniform(-0.03, 0.03)))
    h = [x * 1.01 for x in c]
    l = [x * 0.99 for x in c]
    m = h2._momentum(h, l, c, 6)
    i = len(c) - 1                                           # 손으로: SMA6(src), src = c − ((HH6+LL6)/2 + SMA6(c))/2
    src = []
    for j in range(i - 5, i + 1):
        hh, ll, sma = max(h[j - 5:j + 1]), min(l[j - 5:j + 1]), sum(c[j - 5:j + 1]) / 6
        src.append(c[j] - ((hh + ll) / 2 + sma) / 2)
    assert abs(m[-1] - sum(src) / 6) < 1e-9


def test_held_days_sell_condition():
    import time
    t = cd.resolve('held_days', [])['test']
    now = time.time()
    assert t({'bot_opened_ts': now - 31 * 86400}, {'days': 30}, {})['s_held'] > 30
    assert t({'bot_opened_ts': now - 5 * 86400}, {'days': 30}, {}) is None
    assert t({}, {'days': 30}, {}) is None                       # 산 시각을 모르면 팔지 않음
