"""신뢰도 기록: 가짜 일봉으로 신호 찾기('처음 맞은 날') · 1·3·7일 뒤 결과 · 시장 기준 · 이어서 갱신"""
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import signal_stats as ss  # noqa: E402


def fake_candles(n_days: int, seed: int, end: datetime):
    """업비트 모양 일봉 (최신순, [0] = 오늘 진행 중)"""
    out = []
    p = 100.0 + seed
    rows = []
    for i in range(n_days):
        p *= 1 + 0.03 * math.sin(i / (3 + seed % 5)) + 0.004 * ((i * 7 + seed) % 5 - 2)
        day = end - timedelta(days=n_days - 1 - i)
        rows.append({'candle_date_time_utc': day.strftime('%Y-%m-%dT00:00:00'), 'candle_date_time_kst': day.strftime('%Y-%m-%dT09:00:00'),
                     'high_price': p * 1.02, 'low_price': p * 0.98, 'trade_price': p, 'candle_acc_trade_price': 1e9 * (1 + (i % 7))})
    return list(reversed(rows))


def make_getter(total_days: int):
    end = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    data = {}

    def get(unit, market, count, to=None):
        seed = int(market.split('-')[1][1:])
        allc = data.setdefault(market, fake_candles(total_days, seed, end))
        if to:
            cut = to[:10]
            allc = [c for c in allc if c['candle_date_time_utc'][:10] < cut]
        return allc[:count]
    return get


def test_first_day_only():
    rows = [(f'd{i:03d}', 101.0, 99.0, 100.0 + (i % 2), 1e9) for i in range(130)]
    sig = ss.scan_market(('KRW-X', rows, ss.LONG + 1, []))
    # 같은 조건이 이틀 연속 '처음'으로 잡히지 않음
    seen = {}
    for cid, day, _, _ in sig:
        i = int(day[1:])
        assert seen.get(cid) != i - 1
        seen[cid] = i


def test_update_and_summary(tmp_path):
    st = ss.SignalStats(tmp_path / 'signal_log.db')
    markets = [f'KRW-C{k}' for k in range(25)]
    get = make_getter(300)
    r = st.update(markets, get, [], workers=1)
    assert r['new_signals'] > 0
    s = st.summary()
    assert s['coins'] > 0 and s['stats']
    some = next(v for v in s['stats'].values() if v['h']['3']['n'])
    x = some['h']['3']
    assert 0 <= x['hit'] <= 100 and 0 <= x['base'] <= 100
    assert some['dir'] in ('up', 'down', 'state')
    # 다시 돌리면 새 날이 없어 신호가 더 생기지 않음 (이어서 갱신)
    r2 = st.update(markets, get, [], workers=1)
    assert r2['new_signals'] == 0


def test_signal_dir_matches_screen():
    assert ss.signal_dir('golden_cross') == 'up'
    assert ss.signal_dir('hs_top') == 'down'
    assert ss.signal_dir('diamond_bottom') == 'up'
    assert ss.signal_dir('rsi_high') == 'state'


def _custom(value):
    return {'id': 'my_t', 'title': 'T', 'match': 'all', 'sort': {'rule': 0, 'dir': 'asc'},
            'rules': [{'metric': 'rsi', 'op': '<=', 'value': value, 'period': 14}]}


def _count(st, cid):
    with st._db() as db:
        return db.execute('SELECT COUNT(*), MIN(day) FROM signals WHERE cond=?', (cid,)).fetchone()


def test_new_condition_backfills_past(tmp_path):
    st = ss.SignalStats(tmp_path / 'signal_log.db')
    markets = [f'KRW-C{k}' for k in range(25)]
    get = make_getter(300)
    st.update(markets, get, [], workers=1)
    # 나중에 새 조건 → 그 조건만 지난 기간까지 채움 (새 날이 없어도)
    r = st.update(markets, get, [_custom(45)], workers=1)
    assert r['new_signals'] == 0
    n1, d1 = _count(st, 'my_t')
    assert n1 > 0                                              # 지난 기간 신호가 생김
    assert d1 <= sorted(x[0] for x in st._db().execute("SELECT DISTINCT day FROM signals").fetchall())[5]   # 기간 맨 앞쪽부터
    # 규칙을 고치면 다시 채움 (기준이 느슨해지면 신호 수가 달라짐)
    st.update(markets, get, [_custom(70)], workers=1)
    n2, _ = _count(st, 'my_t')
    assert n2 > 0 and n2 != n1
    # 지우면 기록도 정리
    st.update(markets, get, [], workers=1)
    assert _count(st, 'my_t')[0] == 0
    # 기본 조건 기록은 그대로
    assert _count(st, 'golden_cross')[0] > 0
