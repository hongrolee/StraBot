"""
수요 구역 되돌림 — StraBot 조건 한 쌍 (사기 · 팔기), 시뮬레이션에서 지켜보기용 (2026-10-18, 같은 날 저녁 보강)

생각 (유튜브 TradingLab 'The Only Trading Strategy You'll Ever Need'): 옆으로 조용히 머물다 한 봉이 크게 치솟은 자리
(치솟기 직전 봉의 저가~고가 = '수요 구역')에는 사겠다는 사람이 많았으니, 가격이 그 구역으로 되돌아오면 다시 살 사람이 온다고 본다.
구역 윗선 근처에서 사고, 치솟은 뒤 최고가(목표)에 팔며, 구역 아래로 깨지면 판다. 영상은 '상승 추세(시장 구조)에서만' 사라고 한다.

영상과 같게 (2026-10-22, 이홍로 님 '영상과 동일해야 해'):
  ① 상승 추세에서만 산다 (영상이 '가장 중요한 단계'라 한 시장 구조) — 봇 규칙 기본 trend=1 (conditions.PRESETS)
  ② 급등 바로 앞 봉의 저가~고가 = 수요 구역, ③ 구역에 다시 들어오면 산다 — 구역마다 한 번만(처음 되돌아올 때, 검증과 같음)
  ④ 손절 = 구역 바로 아래(아랫선 −0.1%) · 익절 = 직전 고점(치솟은 뒤 최고가) — 둘 다 닿는 순간 (봉 마감을 기다리지 않음)
  ⑤ 손익비 2.5 이상만.  영상의 공매도(하락 추세 공급 구역)는 업비트 현물이라 없음
  봇 안전장치(손절 % · 익절 %)는 영상에 없으므로 템플릿에서 0(끔)

검증 (strahub-research lab/methods/zone_retest · zone_retest_v2, 1시간봉 245코인 2022~2026-10):
  구역 윗선에 지정가로 바로 체결되면 거래당 +0.5% (아무 때나 산 대조 0.0%), 체결이 0.2% 밀리면 +0.24%, 0.5% 밀리면 0,
  닿은 봉 다음 시가에 사면 −0.08%.  즉 우위는 '되돌아온 순간 바로 사는 것'에서만 나오고, 코인당 1년 4번꼴.
  상승 추세 조건은 거래 수를 줄일 뿐 거래당 %는 같았다 → trend 는 선택(기본 끔). 4시간봉 추세 · ATR 손절 · 고정 2.5R · 보유 한도는 도움 안 됨.

봉: 규칙이 고른 시간 봉(1시간 · 4시간 · 일봉)의 '끝난 봉' (d['day_*_long'], 120개) + 지금 가격.
  시가가 없어 '그 봉 시가 = 앞 봉 종가'로 본다 (코인은 24시간 거래라 거의 같음).

사기 (zone_buy): 끝난 봉 중 '직전 quiet 봉의 폭 ≤ impulse% 뒤 한 봉이 impulse% 이상 오르고 직전 고가 위에서 마감'한 봉을 찾아
  그 직전 봉의 저가~고가를 구역으로. 구역은 (1) 그 뒤 종가가 구역 아래로 내려간 적이 없고 (2) expire 봉 안이어야 살아 있음.
  (3) 처음 되돌아올 때만: 치솟은 뒤 끝난 봉 중 사는 값(윗선 등)까지 내려온 봉이 이미 있으면 그 구역은 쓴 것 (검증 lab 은 닿으면 구역을 지움).
  (4) 실제 봇: 이 봇이 같은 봉 안에서 이미 이 코인을 사고팔았으면(coin['bot_last_trade_ts']) 그 봉이 끝날 때까지 안 삼
      — 구역 아래로 찍혀 손절한 뒤 같은 봉에서 구역으로 돌아와 또 사는 되풀이를 막음 (2026-10-18 하루 47거래 사고)
  지금 가격이 살아 있는 구역의 아랫선 이상 · 윗선 +band% 이하면 '되돌아옴'. 익절 목표 = 치솟은 뒤 최고가,
  (목표 − 지금) / (지금 − 아랫선 × 0.999) 가 rr 이상일 때만 신호. 여러 구역이면 가장 최근 것.
  trend=1 이면 시장 구조가 상승 추세일 때만 (k봉 좌우 흔들림 고점 · 저점, 저점은 직전 고점을 종가가 넘어야 유효).
팔기 (zone_sell): 봇이 산 코인에 대해 (영상 · 검증과 같게 닿는 순간, 2026-10-22 — 전에는 봉 종가가 아랫선 아래일 때만)
  (1) 지금 가격이 산 값이 들어 있던 구역의 아랫선 −0.1% 이하 → 손절 (백테스트는 봉 저가, 시가가 이미 아래면 시가)
  (2) 지금 가격이 목표(산 뒤로 끝난 봉은 빼고, 치솟은 뒤 최고가) 이상 → 익절 (백테스트는 봉 고가, 시가가 이미 위면 시가)
  (3) trend=1 이면 시장 구조가 하락 추세로 바뀜 → 추세 꺾임 (영상에는 없음, 기본 끔)
  잠깐 찍고 돌아오는 되풀이 매수는 사기 쪽 (3) · (4)가 막는다.
  구역을 못 찾으면(오래돼 끝난 봉 120개 밖으로 밀림 등) 신호 없음.

사는 때 (entry, 2026-10-22 — StraHub 영상 '닿는 그 순간, 정말 살 수 있을까요?'의 세 가지 체결 가정을 고를 수 있게):
  0 닿는 순간      가격이 구역 윗선(+band%)까지 내려오면 바로. 검증의 '지정가, 닿으면 체결'(+0.5%)
  1 더 내려오면    윗선보다 depth% 더 내려와야 삼. 검증의 '0.2% 더 뚫은 봉만'(+0.24%)과 같은 깊이
  2 봉이 끝난 뒤   닿은 봉이 마감된 뒤 그다음 가격에. 검증의 '닿은 봉 다음 시가'(−0.08%)
  실제 봇은 1분마다 지금 가격으로 판단해 시장가로 산다. 백테스트(bot_core/backtest.py)는 걸음 봉과 규칙의 봉이 같으면
  coin['bar_open' · 'bar_low'](지금 봉의 시가 · 저가)를 넣어 주고, 여기서 봉 안에서 닿았는지 보고 그 값을 s_fill 로 돌려준다.
  윗선 여유(band) 기본은 0 — 윗선보다 비싸게 사면 '체결이 밀린 것'과 같아 우위가 줄어서 (0.5% 밀리면 0).
"""
from typing import Dict, List, Optional, Tuple

