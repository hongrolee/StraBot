"""
조건 검색 계산 (파이썬판) — 봇이 브라우저 없이 조건대로 사고팔기 위해 사용

화면(static/js/bot_dashboard.js)의 조건 계산과 **똑같이** 동작해야 한다.
tests/test_conditions_parity.py가 JS 결과(tests/fixtures/condition_parity.json)와 비교한다.

입력
  coin c : 시세 한 줄 {currency, korean_name, current_price, change_rate, prev_close, today_low,
           trade_value_24h, warning, cautions, notices, held?, flow_*?}
  data d : 기간 시세 {day_closes, day_highs, day_values, week_closes, week_highs, d1, w1, m1, h6, y1}
           (gui/web_dashboard.py `_fetch_period_lows`와 같은 모양)
  hourly : {'t': [...], 'c': [...], 'l': [...]} (전일저점·24시간 전 가격 계산용, 없어도 됨)
"""
import math
import re
import time
from typing import Callable, Dict, List, Optional

STABLE_COINS = {'USDT', 'USDC', 'USDE', 'USD1', 'USDG', 'USDS', 'PYUSD', 'RLUSD', 'FDUSD', 'TUSD', 'DAI', 'EURC', 'JPYC'}
LOW_PERIODS = [('low_d1_rate', 'd1'), ('low_w1_rate', 'w1'), ('low_m1_rate', 'm1'), ('low_h6_rate', 'h6'), ('low_y1_rate', 'y1')]


def is_stable_coin(c: Dict) -> bool:
    cur = str(c.get('currency') or '')
    return cur in STABLE_COINS or cur.startswith('USD') or bool(re.search('유에스디|달러', c.get('korean_name') or ''))


def is_warning_coin(c: Dict) -> bool:
    return bool(c.get('warning'))


def is_delist_coin(c: Dict) -> bool:
    return any((n or {}).get('type') == 'delisting' for n in (c.get('notices') or []))


def blocked_kind(c: Dict, allow_stable: bool = False, allow_warning: bool = False, allow_delist: bool = False) -> str:
    """봇이 사지 않는 까닭 ('' = 살 수 있음): 스테이블코인 · 유의 종목 · 상장폐지 예정 (고르면 풀 수 있음)"""
    if is_stable_coin(c) and not allow_stable:
        return '스테이블코인'
    if is_warning_coin(c) and not allow_warning:
        return '유의 종목'
    if is_delist_coin(c) and not allow_delist:
        return '상장폐지 예정'
    return ''


def is_risky_coin(c: Dict) -> bool:
    """봇이 사지 않는 코인: 유의 종목, 상장폐지 예정 공지"""
    if c.get('warning'):
        return True
    return any((n or {}).get('type') == 'delisting' for n in (c.get('notices') or []))


