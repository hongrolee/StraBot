"""
차트에서 걸어 두는 예약 주문 (코인 차트 창에서 클릭한 가격으로 매수 · 매도)

2026-10-10 부터 모두 '예약': 업비트에는 아무것도 걸지 않고(원화 · 코인이 묶이지 않음) 프로그램이 지켜보다
가격이 닿으면 시장가로 주문함. 방향(dir)은 걸 때 현재가와 비교해 정함
- down: 현재가보다 아래 → 내려와 닿으면 (매수 = 싸지면 사기, 매도 = 손절)
- up:   현재가보다 위   → 올라 닿으면 (매수 = 뚫으면 사기, 매도 = 익절)
프로그램이 꺼져 있으면 실행되지 않음. 다시 켰을 때 그동안 가격이 지나간 예약은 실행하지 않고 'hold'(확인 필요)로 멈춰
사용자에게 물어봄 (지금 실행 · 취소 · 다시 지켜보기). 켜져 있을 때도 매수 예약이 예약가보다 JUMP_HOLD 넘게 지나친
가격에서 처음 확인되면 멈춤 (매도 예약은 급락 때 손절이 늦지 않게 멈추지 않음)

예전에 건 업비트 지정가 · 시뮬레이션 지정가(VirtualAccount.place_limit)는 목록에 보이고 취소할 수 있게 그대로 둠
업비트 요청은 pyupbit 의 지정가 함수 대신 여기서 직접 보냄 — pyupbit 는 거절 사유를 삼키고 None 만 돌려줌
"""
import json
import logging
import math
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

API = 'https://api.upbit.com'
MIN_ORDER_KRW = 5000
FEE = 0.0005
CHECK_EVERY = 2.0          # 감시 · 시뮬레이션 지정가 확인 간격 (초)
KEEP_DONE = 200            # 끝난 예약은 최근 것만 남김
RESUME_GAP = 60.0          # 확인이 이만큼(초) 끊겼다 이어지면 '꺼져 있었다'로 봄
JUMP_HOLD = 0.05           # 매수 예약: 예약가보다 이만큼 넘게 지나친 가격이면 멈춤
BEAT_EVERY = 10.0          # 마지막 확인 시각을 파일에 남기는 간격 (초)


class ChartOrderError(Exception):
    """화면에 그대로 보여 줄 수 있는 주문 오류"""


# ── 종류 · 호가 단위 ──
def direction(price: float, now: float) -> str:
    """예약 방향: 현재가보다 아래면 'down'(내려와 닿으면) · 위면 'up'(올라 닿으면)"""
    return 'down' if price < now else 'up'


def decide_kind(side: str, price: float, now: float) -> str:
    """'limit'(지정가) | 'stop'(감시). 매수: 현재가보다 낮으면 지정가 · 높으면 감시, 매도: 반대"""
    if side == 'bid':
        return 'limit' if price < now else 'stop'
    return 'limit' if price > now else 'stop'


def table_tick(price: float) -> float:
    """업비트 원화 마켓 호가 단위 (2023년 이후 기준표 — 코인마다 다른 경우는 instruments 조회가 우선)"""
    for floor, tick in ((2_000_000, 1000), (1_000_000, 1000), (500_000, 500), (100_000, 100), (50_000, 50),
                        (10_000, 10), (5_000, 5), (1_000, 1), (100, 1), (10, 0.1), (1, 0.01), (0.1, 0.001),
                        (0.01, 0.0001), (0.001, 0.00001), (0.0001, 0.000001), (0.00001, 0.0000001)):
        if price >= floor:
            return tick
    return 0.00000001


_tick_cache: Dict[str, tuple] = {}


