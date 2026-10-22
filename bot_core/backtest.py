"""
봇 백테스트 — 저장한 봇의 매수·매도 규칙을 한 코인의 과거 캔들로 다시 돌려 봄 (파일 쓰기 · 실제 시계 없음)

실제 봇(bot_engine.py)과 같게 판단한다:
- 조건 판정은 같은 함수 (conditions.resolve · run_condition), 규칙의 시간 봉(tf: 일봉·4시간·1시간)도 같게
- 매수: 첫 확인은 기준만 잡고(지금 맞는 건 사지 않음, '코인 성격' 조건은 예외), 이후 '안 맞다가 맞게 된' 순간 삼
- 매도: 손절 → 익절 → 추적 손절 → 매도 조건 순. 손절·익절·추적은 봉 안의 저가·고가로 먼저 닿았는지 봄
- 금액: RiskManager.calculate_position_size, 수수료 0.05% + 미끄러짐 0.1%
걸음 = 사용자가 고른 봉 하나 (그 봉이 끝나는 순간 한 번 확인)

급등 때 일부 팔기 · 다시 사기(순간 급등 · 하루 급등)는 1분봉으로 실제 봇(spike_rebuy.SpikeRebuy)과 같은 순서로 돌린다.
'급등이 잦던 코인'은 그때의 가격 · 거래대금 + 상장일(그때 기준) · 바이낸스 여부(지금 기준)로 판정한다.
과거 캔들로 재현할 수 없는 규칙은 빼고(SKIPPED) 결과에 알려 준다.
"""
import bisect
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bot_core import conditions as cd
from bot_core import spike_rebuy as sr
from bot_core.bot_engine import rule_list, rule_match, REFILL_COOLDOWN_H
from bot_core.risk_manager import RiskManager, MIN_ORDER_INTERVAL_SEC
from bot_core.candle_store import UNITS

FEE = 0.0005
SLIP = 0.001
REFILL_SEC = REFILL_COOLDOWN_H * 3600     # 실제 봇: 판 코인은 24시간 뒤에야 '코인 성격' 조건으로 다시 삼
# 규칙의 계산 봉 → 캔들 단위 (1분 ~ 월봉 10가지, 실제 봇도 같은 봉을 받음 — web_dashboard.TF_UNIT)
TF_UNIT = {'1m': '1', '5m': '5', '10m': '10', '15m': '15', '30m': '30', '1h': '60', '4h': '240', 'day': 'day', 'w': 'week', 'M': 'month'}
TF_SEC = {'1m': 60, '5m': 300, '10m': 600, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, 'day': 86400, 'w': 604800, 'M': 31 * 86400}
WARMUP_BARS = 202                      # 판단 봉 199개(긴 기간 조건 120 · EMA 120 용 199) + 여유
WEEKS_BACK = 105
CURVE_MAX = 1500

