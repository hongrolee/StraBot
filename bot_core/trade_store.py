"""
거래 기록 저장소 (모드마다 SQLite 한 파일: data/trades_sim.db · data/trades_upbit.db)

- fills: 거래 내역 탭의 한 줄 = 체결 한 건 (시뮬레이션은 가상 주문, 업비트는 받은 체결)
- bot_trades: 봇이 사고판 기록 전부 (봇 · 이유 · 손익 · 그 순간 봇 총 자산) — trades.json 은 최근 것만 두는 빠른 기록
- order_fills: 업비트 주문별 체결 목록 (끝난 주문은 바뀌지 않아 다시 묻지 않게)
- meta: 옮기기 완료 · 받아 둔 업비트 구간 · 봇 초기화 시각 같은 작은 값

JSON 파일처럼 통째로 다시 쓰지 않고 한 줄씩 더하므로 기록이 많아져도 느려지지 않고,
화면은 필요한 만큼(기간 · 종류 · 코인으로 걸러 500건씩)만 읽는다. 시뮬레이션은 오래된 기록을 지울 수 있다(prune).
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# 주문번호 없이 들어간 옛 줄 중, 같은 체결(시각 · 코인 · 매수/매도 · 수량)이 주문번호와 함께 이미 있는 것은 같은 거래 → 빼고 셈
NOT_DUP_FILL = ("NOT (order_id IS NULL AND EXISTS (SELECT 1 FROM fills g WHERE g.order_id IS NOT NULL AND g.time = fills.time "
                "AND g.currency = fills.currency AND g.kind = fills.kind AND ABS(g.volume - fills.volume) < 1e-9))")
FILL_COLS = ('ts', 'time', 'order_id', 'order_time', 'currency', 'market', 'kind', 'volume', 'price', 'amount', 'fee', 'settle')
BOT_COLS = ('bot_id', 'bot_name', 'ts', 'time', 'symbol', 'side', 'price', 'qty', 'fee', 'total', 'profit',
            'reason', 'buy_reason', 'order_id', 'avg_cost', 'snap')
MATCH_WINDOW = 180          # 주문번호가 없을 때 봇 기록과 짝짓는 시각 차이 (초)
BOT_INSERT = f"INSERT OR IGNORE INTO bot_trades({','.join(BOT_COLS)}) VALUES ({','.join('?' * len(BOT_COLS))})"


def bot_row(bot_id: str, bot_name: str, ts: int, item: Dict, avg_cost: Optional[float]) -> tuple:
    snap = item.get('snap')
    return (bot_id, bot_name, ts, str(item['time']), item.get('symbol'), item.get('side'), item.get('price'),
            item.get('qty'), item.get('fee'), item.get('total'), item.get('profit'), item.get('reason'),
            item.get('buy_reason'), item.get('order_id'), avg_cost if item.get('side') == 'buy' else None,
            json.dumps(snap, ensure_ascii=False) if snap else None)


def to_ts(t) -> int:
    """ISO 시각 → unix 초 (시간대가 없으면 이 PC 시각으로 봄)"""
    if isinstance(t, (int, float)):
        return int(t)
    d = datetime.fromisoformat(str(t))
    if d.tzinfo is None:
        d = d.astimezone()
    return int(d.timestamp())


class TradeStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure(self):
        with self._lock, self._conn() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript("""
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL, time TEXT NOT NULL, order_id TEXT, order_time TEXT,
                    currency TEXT NOT NULL, market TEXT, kind TEXT NOT NULL,
                    volume REAL, price REAL, amount REAL, fee REAL, settle REAL);
                CREATE UNIQUE INDEX IF NOT EXISTS fills_uniq ON fills(order_id, time, price, volume);
                CREATE INDEX IF NOT EXISTS fills_ts ON fills(ts);
                CREATE INDEX IF NOT EXISTS fills_cur ON fills(currency, ts);
                CREATE TABLE IF NOT EXISTS bot_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL, bot_name TEXT, ts INTEGER NOT NULL, time TEXT NOT NULL,
                    symbol TEXT NOT NULL, side TEXT NOT NULL, price REAL, qty REAL, fee REAL, total REAL, profit REAL,
                    reason TEXT, buy_reason TEXT, order_id TEXT, avg_cost REAL, snap TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS bot_trades_uniq ON bot_trades(bot_id, time, symbol, side, qty);
                CREATE INDEX IF NOT EXISTS bot_trades_bot ON bot_trades(bot_id, ts);
                CREATE INDEX IF NOT EXISTS bot_trades_sym ON bot_trades(symbol, side, ts);
                CREATE INDEX IF NOT EXISTS bot_trades_order ON bot_trades(order_id);
                CREATE TABLE IF NOT EXISTS order_fills (uuid TEXT PRIMARY KEY, trades TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """)
            # 성적에서 뺄 거래 (맡기기 전부터 있던 손실을 판 것 등) — 기록은 그대로, 승률 · 오늘 판 손익에서만 뺌
            cols = {r[1] for r in c.execute('PRAGMA table_info(bot_trades)').fetchall()}
            if 'excluded' not in cols:
                c.execute('ALTER TABLE bot_trades ADD COLUMN excluded INTEGER DEFAULT 0')

    # ── meta ──
    def get_meta(self, k: str, default=None):
        with self._lock, self._conn() as c:
            row = c.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row['v'])
        except Exception:
            return default

    def set_meta(self, k: str, v):
        with self._lock, self._conn() as c:
            c.execute('INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)', (k, json.dumps(v, ensure_ascii=False)))

    # ── 체결 (거래 내역 줄) ──
    def add_fills(self, rows: Iterable[Dict]) -> int:
        vals = []
        for r in rows:
            if r.get('kind') not in ('매수', '매도') or not r.get('currency'):
                continue
            try:
                ts = to_ts(r['time'])
            except Exception:
                continue
            vals.append((ts, str(r['time']), r.get('order_id'), r.get('order_time'), r['currency'], r.get('market') or 'KRW',
                         r['kind'], r.get('volume'), r.get('price'), r.get('amount'), r.get('fee'), r.get('settle')))
        if not vals:
            return 0
        with self._lock, self._conn() as c:
            before = c.total_changes
            c.executemany(f"INSERT OR IGNORE INTO fills({','.join(FILL_COLS)}) VALUES ({','.join('?' * len(FILL_COLS))})", vals)
            return c.total_changes - before

    @staticmethod
    def _fill_where(since_ts=None, kind=None, currencies=None, q=None, bot=None):
        where, args = [], []
        if bot:
            # 거래 내역의 '봇' 칸과 같은 짝짓기: 주문번호가 같거나, 같은 코인 · 같은 방향에서 시각이 MATCH_WINDOW 안
            match = (f"(b.order_id = fills.order_id OR (b.symbol = 'KRW-' || fills.currency"
                     f" AND b.side = (CASE fills.kind WHEN '매수' THEN 'buy' ELSE 'sell' END)"
                     f" AND b.ts BETWEEN fills.ts - {MATCH_WINDOW} AND fills.ts + {MATCH_WINDOW}))")
            if bot == 'none':
                where.append(f"NOT EXISTS (SELECT 1 FROM bot_trades b WHERE {match})")
            else:
                where.append(f"EXISTS (SELECT 1 FROM bot_trades b WHERE b.bot_id = ? AND {match})"); args.append(str(bot))
        if since_ts:
            where.append('ts >= ?'); args.append(int(since_ts))
        if kind in ('매수', '매도'):
            where.append('kind = ?'); args.append(kind)
        cur_terms = []
        if currencies is not None:
            cs = [x for x in currencies if x]
            if cs:
                cur_terms.append(f"currency IN ({','.join('?' * len(cs))})"); args.extend(cs)
        if q:
            cur_terms.append('currency LIKE ?'); args.append(f'%{str(q).upper()}%')
        if currencies is not None or q:
            where.append('(' + (' OR '.join(cur_terms) or '0') + ')')
        return where, args

    @staticmethod
    def _orders_sql(where: List[str]) -> str:
        """한 주문이 여러 번에 나눠 체결된 줄을 주문 하나로 묶음 (같은 주문번호 · 코인 · 매수/매도)
        수량 · 금액 · 수수료 · 정산금액은 더하고, 단가 = 금액 합 ÷ 수량 합(평균), 시각 = 마지막 체결, fills = 체결 횟수"""
        base = f"SELECT * FROM fills WHERE {' AND '.join([NOT_DUP_FILL] + list(where))}"
        return f"""SELECT MAX(id) AS id, MAX(ts) AS ts, MAX(time) AS time, order_id, MIN(order_time) AS order_time, currency,
                       MAX(market) AS market, kind, SUM(volume) AS volume,
                       CASE WHEN SUM(volume) > 0 THEN SUM(amount) / SUM(volume) ELSE MAX(price) END AS price,
                       SUM(amount) AS amount, SUM(fee) AS fee, SUM(settle) AS settle, COUNT(*) AS fills
                FROM ({base}) GROUP BY COALESCE(order_id, 'id:' || id), currency, kind"""

    def fills_page(self, since_ts=None, kind=None, currencies=None, q=None, before: Optional[str] = None,
                   limit: int = 500, bot: Optional[str] = None) -> Tuple[List[Dict], int, Optional[str]]:
        """최신순 한 쪽 → (줄, 조건에 맞는 전체 수, 다음 쪽 커서 'ts:id' 또는 None) — 나눠 체결된 주문은 한 줄로"""
        where, args = self._fill_where(since_ts, kind, currencies, q, bot)
        g = self._orders_sql(where)
        with self._lock, self._conn() as c:
            total = c.execute(f"SELECT COUNT(*) FROM ({g})", args).fetchone()[0]
            page_where, a2 = [], list(args)
            if before:
                bts, bid = (int(x) for x in str(before).split(':'))
                page_where.append('(ts < ? OR (ts = ? AND id < ?))'); a2.extend([bts, bts, bid])
            rows = c.execute(f"SELECT * FROM ({g}){' WHERE ' + ' AND '.join(page_where) if page_where else ''} ORDER BY ts DESC, id DESC LIMIT ?",
                             a2 + [int(limit) + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        cursor = f"{rows[-1]['ts']}:{rows[-1]['id']}" if (more and rows) else None
        return [self._fill_dict(r) for r in rows], total, cursor

    def fills_all(self, since_ts=None, kind=None, currencies=None, q=None, bot: Optional[str] = None) -> List[Dict]:
        """조건에 맞는 전부 (최신순, 나눠 체결된 주문은 한 줄로) — 코인 차트 · CSV 저장용"""
        where, args = self._fill_where(since_ts, kind, currencies, q, bot)
        with self._lock, self._conn() as c:
            rows = c.execute(f"SELECT * FROM ({self._orders_sql(where)}) ORDER BY ts DESC, id DESC", args).fetchall()
        return [self._fill_dict(r) for r in rows]

    @staticmethod
    def _fill_dict(r) -> Dict:
        d = {k: r[k] for k in FILL_COLS}
        d['_id'] = r['id']
        if 'fills' in r.keys():
            d['fills'] = r['fills']                    # 몇 번에 나눠 체결됐는지 (1 = 한 번)
        return d

    def count_fills(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute('SELECT COUNT(*) FROM fills').fetchone()[0]

    def realized_summary(self, since_ts: int = 0) -> Dict:
        """업비트 '투자손익'처럼: 누적 수수료 · 판 손익(실현손익, 수수료 빼기 전 = 판 금액 − 매수평균가 × 판 수량)
        매수평균가는 업비트와 같이 수수료 미포함, 체결을 처음부터 차례로 따라가며 셈.
        기록 앞쪽에서 산 것을 모르는 매도(기록이 일부만 있을 때)는 아는 수량만 셈 → partial 표시"""
        pos: Dict[str, List[float]] = {}
        fees = realized = 0.0
        buys = sells = 0
        partial = False
        with self._lock, self._conn() as c:
            first = c.execute(f'SELECT MIN(ts) FROM fills WHERE ts >= ? AND {NOT_DUP_FILL}', (int(since_ts),)).fetchone()[0]
            for r in c.execute(f'SELECT kind, currency, volume, amount, fee FROM fills WHERE ts >= ? AND {NOT_DUP_FILL} ORDER BY ts, id', (int(since_ts),)):
                fees += float(r['fee'] or 0)
                vol, amt = float(r['volume'] or 0), float(r['amount'] or 0)
                q, cost = pos.get(r['currency'], (0.0, 0.0))
                if r['kind'] == '매수':
                    buys += 1
                    pos[r['currency']] = (q + vol, cost + amt)
                    continue
                sells += 1
                if vol <= 0:
                    continue
                known = min(vol, q)
                if known < vol * 0.999:
                    partial = True
                if known > 0:
                    avg = cost / q
                    realized += amt * known / vol - avg * known
                    left = q - known
                    pos[r['currency']] = (left, cost - avg * known) if left > 1e-12 else (0.0, 0.0)
        return {'fees': fees, 'realized': realized, 'buys': buys, 'sells': sells, 'since': first, 'partial': partial}

    # ── 업비트 주문별 체결 캐시 ──
    def get_order_fills(self, uuids: List[str]) -> Dict[str, list]:
        out = {}
        uuids = list(uuids)
        with self._lock, self._conn() as c:
            for i in range(0, len(uuids), 500):
                part = uuids[i:i + 500]
                for r in c.execute(f"SELECT uuid, trades FROM order_fills WHERE uuid IN ({','.join('?' * len(part))})", part):
                    out[r['uuid']] = json.loads(r['trades'])
        return out

    def put_order_fills(self, items: Dict[str, list]):
        if not items:
            return
        with self._lock, self._conn() as c:
            c.executemany('INSERT OR REPLACE INTO order_fills(uuid, trades) VALUES (?, ?)',
                          [(k, json.dumps(v, ensure_ascii=False)) for k, v in items.items()])

    # ── 봇 거래 기록 ──
    def reset_ts(self, bot_id: str) -> int:
        """이 봇을 '처음부터 다시' 한 시각 (그 전 기록은 남기되 봇 화면에서는 뺌)"""
        return int(self.get_meta(f'reset:{bot_id}', 0) or 0)

    def mark_reset(self, bot_id: str, ts: Optional[int] = None):
        self.set_meta(f'reset:{bot_id}', int(ts if ts is not None else time.time()))

    def _avg_state(self, c, bot_id: str, symbol: str, since: int) -> List[float]:
        """그 봇 · 코인의 [보유 수량, 들인 돈] (BotManager.with_avg_cost 와 같은 계산)"""
        st = [0.0, 0.0]
        for r in c.execute('SELECT side, qty, total FROM bot_trades WHERE bot_id=? AND symbol=? AND ts>=? ORDER BY ts, id',
                           (bot_id, symbol, since)):
            q = float(r['qty'] or 0)
            if r['side'] == 'buy':
                st[0] += q
                st[1] += float(r['total'] or 0)
            elif st[0] > 0:
                st[1] -= st[1] * min(1.0, q / st[0])
                st[0] = max(0.0, st[0] - q)
                if st[0] <= 1e-12:
                    st[0] = st[1] = 0.0
        return st

    def add_bot_trade(self, bot_id: str, bot_name: str, item: Dict, avg_cost: Optional[float] = None) -> Optional[float]:
        """봇 거래 한 건 더하기 (같은 건이 이미 있으면 그대로) → 산 줄이면 산 뒤 매수평균가"""
        try:
            ts = to_ts(item['time'])
        except Exception:
            return None
        with self._lock, self._conn() as c:
            if avg_cost is None and item.get('side') == 'buy':
                st = self._avg_state(c, bot_id, item.get('symbol'), self.reset_ts(bot_id))
                q = st[0] + float(item.get('qty') or 0)
                avg_cost = (st[1] + float(item.get('total') or 0)) / q if q > 0 else None
            c.execute(BOT_INSERT, bot_row(bot_id, bot_name, ts, item, avg_cost))
        return avg_cost

    @staticmethod
    def _bot_dict(r) -> Dict:
        d = {k: r[k] for k in BOT_COLS if k not in ('snap',)}
        if r['snap']:
            try:
                d['snap'] = json.loads(r['snap'])
            except Exception:
                pass
        for k in ('buy_reason', 'order_id', 'avg_cost'):
            if d.get(k) is None:
                d.pop(k, None)
        return d

    def bot_trades(self, bot_id: str, symbol: Optional[str] = None, limit: Optional[int] = None,
                   newest_first: bool = True, all_runs: bool = False) -> List[Dict]:
        """봇 거래 기록 (기본: 초기화 이후만)"""
        where, args = ['bot_id=?'], [bot_id]
        if not all_runs:
            where.append('ts >= ?'); args.append(self.reset_ts(bot_id))
        if symbol:
            where.append('symbol=?'); args.append(symbol)
        sql = f"SELECT * FROM bot_trades WHERE {' AND '.join(where)} ORDER BY ts {'DESC' if newest_first else 'ASC'}, id {'DESC' if newest_first else 'ASC'}"
        if limit:
            sql += f' LIMIT {int(limit)}'
        with self._lock, self._conn() as c:
            return [self._bot_dict(r) for r in c.execute(sql, args)]

    def realized_points(self, bot_id: str) -> List[Tuple[int, float]]:
        """[(판 시각, 판 손익)] 오래된 순 — 초기화 이후, 성적에서 뺀 거래(맡기기 전 손실 등) 제외"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT ts, profit FROM bot_trades WHERE bot_id=? AND ts >= ? AND side='sell' AND profit IS NOT NULL "
                             "AND COALESCE(excluded, 0) = 0 ORDER BY ts", (bot_id, self.reset_ts(bot_id))).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]

    def bot_today(self, bot_id: str, since_ts: int) -> Dict:
        """오늘(since_ts 이후 · 초기화 이후) 판 손익 합 · 거래 수, 그리고 마지막 거래 시각(초기화 이후 전체)"""
        start = max(int(since_ts), self.reset_ts(bot_id))
        with self._lock, self._conn() as c:
            r = c.execute('SELECT SUM(CASE WHEN COALESCE(excluded, 0) = 0 THEN profit END), COUNT(*) FROM bot_trades WHERE bot_id=? AND ts >= ?', (bot_id, start)).fetchone()
            last = c.execute('SELECT MAX(ts) FROM bot_trades WHERE bot_id=? AND ts >= ?', (bot_id, self.reset_ts(bot_id))).fetchone()[0]
        return {'realized': float(r[0] or 0), 'trades': int(r[1] or 0), 'last_ts': last}

    def recent_bot_trades(self, limit: int = 30, bot_ids: Optional[List[str]] = None) -> List[Dict]:
        """모든 봇(또는 bot_ids)의 최근 거래, 최신순 — 봇마다 초기화 이전 기록은 뺌"""
        where, args = [], []
        if bot_ids is not None:
            if not bot_ids:
                return []
            where.append(f"bot_id IN ({','.join('?' * len(bot_ids))})")
            args.extend(bot_ids)
        with self._lock, self._conn() as c:
            rows = c.execute(f"SELECT * FROM bot_trades{' WHERE ' + ' AND '.join(where) if where else ''} ORDER BY ts DESC, id DESC LIMIT ?",
                             args + [int(limit) * 3]).fetchall()
            resets = {r['bot_id']: self.reset_ts(r['bot_id']) for r in rows}
        out = [self._bot_dict(r) for r in rows if r['ts'] >= resets.get(r['bot_id'], 0)]
        return out[:limit]

    def bot_fee_sum(self, bot_id: str) -> float:
        """이 봇이 낸 수수료 합계 (초기화 이후)"""
        with self._lock, self._conn() as c:
            r = c.execute('SELECT SUM(fee) FROM bot_trades WHERE bot_id=? AND ts >= ?', (bot_id, self.reset_ts(bot_id))).fetchone()
        return float(r[0] or 0)

    def bot_counts(self, bot_id: str) -> Dict:
        """매수 · 매도 수, 이긴 · 진 매도(실현 손익 기준), 오늘 거래 수 (초기화 이후)"""
        since = self.reset_ts(bot_id)
        today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        with self._lock, self._conn() as c:
            r = c.execute("""SELECT COUNT(*) n, SUM(side='buy') buys, SUM(side='sell') sells,
                                    SUM(side='sell' AND profit IS NOT NULL AND profit > 0 AND COALESCE(excluded, 0) = 0) wins,
                                    SUM(side='sell' AND profit IS NOT NULL AND profit <= 0 AND COALESCE(excluded, 0) = 0) losses,
                                    SUM(ts >= ?) today
                             FROM bot_trades WHERE bot_id=? AND ts >= ?""", (today, bot_id, since)).fetchone()
        return {k: int(r[k] or 0) for k in ('n', 'buys', 'sells', 'wins', 'losses', 'today')}

    def reasons_for(self, rows: List[Dict], names: Optional[Dict[str, str]] = None) -> List[Dict]:
        """거래 내역 줄마다 봇 기록을 찾아 bot · reason · buy_reason · profit · avg_cost 를 붙임
        주문번호가 같으면 그것, 없으면 같은 코인 · 같은 방향에서 주문 시각이 3분 안으로 가장 가까운 것. 못 찾으면 봇이 아닌 주문"""
        names = names or {}
        ids = [r.get('order_id') for r in rows if r.get('order_id') and r.get('kind') in ('매수', '매도')]
        by_id = {}
        with self._lock, self._conn() as c:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                for b in c.execute(f"SELECT * FROM bot_trades WHERE order_id IN ({','.join('?' * len(part))})", part):
                    by_id[b['order_id']] = b
            out = []
            for row in rows:
                if row.get('kind') not in ('매수', '매도'):
                    out.append(row)
                    continue
                hit = by_id.get(row.get('order_id'))
                if hit is None:
                    try:
                        at = to_ts(row.get('order_time') or row['time'])
                    except Exception:
                        at = None
                    if at is not None:
                        hit = c.execute("""SELECT * FROM bot_trades WHERE symbol=? AND side=? AND ts BETWEEN ? AND ?
                                           ORDER BY ABS(ts - ?) LIMIT 1""",
                                        (f"KRW-{row.get('currency')}", 'buy' if row['kind'] == '매수' else 'sell',
                                         at - MATCH_WINDOW, at + MATCH_WINDOW, at)).fetchone()
                if hit is not None:
                    row = {**row, 'bot': names.get(hit['bot_id']) or hit['bot_name'] or '지운 봇', 'reason': hit['reason'] or '',
                           'buy_reason': hit['buy_reason'] or '', 'profit': hit['profit'], 'avg_cost': hit['avg_cost']}
                out.append(row)
        return out

    # ── 보유코인 분석 '매수일' ──
    DUST_KRW = 1000            # 이보다 적게 남으면 다 판 것으로 봄 (자투리)

    def buy_times(self, currencies: List[str], since_ts: Optional[int] = None) -> Dict[str, Dict]:
        """코인마다 지금 보유분을 처음 산 때(first) · 마지막으로 산 때(last) · 그동안 산 횟수(n) — 오래된 순으로 사고판 수량을 따라가다
        다 팔면(자투리 DUST_KRW 이하) 새로 셈. 거래 내역 체결 + 봇 거래(아직 거래 내역에 안 받은 최근 봇 매매도 보이게, 같은 주문은 한 번만)
        since_ts: 이때부터만 셈 (시뮬레이션 계좌를 새로 만든 때 — 그 전 계좌의 매매는 지금 보유와 상관없음)"""
        curs = [c for c in dict.fromkeys(currencies) if c]
        if not curs:
            return {}
        q = ','.join('?' * len(curs))
        with self._lock, self._conn() as c:
            fills = c.execute(f"SELECT ts, id, order_id, currency, kind, volume, price FROM fills WHERE currency IN ({q})", curs).fetchall()
            bots = c.execute(f"SELECT ts, id, order_id, symbol, side, qty, price FROM bot_trades WHERE symbol IN ({q})",
                             [f'KRW-{x}' for x in curs]).fetchall()
        seen = {r['order_id'] for r in fills if r['order_id']}
        ev = [(r['ts'], 0, r['id'], r['currency'], r['kind'] == '매수', float(r['volume'] or 0), float(r['price'] or 0)) for r in fills]
        for r in bots:
            if r['order_id'] and r['order_id'] in seen:
                continue
            ev.append((r['ts'], 1, r['id'], r['symbol'][4:], r['side'] == 'buy', float(r['qty'] or 0), float(r['price'] or 0)))
        if since_ts:
            ev = [e for e in ev if e[0] >= since_ts]
        ev.sort()
        state: Dict[str, Dict] = {}
        for ts, _src, _id, cur, is_buy, vol, px in ev:
            s = state.setdefault(cur, {'qty': 0.0, 'first': None, 'last': None, 'n': 0})
            if is_buy:
                if s['qty'] * px <= self.DUST_KRW:            # 비어 있다가 새로 삼 → 여기서부터 지금 보유분
                    s.update(qty=0.0, first=ts, n=0)
                s['qty'] += vol
                s['last'] = ts
                s['n'] += 1
            else:
                s['qty'] = max(0.0, s['qty'] - vol)
        return {cur: {'first': s['first'], 'last': s['last'], 'n': s['n']} for cur, s in state.items() if s['first']}

    # ── 정리 ──
    def prune(self, older_than_days: int) -> int:
        """오래된 체결 · 봇 기록 지우기 (시뮬레이션만 씀 — 업비트 실제 거래는 지우지 않음)"""
        cut = int(time.time()) - int(older_than_days) * 86400
        with self._lock, self._conn() as c:
            n = c.execute('DELETE FROM fills WHERE ts < ?', (cut,)).rowcount
            n += c.execute('DELETE FROM bot_trades WHERE ts < ?', (cut,)).rowcount
        return n