def tick_size(market: str, price: float, ex: str = 'upbit') -> float:
    """업비트가 알려 주는 그 코인의 호가 단위 (10분 기억). 못 받으면 기준표
    다른 거래소(빗썸)는 업비트에 묻지 않고 기준표로 — 차트 주문은 모두 '예약'(지금 가격과 견줘 시장가로 냄)이라 가격 맞춤에만 씀 (2026-10-19)"""
    if ex != 'upbit':
        return table_tick(price)
    hit = _tick_cache.get(market)
    if hit and time.time() - hit[0] < 600 and hit[2] == table_tick(price):
        return hit[1]
    tick = None
    try:
        import requests
        r = requests.get(f'{API}/v1/orderbook/instruments', params={'markets': market}, timeout=4)
        if r.ok:
            row = (r.json() or [{}])[0]
            tick = float(row.get('tick_size') or 0) or None
    except Exception:
        tick = None
    tick = tick or table_tick(price)
    _tick_cache[market] = (time.time(), tick, table_tick(price))
    return tick


def snap_price(price: float, side: str, tick: float) -> float:
    """호가 단위에 맞춤: 매수는 내림, 매도는 올림 (사용자에게 불리하지 않은 쪽)"""
    n = price / tick
    n = math.floor(n + 1e-9) if side == 'bid' else math.ceil(n - 1e-9)
    digits = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(n * tick, digits)


def num_text(x: float) -> str:
    """업비트에 보낼 숫자 글자 (지수 표기 없이)"""
    s = f'{x:.8f}'.rstrip('0').rstrip('.')
    return s or '0'


# ── 업비트 요청 (지정가 · 대기 주문 · 취소) ──
def _upbit_request(account, method: str, path: str, params: Optional[Dict] = None):
    if hasattr(account, '_send'):                    # 빗썸 계좌: 같은 모양 요청을 계좌가 직접 (빗썸 주소 · 인증)
        return account._send(method, path, params or {})
    import requests
    from utils.pyupbit_patch import _raise_for_upbit
    params = params or {}
    headers = account._request_headers(params if params else None)
    url = f'{API}{path}'
    if method == 'POST':
        resp = requests.post(url, json=params, headers=headers, timeout=6)
    elif method == 'DELETE':
        resp = requests.delete(url, params=params, headers=headers, timeout=6)
    else:
        resp = requests.get(url, params=params, headers=headers, timeout=6)
    return _raise_for_upbit(resp).json()


def upbit_open_orders(account, market: Optional[str] = None) -> List[Dict]:
    """업비트 대기 주문 (새 것부터). 업비트 앱에서 건 주문도 그대로 나옴"""
    params = {'limit': 100}
    if market:
        params['market'] = market
    try:
        return _upbit_request(account, 'GET', '/v1/orders/open', params) or []
    except Exception as e:
        if getattr(e, 'status', None) != 404:
            raise
    params = {'state': 'wait', 'limit': 100}                 # 예전 API (orders/open 이 없을 때)
    if market:
        params['market'] = market
    return _upbit_request(account, 'GET', '/v1/orders', params) or []


def upbit_place_limit(account, market: str, side: str, price: float, volume: float) -> Dict:
    return _upbit_request(account, 'POST', '/v1/orders', {
        'market': market, 'side': side, 'volume': num_text(volume), 'price': num_text(price), 'ord_type': 'limit'})


def upbit_cancel(account, order_uuid: str) -> Dict:
    """취소 요청 → 잠시 뒤 다시 읽어 실제 상태(일부 체결 양 포함)"""
    _upbit_request(account, 'DELETE', '/v1/order', {'uuid': order_uuid})
    time.sleep(0.4)
    try:
        return _upbit_request(account, 'GET', '/v1/order', {'uuid': order_uuid})
    except Exception:
        return {'uuid': order_uuid, 'state': 'cancel'}