SKIP_WHY = {
    'value_top': '그때 전체 코인의 거래대금 순위를 알 수 없어 이번 백테스트에서는 빠졌습니다',
    'stronger_than_btc': '비트코인 캔들을 함께 받아야 해서 이번 백테스트에서는 빠졌습니다',
    'buy_flow_spike': '실시간 체결(매수·매도 비율)로 판단하는 조건이라 과거 캔들로는 재현할 수 없습니다',
    'spike_pump': '매수 조건으로 쓴 순간 급등은 실시간 1분 체결로 판단해 이번 백테스트에서는 빠졌습니다',
    'pre_spike': '실시간 1분 체결로 판단하는 조건이라 이번 백테스트에서는 빠졌습니다',
}
BAR_KEYS = ('bar_open', 'bar_low', 'bar_high')     # 걸음 봉의 시가 · 저가 · 고가 — 수요 구역이 봉 안에서 닿았는지 봄 (2026-10-22)
MINUTE_MAX_DAYS = 90                   # 1분봉이 필요한 봇(급등 일부 팔기)은 90일까지
KIND_TEXT = {'spike': {'name': '급등', 'sell': '급등 때 일부 팔기', 'rebuy': '급등 뒤 다시 사기'},
             'surge': {'name': '하루 급등', 'sell': '하루 급등 때 일부 팔기', 'rebuy': '하루 급등 뒤 다시 사기'}}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def plan(bot: Dict, custom: List[Dict]) -> Dict:
    """돌릴 수 있는 규칙 · 빠지는 규칙 · 어림하는 규칙(notes) · 필요한 시간 봉 · 1분봉이 필요한지"""
    rules = bot.get('trade_rules') or {}
    out = {'buy': [], 'sell': [], 'skipped': [], 'notes': [], 'tfs': {'day'}, 'partial': [], 'minute': False}
    if rule_list(rules, 'target') or rules.get('only'):
        out['notes'].append({'side': 'buy', 'title': '매매대상',
                             'note': '백테스트는 고른 코인 하나로 돌리므로 매매대상(코인 성격 · 직접 고른 코인)은 적용하지 않습니다'})
    for side in ('buy', 'sell'):
        for rule in rule_list(rules, side):
            if side == 'sell' and sr.is_partial_rule(rule):
                kind = 'spike' if sr.is_spike_rule(rule) else 'surge'
                if kind not in out['partial']:
                    out['partial'].append(kind)
                    out['minute'] = True
                    out['notes'].append({'side': side, 'title': KIND_TEXT[kind]['sell'] + ' · 다시 사기',
                                         'note': '1분봉으로 실제 봇과 같은 순서(감지 → 꺾이면 일부 팔기 → 싸지면 나눠 다시 사기)로 돌립니다. '
                                                 '실제 봇은 5초마다 봐서 체결 가격이 조금 다를 수 있습니다'})
                continue
            cond = cd.resolve(rule['id'], custom)
            if not cond:
                out['skipped'].append({'side': side, 'title': rule['id'], 'why': '조건을 찾지 못했습니다 (지워진 내 조건일 수 있습니다)'})
                continue
            why = SKIP_WHY.get(cond['id']) or ('실시간 체결로 판단하는 조건이라 빠졌습니다' if cond.get('needs_flow') else None)
            if why:
                out['skipped'].append({'side': side, 'title': cond['title'], 'why': why})
                continue
            if cond['id'] == 'zone_buy':
                out['notes'].append({'side': side, 'title': cond['title'],
                                     'note': '백테스트 봉이 규칙의 봉과 같으면 봉 안에서 가격이 닿았는지 보고, 닿는 순간 · 더 내려오면은 그 값에, '
                                             '봉이 끝난 뒤는 그 봉 종가에 산 것으로 봅니다. 봉이 다르면 봉이 끝날 때 가격으로 봅니다. '
                                             '실제 봇은 1분마다 지금 가격으로 보고 시장가로 삽니다'})
            if cond['id'] == 'spike_prone':
                out['notes'].append({'side': side, 'title': cond['title'],
                                     'note': '그때의 가격 · 하루 거래대금과 그때까지의 상장 기간으로 판정합니다 (바이낸스 상장 여부는 지금 기준)'})
            tf = (rule.get('params') or {}).get('tf') or 'day'
            out['tfs'].add(tf if tf in TF_SEC else 'day')
            htf = (rule.get('params') or {}).get('htf')
            if htf:
                out['tfs'].add(htf if htf in TF_SEC else 'day')     # 두 봉을 함께 보는 규칙의 위 봉
            out[side].append((rule, cond))
    return out


def needed(bot: Dict, custom: List[Dict], unit: str, start: int, end: int) -> List[tuple]:
    """받아야 할 캔들 [(unit, start, end)] — 걸음 봉 + 판단 봉(앞쪽 준비분) + 1시간봉 + 주봉"""
    p = plan(bot, custom)
    want = {unit: (start, end)}
    for tf in p['tfs']:
        u = TF_UNIT[tf]
        s = start - TF_SEC[tf] * WARMUP_BARS
        a, b = want.get(u, (s, end))
        want[u] = (min(a, s), max(b, end))
    a, b = want.get('60', (start - 2 * 86400, end))
    want['60'] = (min(a, start - 2 * 86400), max(b, end))
    want['week'] = (start - 604800 * WEEKS_BACK, end)
    if p['minute']:                                   # 급등 일부 팔기: 1분봉 (평소 거래대금 60분 + 여유)
        a, b = want.get('1', (start - 2 * 3600, end))
        want['1'] = (min(a, start - 2 * 3600), max(b, end))
    return [(u, a, b) for u, (a, b) in want.items()]


class _Series:
    """오래된 순 봉 [[t,o,h,l,c,v]] + 빠른 찾기"""

    def __init__(self, rows: List[list], sec: int):
        self.rows = rows
        self.sec = sec
        self.t = [r[0] for r in rows]
        self.end = [r[0] + sec for r in rows]

    def done_before(self, when: float) -> int:
        """when 이전에 끝난 봉 개수 (rows[:k])"""
        return bisect.bisect_right(self.end, when)

    def since(self, a: float, b: float) -> List[list]:
        """시작 시각이 [a, b] 인 봉"""
        return self.rows[bisect.bisect_left(self.t, a):bisect.bisect_right(self.t, b)]