# ── 계산 도우미 (JS와 같은 이름·같은 방식) ──
def typical_daily_move(closes: List[float]) -> Optional[float]:
    moves = sorted(abs(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes)))
    return moves[len(moves) // 2] if moves else None


def closes_now(c: Dict, d: Dict) -> Optional[List[float]]:
    dc = d.get('day_closes')
    return [*dc, c['current_price']] if dc else None


def sma_at(arr: List[float], n: int, end: int) -> Optional[float]:
    if end + 1 < n or end < 0:
        return None
    return sum(arr[end - n + 1:end + 1]) / n


def rsi_of(closes: Optional[List[float]], n: int = 14) -> Optional[float]:
    if not closes or len(closes) < n + 1:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        if ch > 0:
            gain += ch
        else:
            loss -= ch
    gain /= n
    loss /= n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain = (gain * (n - 1) + max(ch, 0)) / n
        loss = (loss * (n - 1) + max(-ch, 0)) / n
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return 100 - 100 / (1 + gain / loss)


def bollinger_of(closes: Optional[List[float]], n: int = 20, k: float = 2) -> Optional[Dict]:
    if not closes or len(closes) < n:
        return None
    w = closes[-n:]
    mid = sum(w) / n
    sd = math.sqrt(sum((y - mid) ** 2 for y in w) / n)
    return {'mid': mid, 'upper': mid + k * sd, 'lower': mid - k * sd}


def cross_within(closes: List[float], short: int, long: int, within: int, direction: str) -> Optional[Dict]:
    n = len(closes)
    for back in range(within):
        i = n - 1 - back
        s1, l1 = sma_at(closes, short, i), sma_at(closes, long, i)
        s0, l0 = sma_at(closes, short, i - 1), sma_at(closes, long, i - 1)
        if None in (s1, l1, s0, l0):
            return None
        if (s0 <= l0 and s1 > l1) if direction == 'up' else (s0 >= l0 and s1 < l1):
            return {'back': back, 's': s1, 'l': l1}
    return None


def line_cross_within(n: int, within: int, val_at, line_at, direction: str) -> Optional[Dict]:
    """최근 within일 안에 값(val_at)이 기준선(line_at)을 위로(up)/아래로(down) 지났는지 — JS lineCrossWithin과 동일"""
    for back in range(within):
        i = n - 1 - back
        if i < 1:
            return None
        v1, l1, v0, l0 = val_at(i), line_at(i), val_at(i - 1), line_at(i - 1)
        if None in (v1, l1, v0, l0):
            return None
        if (v0 <= l0 and v1 > l1) if direction == 'up' else (v0 >= l0 and v1 < l1):
            return {'back': back, 'v': v1, 'l': l1}
    return None


def _band_line_at(closes: List[float], days: int, k: float, side: str):
    def at(i):
        b = bollinger_of(closes[:i + 1], days, k)
        return b[side] if b else None
    return at


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def streak_of(closes: List[float], direction: int) -> int:
    k = 0
    for i in range(len(closes) - 1, 0, -1):
        if _sign(closes[i] - closes[i - 1]) == direction:
            k += 1
        else:
            break
    return k


def high_months(p: Dict) -> int:
    m = p.get('months')
    if m is None:
        m = p['years'] * 12 if p.get('years') else 12
    m = round(m) if m else 12
    return int(min(max(m or 12, 1), 24))


def months_to_weeks(months: int) -> int:
    return int(min(round(months * 52 / 12), 104))


def compute_low_rates(c: Dict, d: Dict, hourly: Optional[Dict] = None, now: Optional[float] = None):
    """기간 최저가 대비 현재가(%)를 코인에 붙임 (low_w1_rate 등). JS computeLowRates와 같음"""
    price = c.get('current_price') or 0
    today_low = min(c.get('today_low') or math.inf, price or math.inf)
    for rate_key, low_key in LOW_PERIODS:
        low = min(d[low_key], today_low) if d and d.get(low_key) else None
        c[rate_key.replace('_rate', '')] = low
        c[rate_key] = (price / low - 1) * 100 if low else None
    # 전일저점 = 최근 24시간 1시간봉 저가 + 지금 가격
    c['close_24h'] = None
    if hourly and hourly.get('t'):
        idx = recent_hourly_idx(hourly, now)
        lows = [hourly['l'][i] for i in idx]
        if lows:
            c['low_d1'] = min(*lows, price)
            c['low_d1_rate'] = (price / c['low_d1'] - 1) * 100
        c['close_24h'] = close_24h_of(hourly, now)


def recent_hourly_idx(hourly: Dict, now: Optional[float] = None) -> List[int]:
    """최근 24시간 안의 1시간봉 위치 (거래가 드문 코인은 빈 시간이 있어 봉 개수로 자르면 안 됨). JS recentHourly와 같음"""
    since = (now or time.time()) - 24 * 3600
    return [i for i, t in enumerate(hourly.get('t') or []) if t + 3600 > since]


def close_24h_of(hourly: Optional[Dict], now: Optional[float] = None) -> Optional[float]:
    """24시간 전 가격 = 최근 24시간 1시간봉 중 가장 오래된 봉의 종가 (봉이 하나뿐이면 지금 봉이라 없음)"""
    if not hourly or not hourly.get('t'):
        return None
    idx = recent_hourly_idx(hourly, now)
    return hourly['c'][idx[0]] if len(idx) >= 2 else None


# ── 기본 조건 17개 (JS SCREEN_PRESETS와 id·기본값·계산 동일) ──
def _t_surge_pullback(c, p, d):
    closes, highs = d.get('day_closes'), d.get('day_highs')
    if not closes or len(closes) < p['days'] + 5:
        return None
    n = len(closes)
    typical = typical_daily_move(closes[:n - p['days']])
    best = None
    for i in range(n - p['days'], n):
        r = (closes[i] / closes[i - 1] - 1) * 100
        if best is None or r > best[1]:
            best = (i, r)
    if best is None or best[1] < p['surge'] or (typical and best[1] < typical * p['mult']):
        return None
    peak = max(*highs[best[0]:], c['current_price'])
    drop = (peak - c['current_price']) / peak * 100
    if drop < p['pbMin'] or drop > p['pbMax'] or c['current_price'] <= closes[best[0] - 1]:
        return None
    return {'s_days_ago': n - best[0], 's_rate': best[1], 's_peak': peak, 's_drop': -drop}


def _t_short_surge(c, p, d):
    if c['change_rate'] < p['rise']:
        return None
    typical = typical_daily_move(d.get('day_closes') or [])
    if not typical or c['change_rate'] < typical * p['mult']:
        return None
    return {'s_typical': typical, 's_mult': c['change_rate'] / typical}


def _t_volume_spike(c, p, d):
    if p.get('_tf'):
        # 봉 단위(1분 ~ 월봉): 방금 끝난 봉의 거래대금 ÷ 그 앞 N봉 평균 (진행 중인 봉은 아직 덜 차서 쓰지 않음)
        vals = list(d.get('day_values') or [])
        if len(vals) < 4 or not vals[-1]:
            return None
        prev = [v for v in vals[-p['days'] - 1:-1] if v and v > 0]
        if len(prev) < min(3, p['days']):
            return None
        avg = sum(prev) / len(prev)
        mult = vals[-1] / avg
        return {'s_avg_value': avg, 's_mult': mult} if mult >= p['mult'] else None
    values = [v for v in (d.get('day_values') or [])[-p['days']:] if v and v > 0]
    if len(values) < min(3, p['days']) or not c.get('trade_value_24h'):
        return None
    avg = sum(values) / len(values)
    mult = c['trade_value_24h'] / avg
    return {'s_avg_value': avg, 's_mult': mult} if mult >= p['mult'] else None


def _t_buy_flow_spike(c, p, d):
    if c.get('flow_mult') is None:
        return None
    if (c.get('flow_buy') or 0) < p['minBuy'] * 10000 or c['flow_mult'] < p['mult'] or (c.get('flow_ratio') or 0) < p['ratio']:
        return None
    return {'flow_buy': c['flow_buy'], 'flow_mult': c['flow_mult'], 'flow_ratio': c.get('flow_ratio')}


def _t_spike_pump(c, p, d):
    """순간 급등 (크게 오른 뒤): 방금 끝난 1분 거래대금이 평소(직전 60분 평균)의 mult배 이상 · 그 1분 rise% 이상 상승 ·
    직전 1시간 rise_1h% 이상 상승 (1분 거래대금 1천만 원 미만은 무시). 실시간 체결을 켠 뒤 61분이 지나야 계산됨"""
    avg, value = c.get('spike_avg'), c.get('spike_value') or 0
    o, cl, ago = c.get('spike_open'), c.get('spike_close'), c.get('spike_close_1h')
    if avg is None or not o or not cl or not ago or avg <= 0:
        return None
    if value < 10_000_000 or value < avg * p['mult']:
        return None
    rise = (cl / o - 1) * 100
    rise_1h = (cl / ago - 1) * 100
    if rise < p['rise'] or rise_1h < p['rise_1h']:
        return None
    return {'s_spike_mult': value / avg, 's_spike_rise': rise, 's_spike_rise_1h': rise_1h}


def _t_all_coins(c, p, d):
    """모든 코인: 거르지 않음 — 매수 칸에 혼자 넣으면 시작할 때 살 수 있는 코인 전체를 거래 많은 순으로 삼
    (스테이블·유의·상장폐지 예정은 봇이 사기 전에 따로 뺌)"""
    return {}


def _t_spike_prone(c, p, d):
    """급등이 잦던 코인 (2026-10 순간 급등 분석에서 2~3배 자주 튄 특성 — 과거 기록, 예측 아님): 하루 거래대금이 max_value 억 원 이하이고,
    상장 new_days 일 미만 · 가격 max_price 원 미만 · 바이낸스에 없음 중 하나라도 맞는 코인.
    상장일·바이낸스 여부를 아직 모르면(None) 그 항목은 맞지 않은 것으로 봄"""
    value, price = c.get('trade_value_24h'), c.get('current_price')
    if value is None or not price or value > p['max_value'] * 1e8:
        return None
    listed, binance = c.get('listed_days'), c.get('on_binance')
    new = listed is not None and listed < p['new_days']
    cheap = price < p['max_price']
    local = binance is False
    if not (new or cheap or local):
        return None
    why = '·'.join(w for w, ok in (('신규 상장', new), ('저가', cheap), ('바이낸스 없음', local)) if ok)
    return {'s_prone_why': why, 's_listed_days': listed}


def _t_pre_spike(c, p, d):
    """급등 직전 모습 (분석에서 급등 직전에 흔했던 모습): 직전 1시간 rise_1h% 이상 올랐고,
    직전 15분 거래대금이 평소(24시간 평균, 1분당)의 mult 배 이상. 실시간 체결을 켠 뒤 61분이 지나야 계산됨"""
    v15, last, ago, day = c.get('spike_value15'), c.get('spike_close_last'), c.get('spike_close_1h'), c.get('trade_value_24h')
    if not v15 or not last or not ago or not day or day <= 0:
        return None
    rise_1h = (last / ago - 1) * 100
    mult = (v15 / 15) / (day / 1440)
    if rise_1h < p['rise_1h'] or mult < p['mult']:
        return None
    return {'s_pre_rise_1h': rise_1h, 's_pre_mult': mult}


def _t_surge_24h(c, p, d):
    """하루 급등 (24시간 크게 오른 뒤): 지금 가격이 24시간 전(1시간봉 첫 봉 종가)보다 rise_24h% 이상 오름"""
    ago, price = c.get('close_24h'), c.get('current_price')
    if not ago or not price:
        return None
    rise = (price / ago - 1) * 100
    return {'s_close_24h': ago, 's_rise_24h': rise} if rise >= p['rise_24h'] else None


def _t_near_year_low(c, p, d):
    r = c.get('low_y1_rate')
    return {} if r is not None and r <= p['pct'] else None


def _t_far_from_high(c, p, d):
    highs = (d.get('week_highs') or [])[-months_to_weeks(high_months(p)):]
    if not highs or not c.get('current_price'):
        return None
    high = max(*highs, c['current_price'])
    from_high = (c['current_price'] / high - 1) * 100
    if -from_high < p['drop']:
        return None
    return {'s_y1_high': high, 's_from_high': from_high, 's_to_high': (high / c['current_price'] - 1) * 100}


def _t_dip_rebound(c, p, d):
    if not c.get('prev_close') or not c.get('today_low'):
        return None
    dip = (c['today_low'] / c['prev_close'] - 1) * 100
    rebound = (c['current_price'] / c['today_low'] - 1) * 100
    return {'s_dip': dip, 's_rebound': rebound} if dip <= -p['dip'] and rebound >= p['rebound'] else None


def _t_rsi(low: bool):
    def test(c, p, d):
        r = rsi_of(closes_now(c, d))
        if r is None:
            return None
        return {'s_rsi': r} if (r <= p['rsi'] if low else r >= p['rsi']) else None
    return test


def _t_cross(direction: str):
    def test(c, p, d):
        closes = closes_now(c, d)
        if not closes or p['short'] >= p['long']:
            return None
        x = cross_within(closes, int(p['short']), int(p['long']), int(p['within']), direction)
        return {'s_cross_ago': x['back'], 's_gap': (x['s'] / x['l'] - 1) * 100} if x else None
    return test


def _t_new_high(c, p, d):
    highs = (d.get('day_highs') or [])[-int(p['days']):]
    if len(highs) < min(p['days'], 3):
        return None
    hi = max(highs)
    return {'s_prev_high': hi, 's_over': (c['current_price'] / hi - 1) * 100} if c['current_price'] > hi else None


def _t_band(lower: bool):
    def test(c, p, d):
        b = bollinger_of(closes_now(c, d), int(p['days']), p['k'])
        if not b:
            return None
        line = b['lower'] if lower else b['upper']
        ok = c['current_price'] < line if lower else c['current_price'] > line
        return {'s_band': line, 's_band_gap': (c['current_price'] / line - 1) * 100} if ok else None
    return test


def _t_rsi_cross(direction: str):
    def test(c, p, d):
        closes = closes_now(c, d)
        if not closes:
            return None
        x = line_cross_within(len(closes), int(p['within']), lambda i: rsi_of(closes[:i + 1]), lambda i: p['level'], direction)
        return {'s_cross_ago': x['back'], 's_rsi': rsi_of(closes)} if x else None
    return test


def _t_band_cross(lower: bool):
    def test(c, p, d):
        closes = closes_now(c, d)
        if not closes:
            return None
        side = 'lower' if lower else 'upper'
        x = line_cross_within(len(closes), int(p['within']), lambda i: closes[i], _band_line_at(closes, int(p['days']), p['k'], side),
                              'up' if lower else 'down')
        b = bollinger_of(closes, int(p['days']), p['k']) if x else None
        return {'s_cross_ago': x['back'], 's_band': b[side], 's_band_gap': (c['current_price'] / b[side] - 1) * 100} if b else None
    return test


def _t_ma_cross(direction: str):
    def test(c, p, d):
        days = int(p['days'])
        # 50일선 · 100일선처럼 긴 평균은 긴 칸(최대 199봉)으로 — 화면(JS)과 같은 기준
        closes = (long_now(c, d, 'day_closes_xl') or long_now(c, d)) if days > 25 else closes_now(c, d)
        if not closes:
            return None
        x = line_cross_within(len(closes), int(p['within']), lambda i: closes[i], lambda i: sma_at(closes, days, i), direction)
        ma = sma_at(closes, days, len(closes) - 1) if x else None
        return {'s_cross_ago': x['back'], 's_gap': (c['current_price'] / ma - 1) * 100} if ma else None
    return test


def _t_streak(direction: int):
    def test(c, p, d):
        closes = closes_now(c, d)
        if not closes:
            return None
        k = streak_of(closes, direction)
        return {'s_streak': k, 's_streak_rate': (closes[-1] / closes[-1 - k] - 1) * 100} if k >= p['days'] else None
    return test


def _t_squeeze(c, p, d):
    days = int(p['days'])
    highs = (d.get('day_highs') or [])[-days:]
    closes = (d.get('day_closes') or [])[-days:]
    if len(highs) < days or len(closes) < days:
        return None
    hi = max(*highs, c['current_price'])
    lo = min(*closes, c['current_price'], c.get('today_low') or math.inf)
    rng = (hi / lo - 1) * 100
    return {'s_range': rng, 's_pos': (c['current_price'] - lo) / ((hi - lo) or 1) * 100} if rng <= p['range'] else None



# ── 2026-10 추가 조건용 도우미 (JS 와 같은 이름·같은 방식) ──
def long_now(c: Dict, d: Dict, key: str = 'day_closes_long') -> Optional[List[float]]:
    """긴 기간(최대 120봉) 지난 값 + 지금 가격 (종가용). 없으면 None"""
    arr = d.get(key)
    return [*arr, c['current_price']] if arr else None


def ema_series(arr: List[float], n: int) -> List[Optional[float]]:
    """지수이동평균 (처음 n개 평균에서 시작). 모자란 앞부분은 None"""
    out: List[Optional[float]] = [None] * len(arr)
    if len(arr) < n:
        return out
    e = sum(arr[:n]) / n
    out[n - 1] = e
    k = 2 / (n + 1)
    for i in range(n, len(arr)):
        e = arr[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd_series(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9):
    """(MACD 선, 시그널 선) — 같은 길이, 모자란 앞부분은 None"""
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(macd) if v is not None), None)
    signal: List[Optional[float]] = [None] * len(closes)
    if start is not None:
        part = ema_series([v for v in macd[start:]], sig)
        signal[start:] = part
    return macd, signal


def stoch_series(highs: List[float], lows: List[float], closes: List[float], n: int = 14, smooth: int = 3):
    """스토캐스틱 (%K = 원 %K 의 smooth 평균, %D = %K 의 smooth 평균). 모자란 앞부분은 None"""
    raw: List[Optional[float]] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        hh, ll = max(highs[i - n + 1:i + 1]), min(lows[i - n + 1:i + 1])
        raw[i] = (closes[i] - ll) / (hh - ll) * 100 if hh > ll else 50.0

    def sma_opt(arr, m):
        out = [None] * len(arr)
        for i in range(m - 1, len(arr)):
            w = arr[i - m + 1:i + 1]
            if None not in w:
                out[i] = sum(w) / m
        return out
    k = sma_opt(raw, smooth)
    return k, sma_opt(k, smooth)


def atr_of(highs: List[float], lows: List[float], closes: List[float], n: int = 14) -> Optional[float]:
    """평균 진폭 ATR (와일더 방식) — 끝난 봉들로"""
    if len(closes) < n + 1:
        return None
    trs = [max(highs[i], closes[i - 1]) - min(lows[i], closes[i - 1]) for i in range(1, len(closes))]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def _hlc_now(c: Dict, d: Dict):
    """긴 기간 고가·저가·종가 + 오늘(지금까지): 오늘 고가는 따로 없어 지금 가격, 저가는 오늘 저가"""
    hs, ls, cs = d.get('day_highs_long'), d.get('day_lows_long'), d.get('day_closes_long')
    if not hs or not ls or not cs or len(hs) != len(cs) or len(ls) != len(cs):
        return None
    price = c['current_price']
    return [*hs, price], [*ls, min(price, c.get('today_low') or price)], [*cs, price]


def _t_macd_cross(direction: str):
    def test(c, p, d):
        closes = long_now(c, d)
        if not closes:
            return None
        macd, signal = macd_series(closes)
        x = line_cross_within(len(closes), int(p['within']), lambda i: macd[i], lambda i: signal[i], direction)
        if not x:
            return None
        return {'s_cross_ago': x['back'], 's_macd_gap': (macd[-1] - signal[-1]) / c['current_price'] * 100}
    return test


