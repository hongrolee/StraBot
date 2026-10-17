"""
여러 봇 관리 — 업비트·시뮬레이션 두 모드 공용 봇
- 만들기·복제·삭제, 모드마다 따로 실행·기록, 시뮬 = 가상 계좌에 실전과 같은 코드로 주문
- 예산, 한 계좌 한 코인 한 봇, 다시 켤 때 모드별로 이어 켜기, 예전 설정·구조 옮기기, 새로 시작, 시험용 한꺼번에 사기
(실제 주문·업비트 요청 없음: 가짜 시장 데이터·가짜 거래소·가상 계좌)
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot_core.bot_engine as be  # noqa: E402
from bot_core.bot_manager import DEFAULT_BUDGET, BotManager  # noqa: E402
from bot_core.virtual_account import VirtualAccount  # noqa: E402
from test_condition_trader import DOWN, UP, FakeExchange, NullLogger, market  # noqa: E402

RULES = {'buy': [{'id': 'my_up'}], 'sell': [], 'exclude': []}
PRICES = {}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(be.time, 'sleep', lambda s: None)
    PRICES.clear()


class QuietEngine(be.BotEngine):
    """스레드 없이: start() 는 표시만, 확인은 테스트가 tick() 으로. 업비트 모드는 가짜 거래소"""
    fake = None

    def _connect_live(self):                                   # 업비트 모드: 가짜 거래소 (키·연결 확인 없이)
        self.exchange = QuietEngine.fake or FakeExchange(krw=10_000_000)
        return True, ''

    def start(self):
        self.is_running = True
        return True, 'ok'

    def stop(self):
        self.is_running = False


def make(tmp_path, config=None, legacy=None, mode='sim', krw=1_000_000):
    saved = []
    cfg = config if config is not None else {'bots': []}
    cfg.setdefault('app_mode', mode)
    acct = VirtualAccount(tmp_path / 'sim_account.json', lambda m: PRICES.get(m), initial_krw=krw)
    m = BotManager(cfg, lambda c: saved.append(json.dumps(c, default=str)), lambda: {}, None,
                   data_root=tmp_path / 'bots', legacy_dir=legacy, engine_cls=QuietEngine, get_account=lambda: acct)
    m.acct = acct
    return m, cfg, saved


def engine(m, bid, mode=None):
    e = m.engines[(bid, m.mode(mode))]
    e.trade_logger = NullLogger()
    return e


def tick(e, data):
    prices = {f"KRW-{c['currency']}": c['current_price'] for c in data['coins']}
    PRICES.update(prices)
    if isinstance(e.exchange, FakeExchange):
        e.exchange.prices = prices
    e.tick(data)


def data(changes, **kw):
    return {**market(changes, **kw), 'custom': [UP, DOWN]}


# ── 옮기기 ──
def test_migrate_legacy_single_bot(tmp_path):
    legacy = tmp_path / 'data'
    legacy.mkdir()
    (legacy / 'bot_positions.json').write_text('{"KRW-X": {"qty": 1, "entry_price": 100}}', encoding='utf-8')
    cfg = {'mode': 'live', 'trade_rules': RULES, 'risk_config': {'max_positions': 7}}
    m, cfg, saved = make(tmp_path, cfg, legacy)
    bots = m.bots()
    assert len(bots) == 1 and bots[0]['name'] == '기본 봇' and 'mode' not in bots[0] and bots[0]['budget'] == 0
    assert bots[0]['risk_config']['max_positions'] == 7
    assert json.loads((tmp_path / 'bots' / 'b1' / 'upbit' / 'bot_positions.json').read_text(encoding='utf-8'))['KRW-X']
    assert (legacy / 'bot_positions.json.bak').exists() and saved
    m.bots()
    assert len(m.bots()) == 1


def test_migrate_bots_with_mode_to_shared_bots(tmp_path):
    root = tmp_path / 'bots'
    for bid, files in (('a', ('bot_positions.json', 'trades.json')), ('s', ('sim_state.json', 'trades.json'))):
        (root / bid).mkdir(parents=True)
        for f in files:
            (root / bid / f).write_text('{}' if f != 'trades.json' else '[]', encoding='utf-8')
    cfg = {'bots': [{'id': 'a', 'name': '실전', 'mode': 'live', 'budget': 0, 'trade_rules': RULES, 'was_running': True},
                    {'id': 's', 'name': '시뮬', 'mode': 'simulation', 'budget': 10_000_000, 'trade_rules': RULES, 'was_running': True}]}
    m, cfg, _ = make(tmp_path, cfg)
    a, s = m.get('a'), m.get('s')
    assert 'mode' not in a and 'mode' not in s
    assert a['was_running'] == {'upbit': True} and s['was_running'] == {'sim': True}
    assert (root / 'a' / 'upbit' / 'bot_positions.json').exists() and (root / 'a' / 'upbit' / 'trades.json').exists()
    assert not list((root / 's').glob('*.json'))                              # 예전 시뮬 기록은 보관 폴더로
    assert len(list((root / '_archive_sim').glob('s_*/sim_state.json'))) == 1
    assert list(tmp_path.glob('bots_backup_*/a/bot_positions.json'))          # 옮기기 전 통째 백업


# ── 만들기·고치기 ──
def test_create_copy_update_delete_without_mode(tmp_path):
    m, cfg, _ = make(tmp_path)
    ok, _, a = m.create({'name': '급등 켠 봇', 'budget': 3_000_000, 'mode': 'live',
                         'trade_rules': {**RULES, 'spike_rebuy': {'enabled': True}}})
    assert ok and 'mode' not in a and a['budget'] == 3_000_000 and a['was_running'] == {}
    ok, _, b = m.copy(a['id'])
    assert ok and b['name'] == '급등 켠 봇 복사본' and b['trade_rules'] == a['trade_rules']
    ok, _, _ = m.update(b['id'], {'name': '급등 끈 봇', 'trade_rules': {**RULES, 'spike_rebuy': {'enabled': False}}})
    assert ok and m.get(b['id'])['name'] == '급등 끈 봇' and m.get(a['id'])['trade_rules']['spike_rebuy']['enabled']
    assert m.create({'name': '기본'})[2]['budget'] == DEFAULT_BUDGET
    ok, _ = m.delete(b['id'])
    assert ok and m.get(b['id']) is None


def test_run_separately_per_mode(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES})
    assert m.send_to(a['id'], 'upbit')[0]                                     # 두 모드에 다 있게
    assert m.start(a['id'], 'sim')[0]
    assert m.is_running(a['id'], 'sim') and not m.is_running(a['id'], 'upbit')
    assert m.start(a['id'], 'upbit')[0]                                     # 같은 봇을 업비트에서도 따로
    assert m.running_modes(a['id']) == ['upbit', 'sim'] and m.running_total() == 2
    assert m.running_count('sim') == 1 and m.get(a['id'])['was_running'] == {'sim': True, 'upbit': True}
    ok, msg = m.delete(a['id'])
    assert not ok and '실행 중' in msg
    ok, _, _ = m.update(a['id'], {'budget': 500_000})                        # 실행 중에도 고칠 수 있음
    assert ok
    m.stop(a['id'], 'upbit')
    assert m.running_modes(a['id']) == ['sim'] and m.get(a['id'])['was_running'] == {'sim': True, 'upbit': False}
    cfg['app_mode'] = 'upbit'                                                 # 모드를 주지 않으면 지금 보는 모드
    assert not m.summary(a['id'])['running'] and m.summary(a['id'], 'sim')['running']


# ── 시뮬 = 가상 계좌, 실전과 같은 코드 ──
def test_sim_bot_buys_from_virtual_account_and_records_per_mode(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': RULES,
                        'risk_config': {'position_size_percent': 10, 'max_positions': 5}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    tick(e, data({'X': 0, 'Y': 0}))
    tick(e, data({'X': 6, 'Y': 0}))                                          # X 가 새로 들어옴 → 가상 계좌에서 삼
    bal = {b['currency']: float(b['balance']) for b in m.acct.get_balances()}
    assert bal['X'] > 0 and bal['KRW'] < 1_000_000
    assert set(e.ledger) == {'KRW-X'}                                         # 봇 장부 (실전과 같은 코드)
    assert (tmp_path / 'bots' / a['id'] / 'sim' / 'trades.json').exists()
    assert not (tmp_path / 'bots' / a['id'] / 'upbit').exists()              # 업비트 쪽에는 아무것도 없음
    s = m.summary(a['id'], 'sim')
    assert s['holding'] == 1 and s['trades'] == 1 and s['cash'] == pytest.approx(300_000 - s['invested'])
    assert m.summary(a['id'], 'upbit')['holding'] == 0


def test_upbit_budget_limits_buys(tmp_path):
    m, cfg, _ = make(tmp_path, mode='upbit')
    QuietEngine.fake = FakeExchange(krw=10_000_000)
    _, _, a = m.create({'name': '실전', 'budget': 150_000, 'trade_rules': RULES,
                        'risk_config': {'position_size_percent': 10, 'max_positions': 10}})
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({c: 0 for c in 'ABCDE'}))
    tick(e, data({c: 6 for c in 'ABC'} | {'D': 0, 'E': 0}))
    spent = sum(o[2] for o in e.exchange.orders if o[0] == 'buy')
    assert 0 < spent <= 150_000                                               # 원화가 1천만 원이어도 예산 안에서만
    QuietEngine.fake = None


def test_one_coin_one_bot_within_a_mode(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 10 ** 12, 'trade_rules': RULES})
    _, _, b = m.create({'name': 'B', 'budget': 10 ** 12, 'trade_rules': RULES})
    d = m.data_dir(a['id'], 'sim')
    d.mkdir(parents=True)
    (d / 'bot_positions.json').write_text(json.dumps({'KRW-X': {'qty': 1, 'entry_price': 100}}), encoding='utf-8')
    m.start(b['id'], 'sim')
    e = engine(m, b['id'], 'sim')
    tick(e, data({'X': 0, 'Y': 0}))
    tick(e, data({'X': 6, 'Y': 6}))
    assert set(e.ledger) == {'KRW-Y'}                                         # A 가 가진 X 는 안 삼
    assert m._taken_by_others(b['id'], 'upbit') == set()                      # 업비트 쪽은 따로


def test_resume_per_mode_and_upbit_needs_consent(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES})
    _, _, b = m.create({'name': 'B', 'trade_rules': RULES})
    m.send_to(b['id'], 'upbit')
    m.get(a['id'])['was_running'] = {'sim': True}
    m.get(b['id'])['was_running'] = {'upbit': True}                          # 동의 기록이 없으면 업비트는 다시 켜지 않음
    assert [(bid, ok) for bid, ok, _ in m.resume()] == [(a['id'], True)]
    cfg['live_risk_consent_at'] = '2026-09-29T00:00:00'
    for (bid, mm) in list(m.engines):
        m.stop(bid, mm)
    m.get(a['id'])['was_running'] = {'sim': True}
    m.get(b['id'])['was_running'] = {'upbit': True}
    assert sorted(m.running_modes(x) for x in (a['id'], b['id'])) == [[], []]
    m.resume()
    assert m.running_modes(a['id']) == ['sim'] and m.running_modes(b['id']) == ['upbit']
    m.stop_all(remember=True)                                                 # 프로그램 끌 때: 두 모드 모두, 표시는 남김
    assert m.running_total() == 0 and m.get(b['id'])['was_running']['upbit']


def test_start_all_skips_bot_without_buy_rule(tmp_path):
    m, cfg, _ = make(tmp_path)
    m.create({'name': '빈 봇'})
    assert m.start_all() == []


def test_detail_of_stopped_bot_reads_saved_files(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 1_000_000, 'trade_rules': RULES})
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))
    m.stop(a['id'])
    m.engines.pop((a['id'], 'sim'))                                          # 프로그램을 다시 켠 것처럼
    d = m.detail(a['id'])
    assert d['mode'] == 'sim' and [p['symbol'] for p in d['status']['positions']] == ['KRW-X']
    assert d['summary']['holding'] == 1 and d['summary']['trades'] == 1 and not d['summary']['running']
    assert d['recent_trades'][0]['side'] == 'buy' and 'active' in d['status']['spike']


def test_equity_history_and_trade_counts(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 1_000_000, 'trade_rules': RULES})
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))
    e._record_equity(now=__import__('time').time() + 400)
    d = m.detail(a['id'])
    assert len(d['history']) == 2
    assert d['counts']['buys'] == 1 and d['counts']['today'] == 1 and d['counts']['sells'] == 0


def test_sell_record_keeps_real_reason(tmp_path):
    """팔 때 기록의 이유 = 실제로 판 이유 (손절 등), 샀던 조건은 buy_reason 에 따로"""
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES, 'risk_config': {'stop_loss_percent': 5, 'take_profit_percent': 50}})
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))
    assert 'KRW-X' in e.ledger
    tick(e, data({'X': 6}, price=90.0))                                       # −10% → 손절
    items = m.trades(a['id'])
    sell = [t for t in items if t['side'] == 'sell'][-1]
    assert sell['reason'].startswith('손절') and sell['buy_reason'] == '오늘 5% 이상'
    assert 'X' not in {b['currency'] for b in m.acct.get_balances()}          # 가상 계좌에서도 팔림


def test_reset_sim_sells_bot_coins_and_keeps_old_records(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 500_000, 'trade_rules': RULES})
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))
    assert m.summary(a['id'])['holding'] == 1
    ok, msg = m.reset(a['id'])
    assert ok and '새로 시작' in msg and '팔았습니다' in msg
    s = m.summary(a['id'])
    assert s['running'] and s['holding'] == 0 and s['trades'] == 0
    assert 'X' not in {b['currency'] for b in m.acct.get_balances()}
    assert len(list((tmp_path / 'bots' / '_reset').glob(f"{a['id']}_*/trades.json"))) == 1
    assert not m.reset(a['id'], 'upbit')[0]                                  # 업비트 기록은 지우지 않음


def test_seed_top_sim_only_buys_top_coins_once(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 600_000, 'seed_top': 3, 'trade_rules': {'buy': [], 'sell': [], 'exclude': []}})
    assert m.summary(a['id'], 'sim')['seed_top'] == 3 and m.summary(a['id'], 'upbit')['seed_top'] == 0
    assert not m.start(a['id'], 'upbit')[0]                                  # 업비트에서는 시험용 사기 없음 → 매수 조건 필요
    assert m.start(a['id'], 'sim')[0]                                        # 시뮬에서는 매수 조건 없어도 시작
    e = engine(m, a['id'], 'sim')
    vals = {'A': 5e9, 'B': 4e9, 'USDT': 9e9, 'C': 3e9, 'D': 1e9}
    d = data({k: 0 for k in vals}, extra={k: {'trade_value_24h': v} for k, v in vals.items()})
    tick(e, d)
    assert set(e.ledger) == {'KRW-A', 'KRW-B', 'KRW-C'}                      # 스테이블 빼고 상위 3개
    spent = sum(p['entry_price'] * p['qty'] for p in e.ledger.values())
    assert 580_000 < spent <= 600_000                                         # 예산을 3등분
    tick(e, d)
    assert len(m.trades(a['id'], 'sim')) == 3                                 # 한 번만


def test_spike_active_lists_running_and_stopped_bots_per_mode(tmp_path):
    """'일부 팔고 다시 사기' 진행 중인 코인: 실행 중인 봇은 엔진에서, 멈춘 봇은 저장 파일에서 — 모드별로 따로"""
    from bot_core.spike_rebuy import SpikeRebuy
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': '켜진 봇'})
    _, _, b = m.create({'name': '멈춘 봇'})
    s = {'dips': [3, 5, 8], 'max_wait_h': 24}
    m.engines[(a['id'], 'sim')] = type('E', (), {'spike': SpikeRebuy()})()
    m.engines[(a['id'], 'sim')].spike.on_sold('KRW-A', 100.0, 1.0, 30_000, s, now=1000)
    stopped = SpikeRebuy(m.data_dir(b['id'], 'sim') / 'spike_state.json')
    stopped.on_sold('KRW-B', 2000.0, 1.0, 30_000, s, now=1000)
    got = {(x['bot_name'], x['symbol'], x['phase'], x['running']) for x in m.spike_active()}
    assert got == {('켜진 봇', 'KRW-A', 'waiting', True), ('멈춘 봇', 'KRW-B', 'waiting', False)}
    w = next(x for x in m.spike_active() if x['symbol'] == 'KRW-A')
    assert w['sold_price'] == 100.0 and [round(v) for v in w['left']] == [97, 95, 92]
    assert m.spike_active('upbit') == []                    # 업비트 모드에는 없음


def test_seed_all_buys_every_eligible_coin(tmp_path):
    """'전체'(-1): 스테이블 빼고 살 수 있는 코인 전부를 똑같이 나눠 삼 · 개수는 최대 300"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 400_000, 'seed_top': -1, 'trade_rules': {'buy': [], 'sell': [], 'exclude': []}})
    assert m.get(a['id'])['seed_top'] == -1 and m.summary(a['id'], 'sim')['seed_top'] == -1
    assert m.update(a['id'], {'seed_top': 5000})[2]['seed_top'] == 300
    m.update(a['id'], {'seed_top': -1})
    assert m.start(a['id'], 'sim')[0]
    e = engine(m, a['id'], 'sim')
    vals = {'A': 5e9, 'B': 4e9, 'USDT': 9e9, 'C': 3e9, 'D': 1e9}
    tick(e, data({k: 0 for k in vals}, extra={k: {'trade_value_24h': v} for k, v in vals.items()}))
    assert set(e.ledger) == {'KRW-A', 'KRW-B', 'KRW-C', 'KRW-D'}


