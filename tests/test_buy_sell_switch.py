"""봇마다 매수 · 매도 켜기/끄기: 꺼 두면 새로 사지 않음 / 손절만 함, 모드마다 따로"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_manager import RULES, data, engine, make, tick  # noqa: E402

RISK = {'position_size_percent': 10, 'max_positions': 5, 'stop_loss_percent': 5, 'take_profit_percent': 10}


def _bot(tmp_path, **rules):
    m, cfg, _ = make(tmp_path, krw=1_000_000)
    _, _, a = m.create({'name': 'A', 'budget': 300_000, 'trade_rules': {**RULES, **rules}, 'risk_config': RISK})
    m.start(a['id'], 'sim')
    return m, a, engine(m, a['id'], 'sim')


def _set(m, a, **rules):
    cur = dict(m.rules_of(m.get(a['id']), 'sim'))
    ok, msg, _ = m.update(a['id'], {'trade_rules': {**cur, **rules}})
    assert ok, msg


def test_buy_off_skips_new_entries_and_does_not_catch_up(tmp_path):
    m, a, e = _bot(tmp_path, buy_on=False)
    tick(e, data({'X': 0, 'Y': 0}))
    tick(e, data({'X': 6, 'Y': 0}))                       # X 가 새로 들어왔지만 매수 꺼짐
    assert e.ledger == {}
    assert any('매수를 꺼 두어' in d['message'] for d in e.decisions)
    _set(m, a, buy_on=True)
    tick(e, data({'X': 6, 'Y': 0}))                       # 다시 켜도 꺼 둔 사이 들어온 X 는 몰아서 사지 않음
    assert e.ledger == {}
    tick(e, data({'X': 6, 'Y': 6}))                       # 켠 뒤 새로 들어온 Y 는 삼
    assert set(e.ledger) == {'KRW-Y'}


def test_sell_off_keeps_stop_loss_only(tmp_path):
    m, a, e = _bot(tmp_path)
    tick(e, data({'X': 0}))
    tick(e, data({'X': 6}))
    assert 'KRW-X' in e.ledger
    entry = e.ledger['KRW-X']['entry_price']
    _set(m, a, sell_on=False)
    tick(e, data({'X': 6}, price={'X': entry * 1.2}))     # 익절 값이지만 매도 꺼짐 → 안 팖
    assert 'KRW-X' in e.ledger
    tick(e, data({'X': 6}, price={'X': entry * 0.9}))     # 손절 값 → 팖
    assert 'KRW-X' not in e.ledger


def test_switches_are_per_mode(tmp_path):
    m, a, e = _bot(tmp_path)
    _set(m, a, buy_on=False)
    assert m.summary(a['id'], 'sim')['buy_on'] is False
    assert m.summary(a['id'], 'upbit')['buy_on'] is True
