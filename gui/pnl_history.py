"""
수익률 기록 (1분 간격)
- 전체 평가수익률 + 코인별 수익률을 SQLite에 저장
- 조회 시 선택한 간격(1분~1년)으로 묶어 각 구간의 마지막 값(종가)을 반환
- 시각은 KST 기준으로 묶는다 (일/주/월/년 경계가 업비트 화면과 같도록)
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 간격: 구간 길이(초). 주·월·년은 달력 기준이라 None
INTERVALS = {
    '1m': 60, '10m': 600, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400,
    '1d': 86400, '1w': None, '1M': None, '1y': None,
}
# 조회 범위를 정할 때 쓰는 구간 길이 근사치(초)
_APPROX_SECONDS = {'1w': 7 * 86400, '1M': 31 * 86400, '1y': 366 * 86400}


class PnlHistory:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._ensure()

    def _ensure(self):
        """표 만들기 (봇 초기화로 폴더가 비워진 뒤에도 다시)"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pnl_total (
                    ts INTEGER PRIMARY KEY,       -- 분 단위로 자른 unix 초
                    total_buy REAL NOT NULL,
                    total_value REAL NOT NULL,
                    rate REAL NOT NULL,
                    krw REAL                      -- 주문가능 원화 (2026-10-06부터 기록)
                )""")
            try:                                  # 예전 표에 원화 칸 더하기
                conn.execute("ALTER TABLE pnl_total ADD COLUMN krw REAL")
            except sqlite3.OperationalError:
                pass
            # 봇 성과 비교: 봇이 산 코인을 산 수량 그대로 팔지 않고 들고 있었다면 (봇 기록에만 씀)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pnl_hold (
                    ts INTEGER PRIMARY KEY,
                    total_buy REAL NOT NULL,
                    total_value REAL NOT NULL,
                    rate REAL NOT NULL
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pnl_coin (
                    ts INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    buy REAL NOT NULL,
                    value REAL NOT NULL,
                    rate REAL NOT NULL,
                    PRIMARY KEY (ts, currency)
                )""")

    @contextmanager
    def _connect(self):
        """연결 → 커밋 → 닫기 (sqlite3 의 with 는 닫지 않아 파일이 잠긴 채로 남음)"""
        if not self.db_path.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def clear(self):
        """기록을 모두 지움 (시뮬레이션 전체 초기화)"""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM pnl_total")
            conn.execute("DELETE FROM pnl_coin")
            conn.execute("DELETE FROM pnl_hold")

    # ── 들고만 있었으면 (봇이 산 코인을 팔지 않았을 때) ──
    def record_hold(self, total_buy: float, total_value: float, ts: Optional[int] = None, replace: bool = True):
        """한 분(또는 지난 시각 채우기) 기록. replace=False면 이미 있는 분은 그대로 (지난 기간 채우기용)"""
        if total_buy <= 0:
            return
        ts = int(ts or time.time()) // 60 * 60
        rate = (total_value - total_buy) / total_buy * 100
        if not self.db_path.exists():
            self._ensure()
        with self._lock, self._connect() as conn:
            conn.execute(f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO pnl_hold VALUES (?, ?, ?, ?)", (ts, total_buy, total_value, rate))

    def record_hold_many(self, rows: List[tuple]):
        """[(ts, total_buy, total_value)] 지난 기간 채우기 — 이미 있는 분은 그대로"""
        rows = [(int(t) // 60 * 60, b, v, (v - b) / b * 100) for t, b, v in rows if b > 0]
        if not rows:
            return
        if not self.db_path.exists():
            self._ensure()
        with self._lock, self._connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO pnl_hold VALUES (?, ?, ?, ?)", rows)

    def hold_count(self, ts0: int, ts1: int) -> int:
        if not self.db_path.exists():
            return 0
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM pnl_hold WHERE ts >= ? AND ts <= ?", (int(ts0), int(ts1))).fetchone()[0]

    def hold_first_ts(self) -> Optional[int]:
        if not self.db_path.exists():
            return None
        with self._connect() as conn:
            return conn.execute("SELECT MIN(ts) FROM pnl_hold").fetchone()[0]

    def record(self, holdings: Dict, ts: Optional[int] = None):
        """/api/holdings와 같은 형식의 holdings를 한 분 기록으로 저장 (상장폐지 코인 제외)"""
        ts = int(ts or time.time()) // 60 * 60
        total_buy = holdings['total_buy_amount']
        total_value = holdings['total_value']
        if total_buy <= 0:
            return
        rate = (total_value - total_buy) / total_buy * 100
        coins = [(ts, a['currency'], a['buy_amount'], a['current_value'], a['pnl_rate'])
                 for a in holdings['assets'] if not a.get('is_delisted') and a['buy_amount'] > 0]
        if not self.db_path.exists():
            self._ensure()
        with self._lock, self._connect() as conn:
            krw = holdings.get('krw_balance')
            conn.execute("INSERT OR REPLACE INTO pnl_total (ts, total_buy, total_value, rate, krw) VALUES (?, ?, ?, ?, ?)",
                         (ts, total_buy, total_value, rate, float(krw) if krw is not None else None))
            conn.executemany("INSERT OR REPLACE INTO pnl_coin VALUES (?, ?, ?, ?, ?)", coins)

    def query(self, interval: str, currency: Optional[str] = None, limit: int = 200, table: str = 'pnl_total') -> List[Dict]:
        """간격별로 묶은 기록: 점마다 '그 시각의 값' = 구간을 시작한 때의 기록 (오래된 순 최대 limit개)
        그래서 같은 시각이면 1분봉 · 10분봉 · 1시간봉 값이 같음 (2026-10-12 전에는 구간 마지막 값이라 어긋났음)
        맨 끝에는 지금 값 한 점(live)을 더 붙임 — 진행 중인 구간도 최신 값까지 보이게
        table='pnl_hold': 들고만 있었으면 (봇 기록)"""
        if interval not in INTERVALS:
            raise ValueError(f'지원하지 않는 간격: {interval}')
        seconds = INTERVALS[interval] or _APPROX_SECONDS[interval]
        since = int(time.time()) - seconds * (limit + 1)
        if not self.db_path.exists():
            return []

        with self._connect() as conn:
            if currency and currency != 'ALL':
                rows = conn.execute(
                    "SELECT ts, buy, value, rate FROM pnl_coin WHERE currency = ? AND ts >= ? ORDER BY ts",
                    (currency, since)).fetchall()
            else:
                tbl = 'pnl_hold' if table == 'pnl_hold' else 'pnl_total'
                rows = conn.execute(
                    f"SELECT ts, total_buy, total_value, rate FROM {tbl} WHERE ts >= ? ORDER BY ts",
                    (since,)).fetchall()

        buckets: Dict[int, tuple] = {}
        for row in rows:
            buckets.setdefault(_bucket_start(row[0], interval), row)   # 오래된 순이라 구간의 첫 기록이 남음

        points = [{'ts': start, 'at': r[0], 'buy': r[1], 'value': r[2], 'rate': r[3]}   # at = 그 값을 기록한 시각
                  for start, r in sorted(buckets.items())]
        if rows and points and rows[-1][0] != points[-1]['at']:
            r = rows[-1]                                                # 지금 값 (진행 중인 구간의 마지막 기록)
            points.append({'ts': r[0], 'at': r[0], 'buy': r[1], 'value': r[2], 'rate': r[3], 'live': True})
        return points[-limit:]

    def prune_coins(self, keep_minutes_days: int = 8):
        """코인별 기록 줄이기: keep_minutes_days일보다 오래된 것은 매시 정각 기록만 남김

        코인별 1분 기록은 하루 10만 줄 이상 쌓이므로, 1분~1시간 그래프에 필요한 최근 기간만
        그대로 두고 나머지는 4시간·일·주·월·년 그래프에 충분한 1시간 간격으로 줄인다.
        (전체 수익률 pnl_total은 1년에 약 25MB라 그대로 둔다)
        """
        cutoff = int(time.time()) - keep_minutes_days * 86400
        with self._lock, self._connect() as conn:
            deleted = conn.execute("DELETE FROM pnl_coin WHERE ts < ? AND ts % 3600 != 0", (cutoff,)).rowcount
        if deleted:
            logger.info(f"오래된 코인별 수익률 기록 {deleted:,}줄 정리 (1시간 간격만 남김)")

    def unrealized_at(self, ts: int) -> Optional[float]:
        """ts 직전(없으면 직후 첫) 기록의 평가손익 (총 평가 − 총 매수) — 오늘 손익의 기준점"""
        if not self.db_path.exists():
            return None
        with self._connect() as conn:
            r = conn.execute("SELECT total_buy, total_value FROM pnl_total WHERE ts < ? ORDER BY ts DESC LIMIT 1", (int(ts),)).fetchone()
            if not r:
                r = conn.execute("SELECT total_buy, total_value FROM pnl_total WHERE ts >= ? ORDER BY ts LIMIT 1", (int(ts),)).fetchone()
        return (r[1] - r[0]) if r else None

    def stats(self) -> Dict:
        if not self.db_path.exists():
            return {'first_ts': None, 'last_ts': None, 'count': 0}
        with self._connect() as conn:
            first, last, count = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM pnl_total").fetchone()
        return {'first_ts': first, 'last_ts': last, 'count': count}


def _bucket_start(ts: int, interval: str) -> int:
    """ts가 속한 구간의 시작 시각(unix 초, KST 경계)"""
    seconds = INTERVALS[interval]
    if seconds:
        offset = 9 * 3600  # KST 자정 기준으로 자르기
        return (ts + offset) // seconds * seconds - offset
    dt = datetime.fromtimestamp(ts, KST)
    if interval == '1w':
        start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif interval == '1M':
        start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # 1y
        start = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


class PnlRecorder:
    """봇 프로그램이 켜져 있는 동안 매 분 수익률을 기록하는 백그라운드 스레드"""

    def __init__(self, history: PnlHistory, fetch_holdings, interval_sec: int = 60):
        self.history = history
        self.fetch_holdings = fetch_holdings  # 호출하면 holdings dict 반환 (실패 시 예외)
        self.interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name='PnlRecorder', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        logger.info("수익률 기록 시작 (1분 간격)")
        last_error = None
        last_prune = 0
        while not self._stop.is_set():
            try:
                self.history.record(self.fetch_holdings())
                last_error = None
                if time.time() - last_prune > 3600:  # 한 시간에 한 번 오래된 코인별 기록 정리
                    self.history.prune_coins()
                    last_prune = time.time()
            except Exception as e:
                # 같은 오류는 한 번만 남긴다 (API 키 없음 등으로 매분 로그가 쌓이지 않게)
                if str(e) != last_error:
                    logger.warning(f"수익률 기록 실패: {e}")
                    last_error = str(e)
            # 다음 분 정각 + 2초에 실행 (분 경계와 기록 시각을 맞춤)
            now = time.time()
            self._stop.wait(self.interval_sec - now % self.interval_sec + 2)
