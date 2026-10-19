"""빗썸 모드: 실시간 체결이 없으니 급등 세트 · 실시간 조건은 '안 됨'으로 알리고, 거짓 감지를 남기지 않음 (2026-10-19)
실행: venv/Scripts/python.exe -m pytest tests/test_bithumb_mode.py -q"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot_core.conditions as cd  # noqa: E402
from bot_core.bot_engine import BotEngine  # noqa: E402
from test_surge_rebuy import T0, hourly, no_sleep, sim_engine, spike_rule, surge_rule  # noqa: E402,F401


def test_bithumb_surge_set_not_registered_and_explained_once(tmp_path):
    e = sim_engine(tmp_path, sell=[surge_rule(), spike_rule()])       # 하루 급등 · 순간 급등 세트를 켠 봇
    e.account_mode = 'bithumb'
    e.last_prices = {'KRW-A': 125.0}
    assert e.surge_detect({'hourly': {'KRW-A': hourly(T0, 100.0)}}, now=T0) == []   # 감지했다고 남기지 않음
    assert dict(e.surge.state) == {}
    e.spike_tick()                                                    # 실시간 체결 없음 → 안내 한 번 (하루에 한 번)
    e.spike_tick()
    msgs = [d['message'] for d in e.decisions if '빗썸 모드에서 동작하지 않습니다' in d['message']]
    assert len(msgs) == 1


def test_upbit_and_sim_surge_still_detects(tmp_path):
    e = sim_engine(tmp_path)                                          # 시뮬레이션(업비트 시세)은 그대로 감지
    e.last_prices = {'KRW-A': 125.0}
    assert e.surge_detect({'hourly': {'KRW-A': hourly(T0, 100.0)}}, now=T0)


def test_flow_condition_note_depends_on_exchange(tmp_path):
    e = sim_engine(tmp_path)
    acts = []
    e.on_activity_callback = lambda msg, lv, ic: acts.append(msg)
    cond = cd.resolve('buy_flow_spike', [])
    e._flow_wait_note(cond)                                           # 업비트 · 시뮬레이션: 모으는 중
    assert any('모으는 중' in a for a in acts)
    e.account_mode = 'bithumb'
    acts.clear()
    e._flow_wait_note(cond)
    e._flow_wait_note(cond)
    assert sum('빗썸 모드에는 실시간 체결 정보가 없어' in a for a in acts) == 1 and not any('모으는 중' in a for a in acts)
    assert inspect.getsource(BotEngine).count('self._flow_wait_note(cond)') == 2      # 사기 두 군데 모두 이 안내를 씀
