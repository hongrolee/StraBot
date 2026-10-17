"""
가상 계좌 (시뮬레이션 모드)
- pyupbit.Upbit 과 같은 모양의 함수: get_balance / get_balances / buy_market_order / sell_market_order / get_order
  → 화면 API와 봇(ExchangeWrapper)이 업비트 계좌 대신 그대로 씀. 시뮬레이션 매매는 이 파일에만 쌓이고 업비트에는 섞이지 않음
- 초기자본(원화)으로 시작. 시장가 체결 = 지금 가격 ± 불리한 체결(SLIP), 수수료 FEE (업비트처럼 매수는 금액+수수료, 매도는 받는 돈−수수료)
- data/sim_account.json 에 저장 (스레드 안전). 초기화하면 옛 파일은 보관 폴더로 옮김
- 지정가(차트에서 걸어 두는 주문, bot_core/chart_orders.py): state['pending'] 에 두고 돈 · 수량을 묶음.
  get_balances 는 업비트처럼 balance = 쓸 수 있는 양, locked = 묶인 양 / krw · coins 의 qty 는 묶인 것까지 포함한 전체
"""
import json
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

FEE = 0.0005
SLIP = 0.001
MIN_ORDER_KRW = 5000
DEFAULT_INITIAL_KRW = 10_000_000.0
MAX_ORDERS = 300          # 주문 조회(get_order)용 최근 주문만 — 전체 기록은 거래 기록 저장소(bot_core/trade_store.py)에


