"""코인의 잘 변하지 않는 사실: 상장한 지 며칠 · 바이낸스에도 있는지 ('급등이 잦던 코인' 조건용)

- 상장일: 업비트 일봉의 첫날. 코인마다 한 번만 물어 보고 파일에 기억함(상장일은 바뀌지 않음).
  일봉이 200개 넘게 있으면 '오래된 코인'으로만 기억(정확한 날은 몰라도 200일 넘음).
- 바이낸스: 바이낸스 공개 시세 목록(…USDT 쌍)을 하루에 한 번 받아 기억. 받지 못하면 예전 목록을 씀(없으면 '모름').
- 업비트·바이낸스 공개 시세만 씀 (키·계정 없음). 백그라운드에서 천천히 채우고, 모르는 값은 None.
"""
import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

UPBIT_DAYS = 'https://api.upbit.com/v1/candles/days'
BINANCE_PRICES = 'https://api.binance.com/api/v3/ticker/price'
OLD_DAYS = 200                 # 일봉 200개(요청 한 번에 받는 최대)가 다 차면 '200일 넘음'
BINANCE_TTL = 24 * 3600
REQ_GAP = 0.15                 # 업비트 요청 사이 쉬는 시간(초) — 봇의 다른 요청과 나눠 씀


class CoinFacts:
    def __init__(self, path: Path, get_markets: Callable[[], List[str]]):
        self.path = Path(path)
        self._get_markets = get_markets               # ['KRW-BTC', ...]
        self._lock = threading.Lock()
        self.first_day: Dict[str, Optional[str]] = {}  # 'BTC' -> 'YYYY-MM-DD' (None = 200일 넘음)
        self.binance: List[str] = []
        self.binance_at = 0.0
        self._thread = None
        self.pace = None                              # 업비트 요청 전에 부를 속도 조절기 (대시보드가 넣어 줌, 2026-10-17)
        self._load()

    # ── 저장 ──
    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding='utf-8'))
            self.first_day = d.get('first_day') or {}
            self.binance = d.get('binance') or []
            self.binance_at = float(d.get('binance_at') or 0)
        except Exception:
            pass

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            with self._lock:
                data = {'first_day': self.first_day, 'binance': self.binance, 'binance_at': self.binance_at}
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            tmp.replace(self.path)
        except Exception as e:
            logger.warning(f"코인 정보 저장 실패: {e}")

    # ── 읽기 ──
    def facts(self, currency: str, today: Optional[date] = None) -> Dict:
        """{'listed_days': int|None, 'on_binance': bool|None} — 모르면 None"""
        today = today or date.today()
        with self._lock:
            known = currency in self.first_day
            first = self.first_day.get(currency)
            bases = self.binance
        if not known:
            listed = None
        elif first is None:
            listed = OLD_DAYS                       # 200일 넘음 (정확한 날은 모름)
        else:
            listed = (today - datetime.strptime(first, '%Y-%m-%d').date()).days
        return {'listed_days': listed, 'on_binance': (currency in bases) if bases else None}

    # ── 채우기 ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name='coin-facts', daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                self.refresh()
            except Exception as e:
                logger.warning(f"코인 정보 갱신 실패: {e}")
            time.sleep(3600)

    def refresh(self, session=None):
        s = session or requests
        if time.time() - self.binance_at > BINANCE_TTL:
            try:
                r = s.get(BINANCE_PRICES, timeout=15)
                r.raise_for_status()
                bases = sorted({x['symbol'][:-4] for x in r.json() if str(x.get('symbol', '')).endswith('USDT')})
                if bases:
                    with self._lock:
                        self.binance, self.binance_at = bases, time.time()
                    self._save()
            except Exception as e:
                logger.info(f"바이낸스 목록을 받지 못해 예전 목록을 씁니다: {e}")
        todo = [m for m in self._get_markets() if m.split('-', 1)[1] not in self.first_day]
        for k, market in enumerate(todo):
            try:
                if self.pace:
                    self.pace()
                r = s.get(UPBIT_DAYS, params={'market': market, 'count': OLD_DAYS}, timeout=10)
                if r.status_code == 429:
                    time.sleep(2)
                    continue
                r.raise_for_status()
                rows = r.json()
                if rows:
                    first = None if len(rows) >= OLD_DAYS else rows[-1]['candle_date_time_kst'][:10]
                    with self._lock:
                        self.first_day[market.split('-', 1)[1]] = first
            except Exception as e:
                logger.debug(f"{market} 상장일 조회 실패: {e}")
            if k % 20 == 19:
                self._save()
            if not self.pace:
                time.sleep(REQ_GAP)
        if todo:
            self._save()
