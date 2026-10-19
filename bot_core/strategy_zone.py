"""
수요 구역 되돌림 — StraBot 조건 한 쌍 (사기 · 팔기), 시뮬레이션에서 지켜보기용 (2026-10-18, 같은 날 저녁 보강)

생각 (유튜브 TradingLab 'The Only Trading Strategy You'll Ever Need'): 옆으로 조용히 머물다 한 봉이 크게 치솟은 자리
(치솟기 직전 봉의 저가~고가 = '수요 구역')에는 사겠다는 사람이 많았으니, 가격이 그 구역으로 되돌아오면 다시 살 사람이 온다고 본다.
구역 윗선 근처에서 사고, 치솟은 뒤 최고가(목표)에 팔며, 구역 아래로 깨지면 판다. 영상은 '상승 추세(시장 구조)에서만' 사라고 한다.

검증 (strahub-research lab/methods/zone_retest · zone_retest_v2, 1시간봉 245코인 2022~2026-10):
  구역 윗선에 지정가로 바로 체결되면 거래당 +0.5% (아무 때나 산 대조 0.0%), 체결이 0.2% 밀리면 +0.24%, 0.5% 밀리면 0,
  닿은 봉 다음 시가에 사면 −0.08%.  즉 우위는 '되돌아온 순간 바로 사는 것'에서만 나오고, 코인당 1년 4번꼴.
  상승 추세 조건은 거래 수를 줄일 뿐 거래당 %는 같았다 → trend 는 선택(기본 끔). 4시간봉 추세 · ATR 손절 · 고정 2.5R · 보유 한도는 도움 안 됨.

봉: 규칙이 고른 시간 봉(1시간 · 4시간 · 일봉)의 '끝난 봉' (d['day_*_long'], 120개) + 지금 가격.
  시가가 없어 '그 봉 시가 = 앞 봉 종가'로 본다 (코인은 24시간 거래라 거의 같음).

사기 (zone_buy): 끝난 봉 중 '직전 quiet 봉의 폭 ≤ impulse% 뒤 한 봉이 impulse% 이상 오르고 직전 고가 위에서 마감'한 봉을 찾아
  그 직전 봉의 저가~고가를 구역으로. 구역은 (1) 그 뒤 종가가 구역 아래로 내려간 적이 없고 (2) expire 봉 안이어야 살아 있음.
  지금 가격이 살아 있는 구역의 아랫선 이상 · 윗선 +band% 이하면 '되돌아옴'. 익절 목표 = 치솟은 뒤 최고가,
  (목표 − 지금) / (지금 − 아랫선 × 0.999) 가 rr 이상일 때만 신호. 여러 구역이면 가장 최근 것.
  trend=1 이면 시장 구조가 상승 추세일 때만 (k봉 좌우 흔들림 고점 · 저점, 저점은 직전 고점을 종가가 넘어야 유효).
팔기 (zone_sell): 봇이 산 코인에 대해
  (1) 지금 가격이 목표(산 뒤로 끝난 봉은 빼고, 치솟은 뒤 최고가) 이상 → 익절
  (2) 마지막 끝난 봉의 종가가 산 값이 들어 있던 구역의 아랫선 아래 → 구역 깨짐 (손절). 지금 가격이 아니라 봉 종가로 보는 이유:
      가격이 잠깐 아랫선을 스치면 팔고, 되돌아오면 또 사는 되풀이가 났음(2026-10-18 시뮬레이션 하루 47거래). 종가로 깨지면
      zones() 도 그 구역을 버리므로 다시 사지 않는다 — 검증(probe55)과 같은 규칙
  (3) trend=1 이면 시장 구조가 하락 추세로 바뀜 → 추세 꺾임
  구역을 못 찾으면(오래돼 만료 등) 신호 없음 — 큰 손절은 봇 안전장치(손절 %)가 맡는다. 템플릿 기본 손절 2 · 익절 6.
"""
from typing import Dict, List, Optional, Tuple

P = {'impulse': 3.0, 'quiet': 6, 'expire': 150, 'band': 0.5, 'rr': 2.5, 'trend': 0, 'k': 5}
TF_SEC = {'1m': 60, '5m': 300, '10m': 600, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, 'day': 86400, 'w': 604800}


def _bars(d: Dict):
    h, l, c = d.get('day_highs_long'), d.get('day_lows_long'), d.get('day_closes_long')
    if not h or not l or not c or len(c) < 12 or not (len(h) == len(l) == len(c)):
        return None
    o = [c[0]] + c[:-1]                                     # 시가 = 앞 봉 종가로 봄
    return o, list(h), list(l), list(c)


def _impulse_bars(bars, p: Dict, lo: int) -> List[int]:
    """치솟은 봉 번호 (최근 것 먼저), lo 이상만"""
    o, h, l, c = bars
    n = len(c)
    x = float(p.get('impulse', P['impulse'])) / 100
    q = int(p.get('quiet', P['quiet']))
    out = []
    for i in range(n - 1, max(q, lo) - 1, -1):
        if o[i] <= 0 or c[i] / o[i] - 1 < x or c[i] <= h[i - 1]:
            continue
        ref = c[i - 1]
        if ref <= 0 or (max(h[i - q:i]) - min(l[i - q:i])) / ref > x:      # 직전 q봉이 조용하지 않음
            continue
        if h[i - 1] <= l[i - 1]:
            continue
        out.append(i)
    return out