def _aligned(closes, p):
    n = len(closes) - 1
    s, m, l = sma_at(closes, int(p['short']), n), sma_at(closes, int(p['mid']), n), sma_at(closes, int(p['long']), n)
    if None in (s, m, l) or not (s > m > l):
        return None
    return s, m, l


def _t_ma_aligned(c, p, d):
    closes = long_now(c, d)
    if not closes:
        return None
    a = _aligned(closes, p)
    if not a or c['current_price'] <= a[0]:
        return None
    return {'s_gap': (c['current_price'] / a[2] - 1) * 100}


def _t_aligned_pullback(c, p, d):
    closes = long_now(c, d)
    if not closes:
        return None
    a = _aligned(closes, p)
    if not a:
        return None
    gap = (c['current_price'] / a[1] - 1) * 100
    return {'s_gap': gap} if abs(gap) <= p['near'] else None


def _t_stoch_up(c, p, d):
    hlc = _hlc_now(c, d)
    if not hlc:
        return None
    k, dline = stoch_series(*hlc)
    level = p['level']
    x = line_cross_within(len(k), int(p['within']), lambda i: k[i], lambda i: dline[i], 'up')
    if not x or k[len(k) - 1 - x['back'] - 1] is None or k[len(k) - 1 - x['back'] - 1] > level:
        return None                                   # 교차 직전 %K 가 바닥(level 이하)이었을 때만
    return {'s_cross_ago': x['back'], 's_stoch_k': k[-1]}


def _t_disparity(low: bool):
    def test(c, p, d):
        closes = closes_now(c, d) if int(p['days']) < 30 else long_now(c, d)     # 30봉 넘는 평균은 긴 칸(120봉)으로
        if not closes:
            return None
        ma = sma_at(closes, int(p['days']), len(closes) - 1)
        if not ma:
            return None
        gap = (c['current_price'] / ma - 1) * 100
        ok = gap <= -p['pct'] if low else gap >= p['pct']
        return {'s_disparity': gap} if ok else None
    return test


def _t_vs_ma(above: bool):
    """끝난 봉의 종가가 N봉 평균선 위(아래) — 지금 가격이 아니라 어제까지 끝난 봉으로만 봄 (하루에 한 번 바뀜,
    장중 출렁임에 사고팔지 않게 · 블로그 13편 '평균선 위에서만 들고 있기'와 같은 판단). 최대 120봉"""
    def test(c, p, d):
        arr = d.get('day_closes_long')
        n = int(p.get('days') or 100)
        if not arr or n < 2 or len(arr) < n:
            return None
        ma = sum(arr[-n:]) / n
        if not ma:
            return None
        gap = (arr[-1] / ma - 1) * 100
        pct = float(p.get('pct') or 0)          # 평균선에서 이만큼 더 떨어져야 맞음 (팔기를 조금 늦추기 — probe44: 50일 · 아래 2%)
        return {'s_gap_ma': gap} if (gap > pct if above else gap < -pct) else None
    return test


def _t_vs_ema(above: bool):
    """끝난 봉의 종가가 N봉 지수이동평균(EMA) 위(아래) — '종가가 평균선 위 · 아래'의 지수 평균판 (최근 값에 무게, 더 빨리 따라옴)
    EMA 는 받은 아주 긴 칸(최대 199봉) 안에서 처음 N봉 단순 평균으로 시작해 이어 계산 (차트의 EMA 와 조금 다를 수 있음) · 최대 189봉"""
    def test(c, p, d):
        arr = d.get('day_closes_xl') or d.get('day_closes_long')        # 아주 긴 칸(199봉)이 있으면 그것으로 — EMA 120 까지
        n = int(p.get('days') or 50)
        if not arr or n < 2 or len(arr) < n + 10:
            return None
        ema = ema_series(arr, n)[-1]
        if not ema:
            return None
        gap = (arr[-1] / ema - 1) * 100
        pct = float(p.get('pct') or 0)
        return {'s_gap_ma': gap} if (gap > pct if above else gap < -pct) else None
    return test


def _t_squeeze_break(c, p, d):
    closes = long_now(c, d)
    if not closes:
        return None
    days, look, within = int(p['days']), int(p['lookback']), int(p['within'])
    n = len(closes)
    if n - 1 - look < 0:
        return None
    widths = []
    for i in range(n - 1 - look, n - 1):              # 오늘 뺀 지난 look 봉의 밴드 폭(%)
        b = bollinger_of(closes[:i + 1], days, 2)
        if not b or not b['mid']:
            return None
        widths.append((b['upper'] - b['lower']) / b['mid'] * 100)
    if len(widths) < look:
        return None
    narrow_at = widths.index(min(widths))
    b = bollinger_of(closes, days, 2)
    if narrow_at < look - within or not b or c['current_price'] <= b['upper']:
        return None
    return {'s_bw': min(widths), 's_band_gap': (c['current_price'] / b['upper'] - 1) * 100}


def _t_big_candle(c, p, d):
    hlc = _hlc_now(c, d)
    if not hlc:
        return None
    hs, ls, cs = hlc
    a = atr_of(hs[:-1], ls[:-1], cs[:-1])
    if not a:
        return None
    tr = max(hs[-1], cs[-2]) - min(ls[-1], cs[-2])
    mult = tr / a
    return {'s_atr_mult': mult} if mult >= p['mult'] else None


def _t_quiet_rise(c, p, d):
    closes = closes_now(c, d)
    days = int(p['days'])
    if not closes or len(closes) <= days or not c.get('trade_value_24h'):
        return None
    rise = (closes[-1] / closes[-1 - days] - 1) * 100
    values = [v for v in (d.get('day_values') or [])[-int(p['vdays']):] if v and v > 0]
    if rise < p['rise'] or len(values) < 3:
        return None
    ratio = c['trade_value_24h'] / (sum(values) / len(values))
    return {'s_rise': rise, 's_value_ratio': ratio} if ratio <= p['ratio'] else None


def _t_old_listing(c, p, d):
    listed = c.get('listed_days')
    return {'s_listed_days': listed} if listed is not None and listed >= p['days'] else None


def _t_value_under(c, p, d):
    v = c.get('trade_value_24h')
    return {'s_value': v} if v is not None and v <= p['max_value'] * 1e8 else None


def _t_price_range(c, p, d):
    price = c.get('current_price')
    return {} if price and p['min_price'] <= price <= p['max_price'] else None


def _t_held_coins(c, p, d):
    """지금 계좌에 들고 있는 코인 (봇은 코인마다 held 를 붙여 넘김 — bot_engine._universe, 화면은 보유 목록)"""
    return {} if c.get('held') else None


def _t_on_binance(want: bool):
    def test(c, p, d):
        return {} if c.get('on_binance') is want else None      # 아직 모르면(None) 맞지 않은 것으로 봄
    return test


def _t_pick_coins(c, p, d):
    """직접 고른 코인 (매매대상): params['coins'] = ['BTC', ...]"""
    picked = {str(x).strip().upper().replace('KRW-', '') for x in (p.get('coins') or [])}
    return {} if c.get('currency') in picked else None


def _t_new_listing(c, p, d):
    listed = c.get('listed_days')
    return {'s_listed_days': listed} if listed is not None and listed <= p['days'] else None


def _t_value_top(c, p, d):
    r = c.get('value_rank')
    return {'s_value_rank': r} if r is not None and r <= p['top'] else None


def _t_stronger_than_btc(c, p, d):
    closes, btc = closes_now(c, d), d.get('btc_closes')
    days = int(p['days'])
    if not closes or not btc or len(closes) <= days or len(btc) <= days:
        return None
    ret = (closes[-1] / closes[-1 - days] - 1) * 100
    bret = (btc[-1] / btc[-1 - days] - 1) * 100
    return {'s_ret': ret, 's_btc_ret': bret, 's_vs_btc': ret - bret} if ret - bret >= p['gap'] else None


# ── 2026-10 패턴 조건: 봉 모양 · 차트 모양 · 지표 모양 (JS 패턴 조건과 같은 계산) ──
# 끝난 봉만 봄 (진행 중인 봉은 모양이 아직 안 끝남). 시가 = 직전 봉 종가 (코인은 24시간 거래라 거의 같음)
PIVOT_K = 2        # 봉우리 · 골짜기: 양옆 2봉보다 높은(낮은) 봉


def _bars(d: Dict):
    """끝난 봉 (시가 · 고가 · 저가 · 종가 · 거래대금) — 시가는 직전 봉 종가, 맨 앞 봉은 뺌"""
    hs, ls, cs = d.get('day_highs_long'), d.get('day_lows_long'), d.get('day_closes_long')
    if not hs or not ls or not cs or len(hs) != len(cs) or len(ls) != len(cs) or len(cs) < 3:
        return None
    vs = d.get('day_values_long') or []
    vs = vs if len(vs) == len(cs) else [None] * len(cs)
    return {'o': cs[:-1], 'h': hs[1:], 'l': ls[1:], 'c': cs[1:], 'v': vs[1:]}


def _body(b, i):
    return abs(b['c'][i] - b['o'][i])


def _upper(b, i):
    return max(0.0, b['h'][i] - max(b['o'][i], b['c'][i]))


def _lower(b, i):
    return max(0.0, min(b['o'][i], b['c'][i]) - b['l'][i])


def _avg_body(b, end: int, n: int = 20) -> Optional[float]:
    """end 앞 n봉의 평균 몸통 (평소 몸통 크기)"""
    if end - n < 0:
        return None
    a = sum(_body(b, j) for j in range(end - n, end)) / n
    return a if a > 0 else None


def _move(b, end: int, days: int) -> Optional[float]:
    """end 봉 종가가 days봉 전 종가보다 몇 % 움직였나"""
    if end - days < 0 or end >= len(b['c']):
        return None
    return (b['c'][end] / b['c'][end - days] - 1) * 100


def _pivots(arr: List[float], low: bool, start: int, end: int) -> List[int]:
    """start~end 봉 중 골짜기(low) 또는 봉우리 위치 — 양옆 PIVOT_K봉보다 낮은(높은) 봉"""
    out = []
    for i in range(max(start, PIVOT_K), min(end, len(arr) - 1 - PIVOT_K) + 1):
        w = arr[i - PIVOT_K:i + PIVOT_K + 1]
        if (arr[i] == min(w) and arr[i] < arr[i - 1]) if low else (arr[i] == max(w) and arr[i] > arr[i - 1]):
            out.append(i)
    return out


def rsi_series(closes: List[float], n: int = 14) -> List[Optional[float]]:
    """봉마다 RSI (rsi_of 를 앞에서부터 잘라 계산한 값과 같음, 한 번에)"""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        if ch > 0:
            gain += ch
        else:
            loss -= ch
    gain /= n
    loss /= n

    def val(g, l_):
        if l_ == 0:
            return 50.0 if g == 0 else 100.0
        return 100 - 100 / (1 + g / l_)
    out[n] = val(gain, loss)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain = (gain * (n - 1) + max(ch, 0)) / n
        loss = (loss * (n - 1) + max(-ch, 0)) / n
        out[i] = val(gain, loss)
    return out


