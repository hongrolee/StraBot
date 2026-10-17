"""'가격이 평균선 상향 · 하향 돌파'를 50일선 · 100일선으로도"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_core.conditions as cd  # noqa: E402


def coin(closes, price):
    return {'currency': 'X', 'current_price': price}, {'day_closes': closes[-30:], 'day_closes_long': closes[-120:], 'day_closes_xl': closes[-199:]}


def test_cross_up_50_day_line():
    closes = [100.0] * 150 + [90.0] * 49                    # 50일 평균은 100보다 낮고, 마지막 날들은 그 아래
    c, d = coin(closes, 140.0)                                # 오늘 크게 올라 50일선 위로
    up = cd.PRESETS['ma_cross_up']['test']
    assert up(c, {'days': 50, 'within': 3}, d) is not None
    assert up(c, {'days': 50, 'within': 3}, {'day_closes': d['day_closes']}) is None   # 긴 칸이 없으면 판단하지 않음


def test_cross_down_100_day_line_and_short_still_uses_30():
    closes = [100.0] * 100 + [110.0] * 99
    c, d = coin(closes, 80.0)
    down = cd.PRESETS['ma_cross_down']['test']
    assert down(c, {'days': 100, 'within': 3}, d) is not None
    c2, d2 = coin([100.0] * 29 + [90.0], 120.0)
    assert cd.PRESETS['ma_cross_up']['test'](c2, {'days': 20, 'within': 3}, {'day_closes': d2['day_closes']}) is not None
