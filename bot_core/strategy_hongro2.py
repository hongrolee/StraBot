"""
StraHub 'Hongro's Strategy2 (by aviva95)' 를 StraBot 조건 두 개로 옮김 — 시뮬레이션에서 지켜보기용 (2026-10-13)

원본: AutoTrading_Flask/custom_indicators/hongro_strategy2.py (talib · pandas). 여기서는 같은 식을 순수 파이썬으로.
봉: 규칙이 고른 시간 봉(4시간 · 1시간 · 일봉)의 '끝난 봉' 120개 (d['day_*_long']) — 마지막 끝난 봉이 원본의 '지금 봉'.

원본과 다른 점 (화면 설명에도 적음):
- 시가가 없어 '그 봉 시가 = 앞 봉 종가'로 봄, 거래량 대신 거래대금(원)으로 MFI 를 계산
- 원본의 긴 봉 확인(is_long_candle(df, 5, ...))은 늘 데이터 맨 앞 6번째 봉을 보는 실수가 있어, 뜻대로 '마지막 봉 몸통 > 앞 봉 몸통 × 1.07' 로
- B(익절로 판 뒤 다시 사기)의 '익절로 팔았나'는 이 봇이 그 코인을 마지막으로 판 거래가 이익이었나로 (스퀴즈 하향 교차로 풀리는 것은 뺌)
- A1(MFI 80 아래로)은 원본처럼 B · C 로 산 코인만이 아니라 모든 보유 코인에
- EMA110 은 끝난 봉 120개로 계산 (원본은 처음부터 이어 온 값)
- 익절 25% · 고점에서 8% 하락은 봇의 익절 25 · 트레일링 8 · 손절 8 로 (봇 설정)
"""
from math import sqrt
from typing import Dict, List, Optional

P = {'sm_len': 6, 'sm_kc': 1.5, 'bb_len': 20, 'bb_k': 2.0, 'mfi': 14, 'mfi_th': 80, 'rsi': 14, 'rsi_block': 80,
     'ema': 110, 'lc_ratio': 1.07, 'loss_up': -80.0, 'loss_down': -10.0, 'profit_short': 0.07, 'profit_long': 0.25}


