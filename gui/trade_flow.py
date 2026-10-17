"""
실시간 체결 수집: 업비트 WebSocket 체결 스트림 하나로 원화 마켓 전체를 받아
코인별 '1분마다 매수·매도 체결 금액'을 모은다 (매수 거래대금 급증 감시용).

- 요청을 반복하는 방식이 아니라 연결 하나로 받으므로 REST 요청 수 제한(429)과 상관없음
- 봇 프로그램이 켜져 있는 동안만 모음 (메모리에 최근 65분만 보관)
- 코인별 1분 가격(시작·최고·최저·끝)과 마지막 체결가도 모음 (봇의 '급등 때 일부 팔고 다시 사기'용)
- ask_bid: BID = 매수 체결(매수 주문이 매도 호가를 가져감), ASK = 매도 체결
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

WS_URL = 'wss://api.upbit.com/websocket/v1'
KEEP_MINUTES = 65      # 최근 1분 + 평소 계산용 60분 + 여유
BASE_MINUTES = 30      # '평소' = 직전 30분 평균
MIN_BASE_MINUTES = 10  # 켠 뒤 10분은 모아야 배수를 계산
SPIKE_BASE_MINUTES = 60  # 급등 판단의 '평소' = 직전 60분 평균 거래대금 (과거 검증과 같은 기준)
PRE_SPIKE_MINUTES = 15   # '급등 직전 모습' 조건: 직전 15분 거래대금 합계


class TradeFlowMonitor:
    def __init__(self, get_markets: Callable[[], List[str]]):
        self._get_markets = get_markets          # 원화 마켓 목록 ['KRW-BTC', ...]
        self._lock = threading.Lock()
        # {code: {minute: [buy_krw, sell_krw, 시작가, 최고가, 최저가, 끝가]}} minute = 유닉스 분 (체결 시각 기준)
        self._buckets: Dict[str, Dict[int, List[float]]] = defaultdict(dict)
        self._last_price: Dict[str, float] = {}      # 코인별 마지막 체결가
        self._started_minute = None
        self.connected = False
        self.last_error = ''
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name='trade-flow', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ── 수집 ──
    def _run(self):
        backoff = 2
        while not self._stop.is_set():
            try:
                asyncio.run(self._listen())
                backoff = 2
            except Exception as e:
                self.connected = False
                self.last_error = str(e)[:200]
                logger.warning(f"실시간 체결 연결 끊김, {backoff}초 뒤 다시 연결: {e}")
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60)

    async def _listen(self):
        import websockets
        markets = [m for m in (self._get_markets() or []) if m.startswith('KRW-')]
        if not markets:
            raise RuntimeError('원화 마켓 목록을 받지 못했습니다')
        subscribed_at = time.time()
        async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=30, max_size=2 ** 22) as ws:
            await ws.send(json.dumps([
                {'ticket': f'strabot-flow-{uuid.uuid4().hex[:8]}'},
                {'type': 'trade', 'codes': markets, 'isOnlyRealtime': True},
                {'format': 'SIMPLE'},
            ]))
            self.connected = True
            self.last_error = ''
            if self._started_minute is None:
                self._started_minute = int(time.time() // 60)
            logger.info(f"실시간 체결 수집 시작: {len(markets)}개 코인")
            while not self._stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                self._add(json.loads(raw))
                # 10분마다 마켓 목록이 바뀌었는지 확인 (새 상장·상장폐지) → 바뀌면 다시 연결
                if time.time() - subscribed_at > 600:
                    subscribed_at = time.time()
                    latest = [m for m in (self._get_markets() or []) if m.startswith('KRW-')]
                    if latest and set(latest) != set(markets):
                        logger.info("원화 마켓 목록이 바뀌어 실시간 체결을 다시 연결합니다")
                        return
        self.connected = False

    def _add(self, msg: Dict):
        # SIMPLE 형식: cd=코인, tp=체결가, tv=체결량, ab=ASK/BID, ttms=체결 시각(ms)
        code = msg.get('cd') or msg.get('code')
        if not code:
            return
        try:
            price = float(msg.get('tp') or msg.get('trade_price'))
            krw = price * float(msg.get('tv') or msg.get('trade_volume'))
        except (TypeError, ValueError):
            return
        minute = int((msg.get('ttms') or msg.get('trade_timestamp') or time.time() * 1000) // 60000)
        side = 0 if (msg.get('ab') or msg.get('ask_bid')) == 'BID' else 1
        with self._lock:
            bucket = self._buckets[code]
            row = bucket.get(minute)
            if row is None:
                row = bucket[minute] = [0.0, 0.0, price, price, price, price]
                # 오래된 분 정리
                old = [m for m in bucket if m < minute - KEEP_MINUTES]
                for m in old:
                    del bucket[m]
            row[side] += krw
            if price > row[3]:
                row[3] = price
            if price < row[4]:
                row[4] = price
            row[5] = price
            self._last_price[code] = price

    # ── 조회 ──
    def last_prices(self, markets: List[str]) -> Dict[str, float]:
        """고른 코인들의 마지막 체결가 (실시간 연결로 받은 값 그대로 — 차트 분석 실시간 표시용)"""
        with self._lock:
            return {m: self._last_price[m] for m in markets if m in self._last_price}

    def snapshot(self) -> Dict:
        """코인별 최근 1분(끝난 분) 매수·매도 체결 금액과 평소(직전 30분 평균) 대비 배수"""
        now_minute = int(time.time() // 60)
        last = now_minute - 1                       # 방금 끝난 1분
        # 켠 첫 분은 중간부터 모은 반쪽이라 빼고, 그다음 분부터 온전한 1분
        first_full = None if self._started_minute is None else self._started_minute + 1
        collected = 0 if first_full is None else max(0, last - first_full + 1)
        base_start = last if first_full is None else max(last - BASE_MINUTES, first_full)
        base_n = max(0, last - base_start)          # 평소 계산에 쓰는 분 수 (최근 1분 제외)
        coins = {}
        with self._lock:
            for code, bucket in self._buckets.items():
                row = bucket.get(last)
                buy, sell = (row[0], row[1]) if row else (0.0, 0.0)
                base = [bucket.get(m, (0.0, 0.0))[0] for m in range(base_start, last)] if base_n else []
                avg = sum(base) / len(base) if base else None
                total = buy + sell
                coins[code] = {
                    'buy_1m': round(buy),
                    'sell_1m': round(sell),
                    'buy_ratio': round(buy / total * 100, 1) if total else None,
                    'buy_avg': round(avg) if avg is not None else None,
                    # 평소가 0이면 배수 대신 None (조금만 사도 무한대가 되므로)
                    'mult': round(buy / avg, 2) if avg and base_n >= MIN_BASE_MINUTES else None,
                }
        return {
            'connected': self.connected,
            'error': self.last_error,
            'minute': last,
            'minutes_collected': collected,
            'base_minutes': base_n,
            'ready': base_n >= MIN_BASE_MINUTES,
            'coins': coins,
        }

    def spike_snapshot(self) -> Dict:
        """봇의 급등 판단용: 코인별 방금 끝난 1분 (거래대금·시작가·끝가), 직전 60분 평균 거래대금,
        60분 전 끝가, 그리고 코인별 마지막 체결가.
        켠 뒤 61분이 지나야 ready (그 전에는 '평소'를 계산할 수 없음). 거래가 없던 1분은 거래대금 0.
        '급등 직전 모습' 조건용으로 직전 15분 거래대금 합계(value15)와 그 사이 마지막 끝가(close_last)도 넣음 —
        방금 1분에 체결이 없던 코인도 15분 안에 체결이 있으면 들어감(그때 open·close 는 None 이라 급등 판정에는 안 걸림)."""
        now_minute = int(time.time() // 60)
        last = now_minute - 1
        first_full = None if self._started_minute is None else self._started_minute + 1
        ready = first_full is not None and last - first_full >= SPIKE_BASE_MINUTES
        coins = {}
        with self._lock:
            prices = dict(self._last_price)
            if ready:
                for code, bucket in self._buckets.items():
                    recent = [m for m in range(last - PRE_SPIKE_MINUTES + 1, last + 1) if m in bucket]
                    row = bucket.get(last)
                    if not row and not recent:
                        continue
                    base = sum(bucket[m][0] + bucket[m][1] for m in range(last - SPIKE_BASE_MINUTES, last) if m in bucket)
                    ago = None
                    for m in range(last - SPIKE_BASE_MINUTES, last - SPIKE_BASE_MINUTES - 5, -1):
                        if m in bucket:
                            ago = bucket[m][5]
                            break
                    coins[code] = {'value': (row[0] + row[1]) if row else 0, 'avg60': base / SPIKE_BASE_MINUTES,
                                   'open': row[2] if row else None, 'close': row[5] if row else None, 'close_1h': ago,
                                   'value15': sum(bucket[m][0] + bucket[m][1] for m in recent),
                                   'close_last': bucket[recent[-1]][5] if recent else None}
        return {'connected': self.connected, 'ready': ready, 'minute': last, 'coins': coins, 'prices': prices}

