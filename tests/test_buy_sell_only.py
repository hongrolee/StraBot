"""사기만 · 팔기만 하는 봇: 손절 · 익절 0 = 안 함, 보유 코인 맡기기(매수 조건 없이 팔기만)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot_core.risk_manager import RiskManager  # noqa: E402
from test_bot_manager import PRICES, data, engine, make, tick  # noqa: E402

NO_BUY = {'buy': [], 'sell': [], 'exclude': []}


def test_zero_stop_loss_and_take_profit_mean_off():
    risk = RiskManager({'stop_loss_percent': 0, 'take_profit_percent': 0})
    assert not risk.check_stop_loss(100, 1)[0] and not risk.check_take_profit(100, 1000)[0]
    risk = RiskManager({'stop_loss_percent': 5, 'take_profit_percent': 10})
    assert risk.check_stop_loss(100, 94)[0] and risk.check_take_profit(100, 111)[0]


def test_sell_only_bot_with_adopted_coin(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    PRICES.update({'KRW-A': 1000.0, 'KRW-B': 10.0})
    m.acct.buy_market_order('KRW-A', 100_000)                 # 내가 직접 산 코인
    m.acct.buy_market_order('KRW-B', 6_000)                  # 산 뒤 반값이 되어 지금 3,000원 (5,000원 미만)
    _, _, a = m.create({'name': '팔기 봇', 'budget_pct': 0, 'trade_rules': NO_BUY,
                        'risk_config': {'take_profit_percent': 10, 'stop_loss_percent': 0}})
    ok, msg = m.start(a['id'], 'sim')
    assert not ok and '맡겨' in msg                           # 매수 조건도 맡긴 코인도 없으면 시작 안 됨
    ok, _, rows = m.adoptable(a['id'], {'KRW-A': 1000.0, 'KRW-B': 5.0})
    assert ok and {r['currency']: r['state'] for r in rows} == {'A': 'free', 'B': 'free'}
    ok, msg = m.set_adopted(a['id'], ['A', 'B'], {'KRW-A': 1000.0, 'KRW-B': 5.0})
    assert ok and '1개 맡겼습니다' in msg and 'B(5,000원 미만)' in msg
    assert m.start(a['id'], 'sim')[0]                         # 맡긴 코인이 있으면 매수 조건 없이 시작
    e = engine(m, a['id'], 'sim')
    assert set(e.ledger) == {'KRW-A'} and e.ledger['KRW-A']['adopted']
    entry = e.ledger['KRW-A']['entry_price']
    tick(e, data({'A': 15}, price={'A': entry * 1.15}))       # 맡긴 값보다 15% 오름 → 익절 10%
    assert 'KRW-A' not in e.ledger
    snap = m.trades(a['id'], 'sim')[-1]['snap']              # 거래 순간의 봇 총 자산을 기록에 저장
    assert snap['coins'] == 0 and snap['total'] == snap['cash'] and 'rate' in snap


def test_other_bot_cannot_take_and_release_keeps_coin(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    PRICES.update({'KRW-A': 1000.0})
    m.acct.buy_market_order('KRW-A', 100_000)
    _, _, a = m.create({'name': '팔기 봇', 'trade_rules': NO_BUY})
    _, _, b = m.create({'name': 'B봇', 'trade_rules': NO_BUY})
    m.set_adopted(a['id'], ['A'], {'KRW-A': 1000.0})
    ok, _, rows = m.adoptable(b['id'], {'KRW-A': 1000.0})
    assert rows[0]['state'] == 'other' and rows[0]['owner'] == '팔기 봇'
    ok, msg = m.set_adopted(b['id'], ['A'], {'KRW-A': 1000.0})
    assert "다른 봇 '팔기 봇'" in msg
    ok, msg = m.set_adopted(a['id'], [], {'KRW-A': 1000.0})
    assert ok and '풀었습니다' in msg and m._ledger_of(a['id'], 'sim') == {}
    assert m.acct.get_balance('A') > 0                       # 풀어도 팔지 않음


def test_reset_clears_folder_even_with_pnl_history(tmp_path):
    from gui.pnl_history import PnlHistory
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'trade_rules': NO_BUY})
    d = m.data_dir(a['id'], 'sim')
    d.mkdir(parents=True, exist_ok=True)
    (d / 'trades.json').write_text('[{"side": "sell", "profit": 100}]', encoding='utf-8')
    hist = PnlHistory(d / 'pnl_history.db')                   # 봇 수익률 기록이 열려 있던 상태 (예전엔 파일이 잠겨 초기화가 반쯤 실패)
    hist.record({'total_buy_amount': 100, 'total_value': 110, 'assets': []})
    ok, _ = m.reset(a['id'], 'sim')
    assert ok and not (d / 'trades.json').exists()
    hist.record({'total_buy_amount': 100, 'total_value': 120, 'assets': []})   # 비워진 뒤에도 다시 기록
    assert hist.stats()['count'] == 1


def test_skip_kinds_can_be_allowed_only_in_sim(tmp_path):
    from bot_core import conditions as cd
    from bot_core.bot_engine import BotEngine
    coins = [{'currency': 'A', 'current_price': 1}, {'currency': 'W', 'current_price': 1, 'warning': True},
             {'currency': 'D', 'current_price': 1, 'notices': [{'type': 'delisting'}]}]
    assert cd.blocked_kind(coins[1]) == '유의 종목' and cd.blocked_kind(coins[2]) == '상장폐지 예정'
    rules = {'allow_warning': True, 'allow_delist': True}
    sim = BotEngine({'mode': 'simulation', 'trade_rules': rules}, data_dir=tmp_path / 's')
    live = BotEngine({'mode': 'live', 'trade_rules': rules}, data_dir=tmp_path / 'l')
    assert {c['currency'] for c in sim._universe(rules, coins)} == {'A', 'W', 'D'}     # 시뮬레이션: 풀면 삼
    assert {c['currency'] for c in live._universe(rules, coins)} == {'A'}              # 업비트: 늘 뺌
    assert {c['currency'] for c in sim._universe({}, coins)} == {'A'}                  # 기본: 뺌


def test_skip_kinds_per_mode(tmp_path):
    from bot_core.bot_engine import BotEngine
    coins = [{'currency': 'A', 'current_price': 1}, {'currency': 'W', 'current_price': 1, 'warning': True}]
    rules = {'allow': {'sim': {'allow_warning': True}, 'upbit': {}}}
    sim = BotEngine({'mode': 'sim', 'trade_rules': rules}, data_dir=tmp_path / 's')
    live = BotEngine({'mode': 'live', 'trade_rules': rules}, data_dir=tmp_path / 'l')
    assert {c['currency'] for c in sim._universe(rules, coins)} == {'A', 'W'}          # 시뮬레이션에서 푼 값은
    assert {c['currency'] for c in live._universe(rules, coins)} == {'A'}              # 업비트에 넘어가지 않음
    rules = {'allow': {'sim': {}, 'upbit': {'allow_warning': True}}}
    assert {c['currency'] for c in live._universe(rules, coins)} == {'A', 'W'}         # 업비트는 업비트 값대로


def test_rules_and_risk_per_mode(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'trade_rules': {'buy': [{'id': 'my_up'}], 'sell': [], 'exclude': []},
                        'risk_config': {'stop_loss_percent': 5}})
    assert m.rules_of(a, 'sim')['buy'] == m.rules_of(a, 'upbit')['buy'] == [{'id': 'my_up'}]   # 새 봇은 두 모드 같은 규칙
    cfg['app_mode'] = 'sim'
    m.update(a['id'], {'trade_rules': {'buy': [], 'sell': [], 'exclude': ['BTC']}, 'risk_config': {'stop_loss_percent': 9}})
    bot = m.get(a['id'])
    assert m.rules_of(bot, 'sim')['exclude'] == ['BTC'] and m.risk_of(bot, 'sim')['stop_loss_percent'] == 9
    assert m.rules_of(bot, 'upbit')['buy'] == [{'id': 'my_up'}] and m.risk_of(bot, 'upbit')['stop_loss_percent'] == 5   # 업비트는 그대로
    assert m.bot_config(a['id'], 'upbit')['risk_config']['stop_loss_percent'] == 5                                     # 봇 엔진도 모드 것
    cfg['app_mode'] = 'upbit'
    ok, _ = m.copy_mode_rules(a['id'], 'sim')                  # 시뮬레이션에서 시험한 규칙을 업비트로
    bot = m.get(a['id'])
    assert ok and m.rules_of(bot, 'upbit')['exclude'] == ['BTC'] and m.risk_of(bot, 'upbit')['stop_loss_percent'] == 9
