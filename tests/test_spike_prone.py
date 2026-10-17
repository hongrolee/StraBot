"""'급등이 잦던 코인' · '급등 직전 모습' 조건, 체결 요약의 15분 값, 코인 정보 캐시, 가져오기 템플릿"""
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot_core import conditions as cd  # noqa: E402
from bot_core import rule_templates  # noqa: E402
from gui.trade_flow import TradeFlowMonitor  # noqa: E402
from utils.coin_facts import CoinFacts  # noqa: E402

PRONE = cd.PRESETS['spike_prone']
PRE = cd.PRESETS['pre_spike']


def prone(**c):
    base = {'trade_value_24h': 50e8, 'current_price': 500, 'listed_days': 400, 'on_binance': True}
    base.update(c)
    return cd._t_spike_prone(base, PRONE['params'], {})


def test_spike_prone_needs_small_value_and_one_trait():
    assert prone() is None                                        # 특성 없음
    assert prone(listed_days=30)['s_prone_why'] == '신규 상장'
    assert prone(current_price=9.9)['s_prone_why'] == '저가'
    assert prone(on_binance=False)['s_prone_why'] == '바이낸스 없음'
    assert prone(listed_days=30, trade_value_24h=101e8) is None   # 큰 코인은 빼기
    assert prone(listed_days=89) and not prone(listed_days=90)    # 경계: 90일 미만
    assert prone(current_price=10) is None                        # 경계: 10원 미만
    # 모르는 값은 맞지 않은 것으로
    assert prone(listed_days=None, on_binance=None) is None
    assert prone(listed_days=None, on_binance=None, current_price=3)['s_prone_why'] == '저가'


def pre(**c):
    base = {'spike_value15': 0, 'spike_close_last': 103, 'spike_close_1h': 100, 'trade_value_24h': 1440e6}
    base.update(c)
    return cd._t_pre_spike(base, PRE['params'], {})


def test_pre_spike_rise_and_volume():
    # 평소 = 24시간 1440백만 원 / 1440분 = 분당 100만 원. 15분 평균이 5배(분당 500만) 이상이어야
    assert pre(spike_value15=15 * 5e6)['s_pre_mult'] == 5
    assert pre(spike_value15=15 * 4.9e6) is None
    assert pre(spike_value15=15 * 6e6, spike_close_last=102.9) is None   # 1시간 +3% 미만
    assert pre(spike_value15=None) is None and pre(spike_close_1h=None, spike_value15=1e9) is None
    assert PRE.get('needs_flow') and PRE.get('ready_key') == 'spike_ready'


def test_snapshot_has_15min_value_without_changing_spike_fields():
    m = TradeFlowMonitor(lambda: [])
    now = int(time.time() // 60)
    last = now - 1
    m._started_minute = last - 70
    # A: 마지막 1분에 체결 있음 · B: 10분 전에만 체결 (마지막 1분엔 없음)
    m._buckets['KRW-A'] = {k: [1e6, 1e6, 100, 101, 99, 100] for k in range(last - 65, last + 1)}
    m._buckets['KRW-B'] = {last - 10: [3e6, 0, 50, 51, 49, 51], last - 61: [1e6, 0, 48, 48, 48, 48]}
    snap = m.spike_snapshot()
    a, b = snap['coins']['KRW-A'], snap['coins']['KRW-B']
    assert a['value'] == 2e6 and a['open'] == 100 and a['value15'] == 15 * 2e6 and a['close_last'] == 100
    assert b['value'] == 0 and b['open'] is None and b['close'] is None      # 급등 판정에는 안 걸림
    assert b['value15'] == 3e6 and b['close_last'] == 51 and b['close_1h'] == 48


class FakeResp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, (params or {}).get('market')))
        if 'binance' in url:
            return FakeResp([{'symbol': 'BTCUSDT'}, {'symbol': 'ETHBTC'}, {'symbol': 'AAAUSDT'}])
        if params['market'] == 'KRW-NEW':
            return FakeResp([{'candle_date_time_kst': '2026-09-20T09:00:00'}, {'candle_date_time_kst': '2026-09-01T09:00:00'}])
        return FakeResp([{'candle_date_time_kst': '2020-01-01T09:00:00'}] * 200)


def test_coin_facts_cache(tmp_path, monkeypatch):
    import utils.coin_facts as cf
    monkeypatch.setattr(cf, 'REQ_GAP', 0)
    f = CoinFacts(tmp_path / 'facts.json', lambda: ['KRW-BTC', 'KRW-NEW'])
    assert f.facts('BTC') == {'listed_days': None, 'on_binance': None}           # 아직 모름
    s = FakeSession()
    f.refresh(s)
    today = date(2026, 10, 2)
    assert f.facts('NEW', today) == {'listed_days': 31, 'on_binance': False}
    assert f.facts('BTC', today) == {'listed_days': 200, 'on_binance': True}     # 200일 넘음
    assert f.facts('AAA', today)['on_binance'] is True
    # 다시 켜도 기억 · 상장일은 다시 묻지 않음
    g = CoinFacts(tmp_path / 'facts.json', lambda: ['KRW-BTC', 'KRW-NEW'])
    s2 = FakeSession()
    g.refresh(s2)
    assert g.facts('NEW', today)['listed_days'] == 31 and not s2.calls             # 바이낸스도 하루 안엔 안 물음


def test_template_is_a_valid_bot_spec():
    spec = rule_templates.bot_spec('spike-prone-v1')
    assert spec['imported_from'] == 'spike-prone-v1'
    rules = spec['trade_rules']
    assert [r['id'] for r in rules['buy']] == ['spike_prone', 'pre_spike'] and rules['buy_match'] == 'all'
    assert rules['sell'][0]['id'] == 'spike_pump' and rules['sell'][0]['sell_mode'] == 'partial'
    for r in rules['buy'] + rules['sell']:
        assert r['id'] in cd.PRESETS
        assert set(r['params']) <= set(cd.PRESETS[r['id']]['params'])
    assert rule_templates.bot_spec('anything-else') is None and rule_templates.get(None) is None
    # 꺼낸 것을 고쳐도 원본은 그대로
    spec['trade_rules']['buy'].clear()
    assert rule_templates.bot_spec('spike-prone-v1')['trade_rules']['buy']