def _sma(a: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(a)
    for i in range(n - 1, len(a)):
        w = a[i - n + 1:i + 1]
        if any(x is None for x in w):
            continue
        out[i] = sum(w) / n
    return out


def _std(a: List[float], n: int) -> List[Optional[float]]:
    m = _sma(a, n)
    out: List[Optional[float]] = [None] * len(a)
    for i in range(n - 1, len(a)):
        if m[i] is None:
            continue
        out[i] = sqrt(sum((x - m[i]) ** 2 for x in a[i - n + 1:i + 1]) / n)      # talib STDDEV (모집단)
    return out


def _ema(a: List[float], n: int) -> List[Optional[float]]:
    """talib EMA: 처음 n개 평균에서 시작, 그 뒤 2/(n+1)"""
    out: List[Optional[float]] = [None] * len(a)
    if len(a) < n:
        return out
    v = sum(a[:n]) / n
    out[n - 1] = v
    k = 2 / (n + 1)
    for i in range(n, len(a)):
        v = a[i] * k + v * (1 - k)
        out[i] = v
    return out


def _rsi(c: List[float], n: int) -> List[Optional[float]]:
    """talib RSI (와일더): 처음 n개 변화의 평균에서 시작"""
    out: List[Optional[float]] = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def _mfi(h, l, c, v, n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(c)
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(len(c))]
    for i in range(n, len(c)):
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            f = tp[j] * (v[j] or 0)
            if tp[j] > tp[j - 1]:
                pos += f
            elif tp[j] < tp[j - 1]:
                neg += f
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    return out


def _momentum(h, l, c, n: int) -> List[Optional[float]]:
    """원본 calculate_squeeze_momentum(…, kc_length=6): SMA6(종가 − ((6봉 최고+최저)/2 + 6봉 평균)/2)"""
    basis = _sma(c, n)
    src: List[Optional[float]] = [None] * len(c)
    for i in range(n - 1, len(c)):
        hh, ll = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        src[i] = c[i] - ((hh + ll) / 2 + basis[i]) / 2
    return _sma(src, n)


def _bars(d: Dict):
    h, l, c, v = d.get('day_highs_long'), d.get('day_lows_long'), d.get('day_closes_long'), d.get('day_values_long')
    if not h or not l or not c or len(c) < 40 or not (len(h) == len(l) == len(c)):
        return None
    v = v if v and len(v) == len(c) else [0.0] * len(c)
    o = [c[0]] + c[:-1]                                     # 시가 = 앞 봉 종가로 봄
    return o, list(h), list(l), list(c), [x or 0.0 for x in v]


def _bb(c, n=20, k=2.0):
    m, s = _sma(c, n), _std(c, n)
    return [None if m[i] is None else m[i] + k * s[i] for i in range(len(c))], m


def _above_bb(c, pct: float) -> bool:
    up, mid = _bb(c)
    if up[-1] is None or up[-1] == mid[-1]:
        return False
    return (c[-1] - mid[-1]) / (up[-1] - mid[-1]) * 100 >= pct


def _long_candle(o, c, ratio: float) -> bool:
    return abs(c[-1] - o[-1]) > abs(c[-2] - o[-2]) * ratio


def buy_test(coin: Dict, p: Dict, d: Dict) -> Optional[Dict]:
    """사기: (A 또는 B 또는 C) 그리고 D 그리고 RSI < 80"""
    b = _bars(d)
    if not b:
        return None
    o, h, l, c, v = b
    m = _momentum(h, l, c, P['sm_len'])
    if any(x is None for x in m[-10:]):
        return None
    A = m[-3] < 0 < m[-2] < m[-1]                                         # 0 위로 막 넘어 오르는 중
    B = (bool(coin.get('bot_profit_sold')) and m[-4] < m[-3] < m[-2] < m[-1]
         and not _above_bb(c, 120.0) and not _long_candle(o, c, P['lc_ratio']))   # 익절로 판 뒤 다시 오르막
    C = (all(m[-j - 2] > m[-j - 1] for j in range(5, 8))                  # 내리다가
         and all(m[-j - 2] < m[-j - 1] for j in range(0, 5)) and m[-1] > 0)        # 5봉째 오르며 0 위
    D = not (_long_candle(o, c, P['lc_ratio']) and _above_bb(c, 80.0))
    r = _rsi(c, P['rsi'])[-1]
    if not ((A or B or C) and D and (r is None or r < P['rsi_block'])):
        return None
    return {'s_kind': 'A' if A else ('B' if B else 'C'), 's_mom': m[-1], 's_rsi': r}


def _loss_cut(lo_or_close, c, buy: float, pct: float) -> bool:
    """원본 is_loss_cut_candle(back_i=5): 앞 5봉(-7~-3)의 최저가 앞 5봉 종가(-2~-6)보다 모두 높고, 그 값이 산 값 × (1+pct%) 이하"""
    vals = lo_or_close[-7:-2]
    if len(vals) < 5 or len(c) < 7:
        return False
    mv = min(vals)
    return all(mv > c[-k] for k in range(2, 7)) and mv <= buy * (1 + pct / 100)


def sell_test(coin: Dict, p: Dict, d: Dict) -> Optional[Dict]:
    """팔기: 가격이 EMA110 위면 (A 손절선 −80% · A1 MFI 80 아래로), 아래면 (B 손절선 −10% · B1 볼린저 위 선 아래로),
    늘 (C 앞 5봉 최저의 +7% · D 산 값 +25%)"""
    buy = coin.get('bot_entry')
    b = _bars(d)
    if not b or not buy:
        return None
    o, h, l, c, v = b
    ema = _ema(c, P['ema'])[-1]
    above = ema is not None and ema < c[-1]
    why = None
    if above:
        if _loss_cut(l, c, buy, P['loss_up']):
            why = 'A 손절선(EMA 위)'
        else:
            mf = _mfi(h, l, c, v, P['mfi'])
            if mf[-2] is not None and mf[-1] is not None and mf[-2] >= P['mfi_th'] > mf[-1]:
                why = 'A1 MFI 80 아래로'
    else:
        if _loss_cut(c, c, buy, P['loss_down']):
            why = 'B 손절선(EMA 아래 −10%)'
        else:
            up, _ = _bb(c)
            if up[-2] is not None and c[-2] >= up[-2] and c[-1] < up[-1]:
                why = 'B1 볼린저 위 선 아래로'
    if why is None and c[-1] >= min(l[-6:-1]) * (1 + P['profit_short']):
        why = 'C 짧은 익절(앞 5봉 최저 +7%)'
    if why is None and c[-1] >= buy * (1 + P['profit_long']):
        why = 'D 긴 익절(+25%)'
    if why is None:
        return None
    return {'s_why': why, 's_gain': (c[-1] / buy - 1) * 100}
