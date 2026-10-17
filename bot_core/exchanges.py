"""
거래소 정보 · 빗썸 계좌 (2026-10-12)

- 모드: 'upbit' · 'bithumb' (실제 계좌) · 'sim' (가상 계좌, 업비트 시세)
- 빗썸 새 API(v1)는 업비트와 같은 모양 (코인 코드 KRW-XXX · 시세 · 캔들 · 주문 응답 · JWT 인증)
  → 안쪽 코드는 업비트 모양 그대로 쓰고, 다른 점만 여기서 맞춤
- BithumbAccount 는 pyupbit.Upbit 와 같은 함수 모양 (get_balances · get_balance · buy_market_order · sell_market_order ·
  get_order · _request_headers) — ExchangeWrapper · 차트 예약 · 보유 현황이 그대로 씀
"""

import hashlib
import time
import uuid
from typing import Dict, List, Optional
from urllib.parse import urlencode

LIVE_MODES = ('upbit', 'bithumb')

EXCHANGES: Dict[str, Dict] = {
    'upbit': {
        'name': '업비트', 'api': 'https://api.upbit.com',
        'fee': 0.0005, 'min_order': 5000,
        'day_start_kst': 9,            # 일봉이 바뀌는 시각 (KST)
        'trade_url': 'https://upbit.com/exchange?code=CRIX.UPBIT.KRW-{sym}',
    },
    'bithumb': {
        'name': '빗썸', 'api': 'https://api.bithumb.com',
        'fee': 0.0025, 'min_order': 5000,   # 기본 수수료 (쿠폰 등으로 사람마다 다름 → 설정 'bithumb_fee_pct' 로 바꿈)
        'day_start_kst': 0,
        'trade_url': 'https://www.bithumb.com/react/trade/order/{sym}-KRW',
    },
}


def is_live(mode: Optional[str]) -> bool:
    """실제 계좌 모드인지 (업비트 · 빗썸)"""
    return mode in LIVE_MODES


def exchange_of(mode: Optional[str]) -> str:
    """모드가 보는 거래소 시세 — 시뮬레이션은 업비트 시세"""
    return 'bithumb' if mode == 'bithumb' else 'upbit'


def exchange_name(mode: Optional[str]) -> str:
    return EXCHANGES[exchange_of(mode)]['name']


def fee_rate(mode: Optional[str], config: Optional[Dict] = None) -> float:
    """수수료 비율 (0.0005 = 0.05%). 빗썸은 설정에서 직접 정한 값 (없으면 기본값)"""
    ex = exchange_of(mode)
    if ex == 'bithumb':
        try:
            v = float((config or {}).get('bithumb_fee_pct'))
            if 0 <= v <= 1:
                return v / 100
        except (TypeError, ValueError):
            pass
    return EXCHANGES[ex]['fee']


def api_keys(mode: Optional[str], config: Dict) -> tuple:
    """이 모드 거래소의 (access, secret). 업비트는 예전 이름(api_key · api_secret) 그대로"""
    if mode == 'bithumb':
        return config.get('bithumb_api_key', '') or '', config.get('bithumb_api_secret', '') or ''
    return config.get('api_key', '') or '', config.get('api_secret', '') or ''


# ── 빗썸 거절 사유 ──
_BITHUMB_ERRORS = {
    'invalid_access_key': 'Access Key가 올바르지 않습니다. (복사 누락 · 공백 확인, 또는 삭제된 키)',
    'jwt_verification': 'Secret Key가 올바르지 않습니다. (Access Key와 짝이 맞는지 확인)',
    'invalid_jwt': '인증 정보가 올바르지 않습니다. API 키를 다시 확인해 주십시오.',
    'expired_jwt': '인증 시간이 지났습니다. PC 시계가 맞는지 확인해 주십시오.',
    'no_authorization_ip': '허용되지 않은 IP입니다. 빗썸 API 관리에서 이 PC의 공인 IP를 등록하십시오.',
    'out_of_scope': '이 API 키에 필요한 권한이 없습니다. (자산 조회 · 주문 권한 확인)',
    'insufficient_funds_bid': '주문 가능한 원화가 모자랍니다.',
    'insufficient_funds_ask': '주문 가능한 코인 수량이 모자랍니다.',
    'under_min_total_bid': '최소 주문 금액보다 적습니다.',
    'under_min_total_ask': '최소 주문 금액보다 적습니다.',
    'too_many_requests': '요청이 너무 많습니다. 잠시 후 다시 시도하십시오.',
}


class BithumbRequestError(Exception):
    """빗썸이 요청을 거절했을 때 (UpbitRequestError 와 같은 칸: status · name)"""

    def __init__(self, status: int, name: str, message: str):
        self.status, self.name, self.upbit_message = status, name, message
        guide = _BITHUMB_ERRORS.get(name, message or '알 수 없는 오류')
        super().__init__(f"빗썸 거절 [{status} {name or '-'}]: {guide}")


