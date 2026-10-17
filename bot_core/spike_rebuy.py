"""
급등 때 일부 팔고 다시 사기 — 봇이 산 코인에만

과거 검증(tools/spike_backtest.py --mode improve, 업비트 1분봉 90일·50종)에서 고른 규칙:
  1) 봇이 가진 코인이 방금 끝난 1분에
       거래대금이 평소(직전 60분 평균)의 5배 이상 · 그 1분에 5% 이상 상승 · 직전 1시간 10% 이상 상승
     이면 '급등'으로 봄
  2) 그 뒤 최고가에서 3% 꺾이면 (30분 안에 안 꺾이면 그때) 보유의 30%를 팜
  3) 판 돈을 셋으로 나눠 판 가격보다 3% · 5% · 8% 싸지면 하나씩 다시 삼
     24시간 안에 다 못 사면 남은 몫은 그때 가격에 삼 (코인 수를 원래대로 되돌림)

같은 틀로 '하루 급등 · 꺾이면 일부 팔고 다시 사기'(조건 surge_24h)도 돌린다 (설정은 surge_settings):
  1) 봇이 가진 코인이 24시간 전(1시간봉 첫 봉 종가)보다 20% 이상 오르면 지켜보기 시작
  2) 그 뒤 48시간 안에 최고가에서 8% 꺾이면 보유의 30%를 팜 (안 꺾이면 팔지 않고 끝)
  3) 판 가격보다 5% · 10% · 15% 싸지면 셋으로 나눠 다시 삼, 72시간 안에 다 못 사면 남은 몫은 그때 가격에
  4) 끝난 코인은 24시간 동안 다시 보지 않음
  (과거 검증: 업비트 15분봉 1년·74종, 1분봉 90일·50종 — 과거 결과이며 앞으로의 수익을 뜻하지 않는다)

이 모듈은 '언제 무엇을 할지'만 정하고 (주문은 BotEngine 이 함), 진행 상태를 파일에 저장해
봇을 껐다 켜도 다시 사기를 이어간다. 과거 결과이며 앞으로의 수익을 뜻하지 않는다.
"""
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MIN_FLOW_KRW = 10_000_000     # 1분 거래대금이 이보다 적으면 급등으로 보지 않음 (조건 '순간 급등'과 같음)

DEFAULTS = {
    'enabled': False,
    'portion': 30,            # 보유의 몇 %를 팔지
    'mult': 5,                # 1분 거래대금이 평소의 몇 배 이상
    'rise': 5,                # 그 1분에 몇 % 이상 상승
    'rise_1h': 10,            # 직전 1시간에 몇 % 이상 상승 (이미 부풀어 오른 급등만)
    'pullback': 3,            # 최고가에서 몇 % 꺾이면 팜
    'dips': [3, 5, 8],        # 판 가격보다 몇 % 싸지면 다시 살지 (나눠서)
    'max_wait_h': 24,         # 이 시간 안에 못 사면 그때 가격에 삼
}
MIN_VALUE = 10_000_000        # 1분 거래대금 1천만 원 미만은 무시
PULLBACK_WAIT_SEC = 30 * 60   # 급등 뒤 30분 안에 안 꺾이면 그때 팜
COOLDOWN_SEC = 60 * 60        # 한 번 끝난 코인은 60분 동안 다시 보지 않음
MIN_ORDER_KRW = 5000


RULE_ID = 'spike_pump'          # 기본 조건 '순간 급등 (크게 오른 뒤)'
SURGE_RULE_ID = 'surge_24h'     # 기본 조건 '하루 급등 (24시간 크게 오른 뒤)'