def test_seed_count_changed_after_start_applies_until_bought(tmp_path):
    """켠 뒤 아직 사기 전에 개수를 바꾸면 바뀐 개수로 삼 · 산 뒤에는 바꿔도 다시 사지 않음(화면엔 '이미 샀습니다')"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 400_000, 'seed_top': 1, 'trade_rules': {'buy': [], 'sell': [], 'exclude': []}})
    assert m.start(a['id'], 'sim')[0]
    m.update(a['id'], {'seed_top': -1})                                      # 켠 뒤, 사기 전에 '전체'로
    e = engine(m, a['id'], 'sim')
    vals = {'A': 5e9, 'B': 4e9, 'C': 3e9}
    d = data({k: 0 for k in vals}, extra={k: {'trade_value_24h': v} for k, v in vals.items()})
    assert not m.summary(a['id'], 'sim')['seeded']
    tick(e, d)
    assert set(e.ledger) == {'KRW-A', 'KRW-B', 'KRW-C'}
    assert m.summary(a['id'], 'sim')['seeded']
    assert any('전체 3개' in str(v) for v in m.trades(a['id'], 'sim')[0].values())
    m.update(a['id'], {'seed_top': 1})
    tick(e, d)
    assert len(m.trades(a['id'], 'sim')) == 3                                 # 이미 샀으면 다시 안 삼


def test_engine_stopped_while_fetching_does_not_trade(tmp_path):
    """시세를 받는 동안(1분 넘게 걸릴 수 있음) 멈춘 봇은 받은 뒤에도 사고팔지 않음 — 새로 켠 봇과 겹쳐 두 번 사던 문제"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'seed_top': -1, 'trade_rules': {'buy': [], 'sell': [], 'exclude': []}})
    assert m.start(a['id'], 'sim')[0]
    e = engine(m, a['id'], 'sim')
    d = data({'A': 0, 'B': 0}, extra={'A': {'trade_value_24h': 5e9}, 'B': {'trade_value_24h': 4e9}})
    PRICES.update({f"KRW-{c['currency']}": c['current_price'] for c in d['coins']})

    def slow_fetch():
        e.is_running = False                       # 받는 동안 [중지]
        return d
    e._get_market_data = slow_fetch
    e.tick()
    assert e.ledger == {} and m.trades(a['id'], 'sim') == []


