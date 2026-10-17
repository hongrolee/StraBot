"""파이썬 조건 계산(bot_core/conditions.py)이 화면(JS)과 같은 코인을 고르는지 확인

기준 파일은 `node tests/fixtures/make_condition_parity.js`로 만든다 (화면 코드를 그대로 실행한 결과).
"""
import json
import math
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from bot_core import conditions as cd

FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'condition_parity.json')

with open(FIXTURE, encoding='utf-8') as f:
    DATA = json.load(f)


def case_id(case):
    return f"{case['id']}:{json.dumps(case['params'], sort_keys=True)}"


@pytest.mark.parametrize('case', DATA['cases'], ids=case_id)
def test_same_hits_as_screen(case):
    customs = [case['custom']] if case.get('custom') else []
    cond = cd.resolve(case['id'], customs)
    assert cond is not None
    hits = cd.run_condition(cond, case['params'], DATA['coins'], DATA['lows'], DATA.get('hourly'), now=DATA.get('now'))
    assert sorted(h['currency'] for h in hits) == case['hits']
    # 정렬 기준 값도 같아야 함 (봇이 고르는 순서)
    key = case['sort_key']
    for h in hits:
        expected = case['values'].get(h['currency'])
        got = h.get(key)
        if expected is None:
            assert got is None
        else:
            assert got is not None and math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_sorted_like_screen():
    cond = cd.resolve('rsi_low', [])
    hits = cd.run_condition(cond, None, DATA['coins'], DATA['lows'])
    values = [h['s_rsi'] for h in hits]
    assert values == sorted(values)          # RSI 낮음은 낮은 순


def test_risky_and_stable_detection():
    assert cd.is_stable_coin({'currency': 'USDT', 'korean_name': '테더'})
    assert cd.is_stable_coin({'currency': 'XYZ', 'korean_name': '어떤 달러'})
    assert not cd.is_stable_coin({'currency': 'BTC', 'korean_name': '비트코인'})
    assert cd.is_risky_coin({'warning': True})
    assert cd.is_risky_coin({'notices': [{'type': 'delisting'}]})
    assert not cd.is_risky_coin({'notices': [{'type': 'info'}]})