P = {'impulse': 3.0, 'quiet': 6, 'expire': 150, 'band': 0.0, 'rr': 2.5, 'trend': 0, 'k': 5, 'entry': 0, 'depth': 0.2}
ENTRY_TOUCH, ENTRY_DEEP, ENTRY_CLOSE = 0, 1, 2
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


def _bar_sec(p: Dict) -> int:
    return TF_SEC.get(str(p.get('_tf') or 'day'), 86400)       # run_condition 은 일봉이면 _tf 를 안 넣음


def _touched(l: List[float], a: int, b: int, level: float) -> bool:
    """끝난 봉 a ~ b-1 중 저가가 level 이하로 내려온 봉이 있나 (구역을 이미 쓴 것)"""
    return any(l[j] <= level for j in range(max(0, a), b))


def buy_test(coin: Dict, p: Dict, d: Dict) -> Optional[Dict]:
    b = _bars(d)
    if not b:
        return None
    price = float(coin.get('current_price') or 0)
    if price <= 0:
        return None
    band = float(p.get('band', P['band'])) / 100
    depth = max(0.0, float(p.get('depth', P['depth']))) / 100
    rr_min = float(p.get('rr', P['rr']))
    entry = int(float(p.get('entry', P['entry']) or 0))
    if int(p.get('trend', P['trend'])) and trend(b, int(p.get('k', P['k']))) != 'up':
        return None
    bar_open, bar_low = coin.get('bar_open'), coin.get('bar_low')     # 백테스트만: 지금 봉(걸음 봉)의 시가 · 저가
    intrabar = bar_open is not None and bar_low is not None
    last = coin.get('bot_last_trade_ts')                               # 실제 봇만: 이 봇이 이 코인을 마지막으로 사고판 시각
    if last and not intrabar:
        import time
        sec = _bar_sec(p)
        if int(float(last) // sec) == int(float(coin.get('bot_now_ts') or time.time()) // sec):
            return None                                     # 같은 봉 안에서 이미 사고팖 → 봉이 끝날 때까지 안 삼
    o, h, l, c = b
    n = len(c)
    for z in zones(b, p):
        zl, zh = z['low'], z['high']
        if entry == ENTRY_CLOSE:
            if intrabar:            # 백테스트: 지금 봉이 닿은 봉 → 그 종가(≈ 다음 봉 시가)에 삼
                if _touched(l, z['i'] + 1, n, zh * (1 + band)) or not (bar_low <= zh * (1 + band) and price >= zl):
                    continue
            elif not (z['i'] < n - 1 and l[-1] <= zh * (1 + band) and c[-1] >= zl   # 실제 봇: 방금 끝난 봉(치솟은 봉 뒤)이 처음 닿고 구역 위에서 마감
                      and not _touched(l, z['i'] + 1, n - 1, zh * (1 + band))):
                continue
            fill = price
        else:
            trig = zh * (1 + band) if entry == ENTRY_TOUCH else zh * (1 - depth)       # 이 값까지 내려오면 삼
            if trig < zl:                                    # 구역이 '더 내려오는 폭'보다 얇음
                continue
            if _touched(l, z['i'] + 1, n, trig):             # 끝난 봉이 이미 이 값까지 왔었음 = 구역을 쓴 것 (처음 되돌아올 때만)
                continue
            if intrabar:            # 백테스트: 봉 안에서 저가가 trig 에 닿았으면 trig(이미 아래서 열렸으면 시가)에 산 것으로
                if bar_low > trig or bar_open < zl:
                    continue
                fill = min(bar_open, trig)
            else:
                if not (zl <= price <= trig):
                    continue
                fill = price
        stop = zl * 0.999
        if fill <= stop or z['peak'] <= fill:
            return None
        rr = (z['peak'] - fill) / (fill - stop)
        if rr < rr_min:
            return None
        out = {'s_rr': rr, 's_zone_low': zl, 's_zone_high': zh, 's_target': z['peak'],
               's_ago': z['ago'], 's_up': (z['peak'] / fill - 1) * 100, 's_down': (1 - stop / fill) * 100}
        if intrabar:
            out['s_fill'] = fill
        return out
    return None


def _bars_since(coin: Dict, p: Dict) -> int:
    """산 뒤로 끝난 봉 수 (산 시각과 봉 길이로 어림, 모르면 0)"""
    ts = coin.get('bot_opened_ts')
    now = coin.get('bot_now_ts')
    sec = _bar_sec(p)
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
    buy = float(buy)
    # 구역 찾기: 산 값이 구역 안(윗선 +band)이었던 것 중 가장 최근 — '그 뒤 종가가 아래' 조건은 빼고 본다 (지금 그걸 확인하는 중이니).
    # 없으면 '봉이 끝난 뒤' 사서 윗선보다 위에서 산 경우: 산 때보다 앞에 치솟았고, 산 값이 아랫선 ~ 그 뒤 최고가 사이인 가장 최근 구역
    imp = _impulse_bars(b, p, 1)
    pick = next((i for i in imp if l[i - 1] <= buy <= h[i - 1] * (1 + band)), None)
    if pick is None:
        pick = next((i for i in imp if i < n - since and l[i - 1] <= buy < max(h[i:max(i + 1, n - since)])), None)
    if pick is None:
        return None
    zl = l[pick - 1]
    target = max(h[pick:max(pick + 1, n - since)])          # 산 뒤 봉은 목표 계산에서 뺀다 (산 때의 목표 그대로)
    stop = zl * 0.999                                        # 영상: 구역 바로 아래 (검증 lab 과 같은 −0.1%)
    bo, bl, bh = coin.get('bar_open'), coin.get('bar_low'), coin.get('bar_high')   # 백테스트만: 지금 봉 시가 · 저가 · 고가
    if bo is not None and bl is not None and bh is not None:
        # 백테스트: 봉 안에서 먼저 손절(불리한 쪽 먼저, 검증과 같은 순서) → 익절
        if bl <= stop:
            px = min(bo, stop)
            return {'s_why': '구역 아래로 (손절)', 's_zone_low': zl, 's_target': target, 's_gain': (px / buy - 1) * 100, 's_fill': px}
        if bh >= target:
            px = max(bo, target)
            return {'s_why': '목표(치솟은 뒤 최고가) 도달', 's_zone_low': zl, 's_target': target, 's_gain': (px / buy - 1) * 100, 's_fill': px}
        return None
    if price <= stop:
        return {'s_why': '구역 아래로 (손절)', 's_zone_low': zl, 's_target': target, 's_gain': gain}
    if price >= target:
        return {'s_why': '목표(치솟은 뒤 최고가) 도달', 's_zone_low': zl, 's_target': target, 's_gain': gain}
    return None