def _recent(b, within: int):
    """최근 within개 끝난 봉 위치 (가장 최근 먼저)"""
    n = len(b['c'])
    return [n - 1 - k for k in range(min(int(within), n))]


def _t_long_tail(lower: bool):
    """망치형(아래꼬리) · 유성형(위꼬리): 직전 days봉 동안 fall% 이상 내린(오른) 뒤, 꼬리가 몸통의 ratio배 이상"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        for i in _recent(b, p['within']):
            rng = b['h'][i] - b['l'][i]
            mv = _move(b, i - 1, int(p['days']))
            if rng <= 0 or mv is None:
                continue
            tail, other = (_lower(b, i), _upper(b, i)) if lower else (_upper(b, i), _lower(b, i))
            trend_ok = mv <= -p['fall'] if lower else mv >= p['rise']
            if trend_ok and tail >= p['ratio'] * _body(b, i) and tail >= rng * 0.5 and other <= rng * 0.25:
                return {'s_ago': len(b['c']) - 1 - i, 's_trend': mv, 's_tail': tail / rng * 100}
        return None
    return test


def _t_engulf(up: bool):
    """장악형: 앞 봉과 반대 방향 봉이 앞 봉 몸통을 다 덮음 (시가 = 앞 봉 종가라 종가가 앞 봉 시가를 넘으면 덮음)"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        for i in _recent(b, p['within']):
            if i < 1:
                continue
            mv = _move(b, i - 1, int(p['days']))
            if mv is None or not (mv <= -p['fall'] if up else mv >= p['rise']):
                continue
            prev_dir = b['c'][i - 1] - b['o'][i - 1]
            cur_dir = b['c'][i] - b['o'][i]
            if up:
                ok = prev_dir < 0 < cur_dir and b['c'][i] > b['o'][i - 1]
            else:
                ok = prev_dir > 0 > cur_dir and b['c'][i] < b['o'][i - 1]
            if ok and _body(b, i) > _body(b, i - 1):
                return {'s_ago': len(b['c']) - 1 - i, 's_trend': mv, 's_body_ratio': _body(b, i) / (_body(b, i - 1) or 1e-12)}
        return None
    return test


def _t_star(up: bool):
    """샛별형(up) · 석별형: 평소보다 큰 음봉(양봉) → 몸통 작은 봉 → 첫 봉 몸통의 절반 넘게 되돌린 양봉(음봉)"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        for i in _recent(b, p['within']):
            a = i - 2
            avg = _avg_body(b, a) if a >= 0 else None
            if not avg:
                continue
            da, dc = b['c'][a] - b['o'][a], b['c'][i] - b['o'][i]
            big = _body(b, a) >= p['big'] * avg
            small = _body(b, a + 1) <= _body(b, a) * 0.3
            mid = (b['o'][a] + b['c'][a]) / 2
            if up:
                ok = da < 0 < dc and b['c'][i] > mid
            else:
                ok = da > 0 > dc and b['c'][i] < mid
            if big and small and ok:
                return {'s_ago': len(b['c']) - 1 - i, 's_big': _body(b, a) / avg, 's_recover': abs(b['c'][i] - b['c'][a]) / _body(b, a) * 100}
        return None
    return test


def _t_three(up: bool):
    """적삼병(up) · 흑삼병: 같은 방향 봉 3개 연속, 몸통이 평소의 big배 이상, 반대쪽 꼬리는 몸통의 절반 이하"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        for i in _recent(b, p['within']):
            avg = _avg_body(b, i - 2) if i - 2 >= 0 else None
            if not avg:
                continue
            ok = True
            for j in (i - 2, i - 1, i):
                dj = b['c'][j] - b['o'][j]
                wick = _upper(b, j) if up else _lower(b, j)
                if not (dj > 0 if up else dj < 0) or _body(b, j) < p['big'] * avg or wick > _body(b, j) * 0.5:
                    ok = False
                    break
            if ok:
                return {'s_ago': len(b['c']) - 1 - i, 's_move': (b['c'][i] / b['o'][i - 2] - 1) * 100}
        return None
    return test


def _fresh_cross(b, c: Dict, within: int, line_at, up: bool) -> Optional[int]:
    """끝난 봉 종가 + 지금 가격이 최근 within칸 안에 선을 넘었고(up: 위로) 지금도 넘은 상태면, 넘은 지 몇 칸 (0 = 지금)"""
    closes = [*b['c'], c['current_price']]
    x = line_cross_within(len(closes), within, lambda i: closes[i], line_at, 'up' if up else 'down')
    if not x:
        return None
    now = line_at(len(closes) - 1)
    return x['back'] if (closes[-1] > now if up else closes[-1] < now) else None


def _t_double(bottom: bool):
    """이중 바닥(W) · 이중 천정(M): 최근 days봉 안 비슷한(±tol%) 골짜기(봉우리) 2개 → 가운데 봉우리(골짜기) 선을 넘음"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within = len(b['c']), int(p['within'])
        arr = b['l'] if bottom else b['h']
        piv = _pivots(arr, bottom, n - int(p['days']), n - 1 - within)
        if len(piv) < 2:
            return None
        i1, i2 = piv[-2], piv[-1]
        v1, v2 = arr[i1], arr[i2]
        between = arr[i1 + 1:i2]
        if i2 - i1 < 5 or abs(v2 / v1 - 1) * 100 > p['tol'] or not between:
            return None
        if bottom and min(between) < min(v1, v2) or not bottom and max(between) > max(v1, v2):
            return None
        neck = max(b['h'][i1 + 1:i2]) if bottom else min(b['l'][i1 + 1:i2])
        depth = (neck / min(v1, v2) - 1) * 100 if bottom else (1 - neck / max(v1, v2)) * 100
        if depth < p['depth']:
            return None
        # 두 번째 바닥(천정) 뒤에는 아직 목선을 넘지 않았다가, 최근 within칸 안에 넘음
        after = b['c'][i2 + 1:]
        if any((x > neck) if bottom else (x < neck) for x in after[:max(0, len(after) - within + 1)]):
            return None
        ago = _fresh_cross(b, c, within, lambda i: neck, bottom)
        if ago is None:
            return None
        return {'s_ago': ago, 's_depth': depth, 's_neck_gap': (c['current_price'] / neck - 1) * 100}
    return test


def _t_triangle_break(c, p, d):
    """삼각 수렴 돌파: 최근 days봉 봉우리는 낮아지고 골짜기는 높아지다(두 선이 좁아짐), 위 선을 넘음"""
    b = _bars(d)
    if not b:
        return None
    n, within = len(b['c']), int(p['within'])
    start, end = n - int(p['days']), n - 1 - within
    hp, lp = _pivots(b['h'], False, start, end), _pivots(b['l'], True, start, end)
    if len(hp) < 2 or len(lp) < 2:
        return None
    a, e = hp[-2], hp[-1]
    f, g = lp[-2], lp[-1]
    h1, h2, l1, l2 = b['h'][a], b['h'][e], b['l'][f], b['l'][g]
    if not (h2 < h1 and l2 > l1):
        return None
    upper = lambda i: h1 + (h2 - h1) / (e - a) * (i - a)       # noqa: E731
    lower = lambda i: l1 + (l2 - l1) / (g - f) * (i - f)       # noqa: E731
    last = max(e, g)
    if upper(last) <= lower(last):
        return None
    ago = _fresh_cross(b, c, within, upper, True)
    if ago is None:
        return None
    width = (upper(last) / lower(last) - 1) * 100
    return {'s_ago': ago, 's_width': width, 's_line_gap': (c['current_price'] / upper(n) - 1) * 100}


def _t_bull_flag(c, p, d):
    """상승 깃발: pole봉 동안 rise% 이상 급등 → 3~rest봉 좁게 쉼(급등 폭의 절반 넘게 안 내려옴) → 지금 쉼 구간 고가를 넘음"""
    b = _bars(d)
    if not b:
        return None
    n, pole = len(b['c']), int(p['pole'])
    for f in range(3, int(p['rest']) + 1):
        base_i = n - f - pole - 1
        if base_i < 0:
            break
        base = b['c'][base_i]
        top = max(b['h'][n - f - pole:n - f])
        rise = (top / base - 1) * 100
        if rise < p['rise']:
            continue
        fh, fl = max(b['h'][n - f:]), min(b['l'][n - f:])
        retrace = (top - fl) / (top - base) * 100
        if fh <= top and retrace <= 50 and c['current_price'] > fh:
            return {'s_pole': rise, 's_retrace': retrace, 's_rest': f}
    return None


def _t_cup_handle(c, p, d):
    """컵앤핸들: 최근 days봉 안 왼쪽 고점 → depth% 이상 U자 바닥 → 오른쪽 고점(왼쪽의 95% 이상) → handle봉 이내 얕은 눌림 → 지금 고점 돌파"""
    b = _bars(d)
    if not b:
        return None
    n = len(b['c'])
    start = max(0, n - int(p['days']))
    lows = b['l'][start:]
    bot = start + lows.index(min(lows))
    if bot - start < 3 or n - 1 - bot < 4:
        return None
    left = b['h'][start:bot]
    li = start + left.index(max(left))
    right = b['h'][bot + 1:]
    ri = bot + 1 + right.index(max(right))
    hl, hr, lb = b['h'][li], b['h'][ri], b['l'][bot]
    depth = (1 - lb / hl) * 100
    handle = n - 1 - ri
    if bot - li < 3 or ri - bot < 3 or depth < p['depth'] or depth > 60 or hr < hl * 0.95:
        return None
    if handle < 1 or handle > p['handle']:
        return None
    hlow = min(b['l'][ri + 1:])
    if hlow < lb + (hr - lb) * 0.5:
        return None
    rim = max(hl, hr)
    if c['current_price'] <= rim:
        return None
    return {'s_depth': depth, 's_handle': handle, 's_rim_gap': (c['current_price'] / rim - 1) * 100}


def _t_rsi_div(up: bool):
    """RSI 다이버전스: 최근 days봉 안 마지막 두 골짜기(봉우리) — 가격은 더 낮은데(높은데) RSI는 더 높음(낮음), 두 번째가 최근 within봉 안"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n = len(b['c'])
        arr = b['l'] if up else b['h']
        piv = _pivots(arr, up, n - int(p['days']), n - 1)
        if len(piv) < 2:
            return None
        i1, i2 = piv[-2], piv[-1]
        if i2 - i1 < 5 or n - 1 - i2 > p['within']:
            return None
        rsi = rsi_series(b['c'])
        r1, r2 = rsi[i1], rsi[i2]
        if r1 is None or r2 is None:
            return None
        if up:
            ok = arr[i2] < arr[i1] and r2 > r1 and r1 <= p['level']
        else:
            ok = arr[i2] > arr[i1] and r2 < r1 and r1 >= p['level']
        if not ok:
            return None
        return {'s_ago': n - 1 - i2, 's_rsi_gap': abs(r2 - r1), 's_rsi': r2}
    return test


