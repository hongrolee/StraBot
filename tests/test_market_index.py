"""시장 전체 평균 지수: 날마다 평균 등락률(전일 9시 대비)을 이어 붙임 · 봉으로 지난 기간 채우기"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui.market_breadth import MarketBreadth  # noqa: E402

DAY = 86400


def test_index_chains_days(tmp_path):
    mb = MarketBreadth(tmp_path / 'm.db')
    d0 = 1_800_000_000 // DAY * DAY                       # 어느 날 오전 9시 (KST)
    with mb._connect() as c:
        c.executemany("INSERT INTO breadth (ts, up, down, flat, avg_rate, wavg_rate) VALUES (?, 0, 0, 0, ?, 0)",
                      [(d0 + 3600, 5.0), (d0 + DAY - 60, 10.0), (d0 + DAY + 3600, -10.0)])
    s = dict(mb.index_series())
    assert abs(s[d0 + 3600] - 1.05) < 1e-9 and abs(s[d0 + DAY - 60] - 1.10) < 1e-9
    assert abs(s[d0 + DAY + 3600] - 1.10 * 0.90) < 1e-9          # 둘째 날: 첫날 끝 값에서 −10%


def test_backfill_equal_weight_vs_previous_9am(tmp_path):
    mb = MarketBreadth(tmp_path / 'm.db')
    d0 = 1_800_000_000 // DAY * DAY
    step = 3600
    closes = {f'KRW-C{i}': {d0: 100.0, d0 + step: 100.0 + (10 if i % 2 else 0)} for i in range(30)}   # 반은 +10%, 반은 0%
    n = mb.backfill_eq(closes, step, d0, d0 + step)
    assert n == 1
    with mb._connect() as c:
        ts, avg = c.execute('SELECT ts, avg_rate FROM eq_hist').fetchone()
    assert ts == d0 + step - 60 and abs(avg - 5.0) < 1e-9


def test_skips_first_15_minutes_after_9am(tmp_path):
    """9시 직후엔 아직 어제 기준 등락률이 섞여 있어 빼야 하루 한 번 꺾이는 일이 없음"""
    mb = MarketBreadth(tmp_path / 'm.db')
    d0 = 1_800_000_000 // DAY * DAY
    with mb._connect() as c:
        c.executemany("INSERT INTO breadth (ts, up, down, flat, avg_rate, wavg_rate) VALUES (?, 0, 0, 0, ?, 0)",
                      [(d0 - 60, -1.5), (d0, -1.4), (d0 + 60, -0.6), (d0 + 900, 0.0)])
    s = dict(mb.index_series())
    assert d0 not in s and d0 + 60 not in s
    assert abs(s[d0 + 900] - s[d0 - 60]) < 1e-9                      # 어제 끝 값에서 그대로 이어짐
