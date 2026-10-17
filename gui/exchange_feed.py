"""
다른 거래소(빗썸) 시세 묶음 (2026-10-12)

WebDashboard 의 시세 함수(코인 목록 · 현재가 · 캔들 · 유의 종목 · 기간 시세 · 시간봉 · 시장 데이터)를 그대로 빌려 쓰고,
캐시 · 요청 간격 · 저장 파일만 거래소마다 따로 둠. 빗썸 새 API(v1)가 업비트와 같은 모양이라 계산 코드는 하나.
업비트 모드 · 시뮬레이션은 지금처럼 WebDashboard 자체가 업비트 묶음 (dashboard.feed('upbit') is dashboard).

2단계까지 없는 것: 실시간 체결(1분 급등 · 매수세 조건은 '준비 중'), 거래소 공지 배지
"""

import json
import threading
from typing import Dict

from bot_core.exchanges import EXCHANGES


class _NoFlow:
    """실시간 체결 연결이 아직 없는 거래소: 늘 '준비 중'"""

    def snapshot(self):
        return {'coins': {}, 'ready': False}

    def spike_snapshot(self):
        return {'coins': {}, 'ready': False}


class ExchangeFeed:
    def __init__(self, owner, ex: str):
        from gui.web_dashboard import WebDashboard
        info = EXCHANGES[ex]
        self.owner = owner
        self.EX = ex
        self.API_BASE = info['api']
        self.DAY_START_KST = info['day_start_kst']
        self.user_data_dir = owner.user_data_dir
        self.config = owner.config
        self.coin_facts = owner.coin_facts          # 상장 일수 · 바이낸스 상장 (업비트 기준 — 빗썸에만 있는 코인은 모름)
        self.trade_flow = _NoFlow()
        self._price_cache: Dict[str, tuple] = {}
        self._period_low_path = self.user_data_dir / 'data' / f'period_lows_cache_{ex}.json'
        try:
            saved = json.loads(self._period_low_path.read_text(encoding='utf-8'))
            self._period_low_cache: Dict[str, tuple] = {m: tuple(v) for m, v in saved.items()}
        except Exception:
            self._period_low_cache = {}
        self._period_low_lock = threading.Lock()
        self._period_low_inflight: Dict[str, threading.Event] = {}
        from utils.upbit_pace import UpbitPacer
        self._pacer = UpbitPacer()                   # 이 거래소 요청 속도 조절기 (거래소마다 한도가 따로, 2026-10-17)
        self._krw_markets_lock = threading.Lock()
        self._chart_candle_cache = {}
        self._hourly_cache: Dict[str, tuple] = {}
        self._tf_cache = {tf: {} for tf in WebDashboard.TF_UNIT}
        self._tf_locks = {tf: threading.Lock() for tf in WebDashboard.TF_UNIT}

    @property
    def bots(self):
        return self.owner.bots

    def _get_upbit_notices(self) -> Dict:
        """거래소 공지는 2단계 — 지금은 배지 없이"""
        return {'by_symbol': {}, 'global': []}


def _borrow():
    """WebDashboard 시세 함수를 ExchangeFeed 에도 (같은 계산, 상태만 따로)"""
    from gui.web_dashboard import WebDashboard
    for name in ('_get_krw_markets', '_krw_tickers', '_get_candles', '_get_market_events', '_fetch_period_lows',
                 '_fetch_hourly', '_fetch_tf_bars', '_build_market_coins', 'get_market_data', '_preview_market',
                 '_tfs_in_use', '_period_day_key'):
        setattr(ExchangeFeed, name, WebDashboard.__dict__[name])
    for name in ('_long_fields', '_apply_spike', '_save_json'):
        setattr(ExchangeFeed, name, WebDashboard.__dict__[name])          # staticmethod 그대로
    for name in ('TF_UNIT', 'CAUTION_REASONS'):
        setattr(ExchangeFeed, name, getattr(WebDashboard, name))


_borrowed = False


def make_feed(owner, ex: str) -> ExchangeFeed:
    global _borrowed
    if not _borrowed:
        _borrow()
        _borrowed = True
    return ExchangeFeed(owner, ex)
