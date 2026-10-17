"""
'순간 급등 · 일부 팔고 다시 사기' 검증 계산 — 봇(spike_rebuy.py)이 하는 순서 그대로 과거 1분봉에 대 봄

한 코인의 1분봉(오래된 순 [{t, o, h, l, c, v}], v = 1분 거래대금)에서
  1) 급등 감지: 방금 끝난 1분이 '순간 급등' 조건 (conditions._t_spike_pump 와 같은 계산)
  2) 일부 팔기: 그 뒤 최고가에서 pullback% 꺾이면 (30분 안에 안 꺾이면 그때) portion% 팜
  3) 나눠 다시 사기: 판 가격보다 dips% 싸질 때마다 한 몫씩 (24시간 넘으면 남은 몫을 그때 가격에)
  4) 끝나면 60분 쉬고 다시 봄
비용: 수수료 0.05%씩, 시장가로 사고팔 때 불리한 체결 0.2%씩 (예약 가격에 다시 살 때는 불리한 체결 없음)

결과: 한 건씩(판 가격·다시 산 가격·코인 수 변화)과, 처음에 가진 코인을 그냥 들고 있었을 때와의 가치 비교.
과거 계산이며 앞으로의 수익을 뜻하지 않는다.
"""
from typing import Dict, List, Optional

from .conditions import _t_spike_pump
from .spike_rebuy import DEFAULTS

FEE = 0.0005
SLIP = 0.002
AVG_WINDOW = 60
PULLBACK_WAIT = 30          # 분
COOLDOWN = 60               # 분


def _is_spike(bars: List[Dict], i: int, prefix: List[float], s: Dict) -> Optional[Dict]:
    if i < AVG_WINDOW:
        return None
    b = bars[i]
    hit = _t_spike_pump({'spike_value': b['v'], 'spike_avg': (prefix[i] - prefix[i - AVG_WINDOW]) / AVG_WINDOW,
                         'spike_open': b['o'], 'spike_close': b['c'], 'spike_close_1h': bars[i - AVG_WINDOW]['c']},
                        {'mult': s['mult'], 'rise': s['rise'], 'rise_1h': s['rise_1h']}, {})
    return hit


