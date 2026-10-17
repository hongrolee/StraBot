"""실시간 체결 집계 테스트 (WebSocket 연결 없이 메시지를 직접 넣음)"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from gui import trade_flow as tf


def make(monkeypatch, now_minute):
    mon = tf.TradeFlowMonitor(lambda: ['KRW-AAA'])
    monkeypatch.setattr(tf.time, 'time', lambda: now_minute * 60 + 5)
    return mon


def trade(minute, price, vol, side):
    return {'cd': 'KRW-AAA', 'tp': price, 'tv': vol, 'ab': side, 'ttms': minute * 60000 + 1000}


def test_buy_sell_split_and_multiple(monkeypatch):
    start = 1000
    mon = make(monkeypatch, start + 16)          # 지금 = 시작 후 16분 → 방금 끝난 분 = start+15
    mon._started_minute = start
    for m in range(start, start + 15):           # 평소: 분마다 매수 10만 원 (시작한 반쪽 분 포함)
        mon._add(trade(m, 1000, 100, 'BID'))
    mon._add(trade(start + 15, 1000, 1000, 'BID'))   # 최근 1분: 매수 100만 원
    mon._add(trade(start + 15, 1000, 250, 'ASK'))    # 매도 25만 원
    snap = mon.snapshot()
    c = snap['coins']['KRW-AAA']
    assert c['buy_1m'] == 1_000_000 and c['sell_1m'] == 250_000
    assert c['buy_ratio'] == 80.0
    assert snap['base_minutes'] == 14 and snap['ready']   # 첫 반쪽 분은 평소 계산에서 빠짐
    assert c['buy_avg'] == 100_000 and c['mult'] == 10.0


def test_not_ready_before_ten_minutes(monkeypatch):
    start = 2000
    mon = make(monkeypatch, start + 5)
    mon._started_minute = start
    for m in range(start, start + 5):
        mon._add(trade(m, 1000, 100, 'BID'))
    snap = mon.snapshot()
    assert not snap['ready'] and snap['coins']['KRW-AAA']['mult'] is None


def test_old_minutes_pruned(monkeypatch):
    mon = make(monkeypatch, 5000)
    mon._add(trade(4000, 1, 1, 'BID'))
    mon._add(trade(4999, 1, 1, 'BID'))
    assert 4000 not in mon._buckets['KRW-AAA']


def test_spike_snapshot_minute_prices_and_hour(monkeypatch):
    """급등 판단용: 방금 끝난 1분의 시작·끝 가격, 직전 60분 평균 거래대금, 60분 전 끝가"""
    start = 3000
    mon = make(monkeypatch, start + 63)          # 방금 끝난 분 = start+62
    mon._started_minute = start
    for m in range(start, start + 62):           # 평소: 분마다 10만 원, 가격 1000
        mon._add(trade(m, 1000, 100, 'BID'))
    last = start + 62
    for p in (1000, 1080, 990, 1060):            # 급등 1분: 시작 1000 · 최고 1080 · 최저 990 · 끝 1060
        mon._add(trade(last, p, 1000, 'BID' if p != 990 else 'ASK'))
    snap = mon.spike_snapshot()
    c = snap['coins']['KRW-AAA']
    assert snap['ready'] and c['open'] == 1000 and c['close'] == 1060 and c['close_1h'] == 1000
    assert c['avg60'] == 100_000 and c['value'] == 1000 * 1000 + 1080 * 1000 + 990 * 1000 + 1060 * 1000
    assert snap['prices']['KRW-AAA'] == 1060
    assert mon._buckets['KRW-AAA'][last][3:5] == [1080, 990]


def test_spike_snapshot_not_ready_first_hour(monkeypatch):
    start = 4000
    mon = make(monkeypatch, start + 30)
    mon._started_minute = start
    mon._add(trade(start + 29, 1000, 100, 'BID'))
    snap = mon.spike_snapshot()
    assert not snap['ready'] and snap['coins'] == {} and snap['prices']['KRW-AAA'] == 1000
