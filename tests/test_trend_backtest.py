"""추세 따라가기 + 시장 날씨 필터 검증기 (가짜 데이터)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import trend_backtest as tb  # noqa: E402


def make(prices):
    """{market: [종가...]} → simulate에 넣을 P"""
    dates = [f'2026-01-{i + 1:02d}' for i in range(len(prices['KRW-BTC']))]
    series = {m: {d: (c, 1e12) for d, c in zip(dates, cs)} for m, cs in prices.items()}
    P = dict(zip(('close', 'val30', 'age'), tb.prepare(series, dates)))
    P['btc_ma'] = {r: tb.sma_list(P['close']['KRW-BTC'], r) for r in (2,)}
    return P


def test_picks_strongest_and_pays_cost(monkeypatch):
    monkeypatch.setattr(tb, 'MIN_AGE', 1)
    n = 8
    P = make({'KRW-BTC': [100.0] * n, 'KRW-A': [10, 11, 12, 13, 14, 15, 16, 17], 'KRW-B': [10, 9, 8, 7, 6, 5, 4, 3]})
    rets, inv, turns = tb.simulate(P, {'regime': 0, 'mom': 2, 'top': 1, 'hold': 10}, 2, 5)
    # 2일째에 A(가장 많이 오름)를 사며 비용, 그 뒤 A의 하루 상승률
    assert abs(rets[0] + tb.COST) < 1e-12
    assert abs(rets[1] - (13 / 12 - 1)) < 1e-12
    assert turns == 1


def test_rainy_market_stays_cash(monkeypatch):
    monkeypatch.setattr(tb, 'MIN_AGE', 1)
    P = make({'KRW-BTC': [100, 100, 90, 80, 70, 60], 'KRW-A': [10, 11, 12, 13, 14, 15]})
    rets, inv, turns = tb.simulate(P, {'regime': 2, 'mom': 1, 'top': 1, 'hold': 1}, 2, 5)
    assert rets == [0.0] * 4 and turns == 0 and inv == 0


def test_perf_drawdown():
    p = tb.perf([0.1, -0.5, 0.2])
    assert abs(p['total'] - (1.1 * 0.5 * 1.2 - 1) * 100) < 1e-9
    assert abs(p['mdd'] + 50) < 1e-9
