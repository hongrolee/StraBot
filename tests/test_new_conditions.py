"""2026-10 추가 조건: 손으로 셀 수 있는 값으로 도우미(EMA · MACD · 스토캐스틱 · ATR)와 판정 확인"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import conditions as cd  # noqa: E402


def test_ema_series_starts_with_simple_average():
    e = cd.ema_series([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None]
    assert e[2] == 2                              # 처음 3개 평균
    assert e[3] == 4 * 0.5 + 2 * 0.5              # k = 2/(3+1)
    assert e[4] == 5 * 0.5 + e[3] * 0.5


def test_macd_flat_is_zero():
    macd, signal = cd.macd_series([100.0] * 60)
    assert macd[-1] == 0 and signal[-1] == 0
    assert macd[24] is None and macd[25] == 0     # 26번째 봉부터
    assert signal[25 + 7] is None and signal[25 + 8] == 0


def test_atr_constant_range():
    closes = [100.0] * 20
    assert cd.atr_of([101.0] * 20, [99.0] * 20, closes) == 2


def test_stoch_top_and_bottom():
    up = [float(i) for i in range(1, 31)]
    k, d = cd.stoch_series(up, up, up)
    assert k[-1] == 100 and d[-1] == 100          # 계속 오르면 늘 기간 최고가에서 끝남
    down = list(reversed(up))
    k, d = cd.stoch_series(down, down, down)
    assert k[-1] == 0


def coin(price, **kw):
    return {'currency': 'X', 'korean_name': 'x', 'current_price': price, 'today_low': price, 'trade_value_24h': 1e9, **kw}


def test_big_candle_uses_atr():
    d = {'day_closes_long': [100.0] * 20, 'day_highs_long': [101.0] * 20, 'day_lows_long': [99.0] * 20}
    hit = cd.PRESETS['big_candle']['test'](coin(106.0, today_low=100.0), {'mult': 2}, d)
    assert hit and abs(hit['s_atr_mult'] - 3) < 1e-9   # 오늘 폭 6 / 평소 2
    assert cd.PRESETS['big_candle']['test'](coin(102.0, today_low=100.0), {'mult': 2}, d) is None


def test_ma_aligned_and_pullback():
    rising = [100 + i for i in range(120)]
    d = {'day_closes_long': rising}
    p = {'short': 5, 'mid': 20, 'long': 60}
    assert cd.PRESETS['ma_aligned']['test'](coin(230.0), p, d)
    mid = sum((rising + [rising[-1]])[-20:]) / 20       # 대략 20일선 근처 가격 → 눌림
    assert cd.PRESETS['aligned_pullback']['test'](coin(mid), {**p, 'near': 3}, d)
    assert cd.PRESETS['ma_aligned']['test'](coin(150.0), p, {'day_closes_long': list(reversed(rising))}) is None


def test_stronger_than_btc_and_rank():
    d = {'day_closes': [100.0] * 8 + [120.0] * 2, 'btc_closes': [100.0] * 11}
    hit = cd.PRESETS['stronger_than_btc']['test'](coin(120.0), {'days': 7, 'gap': 5}, d)
    assert hit and abs(hit['s_vs_btc'] - 20) < 1e-9
    assert cd.PRESETS['value_top']['test'](coin(1.0, value_rank=3), {'top': 20}, {})
    assert cd.PRESETS['value_top']['test'](coin(1.0, value_rank=30), {'top': 20}, {}) is None
    assert cd.PRESETS['new_listing']['test'](coin(1.0, listed_days=10), {'days': 30}, {})


def test_close_vs_ma_uses_finished_bars_only():
    """종가가 평균선 위 · 아래: 끝난 일봉(day_closes_long)만 봄 — 지금 가격이 출렁여도 하루 동안 바뀌지 않음"""
    up = [100.0] * 99 + [130.0]                       # 100일 평균 100.3, 마지막 종가 130 → 위
    above, below = cd.PRESETS['above_ma_close'], cd.PRESETS['below_ma_close']
    assert above['start_buy'] and not below.get('start_buy')
    c = {'currency': 'BTC', 'current_price': 50.0}    # 지금 가격은 평균보다 한참 아래여도
    p = {'days': 100}
    assert above['test'](c, p, {'day_closes_long': up})['s_gap_ma'] > 29
    assert below['test'](c, p, {'day_closes_long': up}) is None
    down = [100.0] * 99 + [90.0]
    assert above['test'](c, p, {'day_closes_long': down}) is None and below['test'](c, p, {'day_closes_long': down})
    assert above['test'](c, p, {'day_closes_long': up[:50]}) is None    # 봉이 모자라면 판단하지 않음


def test_close_vs_ma_percent_band():
    """평균선과 % 넘게: 팔기를 조금 늦추기 (아래 2% 넘게 내려가야 맞음)"""
    below = cd.PRESETS['below_ma_close']
    c = {'currency': 'BTC', 'current_price': 1.0}
    near = [100.0] * 49 + [99.0]                          # 평균 99.98, 종가 −1%
    far = [100.0] * 49 + [97.0]                           # 종가 −3%
    assert below['test'](c, {'days': 50}, {'day_closes_long': near})            # 0% → 맞음
    assert below['test'](c, {'days': 50, 'pct': 2}, {'day_closes_long': near}) is None
    assert below['test'](c, {'days': 50, 'pct': 2}, {'day_closes_long': far})


def test_close_vs_ema():
    """종가가 지수이동평균 위 · 아래: 끝난 봉 기준 · 최근에 오른 값에 무게"""
    above, below = cd.PRESETS['above_ema_close'], cd.PRESETS['below_ema_close']
    c = {'currency': 'BTC', 'current_price': 1.0}
    rising = [100.0] * 60 + [100.0 + i for i in range(1, 11)]       # 끝에 10봉 오름 → EMA 위
    falling = [100.0] * 60 + [100.0 - i for i in range(1, 11)]
    p = {'days': 50}
    assert above['test'](c, p, {'day_closes_long': rising})['s_gap_ma'] > 0 and below['test'](c, p, {'day_closes_long': rising}) is None
    assert below['test'](c, p, {'day_closes_long': falling}) and above['test'](c, p, {'day_closes_long': falling}) is None
    assert above['test'](c, p, {'day_closes_long': rising[:55]}) is None                           # 봉이 모자라면 판단 안 함
    assert below['test'](c, {'days': 50, 'pct': 20}, {'day_closes_long': falling}) is None         # 20% 넘게 아래는 아님


def test_ema120_uses_extra_long_window():
    """EMA 120: 긴 칸(120봉)만으로는 모자라 계산 안 함 → 아주 긴 칸(199봉)이 있으면 계산"""
    above = cd.PRESETS['above_ema_close']
    c = {'currency': 'BTC', 'current_price': 1.0}
    closes = [100.0] * 180 + [100.0 + i for i in range(1, 20)]
    p = {'days': 120}
    assert above['test'](c, p, {'day_closes_long': closes[-120:]}) is None
    assert above['test'](c, p, {'day_closes_long': closes[-120:], 'day_closes_xl': closes})['s_gap_ma'] > 0
