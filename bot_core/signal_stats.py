"""
조건 · 패턴 신뢰도 기록: 신호가 뜬 뒤 1 · 3 · 7일 뒤 맞았는지

- 일봉 · 조건 기본 숫자 · 봇과 같은 계산(conditions.py). 신호 = 조건이 '처음 맞은 날' (전날은 안 맞음)
- 맞음: 상승 신호 → 그 뒤 종가가 신호 날 종가보다 높음, 하락 신호 → 낮음. 상태(방향 없음)는 '오른 비율'만
- 시장 비교: 같은 날 모든 원화 코인을 샀다면 그만큼 오른(내린) 비율 · 평균 등락 → 신호 날들 평균과 견줌
- 기록은 SQLite (signal_log.db) 에 계속 쌓임: 일봉 · 신호 · 결과 · 날마다 시장 기준
  처음엔 업비트 일봉 약 400일로 지난 신호를 채우고, 그 뒤로는 하루 한 번 새 날만 더함
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bot_core import conditions as cd

HORIZONS = (1, 3, 7)
LONG = 120                      # 조건 계산에 쓰는 지난 봉 수 (conditions 긴 칸과 같음)
SIGNAL_UP = {'golden_cross', 'ma_cross_up', 'macd_cross_up', 'ma_aligned', 'aligned_pullback', 'new_high', 'squeeze_break', 'stoch_up',
             'rsi_cross_up', 'bb_lower_up', 'dip_rebound', 'stronger_than_btc', 'streak_up', 'above_ma_close', 'above_ema_close'}
SIGNAL_DOWN = {'dead_cross', 'ma_cross_down', 'macd_cross_down', 'rsi_cross_down', 'bb_upper_down', 'streak_down', 'below_ma_close', 'below_ema_close'}
PATTERN_SELL = {'shooting_star', 'bear_engulf', 'evening_star', 'three_black', 'double_top', 'rsi_div_down',
                'bear_flag', 'bear_pennant', 'inv_cup_handle', 'desc_triangle', 'triangle_down', 'measured_down', 'desc_scallop',
                'three_desc_peaks', 'diamond_top', 'rect_top', 'hs_top'}
# 일봉만으로는 판단할 수 없는 조건 (실시간 1분 · 1시간봉 · 코인 성격 · 주봉/연간 · 비트코인 비교)
SKIP = {'buy_flow_spike', 'spike_pump', 'pre_spike', 'surge_24h', 'new_listing', 'value_top', 'spike_prone',
        'near_year_low', 'far_from_high', 'stronger_than_btc', 'all_coins',
        'old_listing', 'value_under', 'price_range', 'on_binance', 'not_on_binance', 'buy_targets', 'pick_coins',
        'hongro2_buy', 'hongro2_sell', 'held_days'}                     # 전략 한 쌍: 봇 규칙 전용 (팔기는 산 값이 있어야 계산됨)


def signal_dir(cid: str) -> str:
    """'up' 상승 신호 · 'down' 하락 신호 · 'state' 상태 (화면 signalDir 과 같음)"""
    if cid in SIGNAL_UP:
        return 'up'
    if cid in SIGNAL_DOWN:
        return 'down'
    if cid in cd.PATTERN_IDS:
        return 'down' if cid in PATTERN_SELL else 'up'
    return 'state'


def _conds(customs: List[Dict]):
    out = []
    for cid in [*cd.PRESETS.keys(), *[c.get('id') for c in customs or []]]:
        if not cid or cid in SKIP:
            continue
        cond = cd.resolve(cid, customs or [])
        if cond and not cond.get('needs_flow'):
            out.append((cid, cond['test'], dict(cond['params']), 'state' if cond.get('custom') else signal_dir(cid)))
    return out


def cond_signatures(customs: List[Dict]) -> Dict[str, str]:
    """조건마다 '내용 표시' — 새로 생기거나 바뀌면 그 조건만 지난 기간까지 다시 채움 (기본 숫자 · 나의 조건 규칙)"""
    out = {}
    for cid, _test, params, _dr in _conds(customs):
        spec = next((c for c in customs or [] if c.get('id') == cid), None)
        body = {k: spec.get(k) for k in ('rules', 'match')} if spec else params
        out[cid] = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return out


def scan_market(args) -> List[tuple]:
    """한 코인: 일봉(오래된 순 [(day, h, l, c, v)]) 에서 from_idx 이후 날마다 '처음 맞은' 조건 → [(cond, day, dir, entry)]
    (프로세스로 나눠 돌리므로 맨 위 함수 · 넘기는 값은 단순 자료만)"""
    market, rows, from_idx, customs, *rest = args
    only = set(rest[0]) if rest and rest[0] else None          # 이 조건들만 (새로 생긴 · 바뀐 조건 채우기)
    end = rest[1] if len(rest) > 1 and rest[1] is not None else len(rows)
    conds = [x for x in _conds(customs) if only is None or x[0] in only]
    cur = market.split('-', 1)[-1]
    out = []
    prev_on = set()
    start = max(LONG + 1, from_idx - 1)                 # 하루 앞부터 봐야 '처음 맞은 날'을 앎
    for t in range(start, end):
        lo = max(0, t - LONG)
        w = rows[lo:t]
        w30 = w[-30:]
        d = {'day_closes_xl': [r[3] for r in rows[max(0, t - 199):t]],
             'day_highs_long': [r[1] for r in w], 'day_lows_long': [r[2] for r in w], 'day_closes_long': [r[3] for r in w],
             'day_values_long': [r[4] for r in w],
             'day_closes': [r[3] for r in w30], 'day_highs': [r[1] for r in w30], 'day_lows': [r[2] for r in w30], 'day_values': [r[4] for r in w30]}
        day, h, l, c, v = rows[t]
        prev = rows[t - 1][3]
        coin = {'currency': cur, 'korean_name': '', 'current_price': c, 'today_low': l, 'prev_close': prev,
                'change_rate': (c / prev - 1) * 100 if prev else 0, 'trade_value_24h': v}
        on = set()
        for cid, test, p, dr in conds:
            try:
                m = test(dict(coin), p, d)
            except Exception:
                m = None
            if m is not None:
                on.add(cid)
                if t >= from_idx and cid not in prev_on:
                    out.append((cid, day, dr, c))
        prev_on = on
    return out


class SignalStats:
    """기록 저장소 + 하루 한 번 갱신"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.status = {'running': False, 'step': '', 'done': 0, 'total': 0, 'error': ''}
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candles (market TEXT, day TEXT, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (market, day));
                CREATE TABLE IF NOT EXISTS signals (cond TEXT, market TEXT, day TEXT, dir TEXT, entry REAL, r1 REAL, r3 REAL, r7 REAL,
                                                    PRIMARY KEY (cond, market, day));
                CREATE INDEX IF NOT EXISTS signals_cond ON signals (cond, day);
                CREATE TABLE IF NOT EXISTS scanned (market TEXT PRIMARY KEY, last_day TEXT);
                CREATE TABLE IF NOT EXISTS base (day TEXT, h INTEGER, up REAL, avg REAL, n INTEGER, PRIMARY KEY (day, h));
                CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """)

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def meta(self, k: str) -> Optional[str]:
        with self._db() as db:
            r = db.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, db, k, v):
        db.execute('INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)', (k, str(v)))

    # ── 갱신: 일봉 받기 → 새 날 신호 찾기 → 결과 · 시장 기준 채우기 ──
    def update(self, markets: List[str], get_candles: Callable, customs: List[Dict], workers: int = 4) -> Dict:
        """markets: ['KRW-BTC', ...]. get_candles(unit, market, count, to) → 업비트 캔들(최신순)"""
        if not self.lock.acquire(blocking=False):
            return {'skipped': 'running'}
        try:
            self.status.update(running=True, error='', done=0, total=len(markets), step='일봉 받는 중')
            markets = [m for m in markets if not cd.is_stable_coin({'currency': m.split('-', 1)[-1], 'korean_name': ''})]
            self._fetch(markets, get_candles)
            self._backfill_changed(markets, customs, workers)
            self.status.update(step='신호 찾는 중', done=0)
            new = self._scan(markets, customs, workers)
            self.status.update(step='결과 매기는 중')
            self._fill_outcomes()
            self._fill_base()
            with self._db() as db:
                self._set_meta(db, 'updated', time.time())
            return {'new_signals': new}
        except Exception as e:
            self.status['error'] = str(e)
            raise
        finally:
            self.status.update(running=False, step='')
            self.lock.release()

    def _fetch(self, markets, get_candles):
        from datetime import datetime, timezone
        with self._db() as db:
            have = dict(db.execute('SELECT market, MAX(day) FROM candles GROUP BY market').fetchall())
        for i, m in enumerate(markets):
            self.status['done'] = i
            try:
                rows = []
                if m not in have:                      # 처음: 약 400일 (200개씩 두 번)
                    a = get_candles('days', m, 200) or []
                    rows += a
                    if len(a) == 200:
                        rows += get_candles('days', m, 200, a[-1]['candle_date_time_utc'] + 'Z') or []
                else:
                    gap = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.strptime(have[m], '%Y-%m-%d')).days
                    rows = get_candles('days', m, min(200, gap + 3)) or []
                today = datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d')      # 업비트 일봉은 UTC 0시(KST 9시)에 시작 — 이 날짜 봉은 아직 진행 중
                done = [(m, r['candle_date_time_kst'][:10], r['high_price'], r['low_price'], r['trade_price'], r.get('candle_acc_trade_price'))
                        for r in rows if r.get('candle_date_time_utc', '')[:10] < today]    # 진행 중인 오늘 봉은 뺌
                if done:
                    with self._db() as db:
                        db.executemany('INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?)', done)
            except Exception:
                continue

    def _backfill_changed(self, markets, customs, workers):
        """새로 생긴 · 바뀐 조건만 지난 기간(이미 본 날까지) 채우기, 지운 조건 기록은 정리"""
        sigs = cond_signatures(customs)
        stored_raw = self.meta('cond_sigs')
        with self._db() as db:
            last = dict(db.execute('SELECT market, last_day FROM scanned').fetchall())
            if stored_raw is None:
                # 처음 이 기능이 생긴 때: 지금까지 기록은 모든 조건으로 채운 것이라 그대로 인정
                self._set_meta(db, 'cond_sigs', json.dumps(sigs, ensure_ascii=False))
                return
        stored = json.loads(stored_raw or '{}')
        changed = [cid for cid, sig in sigs.items() if stored.get(cid) != sig]
        removed = [cid for cid in stored if cid not in sigs]
        if removed or changed:
            with self._db() as db:
                db.executemany('DELETE FROM signals WHERE cond=?', [(c,) for c in removed + changed])
        if changed and last:
            self.status.update(step=f'새 조건 {len(changed)}개 지난 기간 채우는 중', done=0, total=len(last))
            jobs = []
            with self._db() as db:
                for m in markets:
                    if m not in last:
                        continue
                    rows = [tuple(r) for r in self._rows(db, m)]
                    days = [r[0] for r in rows]
                    end = next((i + 1 for i in range(len(days) - 1, -1, -1) if days[i] <= last[m]), 0)
                    if end > LONG + 1:
                        jobs.append((m, rows, LONG + 1, customs, changed, end))
            self._run_jobs(jobs, workers, mark_scanned=False)
        with self._db() as db:
            self._set_meta(db, 'cond_sigs', json.dumps(sigs, ensure_ascii=False))

    def _rows(self, db, m):
        return db.execute('SELECT day, h, l, c, v FROM candles WHERE market=? ORDER BY day', (m,)).fetchall()

    def _scan(self, markets, customs, workers) -> int:
        jobs = []
        with self._db() as db:
            last = dict(db.execute('SELECT market, last_day FROM scanned').fetchall())
            for m in markets:
                rows = [tuple(r) for r in self._rows(db, m)]
                if len(rows) < LONG + 2:
                    continue
                days = [r[0] for r in rows]
                from_idx = LONG + 1 if m not in last else next((i for i, dd in enumerate(days) if dd > last[m]), len(days))
                if from_idx < len(rows):
                    jobs.append((m, rows, from_idx, customs))
        return self._run_jobs(jobs, workers)

    def _run_jobs(self, jobs, workers, mark_scanned: bool = True) -> int:
        """scan_market 묶음 실행 → 신호 저장 (mark_scanned: 코인마다 어디까지 봤는지 기록)"""
        if not jobs:
            return 0
        results = []
        if workers > 1 and len(jobs) > 4 and sum(len(j[1]) - j[2] for j in jobs) > 2000:
            # 처음 채울 때처럼 많으면 여러 프로세스로 (화면이 느려지지 않게) · 안 되면 한 줄로
            try:
                from concurrent.futures import ProcessPoolExecutor
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    for i, res in enumerate(pool.map(scan_market, jobs, chunksize=4)):
                        results.append((jobs[i][0], jobs[i][1][-1][0], res))
                        self.status['done'] = i + 1
            except Exception:
                results = []
        if not results:
            for i, j in enumerate(jobs):
                results.append((j[0], j[1][-1][0], scan_market(j)))
                self.status['done'] = i + 1
        n = 0
        with self._db() as db:
            for m, last_day, res in results:
                db.executemany('INSERT OR IGNORE INTO signals (cond, market, day, dir, entry) VALUES (?, ?, ?, ?, ?)',
                               [(cid, m, day, dr, entry) for cid, day, dr, entry in res])
                if mark_scanned:
                    db.execute('INSERT OR REPLACE INTO scanned VALUES (?, ?)', (m, last_day))
                n += len(res)
        return n

    def _fill_outcomes(self):
        with self._db() as db:
            todo = db.execute('SELECT DISTINCT market FROM signals WHERE r1 IS NULL OR r3 IS NULL OR r7 IS NULL').fetchall()
            for (m,) in todo:
                rows = self._rows(db, m)
                idx = {r[0]: i for i, r in enumerate(rows)}
                upd = []
                for cid, day, entry, r1, r3, r7 in db.execute(
                        'SELECT cond, day, entry, r1, r3, r7 FROM signals WHERE market=? AND (r1 IS NULL OR r3 IS NULL OR r7 IS NULL)', (m,)).fetchall():
                    i = idx.get(day)
                    if i is None or not entry:
                        continue
                    rs = [(rows[i + hh][3] / entry - 1) * 100 if i + hh < len(rows) else None for hh in HORIZONS]
                    upd.append((*rs, cid, m, day))
                db.executemany('UPDATE signals SET r1=?, r3=?, r7=? WHERE cond=? AND market=? AND day=?', upd)

    def _fill_base(self):
        """날마다 시장 기준: 모든 코인을 그날 종가에 샀다면 h일 뒤 오른 비율 · 평균 등락(%)"""
        with self._db() as db:
            done = {(d, h) for d, h in db.execute('SELECT day, h FROM base').fetchall()}
            per = {}
            for (m,) in db.execute('SELECT DISTINCT market FROM candles').fetchall():
                rows = self._rows(db, m)
                for i, r in enumerate(rows):
                    for hh in HORIZONS:
                        if i + hh < len(rows) and (r[0], hh) not in done and r[3]:
                            per.setdefault((r[0], hh), []).append((rows[i + hh][3] / r[3] - 1) * 100)
            db.executemany('INSERT OR REPLACE INTO base VALUES (?, ?, ?, ?, ?)',
                           [(d, hh, sum(1 for x in v if x > 0) / len(v) * 100, sum(v) / len(v), len(v)) for (d, hh), v in per.items() if len(v) >= 20])

    # ── 화면용 요약 ──
    def summary(self, recent: int = 12) -> Dict:
        with self._db() as db:
            base = {(d, h): (up, avg) for d, h, up, avg in db.execute('SELECT day, h, up, avg FROM base').fetchall()}
            rng = db.execute('SELECT MIN(day), MAX(day), COUNT(DISTINCT market) FROM signals').fetchone()
            stats = {}
            rows = db.execute('SELECT cond, market, day, dir, r1, r3, r7 FROM signals ORDER BY day').fetchall()
        for cid, m, day, dr, *rs in rows:
            s = stats.setdefault(cid, {'dir': dr, 'n': 0, 'h': {str(h): {'n': 0, 'hit': 0, 'sum': 0.0, 'base': 0.0, 'bsum': 0.0} for h in HORIZONS}, 'recent': []})
            s['n'] += 1
            for hh, r in zip(HORIZONS, rs):
                if r is None:
                    continue
                b = base.get((day, hh))
                if not b:
                    continue
                x = s['h'][str(hh)]
                x['n'] += 1
                x['hit'] += 1 if ((r < 0) if dr == 'down' else (r > 0)) else 0
                x['sum'] += r
                x['base'] += (100 - b[0]) if dr == 'down' else b[0]       # 같은 날 아무 코인: 내린(오른) 비율
                x['bsum'] += b[1]
            s['recent'].append([day, m, rs[1]])
        for s in stats.values():
            for x in s['h'].values():
                n = x.pop('n')
                x.update(n=n, hit=x['hit'] / n * 100 if n else None, avg=x['sum'] / n if n else None,
                         base=x['base'] / n if n else None, base_avg=x['bsum'] / n if n else None)
                x['excess'] = (x['avg'] - x['base_avg']) if n else None
                x.pop('sum')
                x.pop('bsum')
            s['recent'] = s['recent'][-recent:][::-1]
        return {'updated': float(self.meta('updated') or 0), 'from': rng[0], 'to': rng[1], 'coins': rng[2] or 0,
                'horizons': list(HORIZONS), 'stats': stats, 'status': dict(self.status)}