SURGE_DEFAULTS = {
    'enabled': False,
    'portion': 30,            # 보유의 몇 %를 팔지
    'rise_24h': 20,           # 24시간 전보다 몇 % 이상 올랐을 때 지켜보기 시작
    'pullback': 8,            # 최고가에서 몇 % 꺾이면 팜
    'pullback_wait_h': 48,    # 이 시간 안에 안 꺾이면 팔지 않고 끝
    'dips': [5, 10, 15],      # 판 가격보다 몇 % 싸지면 다시 살지 (나눠서)
    'max_wait_h': 72,         # 이 시간 안에 못 사면 그때 가격에 삼
}
SURGE_COOLDOWN_SEC = 24 * 3600  # 하루 급등은 끝난 뒤 24시간 동안 다시 보지 않음 (아직 +20%인 채로 곧바로 다시 걸리지 않게)


def _is_partial(rule: Dict, rule_id: str) -> bool:
    return (rule or {}).get('id') == rule_id and (rule or {}).get('sell_mode', 'partial') != 'all'


def is_spike_rule(rule: Dict) -> bool:
    """매도 칸의 '순간 급등' 조건 중 파는 방식이 '일부 팔고 다시 사기'인 것 (기본값)"""
    return _is_partial(rule, RULE_ID)


def is_surge_rule(rule: Dict) -> bool:
    """매도 칸의 '하루 급등' 조건 중 파는 방식이 '일부 팔고 다시 사기'인 것 (기본값)"""
    return _is_partial(rule, SURGE_RULE_ID)


def is_partial_rule(rule: Dict) -> bool:
    """일부 팔고 다시 사기 세트 (순간 급등·하루 급등) — 일반 매도 조건 판단에서 빼고 spike_tick 이 맡음"""
    return is_spike_rule(rule) or is_surge_rule(rule)


def _sell_rules(rules: Dict) -> List[Dict]:
    v = (rules or {}).get('sell')
    if isinstance(v, dict):
        v = [v]
    return [r for r in (v or []) if isinstance(r, dict) and r.get('id')]


def settings(rules: Dict) -> Dict:
    """매도 칸의 '순간 급등 · 일부 팔고 다시 사기' 조건 → 설정 (없으면 예전 trade_rules['spike_rebuy'])"""
    rule = next((r for r in _sell_rules(rules) if is_spike_rule(r)), None)
    if rule:
        raw = {**(rule.get('params') or {}), 'enabled': True, 'portion': rule.get('portion', DEFAULTS['portion'])}
        if rule.get('dips'):
            raw['dips'] = rule['dips']
    else:
        raw = (rules or {}).get('spike_rebuy') or {}
    s = dict(DEFAULTS)
    s['enabled'] = bool(raw.get('enabled', False))
    num = _clamper(raw, DEFAULTS)
    s['portion'] = num('portion', 5, 100)
    s['mult'] = num('mult', 2, 50)
    s['rise'] = num('rise', 1, 50)
    s['rise_1h'] = num('rise_1h', 0, 200)
    s['pullback'] = num('pullback', 0.5, 20)
    s['max_wait_h'] = num('max_wait_h', 1, 72)
    s['dips'] = _clamp_dips(raw, DEFAULTS)
    s['pullback_wait_h'] = PULLBACK_WAIT_SEC / 3600     # 30분 안에 안 꺾이면
    s['timeout_sell'] = True                            # 그때 팜
    return s


def surge_settings(rules: Dict) -> Dict:
    """매도 칸의 '하루 급등 · 꺾이면 일부 팔고 다시 사기' 조건 → 설정 (없으면 꺼짐)"""
    rule = next((r for r in _sell_rules(rules) if is_surge_rule(r)), None)
    raw = {}
    if rule:
        raw = {**(rule.get('params') or {}), 'enabled': True, 'portion': rule.get('portion', SURGE_DEFAULTS['portion'])}
        if rule.get('dips'):
            raw['dips'] = rule['dips']
    s = dict(SURGE_DEFAULTS)
    s['enabled'] = bool(raw.get('enabled', False))
    num = _clamper(raw, SURGE_DEFAULTS)
    s['portion'] = num('portion', 5, 100)
    s['rise_24h'] = num('rise_24h', 1, 500)
    s['pullback'] = num('pullback', 0.5, 50)
    s['pullback_wait_h'] = num('pullback_wait_h', 1, 168)
    s['max_wait_h'] = num('max_wait_h', 1, 168)
    s['dips'] = _clamp_dips(raw, SURGE_DEFAULTS)
    s['timeout_sell'] = False                           # 안 꺾이면 팔지 않고 끝
    return s