# ── 처음 한 번: 예전 JSON 기록을 저장소로 옮기기 (store 마다 meta 'migrated') ──
def sim_order_row(o: Dict) -> Dict:
    """가상 주문 → 거래 내역 줄 (업비트 거래내역과 같은 모양)"""
    import math
    vol, price, fee = float(o['executed_volume']), float(o['avg_price']), float(o['paid_fee'])
    funds = vol * price
    buy = o['side'] == 'bid'
    return {'time': o['created_at'], 'order_time': o['created_at'], 'order_id': o['uuid'], 'currency': o['market'].split('-')[1],
            'market': 'KRW', 'kind': '매수' if buy else '매도', 'volume': vol, 'price': price, 'amount': funds,
            'fee': fee, 'settle': math.ceil(funds + fee - 1e-6) if buy else math.floor(funds - fee + 1e-6)}


def _read(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return default


def _add_bot_file(store: 'TradeStore', bot_id: str, name: str, path: Path) -> int:
    items = _read(path, [])
    if not isinstance(items, list):
        return 0
    pos: Dict[str, List[float]] = {}
    vals = []
    for t in items:                                          # 파일 하나 = 한 번의 실행 → 그 안에서 매수평균가를 셈
        if not isinstance(t, dict) or not t.get('time') or not t.get('symbol'):
            continue
        try:
            ts = to_ts(t['time'])
        except Exception:
            continue
        st = pos.setdefault(t['symbol'], [0.0, 0.0])
        q = float(t.get('qty') or 0)
        avg = None
        if t.get('side') == 'buy':
            st[0] += q
            st[1] += float(t.get('total') or 0)
            avg = st[1] / st[0] if st[0] > 0 else None
        elif st[0] > 0:
            st[1] -= st[1] * min(1.0, q / st[0])
            st[0] = max(0.0, st[0] - q)
            if st[0] <= 1e-12:
                st[0] = st[1] = 0.0
        vals.append(bot_row(bot_id, name, ts, t, avg))
    if vals:
        with store._lock, store._conn() as c:
            c.executemany(BOT_INSERT, vals)
    return len(vals)


def migrate_legacy(stores: Dict[str, 'TradeStore'], data_dir: Path, bots_root: Path, bots: List[Dict]) -> Dict[str, int]:
    """sim_account.json 주문 · 봇 trades.json(지금 봇 · 초기화 · 지운 봇) · 업비트 캐시 JSON → 저장소 (한 번만)"""
    import logging
    log = logging.getLogger(__name__)
    data_dir, bots_root = Path(data_dir), Path(bots_root)
    names = {b['id']: b.get('name') or '' for b in bots if b.get('id')}
    done = {}
    for mode, store in stores.items():
        if store.get_meta('migrated'):
            continue
        n_fill = n_bot = 0
        try:
            if mode == 'sim':
                st = _read(data_dir / 'sim_account.json', {})
                n_fill += store.add_fills(sim_order_row(o) for o in (st.get('orders') or []) if o.get('executed_volume'))
                if st.get('created_at'):
                    store.set_meta('account_since', to_ts(st['created_at']))
            else:
                chunks = _read(data_dir / 'trade_chunks_cache.json', {})
                for rows in (chunks.values() if isinstance(chunks, dict) else []):
                    n_fill += store.add_fills(rows)
                if isinstance(chunks, dict) and chunks:
                    store.set_meta('synced_chunks', sorted(chunks))
                orders = _read(data_dir / 'order_trades_cache.json', {})
                if isinstance(orders, dict):
                    store.put_order_fills(orders)
            # 봇 기록: 지금 봇
            for bid, name in names.items():
                n_bot += _add_bot_file(store, bid, name, bots_root / bid / mode / 'trades.json')
            # 지운 봇
            for d in sorted((bots_root / '_deleted').glob('*')) if (bots_root / '_deleted').exists() else []:
                bid = d.name.split('_')[0]
                n_bot += _add_bot_file(store, bid, names.get(bid) or '지운 봇', d / mode / 'trades.json')
                if mode == 'sim':
                    n_bot += _add_bot_file(store, bid, names.get(bid) or '지운 봇', d / 'trades.json')   # 예전(모드 나누기 전) 모양
            # 처음부터 다시 한 봇 (시뮬레이션만): 그 전 기록은 남기되 봇 화면에서는 초기화 뒤만
            if mode == 'sim' and (bots_root / '_reset').exists():
                last_reset: Dict[str, int] = {}
                for d in sorted((bots_root / '_reset').glob('*')):
                    bid, _, stamp = d.name.partition('_')
                    n_bot += _add_bot_file(store, bid, names.get(bid) or '지운 봇', d / 'trades.json')
                    try:
                        last_reset[bid] = max(last_reset.get(bid, 0), int(datetime.strptime(stamp, '%Y%m%d%H%M%S').timestamp()))
                    except ValueError:
                        pass
                for bid, ts in last_reset.items():
                    store.mark_reset(bid, ts)
            store.set_meta('migrated', int(time.time()))
            done[mode] = n_fill + n_bot
            log.info(f"거래 기록 옮기기 ({mode}): 체결 {n_fill}건 · 봇 기록 {n_bot}건")
        except Exception as e:
            log.warning(f"거래 기록 옮기기 실패 ({mode}): {e}")
    # 옮긴 업비트 캐시 JSON 은 지우지 않고 이름만 바꿔 둠 (다시 쓰지 않음)
    if stores.get('upbit') is not None and stores['upbit'].get_meta('migrated'):
        for fn in ('trade_chunks_cache.json', 'order_trades_cache.json'):
            p = data_dir / fn
            if p.exists():
                try:
                    p.replace(p.with_suffix('.json.migrated'))
                except Exception:
                    pass
    return done