def _t_value_breakout(c, p, d):
    """거래대금 동반 돌파: 최근 within봉 안 어느 봉이 지난 days봉 최고가 위에서 끝나고, 그 봉 거래대금이 평소의 mult배 이상 · 지금도 그 위"""
    b = _bars(d)
    if not b:
        return None
    days = int(p['days'])
    for i in _recent(b, p['within']):
        if i - days < 0:
            continue
        hi = max(b['h'][i - days:i])
        vals = [v for v in b['v'][i - days:i] if v and v > 0]
        if not vals or not b['v'][i] or b['c'][i] <= hi or c['current_price'] <= hi:
            continue
        mult = b['v'][i] / (sum(vals) / len(vals))
        if mult >= p['mult']:
            return {'s_ago': len(b['c']) - 1 - i, 's_value_mult': mult, 's_over': (c['current_price'] / hi - 1) * 100}
    return None


# ── 2026-10 패턴 2차: 상승 · 하락 · 반전 차트 패턴 (차트 패턴 정리표 참고) — JS 와 같은 계산 ──
def _break(b, c: Dict, within: int, line_at, up: bool, since: int) -> Optional[int]:
    """since 뒤 끝난 봉 종가는 (최근 within칸 전까지) 선을 안 넘었고, 최근 within칸 안에 넘어 지금도 넘은 상태 → 넘은 지 몇 칸"""
    n = len(b['c'])
    for i in range(since + 1, n - within + 1):
        if (b['c'][i] > line_at(i)) if up else (b['c'][i] < line_at(i)):
            return None
    return _fresh_cross(b, c, within, line_at, up)


def _first_ext(arr: List[float], start: int, end: int, high: bool) -> int:
    """start~end-1 중 가장 큰(high) · 작은 값의 첫 위치"""
    seg = arr[start:end]
    return start + (seg.index(max(seg)) if high else seg.index(min(seg)))


def _pole_rest(b, c: Dict, p: Dict, up: bool, narrow: bool) -> Optional[Dict]:
    """깃발 · 페넌트: pole봉 동안 rise% 이상 급등(급락) → rest봉 이하 쉼(급등 폭의 절반 넘게 안 되돌림) → 쉼 구간을 넘음
    narrow(페넌트): 쉼 구간 뒤 절반의 꼭대기는 낮아지고 바닥은 높아짐(좁아짐), 그 뒤 절반의 끝을 넘음"""
    n, pole = len(b['c']), int(p['pole'])
    for f in range(4 if narrow else 3, int(p['rest']) + 1):
        base_i = n - f - pole - 1
        if base_i < 0:
            break
        base = b['c'][base_i]
        top = max(b['h'][n - f - pole:n - f]) if up else min(b['l'][n - f - pole:n - f])
        move = (top / base - 1) * 100 if up else (1 - top / base) * 100
        if move < p['rise']:
            continue
        rh, rl = b['h'][n - f:], b['l'][n - f:]
        fh, fl = max(rh), min(rl)
        retrace = (top - fl) / (top - base) * 100 if up else (fh - top) / (base - top) * 100
        if retrace > 50 or (fh > top if up else fl < top):
            continue
        if narrow:
            k = (f + 1) // 2
            if not (max(rh[:k]) > max(rh[k:]) and min(rl[:k]) < min(rl[k:])):
                continue
            eh, el = max(rh[k:]), min(rl[k:])
        else:
            eh, el = fh, fl
        if (c['current_price'] > eh) if up else (c['current_price'] < el):
            return {'s_pole': move, 's_retrace': retrace, 's_rest': f}
    return None


def _t_pole(up: bool, narrow: bool):
    def test(c, p, d):
        b = _bars(d)
        return _pole_rest(b, c, p, up, narrow) if b else None
    return test


def _t_flat_triangle(up: bool):
    """상승 삼각형(up): 비슷한(차이 tol% 이하) 꼭대기 2개 = 평평한 위 선, 바닥은 높아짐 → 위 선 돌파 / 하락 삼각형: 반대"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within = len(b['c']), int(p['within'])
        start, end = n - int(p['days']), n - 1 - within
        af, asl = (b['h'], b['l']) if up else (b['l'], b['h'])
        flat, slope = _pivots(af, not up, start, end), _pivots(asl, up, start, end)
        if len(flat) < 2 or len(slope) < 2:
            return None
        f1, f2, s1, s2 = flat[-2], flat[-1], slope[-2], slope[-1]
        if f2 - f1 < 3 or abs(af[f2] / af[f1] - 1) * 100 > p['tol'] or s2 < f1:
            return None
        if not (asl[s2] > asl[s1] if up else asl[s2] < asl[s1]):
            return None
        level = max(af[f1], af[f2]) if up else min(af[f1], af[f2])
        ago = _break(b, c, within, lambda i: level, up, max(f2, s2))
        if ago is None:
            return None
        return {'s_ago': ago, 's_gap': (c['current_price'] / level - 1) * 100}
    return test


def _t_triangle_down(c, p, d):
    """대칭 삼각 하향 이탈: 꼭대기는 낮아지고 바닥은 높아지다가 아래 선을 깸 (삼각 수렴 돌파의 반대 방향)"""
    b = _bars(d)
    if not b:
        return None
    n, within = len(b['c']), int(p['within'])
    start, end = n - int(p['days']), n - 1 - within
    hp, lp = _pivots(b['h'], False, start, end), _pivots(b['l'], True, start, end)
    if len(hp) < 2 or len(lp) < 2:
        return None
    a, e, f, g = hp[-2], hp[-1], lp[-2], lp[-1]
    h1, h2, l1, l2 = b['h'][a], b['h'][e], b['l'][f], b['l'][g]
    if not (h2 < h1 and l2 > l1):
        return None
    upper = lambda i: h1 + (h2 - h1) / (e - a) * (i - a)       # noqa: E731
    lower = lambda i: l1 + (l2 - l1) / (g - f) * (i - f)       # noqa: E731
    last = max(e, g)
    if upper(last) <= lower(last):
        return None
    ago = _fresh_cross(b, c, within, lower, False)
    if ago is None:
        return None
    return {'s_ago': ago, 's_width': (upper(last) / lower(last) - 1) * 100, 's_line_gap': (c['current_price'] / lower(n) - 1) * 100}


def _t_measured(up: bool):
    """측정 상승(up): rise% 이상 오른 첫 다리(A→B) → 30~70% 되돌림(C) → B 를 다시 넘음 (두 번째 다리 시작) / 측정 하락: 반대"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within = len(b['c']), int(p['within'])
        start, end = max(0, n - int(p['days'])), n - 1 - within
        piv = _pivots(b['h'] if up else b['l'], not up, start, end)
        if not piv:
            return None
        bi = piv[-1]
        if bi - start < 3 or end - bi < 2:
            return None
        ai = _first_ext(b['l'] if up else b['h'], start, bi, not up)
        ci = _first_ext(b['l'] if up else b['h'], bi + 1, end + 1, not up)
        A = b['l'][ai] if up else b['h'][ai]
        B = b['h'][bi] if up else b['l'][bi]
        C = b['l'][ci] if up else b['h'][ci]
        leg = (B / A - 1) * 100 if up else (1 - B / A) * 100
        if leg < p['rise'] or bi - ai < 3 or ci - bi < 2:
            return None
        retr = (B - C) / (B - A)
        if not 0.3 <= retr <= 0.7:
            return None
        ago = _break(b, c, within, lambda i: B, up, ci)
        if ago is None:
            return None
        return {'s_ago': ago, 's_leg': leg, 's_retrace': retr * 100}
    return test


def _t_scallop(up: bool):
    """상승 스캘럽(up, J자): 왼쪽 꼭대기 → depth% 이상 둥글게 내려갔다 올라와 왼쪽 꼭대기를 넘음 / 하락 스캘럽: 반대"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within = len(b['c']), int(p['within'])
        start, end = max(0, n - int(p['days'])), n - 1 - within
        if end - start < 8:
            return None
        bot = _first_ext(b['l'] if up else b['h'], start, end + 1, not up)
        if bot - start < 3 or end - bot < 3:
            return None
        li = _first_ext(b['h'] if up else b['l'], start, bot, up)
        lv = b['h'][li] if up else b['l'][li]
        bv = b['l'][bot] if up else b['h'][bot]
        depth = (1 - bv / lv) * 100 if up else (bv / lv - 1) * 100
        if bot - li < 3 or depth < p['depth'] or depth > 40:
            return None
        ago = _break(b, c, within, lambda i: lv, up, bot)
        if ago is None:
            return None
        return {'s_ago': ago, 's_depth': depth}
    return test


def _t_three_pivots(up: bool):
    """저점 3번 높아짐(up): 마지막 골짜기 3개가 차례로 높아지고 그 사이 가장 높은 꼭대기를 넘음 / 고점 3번 낮아짐: 반대"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within = len(b['c']), int(p['within'])
        arr = b['l'] if up else b['h']
        piv = _pivots(arr, up, n - int(p['days']), n - 1 - within)
        if len(piv) < 3:
            return None
        v1, v2, v3 = piv[-3:]
        ok = arr[v1] < arr[v2] < arr[v3] if up else arr[v1] > arr[v2] > arr[v3]
        if not ok or v2 - v1 < 3 or v3 - v2 < 3:
            return None
        level = max(b['h'][v1:v3 + 1]) if up else min(b['l'][v1:v3 + 1])
        ago = _break(b, c, within, lambda i: level, up, v3)
        if ago is None:
            return None
        return {'s_ago': ago, 's_gap': (c['current_price'] / level - 1) * 100}
    return test


def _t_inv_cup_handle(c, p, d):
    """역컵앤핸들: 최근 days봉 안 왼쪽 바닥 → depth% 이상 ∩자로 올랐다 바닥 근처로 내려오고, handle봉 이하 얕게 반등한 뒤 바닥을 깸"""
    b = _bars(d)
    if not b:
        return None
    n = len(b['c'])
    start = max(0, n - int(p['days']))
    top = _first_ext(b['h'], start, n, True)
    if top - start < 3 or n - 1 - top < 4:
        return None
    li = _first_ext(b['l'], start, top, False)
    ri = _first_ext(b['l'], top + 1, n, False)
    ll, lr, ht = b['l'][li], b['l'][ri], b['h'][top]
    depth = (ht / ll - 1) * 100
    handle = n - 1 - ri
    if top - li < 3 or ri - top < 3 or depth < p['depth'] or depth > 60 or lr > ll * 1.05:
        return None
    if handle < 1 or handle > p['handle']:
        return None
    if max(b['h'][ri + 1:]) > ht - (ht - lr) * 0.5:
        return None
    rim = min(ll, lr)
    if c['current_price'] >= rim:
        return None
    return {'s_depth': depth, 's_handle': handle, 's_rim_gap': (c['current_price'] / rim - 1) * 100}


