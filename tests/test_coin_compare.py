"""보유코인 분석 '다른 코인 비교' · '고른 코인에 봇을 적용했다면' 계산"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import backtest as bt  # noqa: E402
from gui import coin_compare as cc  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backtest import H, T0, bars, bot  # noqa: E402


def test_value_at_takes_bar_ended_by_then_and_fills_gaps():
    rows = [[T0, 1, 1, 1, 10, 1], [T0 + 60, 1, 1, 1, 11, 1], [T0 + 180, 1, 1, 1, 13, 1]]   # T0+120 봉은 거래 없음
    assert cc.value_at(rows, 60, T0 + 60) == 10          # T0+60 에 끝난 봉 (1분 뒤 봉은 아님)
    assert cc.value_at(rows, 60, T0 + 180) == 11         # 빈 봉 → 바로 앞 값
    assert cc.value_at(rows, 60, T0) is None             # 그때 끝난 봉 없음
    assert cc.value_at(rows, 60, T0 + 60 + 7200) is None  # 너무 오래 빔


def test_scan_stats_return_and_drawdown():
    rows = [[T0 + i * H, 0, 0, 0, p, 1] for i, p in enumerate([100, 100, 120, 90, 110])]
    st = cc.scan_stats(rows, H, T0 + H, 99.0)            # 출발 = T0+H 에 끝난 봉 종가 100
    assert abs(st['ret'] - (-1.0)) < 1e-9
    assert abs(st['mdd'] - 25.0) < 1e-9                  # 120 → 90
    late = cc.scan_stats([[T0 + 5 * H, 50, 0, 0, 60, 1]], H, T0, 75.0)   # 구간 중 상장: 첫 봉 시가에서
    assert late['late'] and abs(late['ret'] - 50.0) < 1e-9


def test_combine_curves_same_money_and_fill_forward():
    a = {'start_krw': 100, 'curve': [{'t': 1, 'bot': 110, 'hold': 100}, {'t': 2, 'bot': 120, 'hold': 90}]}
    b = {'start_krw': 100, 'curve': [{'t': 2, 'bot': 100, 'hold': 110}]}
    out = cc.combine_curves([a, b])
    near = lambda p, t, b_, h: p['t'] == t and abs(p['bot'] - b_) < 1e-9 and abs(p['hold'] - h) < 1e-9   # noqa: E731
    assert near(out[0], 1, 5.0, 0.0)                      # b 는 아직 시작 금액
    assert near(out[1], 2, 10.0, 0.0)


def test_backtest_start_held_buys_first_open_and_bot_sells():
    prices = [100 + i for i in range(30)]                 # 오르기만 → 익절 5%
    rows = bars(prices)
    b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': '1h'}}], take_profit_percent=5)
    r = bt.run(b, 'KRW-X', '60', rows[0][0], rows[-1][0] + H, {'60': rows, 'day': [], 'week': []}, krw=1_000_000, start_held=True)
    first = r['trades'][0]
    assert first['side'] == 'buy' and first['reason'] == '구간 처음에 산 것'
    assert abs(first['total'] * (1 + bt.FEE) - 1_000_000) < 1e-6         # 시작 금액 전부
    assert any(t['side'] == 'sell' and t['reason'].startswith('익절') for t in r['trades'])
    # 봇 미적용 = 같은 때 같은 금액을 끝까지 들고 있기
    assert abs(r['curve'][-1]['hold'] - 1_000_000 / (1 + bt.FEE) / (rows[0][1] * (1 + bt.SLIP)) * prices[-1]) < 2


def test_start_ok_and_scan_unit():
    now = 1_800_000_000
    assert cc.start_ok(now - 200 * 3600, now)
    assert not cc.start_ok(1, now) and not cc.start_ok(now + 10, now)       # 1970년 같은 잘못된 값 · 앞날
    assert cc.scan_unit('1m', now - 200 * 60, now) == '1'                    # 보통: 그래프 간격의 봉
    assert cc.scan_unit('1m', now - 2 * 86400, now) == '10'                  # 꺼 둔 틈으로 구간이 길면 더 큰 봉
    assert cc.scan_unit('1d', now - 200 * 86400, now) == 'day'