def test_seed_only_once_per_bot_folder_even_with_two_engines(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'seed_top': -1, 'trade_rules': {'buy': [], 'sell': [], 'exclude': []}})
    assert m.start(a['id'], 'sim')[0]
    e1 = engine(m, a['id'], 'sim')
    e2 = QuietEngine(m.bot_config(a['id'], 'sim'), data_dir=m.data_dir(a['id'], 'sim'), account=m.acct)   # 겹친 두 번째 엔진
    e2.trade_logger = NullLogger()
    assert e2.initialize()[0] and e2._seed_pending
    d = data({'A': 0, 'B': 0}, extra={'A': {'trade_value_24h': 5e9}, 'B': {'trade_value_24h': 4e9}})
    tick(e1, d)
    tick(e2, d)
    assert len([o for o in m.acct.closed_orders() if o['side'] == 'bid']) == 2          # A·B 한 번씩만


def test_bought_qty_is_the_fill_not_the_balance_change(tmp_path):
    """같은 코인을 다른 곳에서 동시에 사도 봇 장부의 수량·산 가격은 이 주문의 체결 수량으로"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': {'buy': [{'id': 'my_up'}], 'sell': [], 'exclude': []}})
    assert m.start(a['id'], 'sim')[0]
    e = engine(m, a['id'], 'sim')
    PRICES['KRW-A'] = 100.0
    real = e.exchange.buy_market_order

    def buy_and_someone_else_too(symbol, krw):
        out = real(symbol, krw)
        m.acct.buy_market_order(symbol, krw)       # 같은 1초에 다른 곳에서도 같은 코인을 삼
        return out
    e.exchange.buy_market_order = buy_and_someone_else_too
    assert e._order_buy('KRW-A', {'current_price': 100.0, 'korean_name': 'A'}, 50_000, 't', 't', quiet=True)
    pos = e.ledger['KRW-A']
    assert 99 < pos['entry_price'] < 101.5 and pos['qty'] < 500           # 절반 가격·두 배 수량이 아님


# ── 처음 설치: 예시 봇 (만드는 법을 보여 주는 교과서식 규칙, 시뮬레이션 전용) ──
NEW_INSTALL = {'mode': 'simulation', 'trade_rules': {'buy': {}, 'sell': {}, 'exclude': []}}   # 새 설정 파일의 기본값


def test_new_install_gets_two_stopped_example_bots(tmp_path):
    m, cfg, saved = make(tmp_path, dict(NEW_INSTALL))
    bots = m.bots()
    assert [b['id'] for b in bots] == ['ex1', 'ex2'] and all(b['example'] for b in bots) and saved
    assert all(not m.running_modes(b['id']) for b in bots)                       # 넣어 두기만 하고 켜지 않음
    assert be.rule_list(bots[0]['trade_rules'], 'buy')[0]['id'] == 'rsi_low'
    assert be.rule_list(bots[1]['trade_rules'], 'sell')[0]['id'] == 'dead_cross'
    names = ' '.join(b['name'] for b in bots)
    assert '예시' in names and not any(w in names for w in ('추천', '수익', '보장', '유망'))
    assert m.summary('ex1')['example'] is True
    m.bots()
    assert len(m.bots()) == 2                                                    # 한 번만


def test_upgrade_with_old_rules_still_gets_default_bot(tmp_path):
    m, cfg, _ = make(tmp_path, {'mode': 'simulation', 'trade_rules': RULES})
    bots = m.bots()
    assert len(bots) == 1 and bots[0]['name'] == '기본 봇' and not bots[0].get('example')


def test_existing_bots_unchanged(tmp_path):
    m, cfg, _ = make(tmp_path, {'bots': [{'id': 'b1', 'name': '내 봇', 'trade_rules': RULES, 'was_running': {}}]})
    assert [b['id'] for b in m.bots()] == ['b1']


def test_example_bot_runs_only_in_simulation(tmp_path):
    from bot_core.bot_manager import EXAMPLE_ONLY_SIM
    m, cfg, _ = make(tmp_path, dict(NEW_INSTALL))
    m.bots()
    ok, msg = m.start('ex1', 'upbit')
    assert not ok and msg == EXAMPLE_ONLY_SIM and not m.running_modes('ex1')
    assert m.start_all(mode='upbit') == []                                       # [모두 시작]은 조용히 건너뜀
    ok, _ = m.start('ex1', 'sim')
    assert ok and m.running_modes('ex1') == ['sim']


def test_copy_of_example_is_a_normal_bot(tmp_path):
    m, cfg, _ = make(tmp_path, dict(NEW_INSTALL))
    m.bots()
    ok, _, bot = m.copy('ex1')
    assert ok and bot['name'] == '내 봇 (예시에서 복사)' and not bot.get('example')
    assert be.rule_list(bot['trade_rules'], 'buy')[0]['id'] == 'rsi_low'
    assert m.send_to(bot['id'], 'upbit')[0]                                      # 예시는 못 보내도 복사본은 됨
    ok, msg = m.start(bot['id'], 'upbit')                                        # 예시 제한 없음 (가짜 거래소)
    assert ok, msg


def test_example_flag_cannot_be_set_or_cleared_by_edit(tmp_path):
    m, cfg, _ = make(tmp_path, dict(NEW_INSTALL))
    m.bots()
    m.update('ex1', {'name': '바꾼 이름', 'example': False})
    assert m.get('ex1')['example'] is True and m.get('ex1')['name'] == '바꾼 이름'
    ok, _, mine = m.create({'name': '새 봇', 'example': True})
    assert ok and not mine.get('example')


def test_dashboard_refuses_example_start_in_upbit_before_confirm(tmp_path):
    """업비트 모드에서 예시 봇 [시작]: 실전 확인 창(CONFIRM_LIVE)·계정 확인 전에 바로 안내"""
    from gui.web_dashboard import WebDashboard
    wd = WebDashboard(str(tmp_path / 'config' / 'bot_config.json'), port=5998, user_data_dir=tmp_path)
    wd._save_config_internal = lambda *a, **k: None
    wd.config.pop('bots', None)
    wd.config['trade_rules'] = {'buy': {}, 'sell': {}, 'exclude': []}
    wd.bots._migrated = False
    wd.config['app_mode'] = 'upbit'
    assert [b['id'] for b in wd.bots.bots()] == ['ex1', 'ex2']
    r = wd.app.test_client().post('/api/bots/ex1/start', json={})
    assert r.status_code == 400 and r.get_json()['error_code'] == 'EXAMPLE_SIM_ONLY'


# ── 처음에 사 둘 코인 (고른 코인을 시작할 때 한 번 나눠 사기, 두 모드) ──
NO_BUY = {'buy': [], 'sell': [], 'exclude': ['E']}


def test_seed_coins_saved_clean_and_exclusive_with_seed_top(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'seed_top': 10, 'seed_coins': ['krw-drv', 'B3', 'b3', '한글', 'X' * 20, '', None],
                        'trade_rules': NO_BUY})
    assert a['seed_coins'] == ['DRV', 'B3'] and a['seed_top'] == 0          # 정리 · 고른 코인이 있으면 거래 상위 N개는 끔
    many = m.update(a['id'], {'seed_coins': [f'C{i}' for i in range(15)]})[2]
    assert len(many['seed_coins']) == 10
    _, _, c = m.copy(a['id'])
    assert c['seed_coins'] == many['seed_coins']
    s = m.summary(a['id'], 'upbit')
    assert s['seed_coins'] == many['seed_coins'] and s['seeded'] is False
    assert m.can_start(m.get(a['id']), 'upbit') and m.can_start(m.get(a['id']), 'sim')   # 매수 조건 없이도 시작


def test_seed_coins_sim_buys_only_chosen_once_and_skips_bad(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'seed_coins': ['A', 'USDT', 'C', 'E', 'NOPE'], 'trade_rules': NO_BUY})
    assert m.start(a['id'], 'sim')[0]
    e = engine(m, a['id'], 'sim')
    d = data({k: 0 for k in ('A', 'B', 'C', 'E', 'USDT')})
    tick(e, d)
    assert set(e.ledger) == {'KRW-A', 'KRW-C'}                               # 스테이블·사지 않을 코인·없는 코인은 빼고
    spent = sum(p['entry_price'] * p['qty'] for p in e.ledger.values())
    assert 290_000 < spent <= 300_000                                         # 예산을 산 코인 수(2)로 나눠
    tick(e, d)
    assert len(m.trades(a['id'], 'sim')) == 2                                 # 한 번만
    assert m.summary(a['id'], 'sim')['seeded'] is True


def test_dca_buys_same_amount_every_n_days_until_budget(tmp_path):
    """나눠 사기: 켠 날 한 번, 그 뒤 N일마다 같은 금액 · 같은 코인에 더해 모음 · 예산을 다 쓰면 멈춤"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, bad = m.create({'name': 'X', 'dca': {'coin': '한글', 'every_days': 7, 'krw': 100_000}, 'trade_rules': NO_BUY})
    assert 'dca' not in bad and not m.can_start(m.get(bad['id']), 'sim')
    _, _, a = m.create({'name': 'A', 'budget': 250_000, 'dca': {'coin': 'krw-a', 'every_days': 7, 'krw': 100_000}, 'trade_rules': NO_BUY})
    assert a['dca'] == {'coin': 'A', 'every_days': 7.0, 'krw': 100_000.0}
    assert m.summary(a['id'], 'sim')['dca']['coin'] == 'A'
    assert m.start(a['id'], 'sim')[0]
    e = engine(m, a['id'], 'sim')
    d = data({k: 0 for k in ('A', 'B', 'C')})
    st = e.data_dir / 'dca.json'

    def week_later():
        s = json.loads(st.read_text(encoding='utf-8'))
        s['last'] = (be.datetime.now() - be.timedelta(days=7, minutes=1)).isoformat(timespec='seconds')
        st.write_text(json.dumps(s), encoding='utf-8')

    tick(e, d)
    tick(e, d)
    assert len(m.trades(a['id'], 'sim')) == 1 and set(e.ledger) == {'KRW-A'}       # 7일 안에는 다시 사지 않음
    week_later()
    tick(e, d)
    assert len(m.trades(a['id'], 'sim')) == 2 and json.loads(st.read_text(encoding='utf-8'))['n'] == 2
    spent = e.ledger['KRW-A']['entry_price'] * e.ledger['KRW-A']['qty']
    assert 199_000 < spent <= 200_000                                             # 한 코인에 더해 모음
    week_later()
    tick(e, d)                                                                    # 남은 5만 원쯤만
    week_later()
    tick(e, d)                                                                    # 예산을 다 써서 더 사지 않음
    assert len(m.trades(a['id'], 'sim')) == 3
    assert 245_000 < e.ledger['KRW-A']['entry_price'] * e.ledger['KRW-A']['qty'] <= 250_000