def _t_head_shoulders(top: bool):
    """헤드앤숄더 천정(top): 마지막 꼭대기 3개 — 가운데(머리)가 양쪽보다 head% 이상 높고 양 어깨는 비슷(차이 tol% 이하)
    → 두 골짜기를 이은 목선을 아래로 깸 / 바닥: 반대"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within = len(b['c']), int(p['within'])
        arr, other = (b['h'], b['l']) if top else (b['l'], b['h'])
        piv = _pivots(arr, not top, n - int(p['days']), n - 1 - within)
        if len(piv) < 3:
            return None
        s1, hd, s2 = piv[-3:]
        if top:
            ok = arr[hd] > max(arr[s1], arr[s2]) * (1 + p['head'] / 100)
        else:
            ok = arr[hd] < min(arr[s1], arr[s2]) * (1 - p['head'] / 100)
        if not ok or abs(arr[s2] / arr[s1] - 1) * 100 > p['tol'] or hd - s1 < 3 or s2 - hd < 3:
            return None
        t1 = _first_ext(other, s1 + 1, hd, not top)
        t2 = _first_ext(other, hd + 1, s2, not top)
        v1, v2 = other[t1], other[t2]
        neck = lambda i: v1 + (v2 - v1) / (t2 - t1) * (i - t1)       # noqa: E731
        ago = _break(b, c, within, neck, not top, s2)
        if ago is None:
            return None
        return {'s_ago': ago, 's_neck_gap': (c['current_price'] / neck(n) - 1) * 100}
    return test


def _t_rect(top: bool):
    """박스 천정(top): 앞서 trend% 이상 오른 뒤 days봉 동안 range% 안에서 오르내린 박스 → 박스 아래를 깸 / 박스 바닥: 내린 뒤 박스 위를 뚫음"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within, days = len(b['c']), int(p['within']), int(p['days'])
        e = n - within
        s = e - days
        if s - days < 0:
            return None
        hi, lo = max(b['h'][s:e]), min(b['l'][s:e])
        rng = (hi / lo - 1) * 100
        prior = (b['c'][s] / b['c'][s - days] - 1) * 100
        if rng > p['range'] or ((prior < p['trend']) if top else (prior > -p['trend'])):
            return None
        level = lo if top else hi
        ago = _break(b, c, within, lambda i: level, not top, e - 1)
        if ago is None:
            return None
        return {'s_ago': ago, 's_range': rng, 's_trend': prior}
    return test


def _t_diamond(top: bool):
    """다이아몬드 천정(top): 최근 days봉 안 가장 높은 꼭대기와 가장 낮은 바닥이 가운데쯤 있고 양옆 꼭대기 · 바닥은 그 안쪽(넓어졌다 좁아짐)
    → 오른쪽 아래 선(바닥 → 오른쪽 골짜기)을 아래로 깸 / 바닥: 오른쪽 위 선을 위로 뚫음. 앞선 흐름: 천정은 아래에서, 바닥은 위에서 옴"""
    def test(c, p, d):
        b = _bars(d)
        if not b:
            return None
        n, within, days = len(b['c']), int(p['within']), int(p['days'])
        start, end = n - days, n - 1 - within
        hp, lp = _pivots(b['h'], False, start, end), _pivots(b['l'], True, start, end)
        if len(hp) < 3 or len(lp) < 3:
            return None
        m, q = hp[0], lp[0]
        for i in hp:
            if b['h'][i] > b['h'][m]:
                m = i
        for i in lp:
            if b['l'][i] < b['l'][q]:
                q = i
        hl, hr = [i for i in hp if i < m], [i for i in hp if i > m]
        ll, lr = [i for i in lp if i < q], [i for i in lp if i > q]
        if not hl or not hr or not ll or not lr or abs(m - q) > days * 0.4:
            return None
        mid = (b['h'][m] + b['l'][q]) / 2
        first = b['c'][max(0, start)]
        if (first >= mid) if top else (first <= mid):
            return None
        if top:
            r = lr[-1]
            line = lambda i: b['l'][q] + (b['l'][r] - b['l'][q]) / (r - q) * (i - q)    # noqa: E731
        else:
            r = hr[-1]
            line = lambda i: b['h'][m] + (b['h'][r] - b['h'][m]) / (r - m) * (i - m)    # noqa: E731
        ago = _break(b, c, within, line, not top, max(hr[-1], lr[-1]))
        if ago is None:
            return None
        return {'s_ago': ago, 's_width': (b['h'][m] / b['l'][q] - 1) * 100}
    return test


PATTERN_IDS = ['hammer', 'bull_engulf', 'morning_star', 'three_white', 'shooting_star', 'bear_engulf', 'evening_star', 'three_black',
               'double_bottom', 'double_top', 'triangle_break', 'bull_flag', 'cup_handle', 'rsi_div_up', 'rsi_div_down', 'value_breakout',
               # 2차 (상승 · 하락 · 반전)
               'pennant_up', 'asc_triangle', 'measured_up', 'asc_scallop', 'three_rising_valleys',
               'bear_flag', 'bear_pennant', 'inv_cup_handle', 'desc_triangle', 'triangle_down', 'measured_down', 'desc_scallop',
               'three_desc_peaks', 'diamond_bottom', 'diamond_top', 'rect_top', 'rect_bottom', 'hs_top', 'hs_bottom']


from bot_core import strategy_hongro2 as _hongro2  # noqa: E402
from bot_core import strategy_zone as _zone  # noqa: E402


def _t_held_days(c, p, d):
    """파는 조건 '산 지 N일 지남' — 봇이 그 코인을 산 시각(bot_opened_ts)부터 (봇 규칙 전용, 시뮬레이션 봇 비교용 최대 보유 기간)"""
    ts = c.get('bot_opened_ts')
    if not ts:
        return None
    import time as _time
    days = (float(c.get('bot_now_ts') or _time.time()) - float(ts)) / 86400
    return {'s_held': days} if days >= float(p.get('days') or 0) else None