def _num_text(x: float) -> str:
    s = f'{float(x):.8f}'.rstrip('0').rstrip('.')
    return s or '0'


class BithumbAccount:
    """빗썸 실제 계좌 — pyupbit.Upbit 와 같은 함수 모양 (응답도 업비트 모양 그대로)"""

    API_BASE = EXCHANGES['bithumb']['api']
    EXCHANGE = 'bithumb'

    def __init__(self, access: str, secret: str, fee: Optional[float] = None, session=None):
        self.access, self.secret = access, secret
        self.fee_rate = EXCHANGES['bithumb']['fee'] if fee is None else fee
        self.min_order_krw = EXCHANGES['bithumb']['min_order']
        self._session = session

    # 인증: 업비트와 같은 JWT + 빗썸은 timestamp(ms)
    def _request_headers(self, query: Optional[Dict] = None) -> Dict:
        import jwt
        payload = {'access_key': self.access, 'nonce': str(uuid.uuid4()), 'timestamp': int(time.time() * 1000)}
        if query:
            q = urlencode(query, doseq=True).replace('%5B%5D=', '[]=')
            payload['query_hash'] = hashlib.sha512(q.encode()).hexdigest()
            payload['query_hash_alg'] = 'SHA512'
        token = jwt.encode(payload, self.secret, algorithm='HS256')
        if isinstance(token, bytes):
            token = token.decode()
        return {'Authorization': f'Bearer {token}'}

    def _http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _send(self, method: str, path: str, params: Optional[Dict] = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = self._request_headers(params or None)
        url = f'{self.API_BASE}{path}'
        http = self._http()
        if method == 'POST':
            resp = http.post(url, json=params, headers=headers, timeout=6)
        elif method == 'DELETE':
            resp = http.delete(url, params=params, headers=headers, timeout=6)
        else:
            resp = http.get(url, params=params, headers=headers, timeout=6)
        if not resp.ok:
            name, message = '', ''
            try:
                err = resp.json().get('error', {}) or {}
                name, message = err.get('name', ''), err.get('message', '')
            except Exception:
                message = resp.text[:200]
            if resp.status_code == 429 and not name:
                name = 'too_many_requests'
            raise BithumbRequestError(resp.status_code, name, message)
        return resp.json()

    # ── pyupbit.Upbit 와 같은 함수 ──
    def get_balances(self) -> List[Dict]:
        return self._send('GET', '/v1/accounts') or []

    def get_balance(self, ticker: str = 'KRW') -> float:
        """주문 가능 수량 (묶인 것 뺌). ticker 예: 'KRW' · 'BTC' · 'KRW-BTC'"""
        cur = ticker.split('-')[-1] if '-' in ticker else ticker
        for b in self.get_balances():
            if b.get('currency') == cur:
                return float(b.get('balance') or 0)
        return 0.0

    def buy_market_order(self, ticker: str, price: float) -> Dict:
        """시장가 매수: price = 원화 금액"""
        return self._send('POST', '/v1/orders', {'market': ticker, 'side': 'bid', 'price': _num_text(price), 'ord_type': 'price'})

    def sell_market_order(self, ticker: str, volume: float) -> Dict:
        """시장가 매도: volume = 코인 수량"""
        return self._send('POST', '/v1/orders', {'market': ticker, 'side': 'ask', 'volume': _num_text(volume), 'ord_type': 'market'})

    def get_order(self, ticker_or_uuid: str, state: str = 'wait', limit: int = 100):
        """uuid 면 주문 하나 (체결 trades 포함), 'KRW-XXX' 면 그 코인의 주문 목록"""
        if str(ticker_or_uuid).startswith('KRW-'):
            return self._send('GET', '/v1/orders', {'market': ticker_or_uuid, 'state': state, 'limit': limit}) or []
        return self._send('GET', '/v1/order', {'uuid': ticker_or_uuid})

    def cancel_order(self, order_uuid: str) -> Dict:
        return self._send('DELETE', '/v1/order', {'uuid': order_uuid})

    def order_chance(self, market: str = 'KRW-BTC') -> Dict:
        """이 계좌의 실제 수수료 · 최소 주문 금액 (bid_fee · market.bid.min_total)"""
        return self._send('GET', '/v1/orders/chance', {'market': market}) or {}

    def closed_orders(self, state: str = 'done', page: int = 1, limit: int = 100, market: Optional[str] = None) -> List[Dict]:
        """끝난 주문 목록 (새 것부터)"""
        return self._send('GET', '/v1/orders', {'state': state, 'page': page, 'limit': limit, 'market': market, 'order_by': 'desc'}) or []
