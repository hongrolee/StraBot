"""
백테스트용 캔들 저장소 — 요청한 코인 · 봉 · 기간만 업비트에서 받아 이 PC(data/candles.db)에 쌓아 둔다.

- candles: 봉 하나씩 (market, unit, t=봉 시작 UTC 초, o, h, l, c, v=거래대금)
- spans:   '받아 본 구간' [start, end) — 거래가 없어 봉이 비어 있는 시간도 다시 받지 않게
- get_range(): 요청 구간 중 받아 본 적 없는 부분만 받아 채운 뒤, 저장된 봉을 오래된 순으로 돌려줌
- 아직 끝나지 않은 봉(진행 중)은 저장하지 않음 → 다음 요청 때 다시 받음
"""
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# 화면 단위 → (업비트 캔들 경로, 봉 길이 초)
UNITS = {
    '1': ('minutes/1', 60), '5': ('minutes/5', 300), '10': ('minutes/10', 600), '15': ('minutes/15', 900), '30': ('minutes/30', 1800),
    '60': ('minutes/60', 3600), '240': ('minutes/240', 14400),
    'day': ('days', 86400), 'week': ('weeks', 604800),
    'month': ('months', 31 * 86400),          # 월봉: 길이가 달마다 달라 31일로 어림 (끝난 봉 판단 · 페이지 수 셈용)
}
PAGE = 200                                   # 업비트 한 번에 최대 200개


def _ts(s: str) -> int:
    return int(datetime.strptime(s, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp())


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def subtract(want: Tuple[int, int], have: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """want 구간에서 have 구간들을 뺀 나머지 (빈 구간들, 오래된 순)"""
    a, b = want
    gaps, cur = [], a
    for s, e in sorted(have):
        if e <= cur or s >= b:
            continue
        if s > cur:
            gaps.append((cur, min(s, b)))
        cur = max(cur, e)
        if cur >= b:
            break
    if cur < b:
        gaps.append((cur, b))
    return gaps


def merge(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    out: List[List[int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


class CandleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS candles (market TEXT, unit TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,'
                       ' PRIMARY KEY (market, unit, t))')
            db.execute('CREATE TABLE IF NOT EXISTS spans (market TEXT, unit TEXT, start INTEGER, "end" INTEGER)')

    def _db(self):
        return sqlite3.connect(str(self.path), timeout=10)

    def spans(self, market: str, unit: str) -> List[Tuple[int, int]]:
        with self._db() as db:
            return [(s, e) for s, e in db.execute('SELECT start, "end" FROM spans WHERE market=? AND unit=? ORDER BY start', (market, unit))]

    def missing(self, market: str, unit: str, start: int, end: int, now: Optional[float] = None) -> List[Tuple[int, int]]:
        """받아야 할 빈 구간들 (끝은 지금 시각까지만)"""
        end = min(end, int(now or time.time()))
        if end <= start:
            return []
        return subtract((start, end), self.spans(market, unit))

    def pages_needed(self, market: str, unit: str, start: int, end: int, now: Optional[float] = None) -> int:
        sec = UNITS[unit][1]
        return sum(max(1, -(-(e - s) // (sec * PAGE))) for s, e in self.missing(market, unit, start, end, now))

    def _save(self, market: str, unit: str, rows: List[tuple], span: Tuple[int, int]):
        with self._lock, self._db() as db:
            if rows:
                db.executemany('INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)', [(market, unit, *r) for r in rows])
            if span[1] > span[0]:
                old = self.spans(market, unit)
                db.execute('DELETE FROM spans WHERE market=? AND unit=?', (market, unit))
                db.executemany('INSERT INTO spans VALUES (?,?,?,?)', [(market, unit, s, e) for s, e in merge(old + [span])])

    def fetch_gap(self, market: str, unit: str, gap: Tuple[int, int], fetch: Callable, now: float,
                  on_page: Optional[Callable[[], None]] = None):
        """빈 구간 하나를 끝(gap[1])에서부터 200개씩 거꾸로 받아 저장. 끝난 봉만 저장하고, 받아 본 구간도 기록"""
        path, sec = UNITS[unit]
        a, b = gap
        # 받아 본 구간의 끝: 진행 중인 봉 시작 전까지 (그 봉은 다음에 다시 받음)
        cap = min(b, int(now // sec * sec) if unit not in ('week', 'month') else int(now - sec))
        to = b
        while to > a:
            raw = fetch(path, market, PAGE, _iso(to)) or []
            if on_page:
                on_page()
            rows, stamps = [], []
            for c in raw:
                t = c.get('candle_date_time_utc')
                if not t:
                    continue
                t = _ts(t)
                stamps.append(t)
                if t + sec > now:                   # 아직 안 끝난 봉
                    cap = min(cap, t)
                    continue
                if a <= t < b:
                    cl = c.get('trade_price')
                    rows.append((t, c.get('opening_price', cl), c.get('high_price', cl), c.get('low_price', cl), cl,
                                 c.get('candle_acc_trade_price') or 0.0))
            if not stamps:                          # 이보다 앞선 봉이 없음 (상장 전)
                self._save(market, unit, [], (a, min(to, cap)))
                break
            oldest = min(stamps)
            first_page_end = min(to, cap)
            self._save(market, unit, rows, (max(a, oldest) if len(raw) >= PAGE else a, first_page_end))
            if len(raw) < PAGE or oldest <= a:      # 상장 때까지 다 받았거나 구간 앞까지 옴
                break
            to = oldest

    def get_range(self, market: str, unit: str, start: int, end: int, fetch: Callable,
                  on_progress: Optional[Callable[[int, int], None]] = None, now: Optional[float] = None) -> List[list]:
        """[start, end) 의 봉 [[t,o,h,l,c,v], ...] 오래된 순. 받아 본 적 없는 구간만 fetch 로 받음
        fetch(path, market, count, to_iso) → 업비트 캔들 JSON(최신순). on_progress(받은 페이지, 받을 페이지)"""
        now = float(now or time.time())
        gaps = self.missing(market, unit, start, end, now)
        total = sum(max(1, -(-(e - s) // (UNITS[unit][1] * PAGE))) for s, e in gaps)
        got = [0]

        def page():
            got[0] += 1
            if on_progress:
                on_progress(got[0], max(total, got[0]))
        for g in reversed(gaps):                    # 최근 구간부터
            self.fetch_gap(market, unit, g, fetch, now, page)
        with self._db() as db:
            return [list(r) for r in db.execute('SELECT t, o, h, l, c, v FROM candles WHERE market=? AND unit=? AND t>=? AND t<? ORDER BY t',
                                                (market, unit, start, end))]