def test_seed_coins_live_needs_budget_then_buys_split(tmp_path):
    m, cfg, _ = make(tmp_path, mode='upbit')
    QuietEngine.fake = FakeExchange(krw=10_000_000)
    _, _, a = m.create({'name': '실전', 'budget': 0, 'seed_coins': ['A', 'B'], 'trade_rules': NO_BUY})
    ok, msg = m.start(a['id'])
    assert not ok and '최대 투입 금액' in msg                                  # 예산 없이 원화 전체를 나눠 사지 않게
    m.update(a['id'], {'budget': 200_000})
    assert m.start(a['id'])[0]
    e = engine(m, a['id'])
    d = data({'A': 0, 'B': 0, 'C': 0})
    tick(e, d)
    buys = [o for o in e.exchange.orders if o[0] == 'buy']
    assert {o[1] for o in buys} == {'KRW-A', 'KRW-B'} and 0 < sum(o[2] for o in buys) <= 200_000
    tick(e, d)
    assert len([o for o in e.exchange.orders if o[0] == 'buy']) == 2          # 한 번만
    QuietEngine.fake = None


def test_live_start_summary_lists_seed_coins(tmp_path):
    m, cfg, _ = make(tmp_path, mode='upbit')
    _, _, a = m.create({'name': '실전', 'budget': 200_000, 'seed_coins': ['A', 'B'], 'trade_rules': NO_BUY})
    assert m.bot_config(a['id'], 'upbit')['seed_coins'] == ['A', 'B']
    assert m.bot_config(a['id'], 'upbit')['seed_top'] == 0


