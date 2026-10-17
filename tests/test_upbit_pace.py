"""업비트 요청 속도 조절기 — 그룹마다 초당 8회, 앞쪽 줄 끼어들기, 남은 횟수 머리말, 429 쉬기 (2026-10-17)
실행: venv/Scripts/python.exe -m pytest tests/test_upbit_pace.py -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.upbit_pace import UpbitPacer, group_of  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def pacer(rate=7.0):
    c = Clock()
    return UpbitPacer(rate=rate, clock=c, sleep=c.sleep), c


def test_rate_is_capped_per_group():
    p, c = pacer()
    t0 = c.t
    for _ in range(15):                                  # 15번 → 14칸 = 2초 (초당 7회)
        p.wait('candles')
    assert c.t - t0 == pytest_approx(2.0)
    t1 = c.t
    p.wait('market')                                     # 다른 그룹은 따로 셈 (바로)
    assert c.t == t1


def test_priority_cuts_in_but_total_stays():
    p, c = pacer()
    for _ in range(7):                                   # 뒤쪽 줄이 1초 뒤까지 차 있음
        p.wait('candles')
    t = c.t
    waited = p.wait('candles', priority=True)            # 앞쪽 줄: 바로
    assert waited == 0 and c.t == t
    p.wait('candles')                                    # 뒤쪽 줄은 한 칸 더 밀림 (원래 1칸 뒤 → 2칸 뒤)
    assert c.t - t == pytest_approx(2 / 7)


def test_header_with_no_room_holds_until_next_second():
    p, c = pacer()
    p.wait('candles')
    p.note('candles', 200, {'Remaining-Req': 'group=candles; min=590; sec=2'})
    t = c.t
    p.wait('candles')
    assert c.t - t == pytest_approx(1.0)
    p.note('candles', 200, {'Remaining-Req': 'group=candles; sec=9'})   # min 이 없는 형식도 읽음 · 여유 있으면 안 쉼
    t = c.t
    p.wait('candles')
    assert c.t - t == pytest_approx(1 / 7)


def test_429_backs_off_whole_group_and_doubles():
    p, c = pacer()
    p.wait('candles')
    p.note('candles', 429, {})
    t = c.t
    p.wait('candles')
    assert c.t - t == pytest_approx(1.0)
    p.wait('candles')
    p.note('candles', 429, {})
    p.note('candles', 429, {})                           # 쉬는 중에 날아온 429 (같이 나가 있던 요청) 는 더 늘리지 않음
    t = c.t
    p.wait('candles')
    assert c.t - t == pytest_approx(2.0)                 # 연속이면 배로 (4초가 아님)
    p.note('candles', 200, {})
    p.note('candles', 429, {})
    t = c.t
    p.wait('candles')
    assert c.t - t == pytest_approx(1.0)                 # 성공이 끼면 처음부터
    assert p.snapshot()['throttled'] == 4
    t = c.t
    p.wait('ticker')                                     # 다른 그룹은 영향 없음
    assert c.t == t


def test_group_of_path():
    assert group_of('/v1/candles/minutes/60') == 'candles'
    assert group_of('/v1/market/all?isDetails=true') == 'market'
    assert group_of('https://api.upbit.com/v1/ticker') == 'ticker'


def pytest_approx(v):
    import pytest
    return pytest.approx(v, abs=1e-6)