def simulate(bars: List[Dict], s: Optional[Dict] = None, fee: float = FEE, slip: float = SLIP) -> Dict:
    """한 코인 1분봉에 규칙을 대 봄 → {'events': [...], 'curve': [...], 'hold_end', 'rule_end'}"""
    s = {**DEFAULTS, **(s or {})}
    dips = sorted(float(d) for d in s['dips'])
    portion = float(s['portion']) / 100
    max_wait = int(float(s['max_wait_h']) * 60)
    n = len(bars)
    prefix = [0.0]
    for b in bars:
        prefix.append(prefix[-1] + (b['v'] or 0))

    # 가치 비교: 처음에 100만 원어치 코인을 가졌다고 봄
    start_price = bars[0]['c'] if bars else 0
    q0 = 1_000_000 / start_price if start_price else 0
    q, cash = q0, 0.0
    events, curve = [], []
    step = max(1, n // 600)                 # 그래프 점은 600개 안팎
    i, free_at = AVG_WINDOW, -1

    def sample(k):
        if k % step == 0 or k == n - 1:
            c = bars[k]['c']
            curve.append({'t': bars[k]['t'], 'rule': q * c + cash, 'hold': q0 * c})

    for k in range(min(AVG_WINDOW, n)):
        sample(k)
    while i < n:
        hit = _is_spike(bars, i, prefix, s) if i > free_at else None
        if not hit:
            sample(i)
            i += 1
            continue
        # 2) 꺾이면 팔기
        peak = bars[i]['c']
        j, price = i + 1, None
        while j < n and j - i <= PULLBACK_WAIT:
            x = bars[j]
            level = peak * (1 - s['pullback'] / 100)
            if x['l'] <= level:
                price = min(level, x['o'])
                break
            peak = max(peak, x['h'])
            j += 1
        if price is None:
            if j >= n:
                break
            price = bars[j]['c']
        for k in range(i, j):
            sample(k)
        sold_qty = q * portion
        got = sold_qty * price * (1 - slip) * (1 - fee)
        q -= sold_qty
        cash += got
        ev = {'signal_i': i, 'signal_t': bars[i]['t'], 'mult': hit['s_spike_mult'], 'rise': hit['s_spike_rise'],
              'rise_1h': hit['s_spike_rise_1h'], 'sell_i': j, 'sell_t': bars[j]['t'], 'sell_price': price,
              'peak': peak, 'buys': [], 'how': 'dip'}
        # 3) 나눠 다시 사기
        lad = rebuy_ladder(bars, j, price, got, dips, max_wait, fee, slip, on_bar=sample)
        ev['buys'] = lad['buys']
        if lad['open']:                     # 자료가 끝남: 아직 다시 사기를 기다리는 중
            ev['open'] = True
            q += lad['bought']
            cash -= lad['spent']
            events.append({**ev, 'coin_change': None})
            break
        ev['how'] = lad['how']
        bought = lad['bought']
        q += bought
        cash -= got
        ev['coin_change'] = (bought / sold_qty - 1) * 100 if sold_qty else 0.0
        ev['end_i'] = ev['buys'][-1]['i']
        events.append(ev)
        free_at = ev['end_i'] + COOLDOWN
        i = ev['end_i'] + 1
    last = bars[-1]['c'] if bars else 0
    return {'events': events, 'curve': curve, 'hold_end': q0 * last, 'rule_end': q * last + cash,
            'start_value': 1_000_000, 'bars': n}


def rebuy_ladder(bars: List[Dict], j: int, price: float, got: float, dips: List[float], max_wait: int,
                 fee: float = FEE, slip: float = SLIP, on_bar=None) -> Dict:
    """j봉에 price(수수료 전)로 팔아 got원을 받은 뒤: 판 가격보다 dips% 싸질 때마다 한 몫씩 (예약 가격, 불리한 체결 없음),
    max_wait 분이 지나면 남은 몫을 그 봉 끝 가격에. → {bought(코인 수), spent, buys, how, open}"""
    n = len(bars)
    part = got / len(dips)
    left = [price * (1 - d / 100) for d in dips]
    bought, buys = 0.0, []
    k = j + 1
    while left and k < n and k - j <= max_wait:
        x = bars[k]
        while left and x['l'] <= left[0]:
            lv = left.pop(0)
            pay = min(lv, x['o'])
            bought += part / (pay * (1 + fee))
            buys.append({'i': k, 't': x['t'], 'price': pay, 'why': 'dip'})
        if on_bar:
            on_bar(k)
        k += 1
    if left and k >= n:
        return {'bought': bought, 'spent': part * (len(dips) - len(left)), 'buys': buys, 'how': 'open', 'open': True}
    how = 'dip'
    if left:
        k -= 1
        x = bars[k]
        pay = x['c'] * (1 + slip)
        bought += part * len(left) / (pay * (1 + fee))
        buys.append({'i': k, 't': x['t'], 'price': pay, 'why': 'timeout', 'parts': len(left)})
        how = 'timeout' if len(left) == len(dips) else 'mixed'
    return {'bought': bought, 'spent': got, 'buys': buys, 'how': how, 'open': False}


def random_baseline(bars: List[Dict], s: Optional[Dict] = None, every: int = 1440, fee: float = FEE, slip: float = SLIP) -> List[float]:
    """비교용: 급등과 상관없이 하루 한 번(같은 시각) 팔고 같은 방법으로 다시 사면 판 몫의 코인 수가 몇 % 변했나"""
    s = {**DEFAULTS, **(s or {})}
    dips = sorted(float(d) for d in s['dips'])
    max_wait = int(float(s['max_wait_h']) * 60)
    out, i = [], AVG_WINDOW
    while i < len(bars) - 1:
        price = bars[i + 1]['o']
        got = price * (1 - slip) * (1 - fee)
        lad = rebuy_ladder(bars, i + 1, price, got, dips, max_wait, fee, slip)
        if lad['open']:
            break
        out.append((lad['bought'] - 1) * 100)
        i = max(i + every, lad['buys'][-1]['i'] + 1)
    return out


def summarize(events: List[Dict]) -> Dict:
    g = [e['coin_change'] for e in events if e.get('coin_change') is not None]
    if not g:
        return {'n': 0, 'more': 0, 'more_pct': 0.0, 'avg': 0.0, 'worst': 0.0, 'best': 0.0}
    return {'n': len(g), 'more': sum(x > 0 for x in g), 'more_pct': sum(x > 0 for x in g) / len(g) * 100,
            'avg': sum(g) / len(g), 'worst': min(g), 'best': max(g)}


def candles_around(bars: List[Dict], ev: Dict, before: int = 90, after: int = 60, max_candles: int = 240) -> Dict:
    """캔들차트용: 급등 90분 전 ~ 다시 사기 끝 60분 뒤. 길면 몇 분씩 묶음 → {'minutes', 'candles': [{t,o,h,l,c}]}"""
    a = max(0, ev['signal_i'] - before)
    b = min(len(bars) - 1, ev.get('end_i', ev['sell_i']) + after)
    span = b - a + 1
    m = 1
    for size in (1, 3, 5, 15, 30, 60):
        m = size
        if span / size <= max_candles:
            break
    out = []
    for k in range(a, b + 1, m):
        chunk = bars[k:min(b + 1, k + m)]
        out.append({'t': chunk[0]['t'], 'o': chunk[0]['o'], 'h': max(x['h'] for x in chunk),
                    'l': min(x['l'] for x in chunk), 'c': chunk[-1]['c']})
    return {'minutes': m, 'candles': out}


def diagnose(bars: List[Dict], s: Optional[Dict] = None) -> Dict:
    """급등이 없었을 때 '왜'를 보여주려고: 조건마다 몇 번(1분) 맞았는지, 가장 컸던 값"""
    s = {**DEFAULTS, **(s or {})}
    prefix = [0.0]
    for b in bars:
        prefix.append(prefix[-1] + (b['v'] or 0))
    d = {'value': 0, 'rise': 0, 'both': 0, 'all': 0, 'max_mult': 0.0, 'max_rise': 0.0, 'max_rise_1h_at_both': None}
    for i in range(AVG_WINDOW, len(bars)):
        b = bars[i]
        avg = (prefix[i] - prefix[i - AVG_WINDOW]) / AVG_WINDOW
        big = (b['v'] or 0) >= 10_000_000
        mult = (b['v'] or 0) / avg if avg > 0 else 0.0
        rise = (b['c'] / b['o'] - 1) * 100 if b['o'] else 0.0
        ok_v = big and mult >= s['mult']
        ok_r = rise >= s['rise']
        if big:
            d['max_mult'] = max(d['max_mult'], mult)
        d['max_rise'] = max(d['max_rise'], rise)
        d['value'] += ok_v
        d['rise'] += ok_r
        if ok_v and ok_r:
            d['both'] += 1
            ago = bars[i - AVG_WINDOW]['c']
            r1h = (b['c'] / ago - 1) * 100 if ago else 0.0
            d['max_rise_1h_at_both'] = r1h if d['max_rise_1h_at_both'] is None else max(d['max_rise_1h_at_both'], r1h)
            d['all'] += r1h >= s['rise_1h']
    return d