# ── '코인 성격' 조건(급등이 잦던 코인): '그리고'로 묶지 않으면 시작할 때 지금 맞는 코인을 삼 ──
PRONE = {'id': 'spike_prone', 'params': {'max_value': 100, 'new_days': 90, 'max_price': 10}}


def prone_data(prices, values, **kw):
    """10원 미만이면 '급등이 잦던 코인' (바이낸스에 있음 · 오래된 코인으로 둠)"""
    extra = {k: {'trade_value_24h': v, 'on_binance': True, 'listed_days': 400} for k, v in values.items()}
    return data({k: 0 for k in prices}, price=prices, extra=extra, **kw)


def test_trait_condition_alone_buys_at_start_by_value_up_to_max_positions(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'trade_rules': {'buy': [PRONE], 'sell': [], 'exclude': []},
                        'risk_config': {'max_positions': 2, 'position_size_percent': 10}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    prices = {'A': 5.0, 'B': 5.0, 'C': 5.0, 'D': 500.0}
    values = {'A': 1e9, 'B': 5e9, 'C': 3e9, 'D': 9e9}
    tick(e, prone_data(prices, values))
    assert set(e.ledger) == {'KRW-B', 'KRW-C'}                   # 맞는 A·B·C 중 거래 많은 순 2개 (D 는 10원 넘음)
    buys = [d['message'] for d in e.decisions if d['kind'] == 'buy']
    assert len(buys) == 2 and all('시작할 때' in x for x in buys)        # 판단 기록에 '시작할 때' 산 것으로
    tick(e, prone_data(prices, values))
    assert len(m.trades(a['id'], 'sim')) == 2                    # 다시 켜지 않으면 더 사지 않음


def test_trait_condition_with_and_is_only_a_filter(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    rules = {'buy': [PRONE, {'id': 'my_up'}], 'buy_match': 'all', 'sell': [], 'exclude': []}
    _, _, a = m.create({'name': 'A', 'trade_rules': rules, 'risk_config': {'max_positions': 5}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    prices = {'A': 5.0, 'B': 5.0}
    values = {'A': 1e9, 'B': 2e9}
    tick(e, prone_data(prices, values))
    assert not e.ledger                                          # '그리고': 시작할 때는 사지 않음 (기준만)
    d = prone_data(prices, values)
    for c in d['coins']:
        if c['currency'] == 'A':
            c['change_rate'] = 6                                 # A 만 '오름' 조건에 새로 맞음
    tick(e, d)
    assert set(e.ledger) == {'KRW-A'}


def test_trait_condition_later_buys_only_new_entries(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'trade_rules': {'buy': [PRONE], 'sell': [], 'exclude': []},
                        'risk_config': {'max_positions': 5}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    tick(e, prone_data({'A': 5.0, 'B': 50.0}, {'A': 1e9, 'B': 1e9}))
    assert set(e.ledger) == {'KRW-A'}
    tick(e, prone_data({'A': 5.0, 'B': 9.0}, {'A': 1e9, 'B': 1e9}))   # B 가 10원 아래로 → 새로 맞음
    assert set(e.ledger) == {'KRW-A', 'KRW-B'}


def test_trait_start_buy_equal_amounts_and_one_line_when_budget_runs_out(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 0, 'budget_pct': 100, 'trade_rules': {'buy': [PRONE], 'sell': [], 'exclude': []},
                        'risk_config': {'max_positions': 300, 'position_size_percent': 25}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    prices = {k: 5.0 for k in 'ABCDEFG'}
    values = {k: (10 - i) * 1e9 for i, k in enumerate('ABCDEFG')}
    tick(e, prone_data(prices, values))
    spent = [p['entry_price'] * p['qty'] for p in e.ledger.values()]
    assert len(spent) == 3 and max(spent) - min(spent) < 5_000             # 코인마다 같은 금액 (잔고 25% = 25만 원) · 4번째는 모자람
    msgs = [d['message'] for d in e.decisions]
    assert sum('예산을 다 써서' in x for x in msgs) == 1 and not any('매수 보류' in x for x in msgs)


def test_trait_start_buy_after_restart_uses_budget_not_leftover(tmp_path):
    m, cfg, _ = make(tmp_path, krw=10_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 1_000_000, 'trade_rules': {'buy': [PRONE], 'sell': [], 'exclude': []},
                        'risk_config': {'max_positions': 300, 'position_size_percent': 30}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    prices = {k: 5.0 for k in 'ABCDEFG'}
    values = {k: (10 - i) * 1e9 for i, k in enumerate('ABCDEFG')}
    tick(e, prone_data(prices, values))
    first = sorted(round(p['entry_price'] * p['qty']) for p in e.ledger.values())
    assert len(first) == 3 and min(first) > 290_000                      # 예산 100만 원의 30% = 30만 원씩 3개
    m.stop(a['id'], 'sim')
    m.start(a['id'], 'sim')                                               # 다시 켬: 남은 예산 약 10만 원 < 30만 원
    e2 = engine(m, a['id'], 'sim')
    tick(e2, prone_data(prices, values))
    assert len(e2.ledger) == 3                                            # 작게 더 사지 않음
    assert any('더 사지 않습니다' in d['message'] for d in e2.decisions)


def test_trait_condition_buys_more_when_money_frees_up_but_not_recently_sold(tmp_path):
    """켠 뒤에도 돈이 생기면(손절로 판 돈) 매매대상 중 안 가진 코인을 더 삼 — 방금 판 코인은 24시간 동안 빼고"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 0, 'budget_pct': 100, 'trade_rules': {'buy': [PRONE], 'sell': [], 'exclude': []},
                        'risk_config': {'max_positions': 300, 'position_size_percent': 20, 'stop_loss_percent': 10}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    prices = {k: 5.0 for k in 'ABCDEFG'}
    values = {k: (10 - i) * 1e9 for i, k in enumerate('ABCDEFG')}
    tick(e, prone_data(prices, values))
    assert set(e.ledger) == {'KRW-A', 'KRW-B', 'KRW-C', 'KRW-D'}          # 20만 원씩 4개 · 5번째는 돈이 모자람
    tick(e, prone_data(prices, values))
    assert len(e.ledger) == 4                                              # 돈이 없으면 조용히 기다림
    assert not any('예산을 다 써서' in d['message'] for d in e.decisions[-3:])
    tick(e, prone_data({**prices, 'B': 4.0}, values))                      # B −20% → 손절 (돈이 생김)
    assert 'KRW-B' not in e.ledger
    tick(e, prone_data({**prices, 'B': 4.0}, values))
    assert 'KRW-E' in e.ledger and 'KRW-B' not in e.ledger                 # 거래 많은 순으로 E 를 더 삼 · 방금 판 B 는 아님
    assert any('돈이 생겨' in d['message'] for d in e.decisions if d['kind'] == 'buy')



def test_manually_sold_coin_is_not_bought_back(tmp_path):
    """사용자가 직접 판 코인: 돈이 생겨도 · 봇을 다시 켜도 24시간 동안 다시 사지 않음 (전에는 몇 초 뒤 다시 샀음)"""
    import time as _t
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 0, 'budget_pct': 100, 'trade_rules': {'buy': [PRONE], 'sell': [], 'exclude': []},
                        'risk_config': {'max_positions': 300, 'position_size_percent': 20, 'stop_loss_percent': 50}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    prices = {k: 5.0 for k in 'ABCDEFG'}
    values = {k: (10 - i) * 1e9 for i, k in enumerate('ABCDEFG')}
    tick(e, prone_data(prices, values))
    assert 'KRW-A' in e.ledger
    m.acct.sell_market_order('KRW-A', m.acct.get_balance('A'))           # 직접 전부 팔기
    tick(e, prone_data(prices, values))                                   # 한 번 봄 (바로 줄이지 않음)
    e._shrink_seen['KRW-A'] = _t.time() - 10
    tick(e, prone_data(prices, values))                                   # 장부에서 뺌
    assert 'KRW-A' not in e.ledger
    tick(e, prone_data(prices, values))                                   # 돈이 생겨 더 삼 — A 는 아님
    assert 'KRW-A' not in e.ledger and 'KRW-E' in e.ledger
    m.stop(a['id'], 'sim')
    m.start(a['id'], 'sim')                                               # 다시 켜도 A 는 사지 않음
    e2 = engine(m, a['id'], 'sim')
    tick(e2, prone_data(prices, values))
    assert 'KRW-A' not in e2.ledger


def test_order_krw_buys_same_amount_every_time(tmp_path):
    """코인당 금액을 정하면: 켤 때 · 돈이 생겨 더 살 때 모두 같은 금액 (전에는 1회 매수 비율 × 그때 굴리는 돈이라 들쭉날쭉)"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 0, 'budget_pct': 100, 'trade_rules': {'buy': [PRONE], 'sell': [], 'exclude': []},
                        'risk_config': {'max_positions': 300, 'position_size_percent': 40, 'order_krw': 150_000, 'stop_loss_percent': 10}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    prices = {k: 5.0 for k in 'ABCDEFG'}
    values = {k: (10 - i) * 1e9 for i, k in enumerate('ABCDEFG')}
    tick(e, prone_data(prices, values))
    costs = [v['qty'] * v['entry_price'] for v in e.ledger.values()]
    assert len(costs) == 6 and all(abs(c - 150_000) < 2_000 for c in costs)    # 40% 가 아니라 15만 원씩 6개 (100만 원 안쪽)
    tick(e, prone_data({**prices, 'B': 4.0}, values))                          # B 손절 → 돈이 생김
    tick(e, prone_data({**prices, 'B': 4.0}, values))
    assert 'KRW-G' in e.ledger and abs(e.ledger['KRW-G']['qty'] * e.ledger['KRW-G']['entry_price'] - 150_000) < 2_000

def test_all_coins_alone_buys_everything_buyable_at_start(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 0, 'budget_pct': 100, 'trade_rules': {'buy': [{'id': 'all_coins'}], 'sell': [], 'exclude': ['C']},
                        'risk_config': {'max_positions': 300, 'position_size_percent': 10}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    vals = {'A': 5e9, 'B': 4e9, 'USDT': 9e9, 'C': 3e9, 'D': 1e9}
    tick(e, data({k: 0 for k in vals}, extra={k: {'trade_value_24h': v} for k, v in vals.items()}))
    assert set(e.ledger) == {'KRW-A', 'KRW-B', 'KRW-D'}                 # 스테이블·사지 않을 코인 빼고 전부


def test_coin_share_lets_bots_compete_on_same_coin(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 10 ** 12, 'trade_rules': RULES})
    _, _, b = m.create({'name': 'B', 'budget': 10 ** 12, 'trade_rules': RULES})
    PRICES['KRW-X'] = 100.0
    m.acct.buy_market_order('KRW-X', 10_000)                                   # A 몫 (계좌에 실제로 있음)
    a_qty = m.acct.get_balance('X')
    d = m.data_dir(a['id'], 'sim')
    d.mkdir(parents=True)
    (d / 'bot_positions.json').write_text(json.dumps({'KRW-X': {'qty': a_qty, 'entry_price': 100}}), encoding='utf-8')
    m.set_coin_share(True, 'sim')                                             # 봇 경쟁 켬 (시뮬레이션만)
    assert m._taken_by_others(b['id'], 'sim') == set() and not m.coin_share('upbit')
    m.start(b['id'], 'sim')
    e = engine(m, b['id'], 'sim')
    tick(e, data({'X': 0, 'Y': 0}))
    tick(e, data({'X': 6, 'Y': 6}))
    assert set(e.ledger) == {'KRW-X', 'KRW-Y'}                               # A 가 가진 X 도 B 가 따로 삼
    b_qty = e.ledger['KRW-X']['qty']
    assert abs(m.acct.get_balance('X') - (a_qty + b_qty)) < 1e-6              # 계좌 = A 몫 + B 몫
    tick(e, data({'X': 6, 'Y': 6}))                                          # 장부 맞추기: B 가 A 몫을 가져가거나 줄이지 않음
    assert abs(e.ledger['KRW-X']['qty'] - b_qty) < 1e-9


def test_skipped_coin_is_bought_after_coin_share_turned_on(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 10 ** 12, 'trade_rules': RULES})
    _, _, b = m.create({'name': 'B', 'budget': 10 ** 12, 'trade_rules': RULES})
    PRICES['KRW-X'] = 100.0
    m.acct.buy_market_order('KRW-X', 10_000)
    d = m.data_dir(a['id'], 'sim')
    d.mkdir(parents=True)
    (d / 'bot_positions.json').write_text(json.dumps({'KRW-X': {'qty': m.acct.get_balance('X'), 'entry_price': 100}}), encoding='utf-8')
    m.start(b['id'], 'sim')
    e = engine(m, b['id'], 'sim')
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))                                                   # 새로 맞음 → 다른 봇 것이라 건너뜀
    assert 'KRW-X' not in e.ledger
    m.set_coin_share(True, 'sim')                                             # 봇 경쟁 켬
    tick(e, data({'X': 6}))                                                   # 그대로 맞는 중 → 이제 삼
    assert 'KRW-X' in e.ledger


def test_bots_are_created_and_deleted_per_mode(tmp_path):
    """봇은 모드마다 따로: 만들면 지금 모드에만, 지우면 지금 모드 것만 (다른 모드의 봇 · 기록은 그대로), 보내기로 다른 모드에"""
    m, cfg, _ = make(tmp_path)                                                   # 시뮬레이션에서 시작
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES})
    assert m.modes_of(m.get(a['id'])) == ['sim'] and [b['id'] for b in m.list('upbit')] == []
    assert not m.start(a['id'], 'upbit')[0]                                      # 업비트에는 없는 봇
    assert not m.copy_mode_rules(a['id'], 'upbit', 'sim')[0]                     # 없는 모드에서 규칙 가져오기 X
    ok, msg = m.send_to(a['id'], 'upbit')                                        # 시뮬레이션 → 업비트로 보내기
    assert ok and '보냈습니다' in msg and m.modes_of(m.get(a['id'])) == ['upbit', 'sim']
    assert m.rules_of(m.get(a['id']), 'upbit') == m.rules_of(m.get(a['id']), 'sim') and m.pct_of(m.get(a['id']), 'upbit') == 0
    assert [b['id'] for b in m.list('upbit')] == [a['id']]
    sim_dir = m.data_dir(a['id'], 'sim')
    sim_dir.mkdir(parents=True, exist_ok=True)
    (sim_dir / 'trades.json').write_text('[{"side": "buy"}]', encoding='utf-8')
    m.data_dir(a['id'], 'upbit').mkdir(parents=True, exist_ok=True)
    ok, msg = m.delete(a['id'], 'upbit')                                         # 업비트에서만 지움
    assert ok and '시뮬레이션의 봇과 기록은 그대로' in msg
    assert m.get(a['id']) and m.modes_of(m.get(a['id'])) == ['sim'] and (sim_dir / 'trades.json').exists()
    assert not m.data_dir(a['id'], 'upbit').exists() and [b['id'] for b in m.list('upbit')] == []
    ok, msg = m.send_to(a['id'], 'upbit')                                        # 다시 보내면 업비트에 새로
    m.update(a['id'], {'trade_rules': {'buy': [], 'sell': [], 'exclude': []}})   # (지금 모드 = 시뮬레이션) 규칙 바꾸고
    ok, msg = m.send_to(a['id'], 'upbit')                                        # 이미 있으면 규칙만 바꿈
    assert ok and '바꿨습니다' in msg and m.rules_of(m.get(a['id']), 'upbit')['buy'] == []
    assert m.delete(a['id'], 'sim')[0] and m.get(a['id']) and m.modes_of(m.get(a['id'])) == ['upbit']
    assert m.delete(a['id'], 'upbit')[0] and m.get(a['id']) is None              # 마지막 모드에서 지우면 봇이 사라짐


def test_reset_sells_are_recorded_as_this_bot(tmp_path):
    """초기화로 판 코인은 거래 내역에서 '직접 주문'이 아니라 그 봇의 '초기화로 정리'로 (봇 성적에서는 빠짐)"""
    from bot_core.trade_store import TradeStore
    m, cfg, _ = make(tmp_path)
    st = TradeStore(tmp_path / 'sim.db')
    m._get_trade_store = lambda mode: st
    _, _, a = m.create({'name': 'A', 'budget': 500_000, 'trade_rules': RULES})
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))
    assert m.summary(a['id'])['holding'] == 1
    assert m.reset(a['id'])[0]
    rows = st.bot_trades(a['id'], all_runs=True)
    sell = [r for r in rows if r['side'] == 'sell']
    assert len(sell) == 1 and sell[0]['reason'] == '모두 팔고 새로 시작' and sell[0]['order_id']
    assert st.bot_trades(a['id']) == [] or all(r['ts'] >= st.reset_ts(a['id']) for r in st.bot_trades(a['id']))
    assert all(r['reason'] != '모두 팔고 새로 시작' for r in st.bot_trades(a['id']))   # 봇 화면 · 성적에서는 빠짐


