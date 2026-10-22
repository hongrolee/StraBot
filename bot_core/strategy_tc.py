"""
하루 매매 전략 (The Trading Channel) — StraBot 조건 한 쌍 (사기 · 팔기), 2026-10-22

영상: 'The Only Day Trading Strategy You Will Ever Need' (https://www.youtube.com/watch?v=2h5ryPi6ZYo, 조회 568만)
  ① 한 단계 위 봉(5분 → 15분 · 15분 → 1시간 · 1시간 → 4시간 · 4시간 → 일봉)이 상승 추세일 때만.
     저점은 그 앞 고점을 종가로 뚫어야 확정되고, 확정된 저점 아래로 종가가 내려가면 추세 끝.
  ② 그 추세에서 '마지막으로 뚫린 주요 고점'(옛 천장) 구역으로 가격이 되돌아오면,
  ③ 거래하는 봉에서 쌍바닥: 첫 바닥 → 반등 → 첫 바닥의 몸통 아래 ~ 꼬리 사이로 다시 내려옴(꼬리 아래로 마감 안 됨) → 양봉 → 산다.
  ④ 손절 = 쌍바닥 가장 낮은 곳 − ATR(14) · 목표 = 위 봉의 직전 고점(구역을 뚫은 뒤 최고가).
  업비트 현물이라 사는 쪽만. 검증(strahub-research lab tc_retest_db, 2022~): 5분↔15분 거래당 −0.07%, 1시간↔4시간 −0.005% — 아무 때나 산 것과 구분 안 됨.

자료: 규칙의 봉(tf)은 끝난 봉 120개(d['day_*_long']), 위 봉(htf)은 엔진 · 백테스트가 d['htf'] 로 넣어 준다(같은 칸 이름).
  봉 자료에 시가가 없어 '그 봉 시가 = 앞 봉 종가'로 본다 (24시간 거래라 거의 같음). 양봉 = 종가 > 시가.
  백테스트는 지금 봉(걸음 봉)이 끝난 봉 목록에 없어서 coin['bar_open' · 'bar_high' · 'bar_low'] + 지금 가격으로 덧붙여 본다.
사기 (tc_buy): 마지막으로 끝난 봉이 '쌍바닥 뒤 양봉'일 때 신호. 이 봇이 그 구역이 생긴 뒤 이미 이 코인을 사고팔았으면 안 삼(구역 한 번만).
팔기 (tc_sell): 산 시각의 봉 자료로 사기 계산을 다시 해서 손절 · 목표를 되찾고, 지금 가격이 손절 이하면 손절 · 목표 이상이면 익절.
  되찾지 못하면(오래 들고 있어 첫 바닥이 봉 120개 밖으로 밀림) '계획을 다시 찾지 못해 정리'로 판다.
"""
import time
from typing import Dict, List, Optional

P = {'k_htf': 5, 'k_ltf': 2, 'zpad': 0.25, 'max_wait': 6, 'stop_atr': 1.0, 'trend': 1}
HTF_OF = {'5m': '15m', '15m': '1h', '1h': '4h', '4h': 'day'}
TF_SEC = {'1m': 60, '5m': 300, '10m': 600, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, 'day': 86400, 'w': 604800}
SCAN = 80                     # 쌍바닥을 찾아 볼 거래 봉 수 (끝에서부터)


def _arrays(d: Optional[Dict]):
    if not d:
        return None
    h, l, c = d.get('day_highs_long'), d.get('day_lows_long'), d.get('day_closes_long')
    if not h or not l or not c or len(c) < 20 or not (len(h) == len(l) == len(c)):
        return None
    c = [float(x) for x in c]
    return [c[0]] + c[:-1], [float(x) for x in h], [float(x) for x in l], c


def _with_current(b, coin: Dict):
    """백테스트: 지금 봉(방금 끝난 걸음 봉)을 덧붙임 — 실제 봇은 끝난 봉 목록에 이미 들어 있음"""
    bo, bh, bl = coin.get('bar_open'), coin.get('bar_high'), coin.get('bar_low')
    price = float(coin.get('current_price') or 0)
    if bo is None or bh is None or bl is None or price <= 0:
        return b
    o, h, l, c = b
    return o + [float(bo)], h + [float(bh)], l + [float(bl)], c + [price]


def _atr(h: List[float], l: List[float], c: List[float], n: int = 14) -> List[Optional[float]]:
    """Wilder ATR (lab ta.atr 와 같음)"""
    out: List[Optional[float]] = [None] * len(c)
    if len(c) < n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    v = sum(tr[:n]) / n
    out[n - 1] = v
    for i in range(n, len(c)):
        v = v + (tr[i] - v) / n
        out[i] = v
    return out


