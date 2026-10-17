"""봇 백테스트: 캔들 저장소(빈 구간만 받기) · 판단(기준 → 새로 맞게 된 순간 매수, 손절·익절은 봉 안 저가·고가)"""
import sys

import pytest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core import backtest as bt  # noqa: E402
from bot_core.candle_store import CandleStore, subtract, merge  # noqa: E402

H = 3600
T0 = 1_780_000_000 // 86400 * 86400          # UTC 0시에 맞춘 기준 시각


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def fake_upbit(sec, price=lambda t: 100.0, listed=0):
    """업비트처럼: to 이전 봉 count개, 최신순. listed 이전에는 봉 없음"""
    calls = []

    def fetch(path, market, count, to):
        calls.append(to)
        end = int(datetime.strptime(to, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp())
        t = (end - 1) // sec * sec
        out = []
        while len(out) < count and t >= listed:
            p = price(t)
            out.append({'candle_date_time_utc': iso(t), 'opening_price': p, 'high_price': p, 'low_price': p,
                        'trade_price': p, 'candle_acc_trade_price': 1.0})
            t -= sec
        return out
    return fetch, calls


def test_span_math():
    assert subtract((0, 100), [(10, 20), (50, 60)]) == [(0, 10), (20, 50), (60, 100)]
    assert subtract((0, 100), [(0, 100)]) == []
    assert merge([(0, 10), (5, 20), (30, 40)]) == [(0, 20), (30, 40)]


def test_store_fetches_only_missing(tmp_path):
    st = CandleStore(tmp_path / 'c.db')
    fetch, calls = fake_upbit(H)
    now = T0 + 100 * 86400
    a = st.get_range('KRW-X', '60', T0, T0 + 10 * 86400, fetch, now=now)
    assert len(a) == 240 and a[0][0] == T0 and a[-1][0] == T0 + 10 * 86400 - H
    first = len(calls)
    assert first == 2                                   # 240개 = 200 + 40
    st.get_range('KRW-X', '60', T0, T0 + 10 * 86400, fetch, now=now)
    assert len(calls) == first                          # 같은 요청: 받지 않음
    b = st.get_range('KRW-X', '60', T0 + 5 * 86400, T0 + 15 * 86400, fetch, now=now)
    assert len(b) == 240
    assert len(calls) == first + 1                      # 뒤 5일(120개)만 받음
    assert st.spans('KRW-X', '60') == [(T0, T0 + 15 * 86400)]


def test_store_skips_unfinished_bar_and_prelisting(tmp_path):
    st = CandleStore(tmp_path / 'c.db')
    fetch, calls = fake_upbit(H, listed=T0 + 2 * 86400)
    now = T0 + 3 * 86400 + 1800                         # 마지막 봉은 진행 중
    rows = st.get_range('KRW-X', '60', T0, T0 + 4 * 86400, fetch, now=now)
    assert rows[0][0] == T0 + 2 * 86400 and rows[-1][0] == T0 + 3 * 86400 - H
    n = len(calls)
    st.get_range('KRW-X', '60', T0, T0 + 3 * 86400, fetch, now=now)
    assert len(calls) == n                              # 상장 전 · 받은 구간은 다시 받지 않음


def bars(prices, sec=H, start=T0, low=None, high=None):
    out = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i else p
        out.append([start + i * sec, prev, max(prev, p) if high is None else high.get(i, max(prev, p)),
                    min(prev, p) if low is None else low.get(i, min(prev, p)), p, 1e6])
    return out


def bot(buy, sell=None, **risk):
    return {'budget': 1_000_000, 'trade_rules': {'buy': buy, 'sell': sell or []},
            'risk_config': {'position_size_percent': 30, 'stop_loss_percent': 50, 'take_profit_percent': 5, **risk}}


def wave():
    """잔잔 → 내림(RSI 낮아짐) → 오름"""
    flat = [100 + (1 if i % 2 else -1) * 0.5 for i in range(40)]
    down = [flat[-1] * (0.985 ** (i + 1)) for i in range(20)]
    up = [down[-1] * (1.01 ** (i + 1)) for i in range(30)]
    return flat + down + up


def test_buy_on_fresh_match_and_take_profit():
    prices = wave()
    rows = bars(prices)
    start = rows[35][0]
    b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': '1h'}}])
    r = bt.run(b, 'KRW-X', '60', start, rows[-1][0] + H, {'60': rows, 'day': [], 'week': []})
    buys = [t for t in r['trades'] if t['side'] == 'buy']
    sells = [t for t in r['trades'] if t['side'] == 'sell']
    assert buys and buys[0]['reason'] == 'RSI 낮음 (많이 팔림)'
    assert sells and sells[0]['reason'].startswith('익절')
    entry = buys[0]['price']
    assert abs(sells[0]['price'] - entry * 1.05 * (1 - bt.SLIP)) < 1e-6 * entry   # 익절 가격에 체결
    assert sells[0]['buy_reason'] == 'RSI 낮음 (많이 팔림)'
    assert r['kpis']['sells'] >= 1 and r['kpis']['win_rate'] == 100


def test_first_step_is_baseline_only():
    prices = wave()
    rows = bars(prices)
    start = rows[45][0]                                 # 이미 RSI 낮은 상태에서 시작 → 그때는 사지 않음
    b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': '1h'}}])
    r = bt.run(b, 'KRW-X', '60', start, rows[60][0], {'60': rows, 'day': [], 'week': []})
    assert not r['trades']


def test_stop_loss_inside_bar():
    prices = wave()
    rows = bars(prices)
    start = rows[35][0]
    b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': '1h'}}], stop_loss_percent=3, take_profit_percent=50)
    r0 = bt.run(b, 'KRW-X', '60', start, rows[-1][0] + H, {'60': rows, 'day': [], 'week': []})
    k = next(i for i, x in enumerate(rows) if bt._iso(x[0] + H) == r0['trades'][0]['time'])
    rows[k + 1][3] = rows[k][4] * 0.9                  # 다음 봉에서 저가가 10% 아래로 찍힘
    r = bt.run(b, 'KRW-X', '60', start, rows[-1][0] + H, {'60': rows, 'day': [], 'week': []})
    s = [t for t in r['trades'] if t['side'] == 'sell'][0]
    entry = r['trades'][0]['price']
    assert s['reason'].startswith('손절')
    assert abs(s['price'] - entry * 0.97 * (1 - bt.SLIP)) < 1e-6 * entry


def test_plan_skips_live_only_rules():
    b = bot([{'id': 'buy_flow_spike', 'params': {}}, {'id': 'rsi_low', 'params': {}}],
            [{'id': 'spike_pump', 'params': {}, 'sell_mode': 'partial'}])
    p = bt.plan(b, [])
    assert [c['id'] for _, c in p['buy']] == ['rsi_low']
    assert len(p['skipped']) == 1                       # 실시간 체결 조건만 빠짐
    assert p['partial'] == ['spike'] and p['minute']    # 급등 일부 팔기는 1분봉으로 돌림


def spike_minutes():
    """1분봉: 잔잔(100) → 1시간 동안 105까지 → 한 1분에 크게 튐(거래대금 100배) → 꺾임 → 내려옴"""
    rows = []
    def add(o, h, l, c, v=1e6):
        rows.append([T0 + len(rows) * 60, o, h, l, c, v])
    for _ in range(100):
        add(100, 100, 100, 100)
    for i in range(59):
        p0, p1 = 100 + 5 * i / 59, 100 + 5 * (i + 1) / 59
        add(p0, p1, p0, p1)
    add(105, 112, 105, 112, 1e8)                        # 급등 1분: +6.7% · 1시간 +12% · 평소의 100배
    add(112, 113, 112, 112.5)
    add(112.5, 112.5, 109, 109.5)                       # 최고가 113에서 3% 꺾임 → 30% 팜
    for _ in range(5):
        add(109.5, 109.5, 108, 108.5)
    add(108.5, 108.5, 104, 104.5)                       # 판 가격보다 3% · 5% 싸짐 → 두 몫 다시 삼
    for _ in range(20):
        add(104.5, 105, 104.5, 105)
    return rows


def test_spike_partial_sell_and_rebuy_on_minutes():
    rows = spike_minutes()
    b = bot([{'id': 'all_coins', 'params': {}}], [{'id': 'spike_pump', 'params': {}, 'sell_mode': 'partial', 'portion': 30}],
            stop_loss_percent=50, take_profit_percent=1000)
    r = bt.run(b, 'KRW-X', '1', rows[61][0], rows[-1][0] + 60, {'1': rows, '60': [], 'day': [], 'week': []})
    kinds = [(t['side'], t['reason'].split(' — ')[0]) for t in r['trades']]
    assert kinds[0] == ('buy', '모든 코인')
    assert ('sell', '급등 때 일부 팔기') in kinds
    rebuys = [t for t in r['trades'] if t['reason'].startswith('급등 뒤 다시 사기')]
    assert rebuys, kinds
    first_qty = r['trades'][0]['qty']
    sold = next(t for t in r['trades'] if t['reason'].startswith('급등 때 일부 팔기'))
    assert abs(sold['qty'] - first_qty * 0.3) < 1e-9 * first_qty
    assert abs(sold['price'] - 113 * 0.97 * (1 - bt.SLIP)) < 1e-6


def test_needed_includes_warmup_and_week():
    b = bot([{'id': 'rsi_low', 'params': {'tf': '4h'}}])
    need = {u: (a, z) for u, a, z in bt.needed(b, [], '15', T0, T0 + 86400)}
    assert need['15'] == (T0, T0 + 86400)
    assert need['240'][0] <= T0 - 30 * 14400 and need['day'][0] <= T0 - 30 * 86400
    assert 'week' in need and '60' in need


def test_fee_and_slip_can_be_changed():
    """수수료 · 미끄럼: 안 주면 기본값(0.05% · 0.1%), 0으로 주면 비용 없이 — 같은 거래면 비용 없는 쪽이 더 남음"""
    prices = wave()
    rows = bars(prices)
    start = rows[35][0]
    b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': '1h'}}])
    data = {'60': rows, 'day': [], 'week': []}
    base = bt.run(b, 'KRW-X', '60', start, rows[-1][0] + H, data)
    free = bt.run(b, 'KRW-X', '60', start, rows[-1][0] + H, data, fee=0, slip=0)
    assert base['costs'] == {'fee': bt.FEE * 100, 'slip': bt.SLIP * 100}
    assert free['costs'] == {'fee': 0, 'slip': 0}
    assert len(free['trades']) == len(base['trades']) and free['kpis']['final'] > base['kpis']['final']
    buy = next(t for t in free['trades'] if t['side'] == 'buy')
    assert buy['fee'] == 0


def test_rule_bars_cover_all_screen_units():
    """규칙의 계산 봉: 1분 ~ 월봉 9가지 모두 받을 캔들에 들어감 (예전엔 1시간 · 4시간 · 일봉만)"""
    b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': '15m'}}, {'id': 'bb_lower', 'params': {'tf': 'M'}}])
    need = {u: (a, z) for u, a, z in bt.needed(b, [], 'day', T0, T0 + 30 * 86400)}
    assert '15' in need and 'month' in need
    assert need['15'][0] == T0 - 900 * bt.WARMUP_BARS and need['month'][0] <= T0 - 31 * 86400 * 100


def test_daily_limit_zero_means_off_in_backtest():
    """하루 손실 한도 0 = 안 씀 (실제 봇과 같게 — 전에는 0원 한도라 하나도 안 샀음, 2026-10-15)"""
    rows = bars(wave())
    b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': '1h'}}], daily_loss_limit_percent=0)
    r = bt.run(b, 'KRW-X', '60', rows[35][0], rows[-1][0] + H, {'60': rows, 'day': [], 'week': []})
    assert any(t['side'] == 'buy' for t in r['trades'])


def test_start_wait_and_refill_like_live():
    """코인 성격 조건(모든 코인): 켤 때 바로 삼 · start_wait 면 안 삼 · 손절 뒤 24시간 지나 다시 삼(실제 봇의 더 사기)"""
    prices = [100.0] * 5 + [80.0] + [80.0] * 40
    rows = bars(prices)
    end = rows[-1][0] + H
    b = bot([{'id': 'all_coins', 'params': {}}], stop_loss_percent=10, take_profit_percent=0)
    r = bt.run(b, 'KRW-X', '60', rows[1][0], end, {'60': rows, 'day': [], 'week': []})
    buys = [t for t in r['trades'] if t['side'] == 'buy']
    sells = [t for t in r['trades'] if t['side'] == 'sell']
    assert len(buys) == 2 and sells and sells[0]['reason'].startswith('손절')   # 켤 때 삼 → 손절 → 24시간 뒤 다시 삼
    gap = (datetime.fromisoformat(buys[1]['time']) - datetime.fromisoformat(sells[0]['time'])).total_seconds()
    assert gap >= 24 * 3600 - H
    b['trade_rules']['start_wait'] = True
    r = bt.run(b, 'KRW-X', '60', rows[1][0], end, {'60': rows, 'day': [], 'week': []})
    assert not r['trades']                                                   # 켤 때는 사지 않고, 새로 맞게 될 때만


def test_week_step_has_24h_trade_value_from_hourly_bars():
    """주봉 걸음은 봉 하나가 하루보다 길어 24시간 거래대금이 늘 0 이었음 → 1시간봉 24개 합 (2026-10-17)"""
    seen = []
    orig = bt.cd.run_condition

    def spy(cond, params, coins, *a, **k):
        seen.extend(c.get('trade_value_24h') for c in coins)
        return orig(cond, params, coins, *a, **k)
    bt.cd.run_condition = spy
    try:
        W = 604800
        wk = bars([100.0] * 12, sec=W)                          # 주봉 12개, 거래대금 1e6 씩
        hr = bars([100.0] * (12 * 7 * 24), sec=H)               # 같은 기간 1시간봉, 거래대금 1e6 씩
        b = bot([{'id': 'rsi_low', 'params': {'rsi': 30, 'tf': 'w'}}])
        bt.run(b, 'KRW-X', 'week', wk[2][0], wk[-1][0] + W, {'week': wk, '60': hr, 'day': [], '1': []})
    finally:
        bt.cd.run_condition = orig
    assert seen and all(v == pytest.approx(24e6) for v in seen)


def test_partial_spike_respects_buy_sell_switches():
    """1분봉 급등 일부 팔기 · 되사기도 실제 봇처럼 매도 끔이면 팔지 않고, 매수 끔이면 되사지 않는다 (2026-10-17)"""
    rows = spike_minutes()
    data = {'1': rows, '60': [], 'day': [], 'week': []}
    b = bot([{'id': 'all_coins', 'params': {}}], [{'id': 'spike_pump', 'params': {}, 'sell_mode': 'partial', 'portion': 30}],
            stop_loss_percent=50, take_profit_percent=1000)
    b['trade_rules']['sell_on'] = False
    r = bt.run(b, 'KRW-X', '1', rows[61][0], rows[-1][0] + 60, data)
    assert not any(t['reason'].startswith('급등 때 일부 팔기') for t in r['trades'])
    b['trade_rules']['sell_on'] = True
    b['trade_rules']['buy_on'] = False
    r = bt.run(b, 'KRW-X', '1', rows[61][0], rows[-1][0] + 60, data, start_held=True)
    assert any(t['reason'].startswith('급등 때 일부 팔기') for t in r['trades'])
    assert not any(t['reason'].startswith('급등 뒤 다시 사기') for t in r['trades'])


def test_start_held_knows_when_it_bought_for_held_days_rule():
    """보유코인 분석(start_held)에서 '산 지 N일 지남' 매도 조건이 걸린다 (전에는 산 시각이 없어 절대 안 맞았음)"""
    rows = bars([100.0] * 24 * 10)                              # 10일 1시간봉, 값 그대로
    b = bot([{'id': 'all_coins', 'params': {}}], [{'id': 'held_days', 'params': {'days': 2}}], stop_loss_percent=0, take_profit_percent=0)
    r = bt.run(b, 'KRW-X', '60', rows[0][0], rows[-1][0] + H, {'60': rows, 'day': [], 'week': []}, start_held=True)
    sells = [t for t in r['trades'] if t['side'] == 'sell']
    assert sells and '산 지' in sells[0]['reason']


def test_stable_coin_can_be_sold_by_rule():
    """스테이블코인을 들고 있으면 매도 조건으로 팔 수 있다 (실제 봇과 같게, 전에는 매도 판단에서 아예 건너뛰었음)"""
    rows = bars([100.0] * 24 * 10)
    b = bot([{'id': 'all_coins', 'params': {}}], [{'id': 'held_days', 'params': {'days': 2}}], stop_loss_percent=0, take_profit_percent=0)
    r = bt.run(b, 'KRW-USDT', '60', rows[0][0], rows[-1][0] + H, {'60': rows, 'day': [], 'week': []}, start_held=True)
    assert any(t['side'] == 'sell' and '산 지' in t['reason'] for t in r['trades'])