def test_apply_preview_lists_only_newly_triggered_sells(tmp_path, monkeypatch):
    """적용 전 미리 보기: 새 값 때문에 곧바로 팔리게 되는 코인만 · 새로 넣은 '시작할 때 사기' 조건"""
    m, cfg, _ = make(tmp_path)
    ok, _, bot = m.create({'name': '봇', 'budget': 1_000_000, 'trade_rules': RULES,
                           'risk_config': {'stop_loss_percent': 10, 'take_profit_percent': 50, 'trailing_stop_percent': 0}})
    pos = [{'symbol': 'KRW-A', 'quantity': 10, 'entry_price': 100, 'current_price': 96},    # −4%
           {'symbol': 'KRW-B', 'quantity': 1, 'entry_price': 100, 'current_price': 85},     # −15% (지금 값으로도 손절)
           {'symbol': 'KRW-C', 'quantity': 2, 'entry_price': 100, 'current_price': 120}]    # +20%
    monkeypatch.setattr(m, 'detail', lambda bid, mode=None: {'status': {'positions': pos}})
    out = m.apply_preview(bot['id'], {'risk_config': {'stop_loss_percent': 3}})
    assert [s['symbol'] for s in out['sells']] == ['KRW-A'] and out['sells'][0]['why'] == '손절 −3%'
    assert out['sells'][0]['profit'] == pytest.approx(-40)
    out = m.apply_preview(bot['id'], {'risk_config': {'take_profit_percent': 15}})
    assert [s['symbol'] for s in out['sells']] == ['KRW-C'] and out['sells'][0]['why'] == '익절 +15%'
    out = m.apply_preview(bot['id'], {'trade_rules': {**RULES, 'sell_on': False}, 'risk_config': {'take_profit_percent': 15}})
    assert out['sells'] == []                                                            # 매도를 꺼 두면 익절 안 함
    out = m.apply_preview(bot['id'], {'trade_rules': {**RULES, 'buy': [{'id': 'all_coins'}]}})
    assert out['start_buys'] == ['모든 코인'] and out['sells'] == []