def _swings(h, l, k):
    n = len(h)
    sh, sl = [False] * n, [False] * n
    for j in range(k, n - k):
        sh[j] = h[j] >= max(h[j - k:j + k + 1])
        sl[j] = l[j] <= min(l[j - k:j + k + 1])
    return sh, sl


def htf_state(hb, k: int) -> Dict:
    """위 봉 끝난 봉들 → {'up', 'zone': (아래, 위), 'zi': 뚫은 봉 번호, 'tgt': 뚫은 뒤 최고가}  (lab tc_retest_db._htf_structure 와 같은 규칙)"""
    o, h, l, c = hb
    n = len(c)
    sh, sl = _swings(h, l, k)
    trend = None
    last_sh = last_sh_i = last_sl = None
    pend_low = pend_ref = pend_ref_i = None
    pend_high = pend_high_ref = None
    valid_low = valid_high = None
    zone = None
    run_hi = None
    for i in range(1, n):
        j = i - k
        if j >= 0:
            if sh[j]:
                last_sh, last_sh_i = h[j], j
                pend_high, pend_high_ref = h[j], last_sl
            if sl[j]:
                last_sl = l[j]
                pend_low, pend_ref, pend_ref_i = l[j], last_sh, last_sh_i
        if pend_low is not None and pend_ref is not None and c[i] > pend_ref:
            valid_low = pend_low
            bi = pend_ref_i
            zone = (max(o[bi], c[bi]), h[bi], i)
            run_hi = h[i]
            pend_low = None
            if trend is None:
                trend = 'up'
        if pend_high is not None and pend_high_ref is not None and c[i] < pend_high_ref:
            valid_high = pend_high
            pend_high = None
            if trend is None:
                trend = 'down'
        if valid_low is not None and c[i] < valid_low:
            trend = 'down'
        if valid_high is not None and c[i] > valid_high:
            trend = 'up'
        if zone is not None:
            run_hi = max(run_hi, h[i])
    a = _atr(h, l, c)[-1] or 0.0
    return {'up': trend == 'up', 'zone': None if zone is None else (zone[0], zone[1]), 'zi': None if zone is None else zone[2],
            'tgt': run_hi, 'atr': a, 'n': n}


def plan(lb, hb, p: Dict, lsec: Optional[int] = None, hsec: Optional[int] = None) -> Optional[Dict]:
    """거래 봉 lb · 위 봉 hb (둘 다 끝난 봉, 오래된 순) → 마지막 봉에서 '쌍바닥 뒤 양봉' 신호면 {'stop', 'tgt', 'zone', 'b1', ...}
    lsec · hsec 를 주면 구역이 생긴(위 봉이 고점을 뚫은) 뒤의 거래 봉에서만 첫 바닥을 찾는다"""
    hs = htf_state(hb, int(p.get('k_htf', P['k_htf'])))
    if hs['zone'] is None or (int(p.get('trend', P['trend'])) and not hs['up']):
        return None
    pad = hs['atr'] * float(p.get('zpad', P['zpad']))
    zlo, zhi = hs['zone'][0] - pad, hs['zone'][1] + pad
    o, h, l, c = lb
    n = len(c)
    k = int(p.get('k_ltf', P['k_ltf']))
    max_wait = int(p.get('max_wait', P['max_wait']))
    atr = _atr(h, l, c)
    i_from = 0
    if lsec and hsec:
        i_from = max(0, n - int((hs['n'] - hs['zi']) * hsec / lsec))
    b1 = touch = None
    for i in range(max(k * 2, i_from, n - SCAN), n):
        j = i - k
        if b1 is None:
            if j >= k and l[j] == min(l[j - k:i + 1]) and zlo <= l[j] <= zhi and j >= i_from:
                b1, touch = j, None
            continue
        if c[i] < l[b1]:                              # 첫 바닥 꼬리 아래로 마감 → 다시 찾음
            b1 = touch = None
            continue
        if touch is None:
            if i - b1 > k and l[i] <= min(o[b1], c[b1]):
                touch = i
            else:
                continue
        if i - touch > max_wait:
            b1 = touch = None
            continue
        if c[i] > o[i]:                               # 양봉 → 이 봉에서 신호
            if i == n - 1:
                lo = min(l[b1:i + 1])
                stop = lo - float(p.get('stop_atr', P['stop_atr'])) * (atr[i] or 0.0)
                if float(p.get('stop_atr', P['stop_atr'])) == 0:
                    stop = lo * 0.999
                return {'stop': stop, 'tgt': hs['tgt'], 'zone': (zlo, zhi), 'b1': b1, 'touch': touch, 'zi': hs['zi'], 'nh': hs['n']}
            b1 = touch = None
    return None