def _clamper(raw: Dict, defaults: Dict):
    """숫자 설정을 범위 안으로 (잘못된 값이면 기본값)"""
    def num(key, lo, hi):
        try:
            v = float(raw.get(key, defaults[key]))
        except (TypeError, ValueError):
            return defaults[key]
        return min(max(v, lo), hi)
    return num


def _clamp_dips(raw: Dict, defaults: Dict) -> List[float]:
    try:
        dips = sorted({min(max(float(d), 0.5), 50.0) for d in raw.get('dips', defaults['dips'])})[:5]
    except (TypeError, ValueError):
        dips = []
    return dips or list(defaults['dips'])


def is_spike(c: Dict, s: Dict) -> Optional[Dict]:
    """방금 끝난 1분이 급등인지 (조건 목록의 '순간 급등'과 같은 계산). 맞으면 {mult, rise, rise_1h}, 아니면 None"""
    if not c:
        return None
    from .conditions import _t_spike_pump
    hit = _t_spike_pump({'spike_value': c.get('value'), 'spike_avg': c.get('avg60'), 'spike_open': c.get('open'),
                         'spike_close': c.get('close'), 'spike_close_1h': c.get('close_1h')},
                        {'mult': s['mult'], 'rise': s['rise'], 'rise_1h': s['rise_1h']}, {})
    if not hit:
        return None
    return {'mult': hit['s_spike_mult'], 'rise': hit['s_spike_rise'], 'rise_1h': hit['s_spike_rise_1h']}


def is_surge(price: float, close_24h: Optional[float], s: Dict) -> Optional[Dict]:
    """지금 가격이 24시간 전보다 rise_24h% 이상 올랐는지 (조건 목록의 '하루 급등'과 같은 계산). 맞으면 {rise_24h, close_24h}"""
    from .conditions import _t_surge_24h
    hit = _t_surge_24h({'current_price': price, 'close_24h': close_24h}, {'rise_24h': s['rise_24h']}, {})
    return {'rise_24h': hit['s_rise_24h'], 'close_24h': hit['s_close_24h']} if hit else None


def watch_list(snap: Dict, held, s: Dict, n: int = 3) -> List[Dict]:
    """급등 지켜보기: 가진 코인 중 방금 1분이 '순간 급등' 세 조건(1시간 상승·1분 상승·거래 배수)에 얼마나 가까운지
    가까운 순 n개 [{symbol, rise_1h, rise, mult, value, met, score}] — met = 맞은 조건 수, score = 세 조건을 채운 정도 평균(0~1)
    (계산은 is_spike / 조건 '순간 급등'과 같음: 1분 거래대금 1천만 원 미만이면 거래 조건은 안 맞음)"""
    out = []
    for sym in held:
        c = (snap.get('coins') or {}).get(sym)
        if not c or not c.get('open') or not c.get('close') or not c.get('close_1h') or not c.get('avg60'):
            continue
        value = c.get('value') or 0
        rise = (c['close'] / c['open'] - 1) * 100
        rise_1h = (c['close'] / c['close_1h'] - 1) * 100
        mult = value / c['avg60']
        parts = [(rise_1h, s['rise_1h'], True), (rise, s['rise'], True), (mult, s['mult'], value >= MIN_FLOW_KRW)]
        fill = [max(0.0, min(1.0, v / t)) if t > 0 else 1.0 for v, t, _ in parts]
        met = sum(1 for (v, t, ok) in parts if v >= t and ok)
        out.append({'symbol': sym, 'rise_1h': rise_1h, 'rise': rise, 'mult': mult, 'value': value, 'met': met,
                    'score': sum(fill) / 3})
    out.sort(key=lambda x: (x['met'], x['score']), reverse=True)
    return out[:n]