# ── 빗썸 모드 (2026-10-12): 업비트 · 빗썸 · 시뮬레이션 세 모드가 따로 ──
def test_bithumb_mode_is_separate_live_mode(tmp_path):
    seen = []
    m, cfg, _ = make(tmp_path, mode='bithumb')
    m._get_market_data_for = lambda ex: (seen.append(ex), data({'A': 0}))[1]      # 빗썸 봇은 빗썸 시세로
    cfg['live_risk_consent_at'] = '2026-10-12T00:00:00'
    cfg['bithumb_fee_pct'] = 0.04
    _, _, b = m.create({'name': '빗썸 봇', 'budget': 10 ** 9, 'trade_rules': RULES})
    assert m.modes_of(b) == ['bithumb'] and not m.bots_in('upbit') and not m.bots_in('sim')
    ok, msg = m.start(b['id'], 'bithumb')
    assert ok, msg
    e = engine(m, b['id'], 'bithumb')
    assert e.mode == 'live' and e.account_mode == 'bithumb' and e.fee == pytest.approx(0.0004)   # 설정 수수료
    assert m.data_dir(b['id'], 'bithumb') == tmp_path / 'bots' / b['id'] / 'bithumb'
    assert m._market_data('bithumb') and seen == ['bithumb']
    assert m.running_modes(b['id']) == ['bithumb'] and m.summary(b['id'], 'bithumb')['mode'] == 'bithumb'
    # 다시 켜기: 빗썸 모드에서 켜져 있던 봇도
    m.stop_all(remember=True)
    assert not m.running_modes(b['id'])
    out = m.resume()
    assert any(ok for _, ok, _ in out) and m.is_running(b['id'], 'bithumb')


def test_bithumb_mode_respects_license_block_and_send_to(tmp_path):
    m, cfg, _ = make(tmp_path, mode='sim')
    m._live_block = lambda: '계정 확인이 필요합니다'
    _, _, b = m.create({'name': 'A', 'trade_rules': RULES})
    ok, msg = m.send_to(b['id'], 'bithumb', 'sim')
    assert ok and '빗썸' in msg and m.modes_of(m.get(b['id'])) == ['bithumb', 'sim']
    assert m.rules_of(m.get(b['id']), 'bithumb') == m.rules_of(m.get(b['id']), 'sim')
    assert m.live_blocked('bithumb') == '계정 확인이 필요합니다' and m.live_blocked('sim') is None
    ok, msg = m.start(b['id'], 'bithumb')
    assert not ok and '계정' in msg


def test_legacy_bot_without_modes_stays_upbit_and_sim(tmp_path):
    cfg = {'bots': [{'id': 'o', 'name': '예전', 'budget': 0, 'trade_rules': RULES, 'was_running': {}}]}
    m, cfg, _ = make(tmp_path, cfg)
    assert m.modes_of(m.get('o')) == ['upbit', 'sim']


def test_sim_per_bot_money_each_bot_fixed_and_account_grows(tmp_path):
    """시뮬레이션 봇마다 정해진 돈: 비율 0 · 시작 금액 = 봇 몫, 봇이 생기면 계좌에 그만큼 더해지고, 같은 코인도 봇마다 (봇 경쟁 늘 켬)"""
    m, cfg, _ = make(tmp_path, krw=0)
    m.config['sim_per_bot_krw'] = 10_000_000
    _, _, a = m.create({'name': 'A', 'budget_pct': 50, 'trade_rules': RULES})
    _, _, b = m.create({'name': 'B', 'budget_pct': 80, 'trade_rules': RULES})
    assert m.acct.get_balance('KRW') == 20_000_000                              # 봇 둘 = 2,000만 원
    assert m.pct_of(m.get(a['id']), 'sim') == 0 and m.budget_of(m.get(a['id']), 'sim') == 10_000_000
    assert m.bot_config(b['id'], 'sim')['budget'] == 10_000_000
    assert m.coin_share('sim') and not m.coin_share('upbit')
    assert m.summary(a['id'], 'sim')['budget_now'] == 10_000_000


def test_bot_style_tag_saved_cleaned_and_copied(tmp_path):
    """성격 꼬리표: 공격형 · 균형형 · 안정형만 · 빈 값이면 지움 · 복사해도 따라감 · 요약에 보임"""
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'style': 'aggressive', 'trade_rules': RULES})
    assert a['style'] == 'aggressive' and m.summary(a['id'], 'sim')['style'] == 'aggressive'
    assert 'style' not in m.update(a['id'], {'style': 'weird'})[2]
    m.update(a['id'], {'style': 'defensive'})
    _, _, c = m.copy(a['id'])
    assert c['style'] == 'defensive'
    assert 'style' not in m.update(a['id'], {'style': ''})[2] and m.summary(a['id'], 'sim')['style'] == ''


