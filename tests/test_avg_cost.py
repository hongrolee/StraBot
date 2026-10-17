"""봇 매매 내역의 매수평균가: 산 뒤 평균(수수료 포함), 일부 팔아도 평균은 그대로 — EDGE 예 (145원에 사고 일부 판 뒤 180원에 다시 사면 약 156.6원)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_core.bot_manager import BotManager  # noqa: E402


def test_avg_cost_after_partial_sell_and_rebuy():
    t = [{'symbol': 'E', 'side': 'buy', 'qty': 200, 'total': 30000},       # 평균 150
         {'symbol': 'E', 'side': 'sell', 'qty': 100, 'total': 19000},      # 평균 그대로, 100개 남음
         {'symbol': 'E', 'side': 'buy', 'qty': 100, 'total': 18000},       # (15000 + 18000) / 200 = 165
         {'symbol': 'X', 'side': 'buy', 'qty': 10, 'total': 1000}]
    out = BotManager.with_avg_cost(t)
    assert out[0]['avg_cost'] == 150 and 'avg_cost' not in out[1]
    assert abs(out[2]['avg_cost'] - 165) < 1e-9 and out[3]['avg_cost'] == 100
    assert 'avg_cost' not in t[0]                                          # 원본은 그대로


def test_avg_cost_resets_after_full_sell():
    t = [{'symbol': 'E', 'side': 'buy', 'qty': 10, 'total': 1000},
         {'symbol': 'E', 'side': 'sell', 'qty': 10, 'total': 1200},
         {'symbol': 'E', 'side': 'buy', 'qty': 10, 'total': 2000}]
    assert BotManager.with_avg_cost(t)[2]['avg_cost'] == 200


def test_bot_total_is_the_saved_snapshot():
    """봇 총 자산은 거래 순간에 저장한 값 그대로 (나중에 계좌가 바뀌어도 그대로), 저장 안 된 예전 거래는 비움"""
    trades = [{'side': 'buy', 'snap': {'total': 1000, 'coins': 300, 'cash': 700, 'rate': -0.5}}, {'side': 'sell'}]
    BotManager._attach_bot_pnl(trades)
    assert trades[0]['bot_total'] == 1000 and trades[0]['bot_coins'] == 300 and trades[0]['bot_pnl_rate'] == -0.5
    assert 'bot_total' not in trades[1]
