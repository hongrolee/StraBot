"""마켓 목록 · 전체 시세: 업비트가 429 로 거절해도 지난 값으로 계속 (봇 확인이 통째로 실패하지 않게, 2026-10-17)
실행: venv/Scripts/python.exe -m pytest tests/test_market_list_fallback.py -q"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from gui.web_dashboard import WebDashboard  # noqa: E402


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body or [], {'Remaining-Req': 'group=market; min=599; sec=9'}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code}', response=self)


def _wd(tmp_path):
    wd = WebDashboard(str(tmp_path / 'config' / 'bot_config.json'), port=0, user_data_dir=tmp_path)
    wd._save_config_internal = lambda *a, **k: None
    wd._pacer.sleep = lambda s: None
    return wd


def test_market_list_uses_last_good_on_429_and_fetches_once(tmp_path, monkeypatch):
    wd = _wd(tmp_path)
    calls = []
    body = [{'market': 'KRW-BTC', 'korean_name': '비트코인'}, {'market': 'BTC-ETH', 'korean_name': 'x'}]

    def fake_get(url, params=None, timeout=None, **k):
        calls.append(url)
        time.sleep(0.05)
        return Resp(200, body) if len(calls) == 1 else Resp(429)
    monkeypatch.setattr(requests, 'get', fake_get)
    # 13개 스레드가 한꺼번에 불러도 요청은 한 번
    out = [None] * 13
    th = [threading.Thread(target=lambda i=i: out.__setitem__(i, wd._get_krw_markets())) for i in range(13)]
    [t.start() for t in th]; [t.join() for t in th]
    assert len(calls) == 1 and all(o == [{'market': 'KRW-BTC', 'korean_name': '비트코인'}] for o in out)
    # 캐시가 끝난 뒤 429 → 지난 목록을 그대로, 예외 없음
    wd._krw_markets_cache = (time.time() - 601, wd._krw_markets_cache[1])
    assert wd._get_krw_markets() == [{'market': 'KRW-BTC', 'korean_name': '비트코인'}]
    assert len(calls) == 2
    assert wd._get_krw_markets() == [{'market': 'KRW-BTC', 'korean_name': '비트코인'}] and len(calls) == 2   # 1분은 다시 묻지 않음


def test_tickers_fall_back_to_recent_cache_on_429(tmp_path, monkeypatch):
    wd = _wd(tmp_path)
    wd._krw_markets_cache = (time.time(), [{'market': 'KRW-BTC', 'korean_name': '비트코인'}])
    n = [0]

    class S:
        def get(self, url, params=None, timeout=None):
            n[0] += 1
            return Resp(200, [{'market': 'KRW-BTC', 'trade_price': 100.0}]) if n[0] == 1 else Resp(429)
    wd._ticker_session = S()
    assert wd._krw_tickers()['KRW-BTC']['trade_price'] == 100.0
    assert wd._krw_tickers(max_age=0)['KRW-BTC']['trade_price'] == 100.0     # 429 → 조금 전 시세
    assert n[0] == 2