def test_summary_counts_delisted_coin_as_zero(tmp_path, monkeypatch):
    """상장폐지(시세 목록이 다 있는데 그 코인만 없음) 코인은 0원 = 손실 (전에는 산 값으로 쳐 손실이 숨었음, 2026-10-15)"""
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 500_000, 'trade_rules': RULES})
    d = m.data_dir(a['id'], 'sim')
    d.mkdir(parents=True, exist_ok=True)
    (d / 'bot_positions.json').write_text('{"KRW-GONE": {"qty": 10, "entry_price": 1000}}', encoding='utf-8')
    full = {f'KRW-C{i}': 100.0 for i in range(60)}                     # 원화 마켓 시세는 다 있는데 GONE 만 없음
    monkeypatch.setattr(m, '_live_prices', lambda mode=None: full)
    s = m.summary(a['id'], 'sim')
    assert s['delisted'] == 1 and s['unrealized'] == -10_000
    monkeypatch.setattr(m, '_live_prices', lambda mode=None: {})       # 시세를 아직 못 받았으면 상장폐지로 보지 않음
    monkeypatch.setattr(m, '_prices', lambda mode=None: {})
    assert m.summary(a['id'], 'sim')['unrealized'] == 0


def test_pct_bot_rate_base_is_pre_pnl_budget(tmp_path):
    """비율(%) 봇의 수익률 기준 = (계좌 총자산 − 이 봇의 손익) × 비율 — 전에는 손익의 비율 몫만 뺀 한도라 벌면 크게 · 잃으면 작게 보였음 (2026-10-17)"""
    from bot_core.bot_engine import add_realized
    m, cfg, _ = make(tmp_path, mode='upbit')
    QuietEngine.fake = FakeExchange(krw=10_000_000)
    _, _, a = m.create({'name': '비율', 'budget': 0, 'budget_pct': 10, 'trade_rules': RULES,
                        'risk_config': {'position_size_percent': 50, 'max_positions': 2}})
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({'A': 0, 'B': 0}))
    tick(e, data({'A': 6, 'B': 0}))
    assert [o[1] for o in e.exchange.orders if o[0] == 'buy'] == ['KRW-A']
    e.exchange.krw += 500_000                                     # 이 봇이 전에 500,000원 벌어 둔 것처럼
    add_realized(m.data_dir(a['id'], 'upbit'), 500_000, [])
    s = m.summary(a['id'])
    assert s['realized'] == 500_000 and s['account_total']
    assert s['rate_base'] == pytest.approx((s['account_total'] - s['realized'] - s['unrealized']) * 0.10)
    assert s['total_rate'] == pytest.approx(s['profit'] / s['rate_base'] * 100)
    assert s['rate_base'] != pytest.approx(s['budget_now'] - s['realized'] - s['unrealized'])   # 옛 식이 아님
    QuietEngine.fake = None


# ── 2026-10-17 검토: 가상 계좌 체결값으로 손익 · 멈춘 봇 장부 · 계좌 모드 · 상장폐지 줄 ──
def test_sim_sell_records_actual_fill_and_pnl_matches_cash(tmp_path):
    """시뮬레이션 매도 기록은 가상 계좌가 실제로 판 값(시세 × 0.999)이고, 판 손익 = 계좌 원화 변화와 같다"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': RULES,
                        'risk_config': {'position_size_percent': 50, 'max_positions': 1, 'take_profit_percent': 5}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))                                                  # 100원에 삼
    tick(e, data({'X': 6}, price=110.0))                                     # +10% → 익절
    assert set(e.ledger) == set()
    t = m.trades(a['id'], 'sim')[-1]
    assert t['side'] == 'sell' and t['price'] == pytest.approx(110 * 0.999)
    krw_now = float({b['currency']: b['balance'] for b in m.acct.get_balances()}['KRW'])
    assert m.summary(a['id'], 'sim')['realized'] == pytest.approx(krw_now - 1_000_000, abs=1)


def test_stopped_bot_summary_uses_file_not_old_engine_ledger(tmp_path):
    """멈춘 봇의 성적은 (멈춘 동안 직접 판 코인을 맞춘) 파일 장부로 — 옛 엔진 장부가 아님"""
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': RULES,
                        'risk_config': {'position_size_percent': 20, 'max_positions': 5}})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    tick(e, data({'X': 0, 'Y': 0}))
    tick(e, data({'X': 6, 'Y': 6}))
    assert m.summary(a['id'], 'sim')['holding'] == 2
    m.stop(a['id'], 'sim')
    m.acct.sell_market_order('KRW-X', float({b['currency']: b['balance'] for b in m.acct.get_balances()}['X']))   # 직접 다 팖
    assert m.reconcile_stopped(a['id'], 'sim', now=1000.0) == 0 and m.reconcile_stopped(a['id'], 'sim', now=1010.0) == 1   # 5초 뒤 재확인 뒤에만 줄임
    s = m.summary(a['id'], 'sim')
    assert s['holding'] == 1 and 'KRW-X' not in m._taken_by_others('other', 'sim') and 'KRW-Y' in m._taken_by_others('other', 'sim')


def test_account_snapshot_takes_mode(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    assert m.account_snapshot(mode='sim')['total'] == pytest.approx(1_000_000)
    assert m.account_snapshot(mode='upbit') is None                          # 업비트 계좌가 없으면 가상 계좌 숫자를 주지 않음


def test_detail_marks_delisted_coin_as_zero(tmp_path):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': RULES})
    m.start(a['id'], 'sim')
    e = engine(m, a['id'], 'sim')
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))
    many = {f'KRW-C{i}': 1.0 for i in range(60)}                             # 시세 목록은 다 있는데 X 만 없음 = 상장폐지
    m._live_prices = lambda mode=None: many
    p = m.detail(a['id'])['status']['positions'][0]
    assert p['delisted'] and p['current_price'] == 0 and p['profit_rate'] == pytest.approx(-100)
    m.stop(a['id'], 'sim')
    p = m.detail(a['id'])['status']['positions'][0]
    assert p['delisted'] and p['current_price'] == 0 and p['profit_rate'] == pytest.approx(-100)


def test_risk_config_values_are_validated(tmp_path):
    """안전장치 값: 글자 · NaN 은 버리고(이전 값), 최대 보유 0 → 1, 코인당 3,000원 → 5,000원(봇이 그렇게 삼), 범위 밖은 깎음"""
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 100_000, 'trade_rules': RULES,
                        'risk_config': {'max_positions': 0, 'order_krw': 3000, 'stop_loss_percent': 'abc',
                                        'position_size_percent': float('nan'), 'take_profit_percent': -5, 'daily_loss_limit_percent': ''}})
    rc = m.risk_of(m.get(a['id']))
    assert rc['max_positions'] == 1 and rc['order_krw'] == 5000 and rc['stop_loss_percent'] == 5.0
    assert rc['position_size_percent'] == 10.0 and rc['take_profit_percent'] == 0 and rc['daily_loss_limit_percent'] == ''
    m.update(a['id'], {'risk_config': {'position_size_percent': 250, 'max_positions': 'x'}})
    rc = m.risk_of(m.get(a['id']))
    assert rc['position_size_percent'] == 100 and rc['max_positions'] == 1


def test_copy_keeps_dca_and_reset_drops_dust(tmp_path):
    m, cfg, _ = make(tmp_path)
    _, _, a = m.create({'name': 'A', 'budget': 500_000, 'trade_rules': RULES, 'dca': {'coin': 'BTC', 'every_days': 7, 'krw': 10000}})
    ok, _, b = m.copy(a['id'])
    assert ok and m.get(b['id']).get('dca') == m.get(a['id']).get('dca')         # 복사하면 나눠 사기 설정도 따라감
    m.start(a['id'])
    e = engine(m, a['id'])
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))                                                      # 100원에 삼
    PRICES['KRW-X'] = 1.0                                                        # 값이 1/100 → 5,000원 미만이라 팔 수 없음
    ok, msg = m.reset(a['id'])
    assert ok and 'X' not in {bb['currency'] for bb in m.acct.get_balances()}    # 가상 계좌에서 지움 (영영 남지 않게)
