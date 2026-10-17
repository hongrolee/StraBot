"""직접 검증하기의 1분봉 받기: 여러 번 나눠 받기 · 같은 시간대에 받아 둔 더 긴 기간 잘라 쓰기 (가짜 업비트, 요청 없음)"""
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests  # noqa: E402

from gui.web_dashboard import WebDashboard  # noqa: E402


class FakeResp:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def test_fetch_pages_and_reuse_longer_cache(tmp_path, monkeypatch):
    calls = []

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            to = datetime.strptime(params['to'], '%Y-%m-%dT%H:%M:%SZ')
            calls.append(to)
            return FakeResp([{'candle_date_time_utc': (to - timedelta(minutes=k + 1)).strftime('%Y-%m-%dT%H:%M:%S'),
                              'opening_price': 1, 'high_price': 1, 'low_price': 1, 'trade_price': 1,
                              'candle_acc_trade_price': 5} for k in range(200)])

    monkeypatch.setattr(requests, 'Session', FakeSession)
    monkeypatch.setattr('gui.web_dashboard.time.sleep', lambda s: None)
    # 시각 고정: 받는 사이에 분이 바뀌면 기간 끝이 1분 달라져 줄 수가 하나 어긋남 (가끔 실패하던 원인)
    fixed = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr('gui.web_dashboard.datetime', FixedDatetime)
    from utils.upbit_pace import UpbitPacer
    obj = types.SimpleNamespace(user_data_dir=tmp_path, _pacer=UpbitPacer(sleep=lambda s: None))
    progress = []
    bars = WebDashboard._get_minute_bars(obj, 'KRW-X', 7, on_progress=progress.append)
    assert len(bars) == 7 * 1440 and len(calls) == 7 * 1440 // 200 + 1 and progress[-1] == 1.0
    # 90일을 받아 두면 같은 시간대의 30일은 다시 받지 않고 잘라 씀
    long = WebDashboard._get_minute_bars(obj, 'KRW-X', 90)
    n = len(calls)
    short = WebDashboard._get_minute_bars(obj, 'KRW-X', 30)
    assert len(calls) == n and len(long) == 90 * 1440 and len(short) == 30 * 1440
    assert short[-1]['t'] == long[-1]['t']