def _period(rows: List[list]) -> Dict:
    """끝난 봉(오래된 순, 최대 120개) → 조건 계산 칸: 30개 칸(기존 조건) + 긴 기간 칸(2026-10 추가 조건) — 서버의 기간 시세와 같은 모양"""
    short = rows[-30:]
    return {'day_closes': [r[4] for r in short], 'day_highs': [r[2] for r in short], 'day_values': [r[5] for r in short],
            'day_lows': [r[3] for r in short],
            'day_closes_long': [r[4] for r in rows[-120:]], 'day_highs_long': [r[2] for r in rows[-120:]],
            'day_lows_long': [r[3] for r in rows[-120:]], 'day_values_long': [r[5] for r in rows[-120:]],
            'day_closes_xl': [r[4] for r in rows[-199:]]}


def run(bot: Dict, market: str, unit: str, start: int, end: int, candles: Dict[str, List[list]],
        custom: Optional[List[Dict]] = None, krw: Optional[float] = None, facts=None, start_held: bool = False,
        fee: Optional[float] = None, slip: Optional[float] = None) -> Dict:
    """facts(currency, date) → {'listed_days', 'on_binance'} ('급등이 잦던 코인' 판정용, 없으면 모름)
    start_held: 기간 처음에 시작 금액 전부로 이 코인을 산 채로 출발 (보유코인 분석 '고른 코인에 봇을 적용했다면')
      — 그 뒤 봇의 매도 규칙 · 급등 일부 팔기 · 다시 사기 · 매수 조건을 그대로. 다 판 뒤 다시 살 때는 가진 원화 전부(처음 금액만큼)로"""
    custom = custom or []
    # 수수료 · 미끄럼 (비율, 0.0005 = 0.05%) — 안 주면 기본값(업비트 수수료 0.05% · 미끄럼 0.1%), 0~5% 사이로
    fee_r = FEE if fee is None else min(max(float(fee), 0.0), 0.05)
    slip_r = SLIP if slip is None else min(max(float(slip), 0.0), 0.05)
    p = plan(bot, custom)
    rules = bot.get('trade_rules') or {}
    buy_on = rules.get('buy_on', True) is not False            # 실제 봇과 같게: 매수 · 매도 끄기, 켤 때는 사지 않기
    sell_on = rules.get('sell_on', True) is not False
    start_wait = bool(rules.get('start_wait'))
    last_sell_ts = None
    rm = RiskManager(bot.get('risk_config') or {})
    step_sec = UNITS[unit][1]
    steps = [r for r in candles.get(unit, []) if start <= r[0] < end]
    if len(steps) < 2:
        raise ValueError('이 기간에 캔들이 거의 없습니다 (상장 전이거나 거래가 없던 기간일 수 있습니다)')
    series = {u: _Series(candles.get(u, []), UNITS[u][1]) for u in set(TF_UNIT.values()) | {'week', unit, '1'}}
    step_s = series[unit]
    v_prefix = [0.0]
    for r in step_s.rows:
        v_prefix.append(v_prefix[-1] + (r[5] or 0))

    start_krw = float(krw or bot.get('budget') or 1_000_000)
    cash = start_krw
    pos = None                         # {'qty', 'entry', 'cost', 'reason', 'peak'}
    max_invested = 0.0                 # 가장 많이 들어가 있던 돈 (투입 대비 수익률용)
    machines = {'spike': sr.SpikeRebuy(None), 'surge': sr.SpikeRebuy(None, cooldown_sec=sr.SURGE_COOLDOWN_SEC)}
    psets = {'spike': sr.settings(rules), 'surge': sr.surge_settings(rules)}
    mins = series['1']
    m_prefix = [0.0]
    for r in mins.rows:
        m_prefix.append(m_prefix[-1] + (r[5] or 0))
    trades: List[Dict] = []
    events: List[Dict] = []            # 봇이 본 일 (급등 감지 · 건너뜀 · 지켜보기 끝) — 사고팔지 않은 판단도 보여 주려고
    curve: List[Dict] = []
    baselines: Dict[tuple, bool] = {}
    last_order = None
    daily = {'day': None, 'loss': 0.0, 'base': None}
    cur = market.replace('KRW-', '')
    include_stable = bool(rules.get('include_stable'))
    excluded = cur in {str(x).strip().upper().replace('KRW-', '') for x in (rules.get('exclude') or [])}
    first_close = steps[0][4]
    same = {'qty': 0.0, 'cost': 0.0}   # 비교용: 봇의 첫 매수와 같은 때 같은 금액을 사서 끝까지 들고 있기
    peak_value = start_krw
    max_dd = 0.0

    run_day = {'key': None, 'low': None, 'high': None}       # 오늘(KST 09시~) 지금까지 저가·고가
    run_hour = {'key': None, 'low': None}                    # 이번 1시간 지금까지 저가

    def snapshot(i_row: list, held: bool, next_t0: Optional[float] = None) -> tuple:
        """그 걸음이 끝나는 순간(D)의 코인 시세 한 줄 · 기간 시세(일봉) · 1시간봉
        next_t0 = 다음 걸음 봉 시작 (월봉처럼 길이가 달마다 다른 봉: 31일로 치면 짧은 달에 다음 달 일봉을 미리 봄, 2026-10-17)"""
        t0, o, h, l, c, v = i_row
        D = (next_t0 - 1) if (next_t0 and step_sec > 7 * 86400) else t0 + step_sec - 1
        day_s = series['day']
        k = day_s.done_before(D)
        past = day_s.rows[max(0, k - 199):k]              # 끝난 일봉 (오래된 순, 긴 기간 칸 120 · EMA 용 199개까지)
        day_start = D // 86400 * 86400                      # 업비트 일봉 = KST 09시 (UTC 0시)
        if run_day['key'] != day_start:
            run_day.update(key=day_start, low=l, high=h)
        else:
            run_day.update(low=min(run_day['low'], l), high=max(run_day['high'], h))
        today_low, today_high = run_day['low'], run_day['high']
        prev_close = past[-1][4] if past else None
        # 주봉: 끝난 주 + 이번 주(끝난 일봉 + 오늘까지)
        wk = series['week']
        kw = wk.done_before(D)
        weeks_done = wk.rows[max(0, kw - 104):kw]
        week_start = weeks_done[-1][0] + 604800 if weeks_done else day_start
        this_week = [r for r in past if r[0] >= week_start]
        wk_hi = max([r[2] for r in this_week] + [today_high])
        wk_lo = min([r[3] for r in this_week] + [today_low])
        weeks_newest = [(wk_hi, wk_lo)] + [(r[2], r[3]) for r in reversed(weeks_done)]

        def min_low(rows):
            vals = [x for x in rows if x]
            return min(vals) if vals else None
        rev = list(reversed(past))
        d = {**_period(past),
             'd1': min_low([r[3] for r in rev[:1]]), 'w1': min_low([r[3] for r in rev[:6]]), 'm1': min_low([r[3] for r in rev[:29]]),
             'h6': min_low([x[1] for x in weeks_newest[:26]]), 'y1': min_low([x[1] for x in weeks_newest[:52]]),
             'week_highs': [x[0] for x in reversed(weeks_newest[:104])],
             'week_closes': [r[4] for r in weeks_done[-103:]]}
        # 1시간봉 24개: 끝난 봉 + 지금 봉(현재가)
        hs = series['60']
        kh = hs.done_before(D)
        hours = hs.rows[max(0, kh - 23):kh]
        hour_start = D // 3600 * 3600
        if step_sec >= 3600:
            now_hour = [r for r in hs.since(hour_start, hour_start)]
            h_low = now_hour[0][3] if now_hour else l
        else:
            if run_hour['key'] != hour_start:
                run_hour.update(key=hour_start, low=l)
            else:
                run_hour['low'] = min(run_hour['low'], l)
            h_low = run_hour['low']
        hourly = {'t': [r[0] for r in hours] + [hour_start], 'c': [r[4] for r in hours] + [c], 'l': [r[3] for r in hours] + [min(h_low, c)]}
        j = bisect.bisect_right(step_s.t, D)
        a = bisect.bisect_right(step_s.t, D - 86400)
        if step_sec <= 86400:
            value_24h = v_prefix[j] - v_prefix[a]
        else:
            # 주봉 · 월봉 걸음: 봉 하나가 하루보다 길어 위 식은 늘 0 → 1시간봉 24개 합, 없으면 걸음 봉 거래대금을 하루치로 (2026-10-17)
            day_rows = hs.since(D - 86400 + 1, D)
            value_24h = sum((r[5] or 0) for r in day_rows) if day_rows else (v_prefix[j] - v_prefix[j - 1] if j else 0) * 86400 / step_sec
        coin = {'currency': cur, 'korean_name': '', 'current_price': c, 'prev_close': prev_close,
                'change_rate': (c / prev_close - 1) * 100 if prev_close else 0.0, 'today_low': min(today_low, c),
                'trade_value_24h': value_24h, 'warning': False, 'notices': [], 'held': held,
                'listed_days': None, 'on_binance': None, 'bar_open': o, 'bar_low': l, 'bar_high': h}
        if facts:
            try:
                coin.update(facts(cur, datetime.fromtimestamp(D + 9 * 3600, timezone.utc).date()))
            except Exception:
                pass
        return D, coin, d, hourly

    def judge(rule: Dict, cond: Dict, coin: Dict, d: Dict, hourly: Dict, D: float, for_sell: bool = False) -> Optional[Dict]:
        tf = (rule.get('params') or {}).get('tf') or 'day'
        dd = d
        if tf != 'day' and tf in TF_UNIT:
            s = series[TF_UNIT[tf]]
            k = s.done_before(D)
            dd = {**d, **_period(s.rows[max(0, k - 199):k])}
        # 매도 판단은 실제 봇(_check_sells)처럼 스테이블코인도 봄 — 가진 것은 팔 수 있어야 하므로 (2026-10-17)
        htf = (rule.get('params') or {}).get('htf')
        if htf and htf != tf:                               # 위 봉: 이 걸음이 끝나는 순간까지 끝난 봉 (미래 안 봄)
            hs = series[TF_UNIT.get(htf, 'day')]
            kh = hs.done_before(D)
            dd = {**dd, 'htf': _period(hs.rows[max(0, kh - 199):kh])}
        if TF_UNIT.get(tf) != unit:                         # 지금 봉 시가 · 저가는 규칙의 봉이 걸음 봉과 같을 때만 뜻이 있음
            coin = {k: v for k, v in coin.items() if k not in BAR_KEYS}
        hits = cd.run_condition(cond, rule.get('params'), [coin], {market: dd}, {market: hourly},
                                include_stable=True if for_sell else include_stable, now=D)
        return hits[0] if hits else None                   # 맞은 코인 (s_fill = 봉 안에서 산 값, 수요 구역 사기)

    def sell(D: float, price: float, reason: str, part: float = 1.0) -> Optional[Dict]:
        """part = 팔 비율 (1 = 전부). 다 팔면 진행 중이던 급등 일부 팔기 · 다시 사기도 멈춤 (실제 봇과 같음)"""
        nonlocal cash, pos, last_order, last_sell_ts
        fill = price * (1 - slip_r)
        qty = pos['qty'] * part
        gross = qty * fill
        fee = gross * fee_r
        cost = pos['cost'] * part
        profit = gross - fee - cost
        cash += gross - fee
        rec = {'time': _iso(D + 1), 'side': 'sell', 'price': fill, 'qty': qty, 'total': gross, 'fee': fee,
               'profit': profit, 'reason': reason, 'buy_reason': pos['reason']}
        trades.append(rec)
        if profit < 0:
            daily['loss'] += profit
        if part >= 1:
            pos = None
            last_sell_ts = D
            for m in machines.values():
                if market in m.state:
                    m.cancel(market, 'position_closed', D)
        else:
            pos['qty'] -= qty
            pos['cost'] -= cost
        last_order = D
        return rec

    def buy(D: float, price: float, reason: str):
        nonlocal cash, pos, last_order
        if last_order is not None and D - last_order < MIN_ORDER_INTERVAL_SEC:
            return
        if rm.daily_loss_limit_percent is None:
            limit = rm.daily_loss_limit_krw
        else:
            pct_lim = float(rm.daily_loss_limit_percent or 0)
            limit = daily['base'] * pct_lim / 100 if daily['base'] and pct_lim > 0 else float('inf')   # 0 = 안 씀 (2026-10-15)
        if abs(daily['loss']) >= limit:              # 오늘 손실 한도에 닿으면 그날은 새로 사지 않음
            return
        krw_amt, _ = rm.calculate_position_size(cash, price)
        if start_held:
            krw_amt = cash / (1 + fee_r)                 # 한 코인만 보는 비교라 판 돈 전부로 다시 삼
        if krw_amt * (1 + fee_r) > cash * (1 + 1e-9) or (start_held and krw_amt < sr.MIN_ORDER_KRW):   # 부동소수점 오차로 가끔 안 사던 것 (2026-10-17)
            return
        fill = price * (1 + slip_r)
        qty = krw_amt / fill
        fee = krw_amt * fee_r
        cash -= krw_amt + fee
        pos = {'qty': qty, 'entry': fill, 'cost': krw_amt + fee, 'reason': reason, 'peak': fill, 'at': D}
        if not same['cost']:
            same.update(qty=qty, cost=krw_amt + fee)
        nonlocal_max(pos['cost'])
        trades.append({'time': _iso(D + 1), 'side': 'buy', 'price': fill, 'qty': qty, 'total': krw_amt, 'fee': fee,
                       'profit': None, 'reason': reason})
        last_order = D

    def nonlocal_max(x):
        nonlocal max_invested
        max_invested = max(max_invested, x)

    def minute_pass(a: int, b: int):
        """[a, b) 의 1분봉마다 급등 일부 팔기 · 다시 사기 (실제 봇의 spike_tick 과 같은 순서)"""
        nonlocal cash, pos
        lo, hi = bisect.bisect_left(mins.t, a), bisect.bisect_left(mins.t, b)
        for jj in range(lo, hi):
            t, o1, h1, l1, c1, v1 = mins.rows[jj]
            tm = t + 60                                     # 그 1분이 끝난 순간
            if pos is None:
                continue
            spike, surge = machines['spike'], machines['surge']
            fresh = set()                                   # 이 1분 끝에 막 감지한 것: 이 1분의 저가·고가는 감지 전이라 끝값만 봄
            # 하루 급등 감지: 24시간 전(1시간봉) 보다 rise_24h% 이상 (순간 급등 진행 중이면 건너뜀)
            if psets['surge']['enabled'] and market not in spike.state and market not in surge.state:
                hs = series['60']
                k = hs.done_before(tm)
                recent = [r for r in hs.rows[max(0, k - 25):k] if r[0] > tm - 86400]   # 실제 봇 close_24h_of 와 같게 (지금 시각 − 23시간 봉, 2026-10-17)
                c24 = recent[0][4] if len(recent) >= 2 else None
                info = sr.is_surge(c1, c24, psets['surge'])
                if info and surge.start(market, c1, info, tm):
                    fresh.add('surge')
                    events.append({'time': _iso(tm), 'text': f"하루 급등 감지 (24시간 {info['rise_24h']:+.1f}%) — 최고가에서 {psets['surge']['pullback']:g}% 꺾이면 {psets['surge']['portion']:g}% 팔기"})
            # 순간 급등 감지: 그 1분 거래대금이 직전 60분 평균의 mult배 · 1분 rise% · 1시간 rise_1h%
            if (psets['spike']['enabled'] and market not in surge.state and market not in spike.state
                    and spike.cooldown.get(market, 0) <= tm):
                k0 = bisect.bisect_left(mins.t, t - 3600)
                avg60 = (m_prefix[jj] - m_prefix[k0]) / 60
                ka = bisect.bisect_right(mins.t, t - 3600) - 1
                ago = mins.rows[ka][4] if ka >= 0 and t - 3600 - mins.rows[ka][0] <= 300 else None
                info = sr.is_spike({'value': v1, 'avg60': avg60 if mins.t[0] <= t - 3600 else None,
                                    'open': o1, 'close': c1, 'close_1h': ago}, psets['spike'])
                if info:
                    spike.state[market] = {'phase': 'selling', 'peak': c1, 'since': tm, 'info': info}
                    fresh.add('spike')
                    events.append({'time': _iso(tm), 'text': f"급등 감지 (1분 거래대금 평소의 {info['mult']:.1f}배 · 1분 {info['rise']:+.1f}% · 1시간 {info['rise_1h']:+.1f}%)"})
            for kind in ('spike', 'surge'):
                m, ps = machines[kind], psets[kind]
                st = m.state.get(market)
                if not st or pos is None:
                    continue
                if kind in fresh:
                    price = c1
                elif st['phase'] == 'selling':
                    st['peak'] = max(st['peak'], h1)        # 그 1분의 고가까지 오른 뒤
                    thr = st['peak'] * (1 - ps['pullback'] / 100)
                    price = max(l1, min(o1, thr)) if l1 <= thr else c1
                else:
                    hit = [lv for lv in st['left'] if l1 <= lv]
                    price = max(l1, min(hit)) if hit else c1     # 닿은 가장 낮은 다시 살 가격 (그보다 위 몫도 함께)
                act = m.step(market, price, ps, tm)
                if not act:
                    continue
                if act['do'] == 'cancel':
                    m.cancel(market, 'no_pullback', tm)
                    events.append({'time': _iso(tm), 'text': f"{KIND_TEXT[kind]['name']} 지켜보기 끝 — {act['why']}, 팔지 않음"})
                elif act['do'] == 'sell':
                    if not sell_on:                         # 실제 봇과 같게: 매도를 꺼 두면 꺾여도 팔지 않음 (2026-10-17)
                        continue
                    part = ps['portion'] / 100
                    if pos['qty'] * part * price < sr.MIN_ORDER_KRW:
                        m.cancel(market, 'too_small', tm)
                        events.append({'time': _iso(tm), 'text': f"{KIND_TEXT[kind]['sell']} 건너뜀 — 팔 금액({pos['qty'] * part * price:,.0f}원)이 5,000원 미만 (시작 금액이나 한 번에 사는 비율을 늘려 보십시오)"})
                        continue
                    rec = sell(tm - 1, price, f"{KIND_TEXT[kind]['sell']} — {act['why']}", part)
                    m.on_sold(market, rec['price'], rec['qty'], rec['total'] - rec['fee'], ps, tm)
                elif act['do'] == 'rebuy':
                    if not buy_on:                          # 실제 봇과 같게: 매수를 꺼 두면 다시 사지 않음 (2026-10-17)
                        continue
                    krw_amt = min(act['krw'], cash / (1 + fee_r))
                    if krw_amt < sr.MIN_ORDER_KRW:
                        continue                            # 원화가 모자라면 기다림 (실제 봇과 같음)
                    fill = price * (1 + slip_r)
                    qty = krw_amt / fill
                    fee = krw_amt * fee_r
                    cash -= krw_amt + fee
                    pos['entry'] = (pos['entry'] * pos['qty'] + krw_amt) / (pos['qty'] + qty)
                    pos['qty'] += qty
                    pos['cost'] += krw_amt + fee
                    nonlocal_max(pos['cost'])
                    trades.append({'time': _iso(tm), 'side': 'buy', 'price': fill, 'qty': qty, 'total': krw_amt, 'fee': fee,
                                   'profit': None, 'reason': f"{KIND_TEXT[kind]['rebuy']} — {act['why']}"})
                    m.on_rebought(market, act['n'], qty, tm)

    if start_held:                                   # 첫 봉 시가에 시작 금액 전부로 산 채로 출발 (봇 미적용도 같은 때 같은 금액)
        t_open, o_open = steps[0][0], steps[0][1]
        fill = o_open * (1 + slip_r)
        krw_amt = cash / (1 + fee_r)
        fee = krw_amt * fee_r
        cash -= krw_amt + fee
        pos = {'qty': krw_amt / fill, 'entry': fill, 'cost': krw_amt + fee, 'reason': '구간 처음에 산 것', 'peak': fill, 'at': t_open}   # at: '산 지 N일 지남' 조건용 (2026-10-17)
        same.update(qty=pos['qty'], cost=pos['cost'])
        nonlocal_max(pos['cost'])
        trades.append({'time': _iso(t_open), 'side': 'buy', 'price': fill, 'qty': pos['qty'], 'total': krw_amt, 'fee': fee,
                       'profit': None, 'reason': '구간 처음에 산 것'})
        last_order = t_open

    for idx, row in enumerate(steps):
        t0, o, h, l, c, v = row
        if p['partial'] and pos is not None:
            minute_pass(t0, t0 + step_sec)
        D, coin, d, hourly = snapshot(row, pos is not None, steps[idx + 1][0] if idx + 1 < len(steps) else None)
        day_key = (D + 9 * 3600) // 86400                   # 하루 손실 한도는 한국 날짜로
        if daily['day'] != day_key:
            daily.update(day=day_key, loss=0.0, base=cash + (pos['qty'] * c if pos else 0))

        # ── 매도: 손절 → 익절 → 추적 손절 (봉 안 저가·고가) → 매도 조건 ──
        if pos:
            e = pos['entry']
            sl, tp, tr = rm.stop_loss_percent, rm.take_profit_percent, rm.trailing_stop_percent
            if not sell_on:                              # 매도를 꺼 두면 손절만 (익절 · 트레일링 · 매도 조건은 안 봄)
                tp, tr = 0, 0
            stop_px, tp_px = e * (1 - sl / 100), e * (1 + tp / 100)
            done = False
            if sl > 0 and l <= stop_px:                 # 0 = 손절 안 함
                px = min(o, stop_px)
                sell(D, px, rm.check_stop_loss(e, px)[1])
                done = True
            elif tp > 0 and h >= tp_px:                 # 0 = 익절 안 함
                px = max(o, tp_px)
                sell(D, px, rm.check_take_profit(e, px)[1])
                done = True
            elif tr > 0:
                prev_peak = pos['peak']
                if prev_peak > e and l <= prev_peak * (1 - tr / 100):
                    px = min(o, prev_peak * (1 - tr / 100))
                    sell(D, px, f"트레일링 스톱: 최고가 {prev_peak:,.0f}원에서 {(prev_peak - px) / prev_peak * 100:.2f}% 하락")
                    done = True
                else:
                    pos['peak'] = max(prev_peak, h)
                    pk = pos['peak']
                    if pk > e and (pk - c) / pk * 100 >= tr:
                        sell(D, c, f"트레일링 스톱: 최고가 {pk:,.0f}원에서 {(pk - c) / pk * 100:.2f}% 하락")
                        done = True
            if not done and p['sell'] and sell_on:
                coin['held'] = True
                coin['bot_entry'] = pos['entry'] if pos else None          # 산 값이 필요한 매도 조건 (Hongro 전략2 팔기)
                coin['bot_opened_ts'], coin['bot_now_ts'] = (pos.get('at'), D) if pos else (None, None)   # 산 지 N일 지남
                got = [(cond, judge(rule, cond, coin, d, hourly, D, for_sell=True)) for rule, cond in p['sell']]
                got = [(cond, h) for cond, h in got if h is not None]
                hits = [cond['title'] for cond, _ in got]
                if rule_match(rules, 'sell') == 'all':
                    ok = len(hits) == len(p['sell'])
                    name = ' + '.join(hits)
                else:
                    ok = bool(hits)
                    name = hits[0] if hits else ''
                if ok:
                    fill = next((h['s_fill'] for _, h in got if h.get('s_fill')), None)   # 봉 안에서 판 값 (수요 구역 손절 · 익절)
                    why = next((h.get('s_why') for _, h in got if h.get('s_why')), None)
                    sell(D, fill or c, f"매도 조건 '{name}'에 맞음" + (f" ({why})" if why else ''))

        # ── 매수: 안 맞다가 맞게 된 순간 (첫 걸음은 기준만) ──
        if p['buy'] and not excluded and buy_on:
            coin['held'] = pos is not None
            last_sell = next((t for t in reversed(trades) if t['side'] == 'sell'), None)
            coin['bot_profit_sold'] = bool(last_sell and (last_sell['profit'] or 0) > 0)   # Hongro 전략2 의 B
            want = None
            fill = None                                    # 봉 안에서 산 값 (수요 구역 사기 '닿는 순간' 등) — 없으면 봉 끝 가격
            if rule_match(rules, 'buy') == 'all' and len(p['buy']) > 1:
                got = [judge(rule, cond, coin, d, hourly, D) for rule, cond in p['buy']]
                now_ok = all(got)
                key = ('all',)
                if key in baselines and now_ok and not baselines[key]:
                    want = ' + '.join(cond['title'] for _, cond in p['buy'])
                    fill = next((g['s_fill'] for g in got if g.get('s_fill')), None)
                baselines[key] = now_ok
            else:
                for rule, cond in p['buy']:
                    hit = judge(rule, cond, coin, d, hourly, D)
                    now_ok = hit is not None
                    key = (cond['id'], json.dumps(rule.get('params') or {}, sort_keys=True))
                    took = want is None
                    if key not in baselines:
                        if cond.get('start_buy') and now_ok and want is None and not start_wait:
                            want = cond['title']
                    elif now_ok and not baselines[key] and want is None:
                        want = cond['title']
                    elif (now_ok and baselines[key] and want is None and cond.get('start_buy') and not start_wait
                          and pos is None and (last_sell_ts is None or D - last_sell_ts >= REFILL_SEC)):
                        want = cond['title']               # 실제 봇의 '돈이 생기면 더 사기'(_buy_refill): 판 뒤 24시간 지나도 맞으면 다시 삼
                    if took and want is not None:
                        fill = hit.get('s_fill')
                    baselines[key] = now_ok
            if want and pos is None and rm.max_positions > 0:
                buy(D, fill or c, want)

        value = cash + (pos['qty'] * c if pos else 0)
        peak_value = max(peak_value, value)
        max_dd = max(max_dd, (peak_value - value) / peak_value * 100 if peak_value else 0)
        curve.append({'t': t0 + step_sec, 'bot': value, 'hold': start_krw - same['cost'] + same['qty'] * c})

    last_close = steps[-1][4]
    final = cash + (pos['qty'] * last_close * (1 - slip_r) * (1 - fee_r) if pos else 0)
    sells = [t for t in trades if t['side'] == 'sell']
    wins = [t for t in sells if (t['profit'] or 0) > 0]
    if len(curve) > CURVE_MAX:
        k = -(-len(curve) // CURVE_MAX)
        curve = curve[::k] + ([curve[-1]] if (len(curve) - 1) % k else [])
    return {
        'market': market, 'unit': unit, 'start': _iso(steps[0][0]), 'end': _iso(steps[-1][0] + step_sec),
        'start_krw': start_krw,
        'kpis': {'final': final, 'return_pct': (final / start_krw - 1) * 100,
                 'hold_pct': (last_close / first_close - 1) * 100,
                 'same_cost': same['cost'],
                 'same_pnl': (same['qty'] * last_close * (1 - slip_r) * (1 - fee_r) - same['cost']) if same['cost'] else None,
                 'buys': sum(1 for t in trades if t['side'] == 'buy'), 'sells': len(sells),
                 'win_rate': (len(wins) / len(sells) * 100) if sells else None,
                 'realized': sum(t['profit'] or 0 for t in sells),
                 'max_dd': max_dd, 'open': bool(pos), 'max_invested': max_invested,
                 'invested_pct': ((final - start_krw) / max_invested * 100) if max_invested else None,
                 'open_pnl': (pos['qty'] * last_close - pos['cost']) if pos else None},
        'trades': trades,
        'curve': [{'t': x['t'], 'bot': round(x['bot']), 'hold': round(x['hold'])} for x in curve],
        'candles': [[r[0], r[1], r[2], r[3], r[4]] for r in steps],
        'skipped': p['skipped'],
        'notes': p['notes'],
        'events': events[-300:],
        'excluded': excluded,
        'costs': {'fee': fee_r * 100, 'slip': slip_r * 100},
    }
