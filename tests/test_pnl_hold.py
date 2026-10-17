"""봇 성과 비교 '들고만 있었으면': 봇이 산 코인을 팔지 않고 들고 있었을 때의 수익률 기록"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui.pnl_history import PnlHistory  # noqa: E402
from gui.web_dashboard import WebDashboard  # noqa: E402


def test_hold_value_counts_sold_coins_at_current_price_and_skips_delisted():
    lots = [(100, 'KRW-A', 10.0, 1000.0), (200, 'KRW-B', 5.0, 500.0), (300, 'KRW-GONE', 1.0, 100.0)]
    buy, value = WebDashboard._hold_value(lots, {'KRW-A': 120.0, 'KRW-B': 90.0})
    assert buy == 1500.0 and value == 10 * 120 + 5 * 90           # 상장폐지(시세 없음)는 빼고
    buy, value = WebDashboard._hold_value(lots, {'KRW-A': 120.0, 'KRW-B': 90.0}, upto=150)
    assert buy == 1000.0 and value == 1200.0                      # 그 시각까지 산 것만


def test_hold_records_query_and_backfill_does_not_overwrite(tmp_path):
    h = PnlHistory(tmp_path / 'p.db')
    now = 1_800_000_000 // 60 * 60
    h.record_hold(1000, 1100, ts=now)
    h.record_hold_many([(now, 1000, 900), (now - 3600, 1000, 950)])   # 이미 있는 분은 그대로
    with h._connect() as c:
        rows = dict(c.execute('SELECT ts, rate FROM pnl_hold').fetchall())
    assert rows[now] == 10.0 and rows[now - 3600] == -5.0
    assert h.hold_first_ts() == now - 3600
    h.clear()
    assert h.hold_first_ts() is None


def test_same_time_same_value_across_intervals(tmp_path):
    """같은 시각이면 1분 · 10분 · 1시간 간격 모두 같은 값 (점 = 그 시각의 값) · 맨 끝은 지금 값"""
    import time as _t
    from gui.pnl_history import PnlHistory
    h = PnlHistory(tmp_path / 'p.db')
    base = int(_t.time()) // 3600 * 3600 - 3600
    for k in range(75):                                           # 1분마다 수익률 k%
        h.record({'total_buy_amount': 100, 'total_value': 100 + k, 'assets': []}, ts=base + 60 * k)
    m1 = {p['ts']: p['rate'] for p in h.query('1m')}
    m10 = h.query('10m')
    assert len(m10) >= 8
    for p in m10:
        if not p.get('live'):
            assert abs(p['rate'] - m1[p['ts']]) < 1e-9          # 10분봉 11:20 = 1분봉 11:20
    assert m10[-1].get('live') and abs(m10[-1]['rate'] - 74) < 1e-9     # 맨 끝 = 지금 값
    h1 = h.query('1h')
    assert abs(h1[0]['rate'] - m1[h1[0]['ts']]) < 1e-9


import pytest  # noqa: E402


@pytest.mark.parametrize('bench', ['MARKET', 'KRW-BTC', 'TOP10'])
def test_market_line_same_across_intervals(tmp_path, monkeypatch, bench):
    """비교선(모든 코인 · 코인 하나 · 대형 10개)은 첫 기록에서 출발 — 그래프 왼쪽 끝이 간격마다 달라도 같은 시각은 같은 값"""
    import json
    import time as _t
    cfg = tmp_path / 'config' / 'bot_config.json'
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({'app_mode': 'sim', 'bots': []}), encoding='utf-8')
    d = WebDashboard(str(cfg), port=0, user_data_dir=tmp_path)
    h = d._pnl_hist()
    base = int(_t.time()) // 3600 * 3600 - 5 * 3600
    for k in range(300):                                          # 1분봉 200개보다 길게 (1분봉 그래프는 중간부터)
        h.record({'total_buy_amount': 100, 'total_value': 100 + k % 7, 'assets': []}, ts=base + 60 * k)
    px = lambda m, a: (1 + (a - base) / 60 * 0.001) * (1 + len(m) % 3)       # noqa: E731 — 코인마다 값이 다른 가짜 시세
    monkeypatch.setattr(d, '_get_candles', lambda unit, m, count, to=None, priority=False: [
        {'trade_price': px(m, int(__import__('datetime').datetime.strptime(to, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=__import__('datetime').timezone.utc).timestamp()))}])
    monkeypatch.setattr(d, '_top_markets', lambda n=10: [f'KRW-C{i}' + 'X' * i for i in range(10)])
    real = WebDashboard._bench_prices

    def fake(self, m, iv, ats, anchor_at=None):
        if m == 'TOP10':
            return real(self, m, iv, ats, anchor_at=anchor_at)
        return [px(m, a) for a in ats]
    monkeypatch.setattr(WebDashboard, '_bench_prices', fake)

    def line(iv):
        r = d.app.test_client().get(f'/api/pnl/history?interval={iv}&currency=ALL&bench={bench}').get_json()
        a = r['bench_anchor']
        return {p['ts']: ((1 + a['rate'] / 100) * b / a['price'] - 1) * 100 for p, b in zip(r['points'], r['bench'])}
    m1, m10 = line('1m'), line('10m')
    common = [t for t in m10 if t in m1]
    assert len(common) >= 10
    for t in common:
        assert abs(m1[t] - m10[t]) < 1e-9