def zones(bars, p: Dict) -> List[Dict]:
    """살아 있는 수요 구역 (최근 것 먼저): {'low', 'high', 'ago', 'peak', 'i'}"""
    o, h, l, c = bars
    n = len(c)
    exp = int(p.get('expire', P['expire']))
    out = []
    for i in _impulse_bars(bars, p, n - 1 - exp):
        zl, zh = l[i - 1], h[i - 1]
        if any(c[j] < zl for j in range(i + 1, n)):                           # 그 뒤 종가가 구역 아래로 → 구역 죽음
            continue
        out.append({'low': zl, 'high': zh, 'ago': n - 1 - i, 'peak': max(h[i:]), 'i': i})
    return out


def trend(bars, k: int = 5) -> Optional[str]:
    """시장 구조 추세 — 'up' · 'down' · None(아직 모름).  k봉 좌우 흔들림 고점 · 저점, 저점은 그 앞 흔들림 고점을 종가가 넘어야 '유효'
    (strahub-research lab/methods/zone_retest._Trend 와 같은 규칙, 끝난 봉만)"""
    o, h, l, c = bars
    n = len(c)
    if n < 2 * k + 2:
        return None
    sh = [False] * n
    sl = [False] * n
    for j in range(k, n - k):
        sh[j] = h[j] >= max(h[j - k:j + k + 1])
        sl[j] = l[j] <= min(l[j - k:j + k + 1])
    t = None
    last_sh = last_sl = None
    pend_low = pend_low_ref = pend_high = pend_high_ref = None
    valid_low = valid_high = None
    for i in range(1, n):
        j = i - k
        if j >= 0:
            if sh[j]:
                last_sh = h[j]
                pend_high, pend_high_ref = h[j], last_sl
            if sl[j]:
                last_sl = l[j]
                pend_low, pend_low_ref = l[j], last_sh
        if pend_low is not None and pend_low_ref is not None and c[i] > pend_low_ref:
            valid_low = pend_low
            pend_low = None
            if t is None:
                t = 'up'
        if pend_high is not None and pend_high_ref is not None and c[i] < pend_high_ref:
            valid_high = pend_high
            pend_high = None
            if t is None:
                t = 'down'
        if valid_low is not None and c[i] < valid_low:
            t = 'down'
        if valid_high is not None and c[i] > valid_high:
            t = 'up'
    return t


def buy_test(coin: Dict, p: Dict, d: Dict) -> Optional[Dict]:
    b = _bars(d)
    if not b:
        return None
    price = float(coin.get('current_price') or 0)
    if price <= 0:
        return None
    band = float(p.get('band', P['band'])) / 100
    rr_min = float(p.get('rr', P['rr']))
    if int(p.get('trend', P['trend'])) and trend(b, int(p.get('k', P['k']))) != 'up':
        return None
    for z in zones(b, p):
        if not (z['low'] <= price <= z['high'] * (1 + band)):
            continue
        stop = z['low'] * 0.999
        if price <= stop or z['peak'] <= price:
            return None
        rr = (z['peak'] - price) / (price - stop)
        if rr < rr_min:
            return None
        return {'s_rr': rr, 's_zone_low': z['low'], 's_zone_high': z['high'], 's_target': z['peak'],
                's_ago': z['ago'], 's_up': (z['peak'] / price - 1) * 100, 's_down': (1 - stop / price) * 100}
    return None


def _bars_since(coin: Dict, p: Dict) -> int:
    """산 뒤로 끝난 봉 수 (산 시각과 봉 길이로 어림, 모르면 0)"""
    ts = coin.get('bot_opened_ts')
    now = coin.get('bot_now_ts')
    sec = TF_SEC.get(str(p.get('_tf') or '1h'), 3600)
    if not ts:
        return 0
    try:
        import time
        now = float(now) if now else time.time()
        return max(0, int((now - float(ts)) // sec))
    except (TypeError, ValueError):
        return 0


def sell_test(coin: Dict, p: Dict, d: Dict) -> Optional[Dict]:
    """목표(치솟은 뒤 최고가)에 닿으면 익절, 산 값이 들어 있던 구역의 아랫선 아래로 내려오면 손절, (trend=1) 추세가 꺾이면 판다"""
    buy = coin.get('bot_entry')
    b = _bars(d)
    if not b or not buy:
        return None
    price = float(coin.get('current_price') or 0)
    if price <= 0:
        return None
    band = float(p.get('band', P['band'])) / 100
    o, h, l, c = b
    n = len(c)
    gain = (price / float(buy) - 1) * 100
    if int(p.get('trend', P['trend'])) and trend(b, int(p.get('k', P['k']))) == 'down':
        return {'s_why': '추세 꺾임', 's_zone_low': None, 's_gain': gain}
    since = _bars_since(coin, p)
    # 구역 찾기: 산 값이 구역 안(윗선 +band)이었던 것 중 가장 최근 — '그 뒤 종가가 아래' 조건은 빼고 본다 (지금 그걸 확인하는 중이니)
    for i in _impulse_bars(b, p, 1):
        zl, zh = l[i - 1], h[i - 1]
        if not (zl <= float(buy) <= zh * (1 + band)):
            continue
        end = max(i + 1, n - since)                         # 산 뒤 봉은 목표 계산에서 뺀다 (산 때의 목표 그대로)
        target = max(h[i:end])
        if price >= target:
            return {'s_why': '목표(치솟은 뒤 최고가) 도달', 's_zone_low': zl, 's_target': target, 's_gain': gain}
        if c[-1] < zl:                                       # 끝난 봉 종가 기준 (지금 가격 아님)
            return {'s_why': '구역 아래로 마감', 's_zone_low': zl, 's_target': target, 's_gain': gain}
        return None
    return None