class SpikeRebuy:
    """코인별 진행 상태
    - 'selling': 급등을 봤고 꺾이기를 기다림 {peak, since, info}
    - 'waiting': 일부 팔았고 다시 사기를 기다림 {sold_price, sold_qty, left: [가격...], part_krw, deadline}
    순간 급등(spike_state.json)·하루 급등(surge_state.json)이 한 개씩 따로 씀. 꺾이기를 얼마나 기다릴지·안 꺾이면 팔지는
    설정(pullback_wait_h, timeout_sell)으로, 끝난 뒤 다시 보지 않는 시간은 cooldown_sec 로 정함
    """

    def __init__(self, path: Optional[Path] = None, cooldown_sec: float = COOLDOWN_SEC):
        self.path = Path(path) if path else None
        self.cooldown_sec = cooldown_sec
        self.state: Dict[str, Dict] = {}
        self.cooldown: Dict[str, float] = {}       # 코인: 다시 볼 수 있는 시각
        self.history: List[Dict] = []              # 끝난 한 번들 (최근 30개) — 화면·검증용
        self._seen_minute: Dict[str, int] = {}     # 같은 1분을 두 번 판단하지 않게
        self.load()

    # ── 판단 ──
    def detect(self, snap: Dict, held: set, s: Dict, now: Optional[float] = None) -> List[tuple]:
        """봇이 가진 코인 중 방금 급등한 코인 → [(코인, 정보)] 를 'selling' 으로 등록"""
        now = now or time.time()
        if not snap.get('ready'):
            return []
        found = []
        minute = snap.get('minute')
        for sym in held:
            if sym in self.state or self.cooldown.get(sym, 0) > now or self._seen_minute.get(sym) == minute:
                continue
            self._seen_minute[sym] = minute
            info = is_spike((snap.get('coins') or {}).get(sym), s)
            if info:
                c = snap['coins'][sym]
                self.state[sym] = {'phase': 'selling', 'peak': c['close'], 'since': now, 'info': info}
                found.append((sym, info))
        if found:
            self.save()
        return found

    def start(self, sym: str, price: float, info: Dict, now: Optional[float] = None) -> bool:
        """코인 하나를 'selling'(꺾이기 기다림)으로 등록 (하루 급등용). 이미 진행 중이거나 쉬는 중이면 False"""
        now = now or time.time()
        if sym in self.state or self.cooldown.get(sym, 0) > now or not price:
            return False
        self.state[sym] = {'phase': 'selling', 'peak': price, 'since': now, 'info': info}
        self.save()
        return True

    def step(self, sym: str, price: float, s: Dict, now: Optional[float] = None) -> Optional[Dict]:
        """지금 가격으로 할 일 → {'do': 'sell'} / {'do': 'rebuy', 'krw', 'n', 'why'} / None"""
        now = now or time.time()
        st = self.state.get(sym)
        if not st or not price:
            return None
        if st['phase'] == 'selling':
            st['peak'] = max(st['peak'], price)
            if price <= st['peak'] * (1 - s['pullback'] / 100):
                return {'do': 'sell', 'why': f"최고가 {st['peak']:,.8g}원에서 {s['pullback']:g}% 꺾임"}
            wait_h = s.get('pullback_wait_h', PULLBACK_WAIT_SEC / 3600)
            if now - st['since'] >= wait_h * 3600:
                if s.get('timeout_sell', True):
                    return {'do': 'sell', 'why': f"급등 뒤 {wait_h * 60:g}분이 지남" if wait_h < 1 else f"급등 뒤 {wait_h:g}시간이 지남"}
                return {'do': 'cancel', 'why': f"{wait_h:g}시간 안에 최고가에서 {s['pullback']:g}% 꺾이지 않음"}
            return None
        if now >= st['deadline']:
            return {'do': 'rebuy', 'krw': st['part_krw'] * len(st['left']), 'n': len(st['left']),
                    'why': f"{s['max_wait_h']:g}시간 안에 다시 살 가격이 오지 않음"}
        n = sum(1 for level in st['left'] if price <= level)
        if n:
            return {'do': 'rebuy', 'krw': st['part_krw'] * n, 'n': n,
                    'why': f"판 가격 {st['sold_price']:,.8g}원보다 {(1 - price / st['sold_price']) * 100:.1f}% 싸짐"}
        return None

    # ── 주문 결과 반영 ──
    def on_sold(self, sym: str, sold_price: float, sold_qty: float, krw_got: float, s: Dict, now: Optional[float] = None):
        now = now or time.time()
        dips = list(s['dips'])
        if krw_got / len(dips) < MIN_ORDER_KRW * 1.02:
            dips = [dips[len(dips) // 2]]              # 나누면 최소 주문금액 미만 → 한 번에
        st = self.state.get(sym, {})
        self.state[sym] = {'phase': 'waiting', 'sold_price': sold_price, 'sold_qty': sold_qty, 'krw': krw_got,
                           'left': [sold_price * (1 - d / 100) for d in dips], 'part_krw': krw_got / len(dips),
                           'bought_qty': 0.0, 'sold_at': now, 'deadline': now + s['max_wait_h'] * 3600,
                           'info': st.get('info', {})}
        self.save()

    def on_rebought(self, sym: str, n: int, qty: float, now: Optional[float] = None):
        """n몫을 다시 삼. 다 사면 끝 → history 에 코인 수 변화 기록"""
        st = self.state.get(sym)
        if not st:
            return None
        st['left'] = st['left'][n:]
        st['bought_qty'] += qty
        done = None
        if not st['left']:
            done = self._finish(sym, 'done', now)
        self.save()
        return done

    def cancel(self, sym: str, why: str, now: Optional[float] = None):
        if sym in self.state:
            self._finish(sym, why, now)
            self.save()

    def _finish(self, sym: str, how: str, now: Optional[float] = None) -> Dict:
        now = now or time.time()
        st = self.state.pop(sym)
        rec = {'symbol': sym, 'how': how, 'time': now, 'info': st.get('info', {})}
        if st.get('phase') == 'waiting' and st.get('sold_qty'):
            rec['coin_change'] = (st['bought_qty'] / st['sold_qty'] - 1) * 100 if how == 'done' else None
            rec['sold_price'] = st['sold_price']
        self.history.insert(0, rec)
        del self.history[30:]
        self.cooldown[sym] = now + self.cooldown_sec
        return rec

    # ── 저장 ──
    def load(self):
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.state = data.get('state') or {}
            self.history = data.get('history') or []
            self.cooldown = data.get('cooldown') or {}
        except Exception:
            pass

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({'state': self.state, 'history': self.history, 'cooldown': self.cooldown},
                                            ensure_ascii=False, indent=1), encoding='utf-8')
        except Exception as e:
            logger.warning(f"급등 팔고 다시 사기 상태 저장 실패: {e}")

    def status(self) -> Dict:
        """화면용: 진행 중인 코인과 최근 결과"""
        active = []
        for sym, st in self.state.items():
            item = {'symbol': sym, 'phase': st['phase'], 'info': st.get('info', {})}
            if st['phase'] == 'waiting':
                item.update({'sold_price': st['sold_price'], 'left': st['left'], 'deadline': st['deadline']})
            else:
                item.update({'peak': st['peak']})
            active.append(item)
        return {'active': active, 'history': self.history[:10]}