def _sec(p: Dict) -> int:
    return TF_SEC.get(str(p.get('_tf') or 'day'), 86400)        # run_condition 은 일봉이면 _tf 를 안 넣음


def _hsec(p: Dict) -> int:
    return TF_SEC.get(str(p.get('htf') or HTF_OF.get(str(p.get('_tf') or 'day'), 'day')), 86400)


def buy_test(coin: Dict, p: Dict, d: Dict) -> Optional[Dict]:
    lb = _arrays(d)
    hb = _arrays((d or {}).get('htf'))
    price = float(coin.get('current_price') or 0)
    if not lb or not hb or price <= 0:
        return None
    lb = _with_current(lb, coin)
    pl = plan(lb, hb, p, _sec(p), _hsec(p))
    if not pl:
        return None
    # 구역 한 번만: 이 봇이 이 구역이 생긴 뒤 이미 이 코인을 사고팔았으면 안 삼 (실제 봇만, 백테스트는 봉마다 한 번)
    last = coin.get('bot_last_trade_ts')
    if last and coin.get('bar_open') is None:
        now = float(coin.get('bot_now_ts') or time.time())
        if now - float(last) < (pl['nh'] - pl['zi']) * _hsec(p):
            return None
    if not (pl['stop'] < price < pl['tgt']):
        return None
    return {'s_stop': pl['stop'], 's_target': pl['tgt'], 's_rr': (pl['tgt'] - price) / (price - pl['stop']),
            's_zone_low': pl['zone'][0], 's_zone_high': pl['zone'][1], 's_up': (pl['tgt'] / price - 1) * 100, 's_down': (1 - pl['stop'] / price) * 100}


def _since(coin: Dict, sec: int) -> Optional[int]:
    ts = coin.get('bot_opened_ts')
    if not ts:
        return None
    now = float(coin.get('bot_now_ts') or time.time())
    return max(0, int((now - float(ts)) // sec))


def sell_test(coin: Dict, p: Dict, d: Dict) -> Optional[Dict]:
    """산 시각의 봉으로 사기 계산을 다시 해 손절 · 목표를 되찾고, 손절 이하 · 목표 이상이면 판다"""
    buy = coin.get('bot_entry')
    lb = _arrays(d)
    hb = _arrays((d or {}).get('htf'))
    price = float(coin.get('current_price') or 0)
    if not buy or not lb or not hb or price <= 0:
        return None
    buy = float(buy)
    gain = (price / buy - 1) * 100
    s_l = _since(coin, _sec(p))
    s_h = _since(coin, _hsec(p))
    pl = None
    if s_l is not None and s_h is not None:
        n, nh = len(lb[3]), len(hb[3])
        for dl in (0, 1, -1, 2):                      # 실제 봇 · 백테스트의 '산 시각 ↔ 끝난 봉' 한 칸 차이를 흡수
            cut = n - s_l + dl
            if cut < 20 or cut > n:
                continue
            for dh in (0, 1, -1):
                hcut = nh - s_h + dh
                if hcut < 20 or hcut > nh:
                    continue
                q = plan([x[:cut] for x in lb], [x[:hcut] for x in hb], dict(p, trend=0), _sec(p), _hsec(p))   # 추세는 산 때 이미 확인
                if q and q['stop'] < buy < q['tgt'] * 1.02:
                    pl = q
                    break
            if pl:
                break
    if not pl:
        if s_l is not None and s_l < max(10, len(lb[3]) - 30):
            return None                                # 첫 바닥이 아직 봉 목록 안인데 못 찾음 → 판단 보류 (자료가 잠깐 비었을 때 등)
        return {'s_why': '계획을 다시 찾지 못해 정리 (오래 보유)', 's_gain': gain}
    stop, tgt = pl['stop'], pl['tgt']
    bo, bl, bh = coin.get('bar_open'), coin.get('bar_low'), coin.get('bar_high')
    if bo is not None and bl is not None and bh is not None:   # 백테스트: 봉 안에서 손절 먼저 → 익절
        if bl <= stop:
            px = min(float(bo), stop)
            return {'s_why': '쌍바닥 아래 손절', 's_stop': stop, 's_target': tgt, 's_gain': (px / buy - 1) * 100, 's_fill': px}
        if bh >= tgt:
            px = max(float(bo), tgt)
            return {'s_why': '직전 고점 도달', 's_stop': stop, 's_target': tgt, 's_gain': (px / buy - 1) * 100, 's_fill': px}
        return None
    if price <= stop:
        return {'s_why': '쌍바닥 아래 손절', 's_stop': stop, 's_target': tgt, 's_gain': gain}
    if price >= tgt:
        return {'s_why': '직전 고점 도달', 's_stop': stop, 's_target': tgt, 's_gain': gain}
    return None
