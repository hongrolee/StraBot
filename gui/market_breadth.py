"""
시장 분위기 기록 (1분 간격) — 전체코인 분석 '시장 현황'의 추이 그래프용
- 원화 마켓 전체: 상승 · 하락 · 보합 코인 수, 평균 등락률(코인마다 같은 무게), 거래대금 가중 평균 등락률
- 등락률은 업비트 기준(전일 종가 = 오전 9시 대비)이라 매일 9시에 0 근처로 다시 시작
- 조회는 보유코인 분석 수익률 기록과 같은 간격 묶기(gui/pnl_history._bucket_start, 한국 시각 경계)
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from gui.pnl_history import INTERVALS, _APPROX_SECONDS, _bucket_start


class MarketBreadth:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS breadth (
                                ts INTEGER PRIMARY KEY,      -- 분 단위로 자른 unix 초
                                up INTEGER NOT NULL, down INTEGER NOT NULL, flat INTEGER NOT NULL,
                                avg_rate REAL NOT NULL,      -- 평균 등락률 (%)
                                wavg_rate REAL NOT NULL      -- 거래대금 가중 평균 등락률 (%)
                            )""")
            # 시장 분위기 기록을 시작하기 전 기간: 업비트 봉으로 계산한 평균 등락률 (전일 9시 대비, 코인마다 같은 무게)
            conn.execute("""CREATE TABLE IF NOT EXISTS eq_hist (
                                ts INTEGER PRIMARY KEY,
                                avg_rate REAL NOT NULL
                            )""")
            # 시장 전체 평균 지수를 매분 코인 가격으로 직접 이어 붙인 값 (2026-10-09부터): 앞 기록과 같은 코인끼리 가격 변화의 평균만큼 움직임
            # — '전일대비'를 쓰지 않아 9시 기준이 바뀌는 15분에도 끊기지 않음. 마지막 가격은 last_px 에 (프로그램을 다시 켜도 이어짐)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(breadth)").fetchall()}
            if 'idx' not in cols:
                conn.execute("ALTER TABLE breadth ADD COLUMN idx REAL")
            conn.execute("CREATE TABLE IF NOT EXISTS last_px (k TEXT PRIMARY KEY, ts INTEGER, idx REAL, px TEXT)")
        self._px = None                                 # {마켓: 가격} 앞 기록 때 (처음 쓸 때 last_px 에서)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def summarize(tickers: Dict[str, Dict]) -> Optional[Dict]:
        """업비트 원화 마켓 시세 {마켓: ticker} → {up, down, flat, avg_rate, wavg_rate} (시세가 없으면 None)"""
        rates, weights = [], []
        for t in tickers.values():
            r = t.get('signed_change_rate')
            if r is None:
                continue
            rates.append(float(r) * 100)
            weights.append(float(t.get('acc_trade_price_24h') or 0))
        if not rates:
            return None
        wsum = sum(weights)
        return {'up': sum(r > 0 for r in rates), 'down': sum(r < 0 for r in rates), 'flat': sum(r == 0 for r in rates),
                'avg_rate': sum(rates) / len(rates),
                'wavg_rate': sum(r * w for r, w in zip(rates, weights)) / wsum if wsum else sum(rates) / len(rates)}

    def record(self, tickers: Dict[str, Dict], ts: Optional[int] = None) -> bool:
        s = self.summarize(tickers)
        if not s:
            return False
        ts = int(ts or time.time()) // 60 * 60
        px = {m: float(t['trade_price']) for m, t in tickers.items() if t.get('trade_price')}
        with self._lock, self._connect() as conn:
            # 지수 = 그날 9시에 같은 금액씩 사서 들고 있던 값 (하루 한 번만 다시 나눔).
            # 2026-10-12 전에는 매분 다시 나눠, 가격이 1틱씩 오르내리기만 해도 지수가 조금씩 올라가는 문제가 있었음 (그 기록은 쓰지 않음)
            if self._px is None:
                row = conn.execute("SELECT ts, idx, px FROM last_px WHERE k='day'").fetchone()
                if row:
                    st = json.loads(row[2])
                    self._px = (row[0], row[1], st.get('day'), st.get('level'), st.get('ref') or {}, st.get('px') or {})
                else:
                    self._px = (None, None, None, None, {}, {})
                    conn.execute("INSERT OR IGNORE INTO last_px VALUES ('v2_since', ?, NULL, '{}')", (ts,))
            last_ts, last_idx, ref_day, level, ref, last_px = self._px
            day = ts // 86400                                     # KST 9시 = UTC 0시
            idx = None
            if last_idx is None:
                idx, level, ref_day, ref = 1.0, 1.0, day, dict(px)     # 처음: 1에서 시작 (그래프에서는 앞 지수에 이어 붙임)
            elif ts == last_ts:
                idx = last_idx
            elif ts > last_ts:
                if day != ref_day:                                # 새 날: 어제 마지막 값에서, 직전 가격을 기준으로 다시 같은 금액씩
                    level, ref_day, ref = last_idx, day, dict(last_px or px)
                rs = [px[m] / ref[m] for m in px if ref.get(m)]
                if len(rs) >= 20:
                    idx = level * sum(rs) / len(rs)
            if idx is not None:
                self._px = (ts, idx, ref_day, level, ref, px)
                conn.execute("INSERT OR REPLACE INTO last_px VALUES ('day', ?, ?, ?)",
                             (ts, idx, json.dumps({'day': ref_day, 'level': level, 'ref': ref, 'px': px})))
            conn.execute("INSERT OR REPLACE INTO breadth (ts, up, down, flat, avg_rate, wavg_rate, idx) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (ts, s['up'], s['down'], s['flat'], s['avg_rate'], s['wavg_rate'], idx))
        return True

    def query(self, interval: str, limit: int = 200) -> List[Dict]:
        """간격별로 묶은 기록 — 각 구간의 마지막 기록, 오래된 순 최대 limit개 {ts, up_ratio, avg_rate, wavg_rate, up, down, flat}"""
        if interval not in INTERVALS:
            raise ValueError(f'지원하지 않는 간격: {interval}')
        since = int(time.time()) - (INTERVALS[interval] or _APPROX_SECONDS[interval]) * (limit + 1)
        with self._connect() as conn:
            rows = conn.execute("SELECT ts, up, down, flat, avg_rate, wavg_rate FROM breadth WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        buckets: Dict[int, tuple] = {}
        for r in rows:
            buckets.setdefault(_bucket_start(r[0], interval), r)        # 점 = 그 시각의 값 (구간 첫 기록) — 간격을 바꿔도 같은 시각은 같은 값

        def point(ts, r, live=False):
            _, up, down, flat, avg, wavg = r
            n = up + down + flat
            p = {'ts': ts, 'up': up, 'down': down, 'flat': flat, 'up_ratio': up / n * 100 if n else 0.0, 'avg_rate': avg, 'wavg_rate': wavg}
            return {**p, 'live': True} if live else p
        out = [point(start, r) for start, r in sorted(buckets.items())]
        if rows and out and rows[-1][0] != sorted(buckets.items())[-1][1][0]:
            out.append(point(rows[-1][0], rows[-1], True))                # 지금 값
        return out[-limit:]

    # ── 시장 전체 평균 지수: 원화 코인 전부를 같은 금액씩 (매일 9시에 같은 금액으로 다시 나눔) ──
    # 하루 안에서는 평균 등락률(전일 9시 대비)이 곧 그날 수익률 → 날마다 이어 붙여 지수로 (KST 9시 = UTC 0시라 날짜 = ts // 86400)
    def index_series(self) -> List[tuple]:
        """[(ts, 지수)] 오래된 순 — 시장 분위기 기록이 있는 때는 그것, 그 전은 봉으로 채운 기록"""
        with self._connect() as conn:
            # 9시 직후 15분은 뺌: 업비트 '전일대비'는 9시 뒤 코인마다 첫 거래가 있어야 새 기준으로 바뀌어
            # 그동안 평균이 어제 값에 머물다 0 근처로 뛰어, 이어 붙이면 지수가 하루 한 번 크게 꺾여 보임
            live = conn.execute("SELECT ts, avg_rate FROM breadth WHERE ts % 86400 >= 900 ORDER BY ts").fetchall()
            start = live[0][0] if live else None
            old = conn.execute("SELECT ts, avg_rate FROM eq_hist WHERE ts < ? ORDER BY ts", (start if start else 2 ** 40,)).fetchall()
            v2 = conn.execute("SELECT ts FROM last_px WHERE k='v2_since'").fetchone()
            # 하루 한 번 나누는 지수(v2) 기록만 이어 붙임 — 그 전(매분 나누던 때)은 평균 등락률로 만든 하루 지수로
            chain = conn.execute("SELECT ts, idx FROM breadth WHERE idx IS NOT NULL AND ts >= ? ORDER BY ts",
                                 (v2[0] if v2 else 2 ** 40,)).fetchall()
        rows = old + live
        out, level_day, cur_day, last_avg = [], 1.0, None, 0.0
        for ts, avg in rows:
            day = ts // 86400
            if cur_day is not None and day != cur_day:
                level_day *= 1 + last_avg / 100                  # 어제 마지막 등락률만큼 오른 값에서 오늘 시작
            cur_day, last_avg = day, avg
            out.append((ts, level_day * (1 + avg / 100)))
        if chain:
            # 가격으로 이어 붙인 지수가 있는 때부터는 그것으로 (9시 근처도 끊기지 않음) — 시작점에서 앞 지수와 같은 높이로 맞춤
            t1 = chain[0][0]
            before = [v for t, v in out if t <= t1]
            scale = (before[-1] if before else 1.0) / chain[0][1]
            out = [(t, v) for t, v in out if t < t1] + [(t, v * scale) for t, v in chain]
        return out

    def eq_hist_count(self, ts0: int, ts1: int) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM eq_hist WHERE ts >= ? AND ts <= ?", (int(ts0), int(ts1))).fetchone()[0]

    def backfill_eq(self, closes_by_market: Dict[str, Dict[int, float]], step: int, since: int, until: int) -> int:
        """봉 종가 {마켓: {봉이 끝난 시각: 종가}}로 since~until 의 평균 등락률(전일 9시 대비)을 step 간격으로 채움 (이미 있으면 그대로)"""
        import bisect
        series = {m: sorted(c.items()) for m, c in closes_by_market.items() if c}

        def price_at(arr, t):
            k = bisect.bisect_right(arr, (t, float('inf'))) - 1
            return arr[k][1] if k >= 0 and t - arr[k][0] < 2 * step else None

        rows = []
        t = since // step * step + step
        while t <= until:
            rec = t - 60                                         # 그 봉의 마지막 분
            day_start = rec // 86400 * 86400                     # 그날 오전 9시 (KST)
            rates = []
            for arr in series.values():
                p, ref = price_at(arr, t), price_at(arr, day_start)
                if p and ref:
                    rates.append((p / ref - 1) * 100)
            if len(rates) >= 20:
                rows.append((rec, sum(rates) / len(rates)))
            t += step
        if rows:
            with self._lock, self._connect() as conn:
                conn.executemany("INSERT OR IGNORE INTO eq_hist VALUES (?, ?)", rows)
        return len(rows)

    def stats(self) -> Dict:
        with self._connect() as conn:
            first, last, count = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM breadth").fetchone()
        return {'first_ts': first, 'last_ts': last, 'count': count}

    def prune(self, keep_minutes_days: int = 8) -> int:
        """keep_minutes_days일보다 오래된 것은 매시 정각 기록만 남김 (1년에 약 9천 줄)"""
        cutoff = int(time.time()) - keep_minutes_days * 86400
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM breadth WHERE ts < ? AND ts % 3600 != 0", (cutoff,)).rowcount
