"""
백테스트 메뉴 (저장하지 않은 규칙 · 여러 코인) — /api/backtest/spec/check · /api/backtest/spec/run
실행: venv/Scripts/python.exe -m pytest tests/test_backtest_menu.py -q
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.web_dashboard import WebDashboard  # noqa: E402

SPEC = {'trade_rules': {'buy': [{'id': 'rsi_low', 'params': {'rsi': 30}}], 'sell': [], 'target': [{'id': 'value_top'}],
                        'buy_match': 'any', 'junk': 1},
        'risk_config': {'stop_loss_percent': '15', 'take_profit_percent': 50, 'max_positions': 0, 'bad_key': 9}}


def _wd(tmp_path):
    wd = WebDashboard(str(tmp_path / 'config' / 'bot_config.json'), port=0, user_data_dir=tmp_path)
    wd._save_config_internal = lambda *a, **k: None
    return wd


def test_spec_bot_is_cleaned():
    bot = WebDashboard._bt_spec_bot(SPEC)
    r = bot['trade_rules']
    assert r['buy'] == [{'id': 'rsi_low', 'params': {'rsi': 30}}] and r['sell'] == [] and r['buy_match'] == 'any'
    assert 'junk' not in r and r['exclude'] == []
    rc = bot['risk_config']
    assert rc['stop_loss_percent'] == 15.0 and rc['take_profit_percent'] == 50 and rc['max_positions'] == 1 and 'bad_key' not in rc
    assert WebDashboard._bt_spec_bot(None)['trade_rules']['buy'] == []


def test_markets_and_args(tmp_path):
    wd = _wd(tmp_path)
    assert wd._bt_markets(['btc', 'KRW-BTC', 'eth'], 5) == (['KRW-BTC', 'KRW-ETH'], None)
    assert wd._bt_markets([], 5)[0] is None
    assert wd._bt_markets(['A', 'B', 'C'], 2)[0] is None
    assert wd._bt_markets(['bad coin!'], 5)[0] is None
    bot = WebDashboard._bt_spec_bot(SPEC)
    args, err = wd._bt_args({'unit': '60', 'start': '2026-01-01', 'end': '2026-02-01'}, bot)
    assert err is None and args[0] == '60' and args[2] - args[1] > 30 * 86400
    assert wd._bt_args({'unit': '60', 'start': '2020-01-01', 'end': '2026-02-01'}, bot)[1]         # 1시간봉은 최대 365일
    nobuy = WebDashboard._bt_spec_bot({'trade_rules': {'buy': []}})
    assert '매수 조건' in wd._bt_args({'unit': 'day', 'start': '2026-01-01', 'end': '2026-02-01'}, nobuy)[1]


def test_spec_routes_run_each_coin(tmp_path):
    wd = _wd(tmp_path)
    seen = []

    def fake(job, bot, market, unit, start, end, krw, lo=0.0, hi=1.0, label='', costs=None):
        seen.append((market, round(lo, 2), round(hi, 2), bot['risk_config']['stop_loss_percent'], costs))
        if market == 'KRW-BAD':
            raise RuntimeError('캔들 없음')
        return {'market': market, 'kpis': {}}
    wd._bt_compute = fake
    c = wd.app.test_client()
    chk = c.post('/api/backtest/spec/check', json={'spec': SPEC}).get_json()
    assert chk['success'] and chk['buy'] and not any(n['title'] == '매매대상' for n in chk['notes'])
    r = c.post('/api/backtest/spec/run', json={'spec': SPEC, 'coins': ['BTC', 'BAD', 'ETH'], 'unit': 'day',
                                              'start': '2026-01-01', 'end': '2026-03-01', 'krw': 1000000,
                                              'fee_pct': 0.25, 'slip_pct': 0}).get_json()
    assert r['success']
    for _ in range(50):
        j = c.get(f"/api/backtest/{r['job']}").get_json()
        if j['stage'] == 'done':
            break
        time.sleep(0.05)
    coins = j['result']['coins']
    assert [x['market'] for x in coins] == ['KRW-BTC', 'KRW-BAD', 'KRW-ETH']
    assert 'result' in coins[0] and coins[1]['error'] == '캔들 없음' and 'result' in coins[2]
    assert seen[0][1:3] == (0.0, 0.33) and seen[2][1:3] == (0.67, 1.0) and seen[0][3] == 15.0
    assert seen[0][4] == {'fee': 0.0025, 'slip': 0.0}                                              # % → 비율
    bad = c.post('/api/backtest/spec/run', json={'spec': SPEC, 'coins': ['BTC'], 'unit': 'day', 'start': '2026-01-01',
                                                 'end': '2026-03-01', 'fee_pct': 9})
    assert bad.status_code == 400
    too_many = c.post('/api/backtest/spec/run', json={'spec': SPEC, 'coins': [f'C{i}' for i in range(21)], 'unit': 'day',
                                                     'start': '2026-01-01', 'end': '2026-03-01'})
    assert too_many.status_code == 400