PRESETS: Dict[str, Dict] = {
    'surge_pullback': {'title': '급등 후 되돌림 구간', 'params': {'days': 7, 'surge': 15, 'mult': 3, 'pbMin': 5, 'pbMax': 20},
                       'test': _t_surge_pullback, 'sort': ('s_drop', 'asc')},
    'short_surge': {'title': '단기 급등 (평소 대비)', 'params': {'rise': 10, 'mult': 3}, 'test': _t_short_surge, 'sort': ('change_rate', 'desc')},
    'volume_spike': {'title': '거래대금 급증', 'params': {'days': 20, 'mult': 3}, 'test': _t_volume_spike, 'sort': ('s_mult', 'desc')},
    'buy_flow_spike': {'title': '1분 매수 급증 (실시간)', 'params': {'mult': 10, 'minBuy': 2000, 'ratio': 70},
                       'test': _t_buy_flow_spike, 'sort': ('flow_mult', 'desc'), 'needs_flow': True},
    'spike_pump': {'title': '순간 급등 (크게 오른 뒤)', 'params': {'mult': 5, 'rise': 5, 'rise_1h': 10},
                   'test': _t_spike_pump, 'sort': ('s_spike_mult', 'desc'), 'needs_flow': True, 'ready_key': 'spike_ready'},
    'surge_24h': {'title': '하루 급등 (24시간 크게 오른 뒤)', 'params': {'rise_24h': 20}, 'test': _t_surge_24h, 'sort': ('s_rise_24h', 'desc')},
    # start_buy: 잘 안 바뀌는 '코인 성격' 조건 — '그리고'로 다른 조건과 묶지 않으면 봇을 켤 때 지금 맞는 코인을 삼
    'spike_prone': {'title': '급등이 잦던 코인', 'start_buy': True, 'params': {'max_value': 100, 'new_days': 90, 'max_price': 10},
                    'test': _t_spike_prone, 'sort': ('trade_value_24h', 'asc')},
    'all_coins': {'title': '모든 코인', 'start_buy': True, 'params': {},
                  'test': _t_all_coins, 'sort': ('trade_value_24h', 'desc')},
    'pre_spike': {'title': '급등 직전 모습', 'params': {'rise_1h': 3, 'mult': 5},
                  'test': _t_pre_spike, 'sort': ('s_pre_mult', 'desc'), 'needs_flow': True, 'ready_key': 'spike_ready'},
    'near_year_low': {'title': '연간 저점 근처', 'params': {'pct': 5}, 'test': _t_near_year_low, 'sort': ('low_y1_rate', 'asc')},
    'far_from_high': {'title': '최고가 대비 많이 내려온 코인', 'params': {'months': 6, 'drop': 70}, 'test': _t_far_from_high, 'sort': ('s_to_high', 'desc')},
    'dip_rebound': {'title': '장중 급락 후 반등', 'params': {'dip': 7, 'rebound': 3}, 'test': _t_dip_rebound, 'sort': ('s_rebound', 'desc')},
    'rsi_low': {'title': 'RSI 낮음 (많이 팔림)', 'params': {'rsi': 30}, 'test': _t_rsi(True), 'sort': ('s_rsi', 'asc')},
    'rsi_high': {'title': 'RSI 높음 (많이 사짐)', 'params': {'rsi': 70}, 'test': _t_rsi(False), 'sort': ('s_rsi', 'desc')},
    'golden_cross': {'title': '이동평균 상향 교차', 'params': {'short': 5, 'long': 20, 'within': 3}, 'test': _t_cross('up'), 'sort': ('s_cross_ago', 'asc')},
    'dead_cross': {'title': '이동평균 하향 교차', 'params': {'short': 5, 'long': 20, 'within': 3}, 'test': _t_cross('down'), 'sort': ('s_cross_ago', 'asc')},
    'new_high': {'title': '기간 최고가 돌파', 'params': {'days': 20}, 'test': _t_new_high, 'sort': ('s_over', 'desc')},
    'bb_lower': {'title': '볼린저 밴드 아래로 벗어남', 'params': {'days': 20, 'k': 2}, 'test': _t_band(True), 'sort': ('s_band_gap', 'asc')},
    'bb_upper': {'title': '볼린저 밴드 위로 벗어남', 'params': {'days': 20, 'k': 2}, 'test': _t_band(False), 'sort': ('s_band_gap', 'desc')},
    'rsi_cross_up': {'title': 'RSI 기준선 상향 돌파', 'params': {'level': 30, 'within': 3}, 'test': _t_rsi_cross('up'), 'sort': ('s_cross_ago', 'asc')},
    'rsi_cross_down': {'title': 'RSI 기준선 하향 돌파', 'params': {'level': 70, 'within': 3}, 'test': _t_rsi_cross('down'), 'sort': ('s_cross_ago', 'asc')},
    'bb_lower_up': {'title': '볼린저 아래 선 상향 돌파', 'params': {'days': 20, 'k': 2, 'within': 3}, 'test': _t_band_cross(True), 'sort': ('s_cross_ago', 'asc')},
    'bb_upper_down': {'title': '볼린저 위 선 하향 돌파', 'params': {'days': 20, 'k': 2, 'within': 3}, 'test': _t_band_cross(False), 'sort': ('s_cross_ago', 'asc')},
    'ma_cross_up': {'title': '가격이 평균선 상향 돌파', 'params': {'days': 20, 'within': 3}, 'test': _t_ma_cross('up'), 'sort': ('s_cross_ago', 'asc')},
    'ma_cross_down': {'title': '가격이 평균선 하향 돌파', 'params': {'days': 20, 'within': 3}, 'test': _t_ma_cross('down'), 'sort': ('s_cross_ago', 'asc')},
    'streak_up': {'title': '연속 상승', 'params': {'days': 5}, 'test': _t_streak(1), 'sort': ('s_streak', 'desc')},
    'streak_down': {'title': '연속 하락', 'params': {'days': 5}, 'test': _t_streak(-1), 'sort': ('s_streak', 'desc')},
    'squeeze': {'title': '좁은 박스권 (횡보)', 'params': {'days': 14, 'range': 10}, 'test': _t_squeeze, 'sort': ('s_range', 'asc')},
    # 2026-10 추가: 추세 · 과매도/과매수 · 변동성/거래량 · 코인 성격/시장
    'macd_cross_up': {'title': 'MACD 상향 교차', 'params': {'within': 3}, 'test': _t_macd_cross('up'), 'sort': ('s_cross_ago', 'asc')},
    'macd_cross_down': {'title': 'MACD 하향 교차', 'params': {'within': 3}, 'test': _t_macd_cross('down'), 'sort': ('s_cross_ago', 'asc')},
    'ma_aligned': {'title': '이동평균 정배열', 'params': {'short': 5, 'mid': 20, 'long': 60}, 'test': _t_ma_aligned, 'sort': ('s_gap', 'desc')},
    'aligned_pullback': {'title': '정배열 중 눌림', 'params': {'short': 5, 'mid': 20, 'long': 60, 'near': 3},
                         'test': _t_aligned_pullback, 'sort': ('s_gap', 'asc')},
    'stoch_up': {'title': '스토캐스틱 바닥 반등', 'params': {'level': 20, 'within': 3}, 'test': _t_stoch_up, 'sort': ('s_cross_ago', 'asc')},
    'disparity_low': {'title': '평균선보다 많이 아래', 'params': {'days': 20, 'pct': 10}, 'test': _t_disparity(True), 'sort': ('s_disparity', 'asc')},
    'disparity_high': {'title': '평균선보다 많이 위', 'params': {'days': 20, 'pct': 10}, 'test': _t_disparity(False), 'sort': ('s_disparity', 'desc')},
    'squeeze_break': {'title': '밴드 수축 뒤 돌파', 'params': {'days': 20, 'lookback': 20, 'within': 5},
                      'test': _t_squeeze_break, 'sort': ('s_band_gap', 'desc')},
    'big_candle': {'title': '평소보다 큰 움직임', 'params': {'mult': 2}, 'test': _t_big_candle, 'sort': ('s_atr_mult', 'desc')},
    'quiet_rise': {'title': '거래 줄며 오름', 'params': {'days': 7, 'rise': 10, 'vdays': 20, 'ratio': 1},
                   'test': _t_quiet_rise, 'sort': ('s_rise', 'desc')},
    'new_listing': {'title': '새로 상장한 코인', 'params': {'days': 30}, 'test': _t_new_listing, 'sort': ('s_listed_days', 'asc')},
    # 2026-10-10 매매대상(코인 성격)에 더함 · '바로 사기': 매매대상 코인을 봇을 켤 때 지금 바로 (예전 '모든 코인'을 매수 칸에 넣던 것)
    'old_listing': {'title': '상장한 지 오래된 코인', 'params': {'days': 180}, 'test': _t_old_listing, 'sort': ('s_listed_days', 'desc')},
    'value_under': {'title': '거래대금 작은 코인', 'params': {'max_value': 10}, 'test': _t_value_under, 'sort': ('s_value', 'asc')},
    'price_range': {'title': '가격대', 'params': {'min_price': 0, 'max_price': 100}, 'test': _t_price_range, 'sort': ('current_price', 'asc')},
    'on_binance': {'title': '바이낸스에도 있는 코인', 'params': {}, 'test': _t_on_binance(True), 'sort': ('trade_value_24h', 'desc')},
    'not_on_binance': {'title': '업비트에만 있는 코인', 'params': {}, 'test': _t_on_binance(False), 'sort': ('trade_value_24h', 'desc')},
    'held_coins': {'title': '지금 보유한 코인', 'params': {}, 'test': _t_held_coins, 'sort': ('trade_value_24h', 'desc')},
    'pick_coins': {'title': '직접 고른 코인', 'params': {'coins': []}, 'test': _t_pick_coins, 'sort': ('trade_value_24h', 'desc')},
    # StraHub 'Hongro's Strategy2 (by aviva95)' — 사는 쪽 · 파는 쪽 한 쌍 (bot_core/strategy_hongro2.py, 시뮬레이션에서 지켜보기용)
    'hongro2_buy': {'title': 'Hongro 전략2 사기', 'params': {'tf': '4h'}, 'test': _hongro2.buy_test, 'sort': ('s_mom', 'desc')},
    'above_ma_close': {'title': '종가가 평균선 위', 'start_buy': True, 'params': {'days': 100, 'pct': 0},
                       'test': _t_vs_ma(True), 'sort': ('s_gap_ma', 'desc')},
    'below_ma_close': {'title': '종가가 평균선 아래', 'params': {'days': 100, 'pct': 0}, 'test': _t_vs_ma(False), 'sort': ('s_gap_ma', 'asc')},
    'above_ema_close': {'title': '종가가 지수이동평균 위', 'start_buy': True, 'params': {'days': 50, 'pct': 0},
                        'test': _t_vs_ema(True), 'sort': ('s_gap_ma', 'desc')},
    'below_ema_close': {'title': '종가가 지수이동평균 아래', 'params': {'days': 50, 'pct': 0}, 'test': _t_vs_ema(False), 'sort': ('s_gap_ma', 'asc')},
    'held_days': {'title': '산 지 N일 지남', 'params': {'days': 30}, 'test': _t_held_days, 'sort': ('s_held', 'desc')},
    'hongro2_sell': {'title': 'Hongro 전략2 팔기', 'params': {'tf': '4h'}, 'test': _hongro2.sell_test, 'sort': ('s_gain', 'desc')},
    # 수요 구역 되돌림 한 쌍 (bot_core/strategy_zone.py, 2026-10-18) — lab zone_retest 검증(2022~, 245코인): 되돌아온 순간 사면 거래당 +0.5%,
    # 체결이 0.5% 밀리면 0 · 코인당 1년 4번꼴. trend=1 은 영상의 '상승 추세에서만' (거래 수만 줄고 거래당 % 는 같았음)
    'zone_buy': {'title': '수요 구역 되돌림 사기', 'params': {'tf': '1h', 'impulse': 3, 'quiet': 6, 'expire': 150, 'band': 0.5, 'rr': 2.5, 'trend': 0, 'k': 5},
                 'test': _zone.buy_test, 'sort': ('s_rr', 'desc')},
    'zone_sell': {'title': '수요 구역 목표 · 깨짐 팔기', 'params': {'tf': '1h', 'impulse': 3, 'quiet': 6, 'expire': 150, 'band': 0.5, 'trend': 0, 'k': 5},
                  'test': _zone.sell_test, 'sort': ('s_gain', 'asc')},
    'buy_targets': {'title': '매매대상 코인 바로 사기', 'start_buy': True, 'params': {},
                    'test': _t_all_coins, 'sort': ('trade_value_24h', 'desc')},
    'value_top': {'title': '거래대금 상위', 'params': {'top': 20}, 'test': _t_value_top, 'sort': ('s_value_rank', 'asc')},
    'stronger_than_btc': {'title': '비트코인보다 강한 코인', 'params': {'days': 7, 'gap': 5},
                          'test': _t_stronger_than_btc, 'sort': ('s_vs_btc', 'desc')},
    # 2026-10 패턴: 봉 모양 (사는 쪽 → 파는 쪽) · 차트 모양 · 지표 모양
    'hammer': {'title': '망치형 (아래꼬리 긴 봉)', 'params': {'days': 5, 'fall': 5, 'ratio': 2, 'within': 1},
               'test': _t_long_tail(True), 'sort': ('s_trend', 'asc')},
    'bull_engulf': {'title': '상승 장악형', 'params': {'days': 5, 'fall': 5, 'within': 1},
                    'test': _t_engulf(True), 'sort': ('s_body_ratio', 'desc')},
    'morning_star': {'title': '샛별형', 'params': {'big': 1.5, 'within': 1}, 'test': _t_star(True), 'sort': ('s_recover', 'desc')},
    'three_white': {'title': '적삼병 (양봉 3개)', 'params': {'big': 1, 'within': 1}, 'test': _t_three(True), 'sort': ('s_move', 'desc')},
    'shooting_star': {'title': '유성형 (위꼬리 긴 봉)', 'params': {'days': 5, 'rise': 5, 'ratio': 2, 'within': 1},
                      'test': _t_long_tail(False), 'sort': ('s_trend', 'desc')},
    'bear_engulf': {'title': '하락 장악형', 'params': {'days': 5, 'rise': 5, 'within': 1},
                    'test': _t_engulf(False), 'sort': ('s_body_ratio', 'desc')},
    'evening_star': {'title': '석별형', 'params': {'big': 1.5, 'within': 1}, 'test': _t_star(False), 'sort': ('s_recover', 'desc')},
    'three_black': {'title': '흑삼병 (음봉 3개)', 'params': {'big': 1, 'within': 1}, 'test': _t_three(False), 'sort': ('s_move', 'asc')},
    'double_bottom': {'title': '이중 바닥 (W) 돌파', 'params': {'days': 60, 'tol': 3, 'depth': 5, 'within': 3},
                      'test': _t_double(True), 'sort': ('s_ago', 'asc')},
    'double_top': {'title': '이중 천정 (M) 이탈', 'params': {'days': 60, 'tol': 3, 'depth': 5, 'within': 3},
                   'test': _t_double(False), 'sort': ('s_ago', 'asc')},
    'triangle_break': {'title': '삼각 수렴 돌파', 'params': {'days': 30, 'within': 3}, 'test': _t_triangle_break, 'sort': ('s_ago', 'asc')},
    'bull_flag': {'title': '상승 깃발 돌파', 'params': {'pole': 5, 'rise': 15, 'rest': 7}, 'test': _t_bull_flag, 'sort': ('s_pole', 'desc')},
    'cup_handle': {'title': '컵앤핸들 돌파', 'params': {'days': 60, 'depth': 15, 'handle': 10}, 'test': _t_cup_handle, 'sort': ('s_rim_gap', 'asc')},
    'rsi_div_up': {'title': 'RSI 상승 다이버전스', 'params': {'days': 40, 'level': 40, 'within': 5},
                   'test': _t_rsi_div(True), 'sort': ('s_rsi_gap', 'desc')},
    'rsi_div_down': {'title': 'RSI 하락 다이버전스', 'params': {'days': 40, 'level': 60, 'within': 5},
                     'test': _t_rsi_div(False), 'sort': ('s_rsi_gap', 'desc')},
    'value_breakout': {'title': '거래대금 동반 돌파', 'params': {'days': 20, 'mult': 2, 'within': 1},
                       'test': _t_value_breakout, 'sort': ('s_value_mult', 'desc')},
    # 2026-10 패턴 2차: 상승 · 하락 · 반전 차트 패턴
    'pennant_up': {'title': '상승 페넌트 돌파', 'params': {'pole': 5, 'rise': 15, 'rest': 10}, 'test': _t_pole(True, True), 'sort': ('s_pole', 'desc')},
    'asc_triangle': {'title': '상승 삼각형 돌파', 'params': {'days': 40, 'tol': 2, 'within': 3}, 'test': _t_flat_triangle(True), 'sort': ('s_ago', 'asc')},
    'measured_up': {'title': '측정 상승 (두 번째 다리)', 'params': {'days': 60, 'rise': 20, 'within': 3}, 'test': _t_measured(True), 'sort': ('s_ago', 'asc')},
    'asc_scallop': {'title': '상승 스캘럽 (J자)', 'params': {'days': 30, 'depth': 8, 'within': 3}, 'test': _t_scallop(True), 'sort': ('s_ago', 'asc')},
    'three_rising_valleys': {'title': '저점 3번 높아짐', 'params': {'days': 60, 'within': 3}, 'test': _t_three_pivots(True), 'sort': ('s_ago', 'asc')},
    'bear_flag': {'title': '하락 깃발 이탈', 'params': {'pole': 5, 'rise': 15, 'rest': 7}, 'test': _t_pole(False, False), 'sort': ('s_pole', 'desc')},
    'bear_pennant': {'title': '하락 페넌트 이탈', 'params': {'pole': 5, 'rise': 15, 'rest': 10}, 'test': _t_pole(False, True), 'sort': ('s_pole', 'desc')},
    'inv_cup_handle': {'title': '역컵앤핸들 이탈', 'params': {'days': 60, 'depth': 15, 'handle': 10}, 'test': _t_inv_cup_handle, 'sort': ('s_rim_gap', 'desc')},
    'desc_triangle': {'title': '하락 삼각형 이탈', 'params': {'days': 40, 'tol': 2, 'within': 3}, 'test': _t_flat_triangle(False), 'sort': ('s_ago', 'asc')},
    'triangle_down': {'title': '대칭 삼각 하향 이탈', 'params': {'days': 30, 'within': 3}, 'test': _t_triangle_down, 'sort': ('s_ago', 'asc')},
    'measured_down': {'title': '측정 하락 (두 번째 다리)', 'params': {'days': 60, 'rise': 20, 'within': 3}, 'test': _t_measured(False), 'sort': ('s_ago', 'asc')},
    'desc_scallop': {'title': '하락 스캘럽 (뒤집힌 J자)', 'params': {'days': 30, 'depth': 8, 'within': 3}, 'test': _t_scallop(False), 'sort': ('s_ago', 'asc')},
    'three_desc_peaks': {'title': '고점 3번 낮아짐', 'params': {'days': 60, 'within': 3}, 'test': _t_three_pivots(False), 'sort': ('s_ago', 'asc')},
    'diamond_bottom': {'title': '다이아몬드 바닥 돌파', 'params': {'days': 60, 'within': 3}, 'test': _t_diamond(False), 'sort': ('s_ago', 'asc')},
    'diamond_top': {'title': '다이아몬드 천정 이탈', 'params': {'days': 60, 'within': 3}, 'test': _t_diamond(True), 'sort': ('s_ago', 'asc')},
    'rect_top': {'title': '박스 천정 이탈', 'params': {'days': 20, 'range': 12, 'trend': 10, 'within': 3}, 'test': _t_rect(True), 'sort': ('s_ago', 'asc')},
    'rect_bottom': {'title': '박스 바닥 돌파', 'params': {'days': 20, 'range': 12, 'trend': 10, 'within': 3}, 'test': _t_rect(False), 'sort': ('s_ago', 'asc')},
    'hs_top': {'title': '헤드앤숄더 천정', 'params': {'days': 60, 'tol': 5, 'head': 3, 'within': 3}, 'test': _t_head_shoulders(True), 'sort': ('s_ago', 'asc')},
    'hs_bottom': {'title': '역헤드앤숄더 (바닥)', 'params': {'days': 60, 'tol': 5, 'head': 3, 'within': 3}, 'test': _t_head_shoulders(False), 'sort': ('s_ago', 'asc')},
}