class VirtualAccount:
    def __init__(self, path, get_price: Callable[[str], Optional[float]], initial_krw: float = DEFAULT_INITIAL_KRW,
                 on_order: Optional[Callable[[Dict], None]] = None):
        self.path = Path(path)
        self.get_price = get_price                     # 'KRW-BTC' → 지금 가격 (없으면 None)
        self.on_order = on_order                       # 체결된 주문마다 (거래 기록 저장소에 한 줄 더하기)
        self._lock = threading.RLock()
        self.state = self._load(initial_krw)

    # ── 저장 ──
    def _new_state(self, initial_krw: float) -> Dict:
        now = datetime.now().isoformat(timespec='seconds')
        return {'initial_krw': float(initial_krw), 'krw': float(initial_krw), 'coins': {}, 'orders': [], 'pending': [],
                'created_at': now}

    def _load(self, initial_krw: float) -> Dict:
        try:
            st = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(st, dict) and 'krw' in st:
                st.setdefault('coins', {})
                st.setdefault('orders', [])
                st.setdefault('pending', [])
                return st
        except Exception:
            pass
        st = self._new_state(initial_krw)
        self._write(st)
        return st

    def _write(self, st: Dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.path)

    def save(self):
        with self._lock:
            self._write(self.state)

    def reset(self, initial_krw: float) -> Path:
        """처음 자본으로 새로 시작. 옛 파일은 같은 폴더의 sim_account_archive/ 로 옮겨 둠 → 옮긴 경로"""
        with self._lock:
            keep = self.path.parent / 'sim_account_archive' / f"sim_account_{datetime.now():%Y%m%d%H%M%S}.json"
            if self.path.exists():
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.path, keep)
            self.state = self._new_state(initial_krw)
            self._write(self.state)
            return keep

    def deposit(self, krw: float):
        """가상 원화를 더 넣음 (시뮬레이션은 봇마다 정해진 돈: 봇이 새로 생기면 그만큼 계좌도 늘림) — 처음 자본도 같이 늘려 수익률이 흔들리지 않게"""
        with self._lock:
            self.state['krw'] = float(self.state['krw']) + float(krw)
            self.state['initial_krw'] = float(self.state['initial_krw']) + float(krw)
            self._write(self.state)

    # ── 묶인 양 (지정가) ──
    def _locked_krw(self) -> float:
        return sum(p['krw'] for p in self.state['pending'] if p['side'] == 'bid')

    def _locked_qty(self, cur: str) -> float:
        return sum(p['qty'] for p in self.state['pending'] if p['side'] == 'ask' and p['market'] == f'KRW-{cur}')

    # ── 업비트와 같은 모양 ──
    def get_balances(self) -> List[Dict]:
        with self._lock:
            lk = self._locked_krw()
            out = [{'currency': 'KRW', 'balance': str(self.state['krw'] - lk), 'locked': str(lk), 'avg_buy_price': '0',
                    'avg_buy_price_modified': False, 'unit_currency': 'KRW'}]
            for cur, c in self.state['coins'].items():
                if c['qty'] > 0:
                    lq = self._locked_qty(cur)
                    out.append({'currency': cur, 'balance': str(c['qty'] - lq), 'locked': str(lq), 'avg_buy_price': str(c['avg']),
                                'avg_buy_price_modified': False, 'unit_currency': 'KRW'})
            return out

    def get_balance(self, ticker: str = 'KRW') -> float:
        """쓸 수 있는 양 (지정가로 묶인 것은 뺌)"""
        cur = ticker.split('-')[-1]
        with self._lock:
            if cur == 'KRW':
                return float(self.state['krw'] - self._locked_krw())
            return float((self.state['coins'].get(cur) or {}).get('qty', 0.0) - self._locked_qty(cur))

    def _order(self, market: str, side: str, qty: float, price: float, fee: float, amount: float,
               ord_type: Optional[str] = None) -> Dict:
        o = {'uuid': str(uuid.uuid4()), 'market': market, 'side': side,
             'ord_type': ord_type or ('price' if side == 'bid' else 'market'),
             'state': 'done', 'executed_volume': str(qty), 'avg_price': str(price), 'paid_fee': str(fee),
             'trade_amount': amount, 'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
             'trades_count': 1}
        self.state['orders'].append(o)
        del self.state['orders'][:-MAX_ORDERS]
        if self.on_order:
            try:
                self.on_order(dict(o))
            except Exception:
                pass                                   # 기록이 실패해도 주문은 그대로
        return o

    def _price(self, market: str) -> Optional[float]:
        try:
            p = self.get_price(market)
            return float(p) if p else None
        except Exception:
            return None

    def buy_market_order(self, ticker: str, price: float) -> Dict:
        """원화 price 만큼 시장가 매수 (수수료는 따로 더 빠짐)"""
        krw = float(price or 0)
        if krw < MIN_ORDER_KRW:
            return {'error': {'name': 'under_min_total_bid', 'message': f'최소 주문금액은 {MIN_ORDER_KRW:,}원입니다'}}
        now = self._price(ticker)
        if not now:
            return {'error': {'name': 'no_price', 'message': '지금 가격을 알 수 없습니다'}}
        with self._lock:
            fee = krw * FEE
            free = self.state['krw'] - self._locked_krw()
            if krw + fee > free + 1e-6:
                return {'error': {'name': 'insufficient_funds_bid', 'message': f"주문 가능한 원화({free:,.0f}원)가 모자랍니다"}}
            fill = now * (1 + SLIP)
            qty = krw / fill
            cur = ticker.split('-')[-1]
            c = self.state['coins'].setdefault(cur, {'qty': 0.0, 'avg': 0.0})
            c['avg'] = (c['avg'] * c['qty'] + krw) / (c['qty'] + qty)          # 업비트처럼 수수료는 평균 단가에 넣지 않음
            c['qty'] += qty
            self.state['krw'] -= krw + fee
            o = self._order(ticker, 'bid', qty, fill, fee, krw)
            self._write(self.state)
            return dict(o)

    def sell_market_order(self, ticker: str, volume: float) -> Dict:
        cur = ticker.split('-')[-1]
        now = self._price(ticker)
        if not now:
            return {'error': {'name': 'no_price', 'message': '지금 가격을 알 수 없습니다'}}
        with self._lock:
            c = self.state['coins'].get(cur)
            have = (c['qty'] - self._locked_qty(cur)) if c else 0.0
            qty = float(volume or 0)
            if qty <= 0 or qty > have * (1 + 1e-9):
                return {'error': {'name': 'insufficient_funds_ask', 'message': f'주문 가능한 수량({have:g})이 모자랍니다'}}
            qty = min(qty, have)
            fill = now * (1 - SLIP)
            gross = qty * fill
            if gross < MIN_ORDER_KRW:
                return {'error': {'name': 'under_min_total_ask', 'message': f'최소 주문금액은 {MIN_ORDER_KRW:,}원입니다 (약 {gross:,.0f}원)'}}
            fee = gross * FEE
            c['qty'] -= qty
            if c['qty'] <= max(have, 1e-12) * 1e-9 and not self._locked_qty(cur):
                self.state['coins'].pop(cur, None)
            self.state['krw'] += gross - fee
            o = self._order(ticker, 'ask', qty, fill, fee, gross)
            self._write(self.state)
            return dict(o)

    def drop_dust(self, ticker: str) -> bool:
        """5,000원 미만이라 팔 수 없는 코인을 계좌에서 지움 (시뮬레이션 초기화용) — 원화는 늘지 않음 (2026-10-17)"""
        cur = ticker.split('-')[-1]
        with self._lock:
            c = self.state['coins'].get(cur)
            if not c or self._locked_qty(cur):
                return False
            px = self._price(ticker) or float(c.get('avg') or 0)
            if float(c.get('qty') or 0) * px >= MIN_ORDER_KRW:
                return False
            self.state['coins'].pop(cur, None)
            self._write(self.state)
            return True

    # ── 지정가 (차트에서 걸어 두는 주문) ──
    def place_limit(self, ticker: str, side: str, price: float, krw: Optional[float] = None,
                    qty: Optional[float] = None) -> Dict:
        """매수: 원화 krw 만큼(수수료도 같이 묶음) / 매도: qty 개. 돈 · 수량을 묶고 pending 에 둠 → 주문 한 줄 또는 {'error'}"""
        price = float(price or 0)
        if price <= 0:
            return {'error': {'name': 'invalid_price', 'message': '가격이 올바르지 않습니다'}}
        cur = ticker.split('-')[-1]
        with self._lock:
            if side == 'bid':
                krw = float(krw or 0)
                if krw < MIN_ORDER_KRW:
                    return {'error': {'name': 'under_min_total_bid', 'message': f'최소 주문금액은 {MIN_ORDER_KRW:,}원입니다'}}
                free = self.state['krw'] - self._locked_krw()
                if krw * (1 + FEE) > free + 1e-6:
                    return {'error': {'name': 'insufficient_funds_bid', 'message': f'주문 가능한 원화({free:,.0f}원)가 모자랍니다'}}
                p = {'krw': krw * (1 + FEE), 'qty': krw / price}
            else:
                qty = float(qty or 0)
                c = self.state['coins'].get(cur)
                free = (c['qty'] - self._locked_qty(cur)) if c else 0.0
                if qty <= 0 or qty > free * (1 + 1e-9):
                    return {'error': {'name': 'insufficient_funds_ask', 'message': f'주문 가능한 수량({free:g})이 모자랍니다'}}
                if qty * price < MIN_ORDER_KRW:
                    return {'error': {'name': 'under_min_total_ask',
                                      'message': f'최소 주문금액은 {MIN_ORDER_KRW:,}원입니다 (약 {qty * price:,.0f}원)'}}
                p = {'krw': 0.0, 'qty': min(qty, free)}
            p.update({'id': str(uuid.uuid4()), 'source': 'sim', 'market': ticker, 'side': side, 'kind': 'limit', 'price': price,
                      'created_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'state': 'wait'})
            self.state['pending'].append(p)
            self._write(self.state)
            return dict(p)

    def pending_orders(self, market: Optional[str] = None) -> List[Dict]:
        with self._lock:
            return [dict(p) for p in self.state['pending'] if not market or p['market'] == market]

    def cancel_limit(self, order_id: str) -> Optional[Dict]:
        """묶인 것을 풀고 지움 → 지운 주문 (없으면 None)"""
        with self._lock:
            p = next((p for p in self.state['pending'] if p['id'] == order_id), None)
            if p is None:
                return None
            self.state['pending'].remove(p)
            self._write(self.state)
            return dict(p, state='cancel')

    def fill_limit(self, order_id: str) -> Optional[Dict]:
        """그 지정 가격으로 체결 (수수료 FEE, 미끄러짐 없음) → 체결 주문 (없으면 None)"""
        with self._lock:
            p = next((p for p in self.state['pending'] if p['id'] == order_id), None)
            if p is None:
                return None
            self.state['pending'].remove(p)
            cur, price, qty = p['market'].split('-')[-1], p['price'], p['qty']
            if p['side'] == 'bid':
                amount = qty * price
                fee = amount * FEE
                c = self.state['coins'].setdefault(cur, {'qty': 0.0, 'avg': 0.0})
                c['avg'] = (c['avg'] * c['qty'] + amount) / (c['qty'] + qty)
                c['qty'] += qty
                self.state['krw'] -= amount + fee
            else:
                c = self.state['coins'].get(cur)
                qty = min(qty, c['qty'] if c else 0.0)
                if qty <= 0:
                    self._write(self.state)
                    return None
                amount = qty * price
                fee = amount * FEE
                c['qty'] -= qty
                if c['qty'] <= 1e-12 and not self._locked_qty(cur):
                    self.state['coins'].pop(cur, None)
                self.state['krw'] += amount - fee
            o = self._order(p['market'], p['side'], qty, price, fee, amount, ord_type='limit')
            self._write(self.state)
            return dict(o)

    def get_order(self, order_uuid: str) -> Optional[Dict]:
        with self._lock:
            return next((dict(o) for o in reversed(self.state['orders']) if o['uuid'] == order_uuid), None)

    # ── 화면용 ──
    def closed_orders(self, since: Optional[str] = None) -> List[Dict]:
        """끝난 주문 (새 것부터). since: ISO 시각 이후만"""
        with self._lock:
            items = [dict(o) for o in reversed(self.state['orders'])]
        return [o for o in items if not since or o['created_at'] >= since]

    def summary(self) -> Dict:
        """원화 · 코인 평가금 · 처음 자본 대비"""
        with self._lock:
            krw, coins, initial = self.state['krw'], dict(self.state['coins']), self.state['initial_krw']
            created = self.state.get('created_at')
        value = 0.0
        for cur, c in coins.items():
            p = self._price(f'KRW-{cur}') or c['avg']
            value += p * c['qty']
        total = krw + value
        return {'initial_krw': initial, 'krw': krw, 'coin_value': value, 'total': total,
                'profit': total - initial, 'profit_rate': (total / initial - 1) * 100 if initial else 0.0, 'created_at': created}