def upbit_row(o: Dict, source: str = 'upbit') -> Dict:
    """거래소(업비트 · 빗썸) 대기 주문 → 화면 한 줄 (source = 거래소)"""
    vol = float(o.get('volume') or 0)
    left = float(o.get('remaining_volume') or vol)
    return {'id': o.get('uuid'), 'source': source, 'market': o.get('market'), 'side': o.get('side'),
            'kind': 'limit' if o.get('ord_type') == 'limit' else (o.get('ord_type') or ''),
            'price': float(o.get('price') or 0), 'qty': vol, 'left': left,
            'filled': float(o.get('executed_volume') or 0), 'krw': None,
            'created_at': o.get('created_at'), 'state': 'wait'}


# ── 예약 저장 ──
ACTIVE = ('wait', 'hold')


class ChartOrderStore:
    """예약 (모드마다 파일 하나: data/chart_orders_{mode}.json). state: wait(지켜보는 중) · hold(확인 필요) · done · failed · cancel"""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.orders: List[Dict] = data if isinstance(data, list) else []
        except Exception:
            self.orders = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.orders, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.path)

    def add(self, market: str, side: str, price: float, krw: Optional[float] = None, qty: Optional[float] = None,
            dir: Optional[str] = None) -> Dict:
        o = {'id': 'w-' + uuid.uuid4().hex[:12], 'source': 'watch', 'market': market, 'side': side, 'kind': 'stop',
             'dir': dir or ('up' if side == 'bid' else 'down'), 'price': float(price), 'krw': krw, 'qty': qty,
             'created_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'state': 'wait', 'note': ''}
        with self._lock:
            self.orders.append(o)
            self._save()
        return dict(o)

    def get(self, order_id: str) -> Optional[Dict]:
        with self._lock:
            o = next((o for o in self.orders if o['id'] == order_id), None)
            return dict(o) if o else None

    def waiting(self, market: Optional[str] = None) -> List[Dict]:
        """지켜보는 중인 것만 (확인 필요는 빼고)"""
        with self._lock:
            return [dict(o) for o in self.orders if o['state'] == 'wait' and (not market or o['market'] == market)]

    def active(self, market: Optional[str] = None) -> List[Dict]:
        """화면 목록: 지켜보는 중 + 확인 필요"""
        with self._lock:
            return [dict(o) for o in self.orders if o['state'] in ACTIVE and (not market or o['market'] == market)]

    def recent(self, market: Optional[str] = None, limit: int = 30) -> List[Dict]:
        """끝난 것 (새 것부터) — 화면이 '체결 · 실패'를 알려 주는 용도"""
        with self._lock:
            done = [dict(o) for o in reversed(self.orders) if o['state'] not in ACTIVE and (not market or o['market'] == market)]
        return done[:limit]

    def hold(self, order_id: str, note: str, now: float) -> Optional[Dict]:
        """실행하지 않고 멈춤 (사용자가 지금 실행 · 취소 · 다시 지켜보기 중 고름)"""
        with self._lock:
            o = next((o for o in self.orders if o['id'] == order_id), None)
            if o is None or o['state'] != 'wait':
                return None
            o.update(state='hold', note=note, hold_price=now, hold_at=datetime.now().astimezone().isoformat(timespec='seconds'))
            self._save()
            return dict(o)

    def rewatch(self, order_id: str, now: float) -> Optional[Dict]:
        """확인 필요 → 다시 지켜보기: 지금 가격 기준으로 방향을 다시 정해 그 가격에 다시 닿을 때를 기다림"""
        with self._lock:
            o = next((o for o in self.orders if o['id'] == order_id), None)
            if o is None or o['state'] != 'hold':
                return None
            o.update(state='wait', note='', dir=direction(o['price'], now))
            o.pop('hold_price', None)
            o.pop('hold_at', None)
            self._save()
            return dict(o)

    def finish(self, order_id: str, state: str, note: str = '') -> Optional[Dict]:
        with self._lock:
            o = next((o for o in self.orders if o['id'] == order_id), None)
            if o is None or o['state'] not in ACTIVE:
                return None
            o['state'], o['note'] = state, note
            o['done_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            live = [x for x in self.orders if x['state'] in ACTIVE]
            done = [x for x in self.orders if x['state'] not in ACTIVE][-KEEP_DONE:]
            self.orders = live + done
            self.orders.sort(key=lambda x: x['created_at'])
            self._save()
            return dict(o)

    def clear(self):
        with self._lock:
            self.orders = []
            self._save()


# ── 지켜보기 ──
def crossed(o: Dict, now: float) -> bool:
    """예약(dir): up = 그 가격 이상, down = 그 가격 이하 / dir 없는 예전 감시: 매수는 이상 · 매도는 이하
    시뮬레이션 지정가(kind limit): 매수는 이하 · 매도는 이상"""
    if o.get('kind') == 'limit':
        return now <= o['price'] if o['side'] == 'bid' else now >= o['price']
    d = o.get('dir') or ('up' if o['side'] == 'bid' else 'down')
    return now >= o['price'] if d == 'up' else now <= o['price']


TAGS = {'sim': '[시뮬레이션] ', 'bithumb': '[빗썸] '}      # 알림 앞머리 (업비트는 없음)


class ChartOrderWatcher:
    """예약(두 모드) · 예전 시뮬레이션 지정가를 CHECK_EVERY 초마다 확인

    get_price(market) → 현재가 (원화 전체 시세 캐시) · get_account(mode) → 계좌 · live_blocked() → 업비트 매수가 막혔으면 안내
    beat_path: 마지막으로 확인한 시각을 남기는 파일 (다시 켰을 때 꺼져 있던 시간을 알기 위해)
    """

    def __init__(self, stores: Dict[str, ChartOrderStore], sim_account, get_price: Callable[[str], Optional[float]],
                 get_account: Callable[[str], object], live_blocked: Callable[[], Optional[str]],
                 notify: Optional[Callable[[str, str], None]] = None, beat_path=None):
        self.stores, self.sim = stores, sim_account
        self.get_price, self.get_account, self.live_blocked = get_price, get_account, live_blocked
        self.notify = notify or (lambda msg, level: None)
        self.beat_path = Path(beat_path) if beat_path else None
        self._prev: Optional[float] = None          # 이번 실행에서 마지막으로 확인한 시각
        self._beat_written = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name='ChartOrders', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(CHECK_EVERY):
            try:
                self.check()
            except Exception as e:
                logger.warning(f"예약 확인 오류: {e}")

    def _gap(self, t: float) -> float:
        """앞 확인에서 지난 시간 (처음이면 파일에 남은 마지막 확인 시각 기준, 기록이 없으면 0)"""
        last = self._prev
        if last is None and self.beat_path:
            try:
                last = float(self.beat_path.read_text(encoding='utf-8').strip())
            except Exception:
                last = None
        return max(0.0, t - last) if last else 0.0

    def _beat(self, t: float):
        self._prev = t
        if self.beat_path and t - self._beat_written >= BEAT_EVERY:
            self._beat_written = t
            try:
                self.beat_path.parent.mkdir(parents=True, exist_ok=True)
                self.beat_path.write_text(f'{t:.0f}', encoding='utf-8')
            except Exception:
                pass

    def _price(self, market: str, mode: str) -> Optional[float]:
        """이 모드 거래소의 현재가 (예전 모양 get_price(market) 도 받음)"""
        try:
            return self.get_price(market, mode)
        except TypeError:
            return self.get_price(market)

    def _blocked(self, mode: str) -> Optional[str]:
        try:
            return self.live_blocked(mode)
        except TypeError:
            return self.live_blocked()

    def check(self, t: Optional[float] = None):
        t = time.time() if t is None else t
        with self._lock:
            resumed = self._gap(t) > RESUME_GAP
            for mode, store in self.stores.items():
                tag = TAGS.get(mode, '')
                for o in store.waiting():
                    now = self._price(o['market'], mode)
                    if not now or not crossed(o, now):
                        continue
                    if resumed:
                        note = f'프로그램이 꺼져 있던 동안 가격이 예약가를 지나갔습니다 (지금 {now:,.8g}원).'
                    elif o['side'] == 'bid' and abs(now / o['price'] - 1) > JUMP_HOLD:
                        note = f'가격이 예약가보다 {abs(now / o["price"] - 1) * 100:.1f}% 지나쳐 있어 사지 않고 멈췄습니다 (지금 {now:,.8g}원).'
                    else:
                        self._fire(mode, store, o, now)
                        continue
                    if store.hold(o['id'], note, now):
                        self.notify(f"{tag}{coin(o)} {side_word(o)} 예약({o['price']:,.8g}원)을 멈췄습니다 — {note} 차트 창에서 지금 실행 · 취소 · 다시 지켜보기 중 골라 주십시오.", 'warning')
            for o in self.sim.pending_orders():
                now = self._price(o['market'], 'sim')
                if now and crossed(o, now):
                    done = self.sim.fill_limit(o['id'])
                    if done:
                        self.notify(f"[시뮬레이션] {coin(o)} {side_word(o)} 지정가 {o['price']:,.8g}원이 체결되었습니다.", 'success')
            self._beat(t)

    def run_now(self, mode: str, order_id: str) -> Dict:
        """확인 필요 → 지금 실행 (지금 가격 그대로 시장가) → {'ok', 'message'}"""
        store = self.stores[mode]
        o = store.get(order_id)
        if not o or o['state'] != 'hold':
            return {'ok': False, 'message': '이미 처리되었거나 없는 예약입니다.'}
        now = self._price(o['market'], mode)
        if not now:
            return {'ok': False, 'message': '지금 가격을 알 수 없습니다. 잠시 뒤 다시 시도해 주십시오.'}
        with self._lock:
            ok = self._fire(mode, store, o, now)
        return {'ok': ok, 'message': f"{coin(o)} {side_word(o)}을 지금 가격(약 {now:,.8g}원)으로 실행했습니다." if ok
                else f"{coin(o)} {side_word(o)} 예약을 실행하지 못했습니다. 알림을 확인해 주십시오."}

    def _fire(self, mode: str, store: ChartOrderStore, o: Dict, now: float) -> bool:
        tag = TAGS.get(mode, '')
        try:
            if mode != 'sim' and o['side'] == 'bid':
                why = self._blocked(mode)
                if why:
                    raise ChartOrderError(why)
            account = self.get_account(mode)
            if o['side'] == 'bid':
                result = account.buy_market_order(o['market'], float(o['krw']))
            else:
                have = account.get_balance(o['market'])
                qty = min(float(o['qty']), float(have or 0))
                if qty <= 0:
                    raise ChartOrderError('팔 수 있는 수량이 없습니다.')
                result = account.sell_market_order(o['market'], qty)
            if not result:
                raise ChartOrderError('거래소 응답이 없습니다.')
            if isinstance(result, dict) and result.get('error'):
                raise ChartOrderError(result['error'].get('message') or '거래소가 주문을 거절했습니다.')
        except Exception as e:
            store.finish(o['id'], 'failed', str(e)[:200])
            self.notify(f"{tag}{coin(o)} {side_word(o)} 예약({o['price']:,.8g}원)을 실행하지 못했습니다: {e}", 'error')
            logger.warning(f"예약 실행 실패 {o['market']} {o['side']} {o['price']}: {e}")
            return False
        store.finish(o['id'], 'done', f'{now:,.8g}원에서 시장가로 주문했습니다.')
        self.notify(f"{tag}{coin(o)} 가격이 {o['price']:,.8g}원에 닿아 시장가로 {side_word(o)}했습니다.", 'success')
        logger.info(f"예약 실행 {mode} {o['market']} {o['side']} 기준 {o['price']} 현재 {now}")
        return True


def coin(o: Dict) -> str:
    return str(o.get('market', '')).split('-')[-1]


def side_word(o: Dict) -> str:
    return '매수' if o.get('side') == 'bid' else '매도'