# ── 내 조건 (gui/condition_llm.py 규칙 JSON) — JS customMetricValue / customRulePasses와 동일 ──
BOOL_METRICS = {'ma_cross', 'held'}
PCT_METRICS = {'change_rate', 'sma_gap', 'bb_pos', 'from_high', 'from_low', 'range_pct', 'rebound'}


def custom_metric_value(rule: Dict, c: Dict, d: Dict) -> Optional[float]:
    closes = closes_now(c, d)
    price = c.get('current_price')
    m = rule['metric']
    if m == 'change_rate':
        return c.get('change_rate')
    if m == 'rsi':
        return rsi_of(closes, int(rule.get('period', 14)))
    if m == 'sma_gap':
        n = int(rule['days'])
        if n > 29:                                  # 50일선 · 100일선처럼 긴 평균은 긴 칸(최대 199봉)으로
            closes = long_now(c, d, 'day_closes_xl') or long_now(c, d)
        if not closes:
            return None
        sma = sma_at(closes, n, len(closes) - 1)
        return (price / sma - 1) * 100 if sma else None
    if m == 'ma_cross':
        if not closes:
            return None
        x = cross_within(closes, int(rule['short']), int(rule['long']), int(rule['within']), 'down' if rule.get('dir') == 'down' else 'up')
        return 1 if x else 0
    if m == 'bb_pos':
        b = bollinger_of(closes, int(rule['days']), rule['k'])
        if not b:
            return None
        if price > b['upper']:
            return (price / b['upper'] - 1) * 100
        if price < b['lower']:
            return (price / b['lower'] - 1) * 100
        return 0
    if m == 'from_high':
        highs = (d.get('week_highs') or [])[-months_to_weeks(int(rule['months'])):]
        if not highs or not price:
            return None
        return (price / max(*highs, price) - 1) * 100
    if m == 'from_low':
        return c.get(f"low_{rule['span']}_rate")
    if m == 'value_mult':
        values = [v for v in (d.get('day_values') or [])[-int(rule['days']):] if v and v > 0]
        if not values or not c.get('trade_value_24h'):
            return None
        return c['trade_value_24h'] / (sum(values) / len(values))
    if m == 'streak':
        return streak_of(closes, -1 if rule.get('dir') == 'down' else 1) if closes else None
    if m == 'move_mult':
        typical = typical_daily_move(d.get('day_closes') or [])
        return c['change_rate'] / typical if typical else None
    if m == 'range_pct':
        days = int(rule['days'])
        highs = (d.get('day_highs') or [])[-days:]
        cl = (d.get('day_closes') or [])[-days:]
        if len(highs) < days or not price:
            return None
        hi = max(*highs, price)
        lo = min(*cl, price, c.get('today_low') or math.inf)
        return (hi / lo - 1) * 100
    if m == 'rebound':
        return (price / c['today_low'] - 1) * 100 if c.get('today_low') else None
    if m == 'held':
        return 1 if c.get('held') else 0
    if m == 'trade_value':
        return c.get('trade_value_24h')
    return None


def custom_default_params(spec: Dict) -> Dict:
    p = {}
    for i, r in enumerate(spec['rules']):
        if r['metric'] in BOOL_METRICS:
            continue
        p[f'r{i}'] = r['value']
        if r['op'] == 'between':
            p[f'r{i}b'] = r.get('value2')
    return p


def custom_rule_passes(rule: Dict, value, p: Dict, i: int) -> bool:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return False
    if rule['metric'] in BOOL_METRICS:
        return value == (1 if rule.get('value') else 0)
    v, v2 = p.get(f'r{i}'), p.get(f'r{i}b')
    op = rule['op']
    if op == '<=':
        return value <= v
    if op == '>=':
        return value >= v
    if op == '<':
        return value < v
    if op == '>':
        return value > v
    if op == 'between':
        return v <= value <= v2
    return False


def custom_key(spec_id: str, i: int, metric: str) -> str:
    return f"{spec_id}__{'pct' if metric in PCT_METRICS else 'v'}{i}"


def make_custom_test(spec: Dict) -> Callable:
    spec_id = spec.get('id') or 'my'

    def test(c, p, d):
        out, passes = {}, []
        for i, r in enumerate(spec['rules']):
            value = custom_metric_value(r, c, d)
            if r['metric'] not in BOOL_METRICS:
                out[custom_key(spec_id, i, r['metric'])] = value
            passes.append(custom_rule_passes(r, value, p, i))
        ok = any(passes) if spec.get('match') == 'any' else all(passes)
        return out if ok else None
    return test


def custom_sort(spec: Dict):
    spec_id = spec.get('id') or 'my'
    sort = spec.get('sort') or {}
    idx = sort.get('rule', 0)
    rules = spec['rules']
    if 0 <= idx < len(rules) and rules[idx]['metric'] not in BOOL_METRICS:
        return custom_key(spec_id, idx, rules[idx]['metric']), sort.get('dir', 'desc')
    for i, r in enumerate(rules):
        if r['metric'] not in BOOL_METRICS:
            return custom_key(spec_id, i, r['metric']), 'desc'
    return 'trade_value_24h', 'desc'


# ── 실행 ──
def resolve(cond_id: str, custom_conditions: List[Dict]) -> Optional[Dict]:
    """조건 id → {title, test, default params, sort, needs_flow}. 없으면 None"""
    if cond_id in PRESETS:
        pr = PRESETS[cond_id]
        return {'id': cond_id, 'title': pr['title'], 'test': pr['test'], 'params': dict(pr['params']),
                'sort': pr['sort'], 'needs_flow': pr.get('needs_flow', False),
                'ready_key': pr.get('ready_key', 'flow_ready'), 'start_buy': pr.get('start_buy', False)}
    for spec in custom_conditions or []:
        if spec.get('id') == cond_id:
            return {'id': cond_id, 'title': spec.get('title', '내 조건'), 'test': make_custom_test(spec),
                    'params': custom_default_params(spec), 'sort': custom_sort(spec), 'needs_flow': False, 'custom': True}
    return None


TF_AWARE = {'volume_spike', 'zone_sell'}      # zone_sell: 산 뒤로 끝난 봉 수를 봉 길이로 셈


def run_condition(cond: Dict, params: Optional[Dict], coins: List[Dict], lows: Dict[str, Dict],
                  hourly: Optional[Dict[str, Dict]] = None, include_stable: bool = False,
                  now: Optional[float] = None) -> List[Dict]:
    """조건에 맞는 코인 목록 (조건 정렬 순). coins의 각 항목은 복사해서 계산값을 붙임"""
    p = {**cond['params'], **(params or {})}
    tf = p.pop('tf', None)             # 시간 봉은 lows 를 고를 때 이미 씀 (계산 함수에는 안 넘김)
    if tf and tf != 'day' and cond.get('id') in TF_AWARE:
        p['_tf'] = tf                  # 봉 단위에 따라 계산이 달라지는 조건 (거래대금 급증: 24시간 대신 방금 끝난 봉)
    hits = []
    for coin in coins:
        if not include_stable and is_stable_coin(coin):
            continue
        c = dict(coin)
        d = lows.get(f"KRW-{c['currency']}") or {}
        compute_low_rates(c, d, (hourly or {}).get(f"KRW-{c['currency']}"), now)
        try:
            m = cond['test'](c, p, d)
        except Exception:
            m = None
        if m is not None:
            c.update(m)
            hits.append(c)
    key, direction = cond['sort']
    have = [h for h in hits if h.get(key) is not None]
    missing = [h for h in hits if h.get(key) is None]
    have.sort(key=lambda h: h[key], reverse=(direction == 'desc'))
    return have + missing
