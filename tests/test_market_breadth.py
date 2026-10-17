"""시장 분위기 기록 (gui/market_breadth.py): 상승 · 하락 · 보합 수 · 평균 · 거래대금 가중 평균, 간격 묶기, 오래된 기록 줄이기"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui.market_breadth import MarketBreadth  # noqa: E402


def tk(rate, value):
    return {'signed_change_rate': rate, 'acc_trade_price_24h': value}


def test_summarize_counts_and_averages():
    s = MarketBreadth.summarize({'KRW-A': tk(0.10, 300), 'KRW-B': tk(-0.02, 100), 'KRW-C': tk(0, 0), 'KRW-D': {'signed_change_rate': None}})
    assert (s['up'], s['down'], s['flat']) == (1, 1, 1)
    assert abs(s['avg_rate'] - (10 - 2 + 0) / 3) < 1e-9
    assert abs(s['wavg_rate'] - (10 * 300 - 2 * 100) / 400) < 1e-9
    assert MarketBreadth.summarize({}) is None


def test_record_query_and_prune(tmp_path):
    mb = MarketBreadth(tmp_path / 'b.db')
    now = int(time.time()) // 3600 * 3600
    for k in range(3):                                            # 같은 시간 안 1분 기록 3개
        mb.record({'KRW-A': tk(0.01 * (k + 1), 1)}, ts=now - 3600 + 60 * k)
    pts = mb.query('1h')
    assert abs(pts[0]['avg_rate'] - 1.0) < 1e-9 and pts[0]['up_ratio'] == 100 and not pts[0].get('live')   # 점 = 그 시각(구간 시작)의 값
    assert pts[-1].get('live') and abs(pts[-1]['avg_rate'] - 3.0) < 1e-9          # 맨 끝 = 지금 값
    assert len(mb.query('1m')) == 3
    mb.record({'KRW-A': tk(0.01, 1)}, ts=now - 20 * 86400 + 60)     # 오래된 정시가 아닌 기록
    mb.record({'KRW-A': tk(0.01, 1)}, ts=now - 20 * 86400)          # 오래된 정시 기록
    assert mb.prune(8) == 1 and mb.stats()['count'] == 4


def test_index_chains_prices_through_9am(tmp_path):
    """시장 전체 평균 지수: 매분 가격 변화로 이어 붙여 9시(전일대비 기준이 바뀌는 때)에도 끊기거나 튀지 않음 · 다시 켜도 이어짐"""
    def tks(price, rate):
        return {f'KRW-C{i}': {'trade_price': price * (1 + i / 100), 'signed_change_rate': rate, 'acc_trade_price_24h': 1} for i in range(30)}
    t0 = 1791503940                                       # 08:59 KST (= UTC 23:59)
    mb = MarketBreadth(tmp_path / 'b.db')
    mb.record(tks(100, -0.02), t0)                         # 08:59 전일대비 −2%
    mb.record(tks(101, -0.01), t0 + 60)                    # 09:00 — 전일대비가 아직 어제 기준
    mb2 = MarketBreadth(tmp_path / 'b.db')                 # 프로그램을 다시 켜도
    mb2.record(tks(102, 0.0), t0 + 120)                    # 09:01
    idx = dict(mb2.index_series())
    assert t0 + 60 in idx and t0 + 120 in idx              # 9:00 · 9:01 도 점이 있음 (예전엔 9:15 까지 비었음)
    assert abs(idx[t0 + 60] / idx[t0] - 1.01) < 1e-9       # 가격이 1% 오른 만큼
    assert abs(idx[t0 + 120] / idx[t0] - 1.02) < 1e-9


def test_index_does_not_drift_on_price_bounce(tmp_path):
    """가격이 1틱씩 오르내리기만 하면(100 ↔ 101) 지수도 제자리 — 매분 다시 나누면 조금씩 올라가던 문제"""
    def tks(price):
        return {f'KRW-C{i}': {'trade_price': price, 'signed_change_rate': 0.0, 'acc_trade_price_24h': 1} for i in range(30)}
    t0 = 1791504000 + 3600                                # 같은 날 (KST 10시)
    mb = MarketBreadth(tmp_path / 'b.db')
    for k in range(200):
        mb.record(tks(100 if k % 2 == 0 else 101), t0 + 60 * k)
    idx = dict(mb.index_series())
    assert abs(idx[t0 + 60 * 198] / idx[t0] - 1) < 1e-9     # 100 에 돌아오면 지수도 그대로
