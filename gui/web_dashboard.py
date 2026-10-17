"""
웹 기반 대시보드 서버
- Flask 서버로 웹 대시보드 제공
- 실시간 데이터 업데이트 (WebSocket)
- 현대적인 웹 UI
"""

import logging
import threading
import os
import sys
import importlib.util
from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO, emit
from typing import Dict, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

# 일꾼 스레드 묶음은 이름별로 한 번 만들어 계속 씀. 부를 때마다 새로 만들면 스레드를 계속 새로 띄우는데,
# 2026-10-14 23:20 새 스레드 하나가 끝내 시작되지 않아 파이썬 전체 잠금(concurrent.futures 의 _global_shutdown_lock)이
# 잡힌 채 서버가 멈춘 일이 있었음 — 거래 내역 · 차트 · 봇 시세 확인까지 모두 멈춤. 계속 쓰면 스레드는 처음 몇 번만 생김
_POOLS: Dict[str, ThreadPoolExecutor] = {}
_POOLS_LOCK = __import__('threading').Lock()


def _shared_pool(name: str, workers: int) -> ThreadPoolExecutor:
    with _POOLS_LOCK:
        pool = _POOLS.get(name)
        if pool is None:
            pool = _POOLS[name] = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f'pool-{name}')
        return pool
import json
import math
from decimal import Decimal, ROUND_DOWN
import shutil
from pathlib import Path
import webbrowser
import time
import uuid
import re
import ast
from datetime import datetime, timedelta, timezone
from bot_core.exchanges import EXCHANGES, LIVE_MODES, api_keys, exchange_name, exchange_of, fee_rate, is_live

from gui import __version__
from gui.pnl_history import PnlHistory, PnlRecorder

logger = logging.getLogger(__name__)


class HoldingsUnavailable(Exception):
    """보유 자산을 조회할 수 없음 (API 키 없음 등) - 화면에 그대로 보여줄 메시지"""

# PyInstaller 환경에서 async_mode 자동 감지 문제 해결
# 환경 변수로 설정하면 더 안정적
if getattr(sys, 'frozen', False):
    os.environ['EVENTLET_NO_GREENDNS'] = 'yes'


class WebDashboard:
    """웹 기반 대시보드"""

    def __init__(self, config_path: str = "config/bot_config.json", port: int = 5001, user_data_dir=None):
        """
        Args:
            config_path: 설정 파일 경로
            port: 웹 서버 포트
            user_data_dir: 사용자 데이터 폴더를 따로 지정 (테스트용: 실제 설정·기록 파일을 건드리지 않게). 보통은 비워 둠
        """
        self.port = port

        # 현재 파일의 디렉토리 기준으로 경로 설정
        import sys
        if getattr(sys, 'frozen', False):
            # PyInstaller로 패키징된 경우
            # --onefile 모드에서는 --add-data로 추가한 파일들이 sys._MEIPASS에 있음
            self.bundle_dir = Path(sys._MEIPASS)  # 내부 리소스 (템플릿, 정적파일)
            self.base_dir = self.bundle_dir
            # 실행 파일 디렉토리 (읽기 전용)
            self.exe_dir = Path(sys.executable).parent
            # 사용자 데이터 디렉토리 (설정, 전략, 로그는 여기에 저장)
            self.user_data_dir = Path(os.getenv('LOCALAPPDATA', os.path.expanduser('~'))) / 'StraBot'
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            # 설정 파일 경로는 사용자 데이터 디렉토리에
            self.config_path = self.user_data_dir / Path(config_path).name if not Path(config_path).is_absolute() else Path(config_path)
        else:
            # 소스 코드로 실행되는 경우 (gui/web_dashboard.py -> parent -> parent = root)
            self.base_dir = Path(__file__).parent.parent
            self.bundle_dir = self.base_dir
            self.exe_dir = self.base_dir
            self.user_data_dir = self.base_dir
            self.config_path = Path(config_path) if Path(config_path).is_absolute() else self.base_dir / Path(config_path)
        if user_data_dir is not None:
            self.user_data_dir = Path(user_data_dir)
            self.user_data_dir.mkdir(parents=True, exist_ok=True)

        # 암호화 유틸리티 초기화 (설정 로드 전에 초기화 필요)
        from utils.config_encryption import ConfigEncryption
        self.encryption = ConfigEncryption(user_data_dir=self.user_data_dir)

        self.config = self._load_config()

        # Flask 앱 생성
        self.app = Flask(__name__, 
                        template_folder=str(self.base_dir / 'templates'),
                        static_folder=str(self.base_dir / 'static'))
        # SECRET_KEY는 암호화 유틸리티에서 생성된 것 사용
        self.app.config['SECRET_KEY'] = self.encryption.secret_key
        # 템플릿 수정 시 재시작 없이 새로고침만으로 반영 (기본값은 처음 읽은 것을 계속 사용)
        self.app.config['TEMPLATES_AUTO_RELOAD'] = True
        # PyInstaller 환경에서 async_mode 자동 감지 문제 해결
        # eventlet/gevent가 패키징에 포함되면 auto-detect에서 충돌 발생
        # threading 모드 강제 사용을 위해 eventlet/gevent 모듈 차단
        if getattr(sys, 'frozen', False):
            # PyInstaller 환경에서 eventlet이 잘못 감지되는 것 방지
            import importlib
            for mod_name in ['eventlet', 'gevent']:
                if mod_name in sys.modules:
                    del sys.modules[mod_name]
            # eventlet.green 모듈도 제거
            to_remove = [k for k in sys.modules if k.startswith('eventlet.') or k.startswith('gevent.')]
            for k in to_remove:
                del sys.modules[k]

        self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode='threading', logger=False, engineio_logger=False)

        # 봇 엔진 참조
        self.bot_engine = None

        # 콜백 함수
        self.on_save: Optional[Callable] = None
        self.on_start: Optional[Callable] = None
        self.on_stop: Optional[Callable] = None
        self.on_restart: Optional[Callable] = None

        # 상태
        self.bot_status = "중지됨"
        self.is_running = False
        self.server_thread = None
        # 저점수익률 탭용 기간 최저가 캐시: {market: (UTC 날짜/버전, {'d1','w1','m1','h6','y1'})}
        # 하루 동안 바뀌지 않으므로 파일에도 보관 → 봇을 다시 켜도 처음부터 받지 않음 (날짜가 바뀌면 새로 받음)
        self._period_low_path = self.user_data_dir / 'data' / 'period_lows_cache.json'
        # 조건 · 패턴 신뢰도 기록 (신호 뒤 1·3·7일 결과, 하루 한 번 갱신) — bot_core/signal_stats.py
        from bot_core.signal_stats import SignalStats
        self.signal_stats = SignalStats(self.user_data_dir / 'data' / 'signal_log.db')
        self._signal_summary = None          # (계산한 시각, 요약) — 갱신 때 새로
        try:
            saved = json.loads(self._period_low_path.read_text(encoding='utf-8'))
            self._period_low_cache: Dict[str, tuple] = {m: tuple(v) for m, v in saved.items()}
        except Exception:
            self._period_low_cache = {}
        self._period_low_lock = threading.Lock()   # 파일 저장·동시 조회 정리용
        self._period_low_inflight: Dict[str, threading.Event] = {}  # 지금 받고 있는 코인 (중복 요청 방지)
        # 업비트 시세 API 요청 속도 조절기: 캔들 · 마켓 목록 · 시세가 모두 여기를 지남 (그룹마다 초당 8회, 머리말 · 429 반영, 2026-10-17)
        from utils.upbit_pace import UpbitPacer
        self._pacer = UpbitPacer()
        self._chart_candle_cache = {}                 # 봇 현황 보유 코인 캔들 (15초)
        self._krw_markets_lock = threading.Lock()     # 마켓 목록은 한 번에 하나만 받고 나머지는 그 결과를 씀
        # '순간 급등' 직접 검증하기: 작업 번호 → 진행 상황·결과 (한 번에 하나만)
        self._verify_jobs: Dict[str, Dict] = {}
        self._verify_lock = threading.Lock()
        # 봇 백테스트: 작업 상태 · 캔들 저장소(data/candles.db, 처음 쓸 때 만듦)
        self._bt_jobs: Dict[str, Dict] = {}
        self._bt_lock = threading.Lock()
        self._cmp_jobs: Dict[str, Dict] = {}         # 보유코인 분석 '다른 코인 비교' 작업 (코인 고르기 값 · 봇 적용 비교)
        self._candle_store = None
        # 거래 기록 저장소 (모드마다 SQLite 한 파일): 체결 · 봇 거래 전부 · 업비트 주문별 체결 — bot_core/trade_store.py
        from bot_core.trade_store import TradeStore
        self.trade_stores = {'sim': TradeStore(self.user_data_dir / 'data' / 'trades_sim.db'),
                             'upbit': TradeStore(self.user_data_dir / 'data' / 'trades_upbit.db'),
                             'bithumb': TradeStore(self.user_data_dir / 'data' / 'trades_bithumb.db')}
        self._feeds = {}                              # 거래소별 시세 묶음 (업비트 = 이 객체 자신, 빗썸 = gui/exchange_feed.py)
        self._bithumb_sync_at = 0.0
        self._bithumb_sync_lock = threading.Lock()
        self._upbit_sync_at = 0.0                     # 업비트 최근 7일을 마지막으로 받은 때 (쪽 넘김마다 다시 묻지 않게)
        self._upbit_sync_lock = threading.Lock()

        # 상단 연결 램프용 업비트 연결 상태 캐시 (받은 시각, 결과)
        self._upbit_status_cache = None
        # 1시간봉 캐시 (전일저점·24시간/오늘 추이): {market: (받은 시각, data)}
        self._hourly_cache: Dict[str, tuple] = {}
        self._tf_cache: Dict[str, Dict[str, tuple]] = {tf: {} for tf in self.TF_UNIT}     # 조건용 분봉 · 시간봉 · 주봉 · 월봉
        self._tf_locks = {tf: threading.Lock() for tf in self.TF_UNIT}
        # 보유 현황 매도 버튼: 같은 코인 중복 주문 방지
        self._sell_lock = threading.Lock()
        self._selling: set = set()
        # 일괄 주문은 동시에 하나만
        self._batch_lock = threading.Lock()
        # 수익률 그래프 탭용 1분 기록
        self.pnl_history = PnlHistory(self.user_data_dir / 'data' / 'pnl_history.db')
        self.pnl_recorder = PnlRecorder(self.pnl_history, lambda: self._fetch_holdings('upbit'))
        # 시뮬레이션 모드: 업비트 계좌와 같은 모양의 가상 계좌 + 수익률 기록도 따로
        from bot_core.virtual_account import VirtualAccount
        self._price_cache: Dict[str, tuple] = {}          # 가상 계좌 체결 가격 {market: (시각, 가격)}
        from bot_core.trade_store import sim_order_row
        self.sim_account = VirtualAccount(self.user_data_dir / 'data' / 'sim_account.json', self._sim_price,
                                          initial_krw=float(self.config.get('sim_initial_krw') or 10_000_000),
                                          on_order=lambda o: self.trade_stores['sim'].add_fills([sim_order_row(o)]))
        # 차트에서 걸어 두는 주문: 감시 주문(모드마다) + 시뮬레이션 지정가 확인 (bot_core/chart_orders.py)
        from bot_core.chart_orders import ChartOrderStore, ChartOrderWatcher
        self.chart_orders = {m: ChartOrderStore(self.user_data_dir / 'data' / f'chart_orders_{m}.json') for m in ('upbit', 'bithumb', 'sim')}
        self.chart_order_watcher = ChartOrderWatcher(
            self.chart_orders, self.sim_account, lambda m, mode='sim': self._mode_price(m, mode), lambda m: self._account(m),
            lambda mode='upbit': self.bots.live_blocked(mode) or (is_live(mode) and self.license.feature_block('live_chart_orders')) or None, notify=lambda msg, level: self.emit_activity(msg, 'warning' if level == 'error' else level),
            beat_path=self.user_data_dir / 'data' / 'chart_orders_beat.txt')
        self.pnl_history_sim = PnlHistory(self.user_data_dir / 'data' / 'pnl_history_sim.db')
        self.pnl_recorder_sim = PnlRecorder(self.pnl_history_sim, lambda: self._fetch_holdings('sim'))
        self.pnl_history_bithumb = PnlHistory(self.user_data_dir / 'data' / 'pnl_history_bithumb.db')
        self.pnl_recorder_bithumb = PnlRecorder(self.pnl_history_bithumb, lambda: self._fetch_holdings('bithumb'))
        # 전체코인 분석 '시장 현황': 원화 마켓 상승 · 하락 수 · 평균 등락률을 1분마다 (gui/market_breadth.py)
        from gui.market_breadth import MarketBreadth
        self.market_breadth = MarketBreadth(self.user_data_dir / 'data' / 'market_breadth.db')
        # 실시간 체결 수집 (1분 매수 거래대금 급증 감시): 업비트 WebSocket 연결 하나
        from gui.trade_flow import TradeFlowMonitor
        self.trade_flow = TradeFlowMonitor(lambda: [m['market'] for m in self._get_krw_markets()])
        # 상장 일수 · 바이낸스 상장 여부 ('급등이 잦던 코인' 조건용, 하루 한 번 · 상장일은 한 번만 물어봄)
        from utils.coin_facts import CoinFacts
        self.coin_facts = CoinFacts(self.user_data_dir / 'data' / 'coin_facts.json',
                                    lambda: [m['market'] for m in self._get_krw_markets()])
        self.coin_facts.pace = lambda: self._pacer.wait('candles')     # 상장일 조회도 같은 줄로 (전에는 따로 초당 6.7회, 2026-10-17)
        # 계정 권한표: 업비트(실제 주문) 모드의 봇 시작·새로 사기에만 필요 (시뮬레이션·팔기는 계정 없이) — bot_core/license.py
        from bot_core.license import LicenseManager
        self.license = LicenseManager(self.user_data_dir / 'data', self.encryption)
        # 여러 봇: 봇마다 규칙·안전장치·모드·예산, 시장 데이터·실시간 체결은 같이 씀 (bot_core/bot_manager.py)
        from bot_core.bot_manager import BotManager
        self.bots = BotManager(self.config, lambda c: self._save_config_internal(c), self.get_market_data,
                               lambda: self.trade_flow.spike_snapshot(), data_root=self.user_data_dir / 'data' / 'bots',
                               legacy_dir=self.user_data_dir / 'data',
                               on_activity=lambda m, l='info', i=None: self.emit_activity(m, l, i),
                               get_mode=self.app_mode, get_account=lambda: self.sim_account, get_mode_account=lambda m: self._account(m),
                               live_block=lambda mode=None: self.license.block_message(mode), live_limit=self.license.live_bot_limit,
                               get_trade_store=lambda m: self.trade_stores.get(m),
                               get_market_data_for=lambda ex: self.feed(ex).get_market_data())
        # 처음 한 번: 예전 JSON 기록(가상 주문 · 봇 trades.json · 업비트 캐시)을 저장소로
        try:
            from bot_core.trade_store import migrate_legacy
            migrate_legacy(self.trade_stores, self.user_data_dir / 'data', self.user_data_dir / 'data' / 'bots', self.bots.bots())
        except Exception as e:
            logger.warning(f"거래 기록 옮기기 실패: {e}")

        # 라우트 설정
        self._setup_routes()

        logger.info(f"웹 대시보드 초기화: 포트 {port}")

    def _load_config(self) -> Dict:
        """설정 파일 로드 (암호화된 필드 자동 복호화 및 마이그레이션)"""
        default_config = {
            'mode': 'simulation',
            # 봇 매매 규칙: 조건 목록에서 고른 매수·매도 조건 (설정 탭 '봇 매매 규칙')
            'trade_rules': {'buy': {}, 'sell': {}, 'exclude': []},
            'symbol': 'KRW-BTC',
            'interval': 'day',
            'api_key': '',
            'api_secret': '',
            'risk_config': {
                'position_size_percent': 10.0,
                'stop_loss_percent': 5.0,
                'take_profit_percent': 10.0,
                'daily_loss_limit_percent': 10.0,
                'max_positions': 3,
                'risk_level': 'basic'
            },
            'simulation_config': {
                'initial_balance': 10000000.0
            }
        }

        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                # 기본값 병합
                merged_config = {**default_config, **config}

                # 암호화된 필드 복호화
                decrypted_config = self.encryption.decrypt_config(merged_config)

                # 평문으로 저장된 기존 설정 자동 마이그레이션 (한 번만)
                needs_migration = False
                for field in self.encryption.ENCRYPTED_FIELDS:
                    if field in decrypted_config and decrypted_config[field]:
                        value = str(decrypted_config[field])
                        # 평문이면 암호화 필요
                        if value and not self.encryption.is_encrypted(value):
                            needs_migration = True
                            break

                # 마이그레이션이 필요하면 암호화하여 재저장
                if needs_migration:
                    logger.info("기존 평문 설정을 암호화하여 마이그레이션합니다...")
                    encrypted_config = self.encryption.encrypt_config(decrypted_config)
                    self._save_config_internal(encrypted_config)
                    # 복호화된 버전 반환 (메모리에서 사용)
                    return decrypted_config

                return decrypted_config

            except Exception as e:
                logger.error(f"설정 파일 로드 오류: {e}")

        return default_config

    def _save_config_internal(self, config: Dict):
        """설정 파일 내부 저장 (암호화 적용)"""
        try:
            # 암호화된 필드 암호화
            encrypted_config = self.encryption.encrypt_config(config)

            # 설정 파일 저장
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(encrypted_config, f, ensure_ascii=False, indent=2)

            # Windows 파일 보호 (선택사항)
            try:
                self.encryption._protect_file_with_acl(self.config_path)
            except Exception:
                pass  # 실패해도 계속 진행

            logger.debug("설정 파일 저장 완료 (암호화됨)")
        except Exception as e:
            logger.error(f"설정 파일 저장 오류: {e}")
            raise

    def _setup_routes(self):
        """Flask 라우트 설정"""

        @self.app.route('/')
        def index():
            """메인 대시보드 페이지"""
            from bot_core.license import enforced
            ex = self._exchange_info()
            return render_template('bot_dashboard.html', config=self.config, app_mode=self.app_mode(),
                                   ex_name=ex['name'], ex_fee=ex['fee'], ex_id=ex['id'],
                                   version=__version__, license_enforced=enforced())

        @self.app.route('/api/status', methods=['GET'])
        def get_status():
            """봇 상태 조회"""
            mode = self.app_mode()
            running = self.bots.running_count()
            bots = self.bots.bots()
            first = bots[0] if bots else {}
            status = {
                'status': f"🟢 {running}개 실행 중" if running else "⚪ 중지됨",
                'stalled': self.watchdog.state if getattr(self, 'watchdog', None) else None,   # 멈춤 감시 (gui/watchdog.py)
                'app_mode': mode,
                'mode': 'live' if is_live(mode) else 'simulation',
                'symbol': self.config.get('symbol', 'KRW-BTC'),
                'is_running': running > 0,
                'bots_running': running,
                'bots_running_other': self.bots.running_total() - running,     # 다른 모드에서 도는 봇
                'bots_total': len(bots),
                'modes': [mode],
            }
            if mode == 'sim':
                status['sim_account'] = self.sim_account.summary()
            # 대표 봇(실행 중인 첫 봇, 없으면 첫 봇)의 상세 — 봇 현황 화면용
            lead = next((b['id'] for b in bots if self.bots.is_running(b['id'])), first.get('id'))
            engine = self.bots.engines.get((lead, mode)) if lead else None
            if engine:
                status.update(engine.get_status())
                status['is_running'] = running > 0
            return jsonify(status)

        @self.app.route('/api/config', methods=['GET', 'POST'])
        def config_api():
            """설정 조회/저장"""
            if request.method == 'GET':
                return jsonify(self.config)

            # POST: 설정 저장
            try:
                new_config = request.get_json() or {}
                new_config.pop('bots', None)          # 봇 목록은 /api/bots 로만
                new_config.pop('app_mode', None)      # 모드는 /api/app-mode 로만 (저장 전에 빼야 함)
                # 봇의 매매 규칙 · 안전장치는 봇마다 /api/bots 로만 — 예전엔 키 저장 때 옛 전역 값이 첫 봇을 덮어씀
                for k in ('trade_rules', 'risk_config'):
                    new_config.pop(k, None)
                self.config.update(new_config)

                # 로그에는 민감 정보 마스킹
                from utils.config_encryption import sanitize_config_for_logging
                sanitized_config = sanitize_config_for_logging(self.config)
                logger.info(f"설정 저장: {sanitized_config}")

                # 설정 파일 저장 (암호화 적용)
                self._save_config_internal(self.config)
                self._upbit_status_cache = None  # 키가 바뀌었을 수 있으니 업비트 연결 상태를 다시 확인

                if self.on_save:
                    self.on_save(self.config)

                return jsonify({'success': True, 'message': '설정이 저장되었습니다'})
            except Exception as e:
                logger.error(f"설정 저장 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 400

        @self.app.route('/api/trade-flow', methods=['GET'])
        def trade_flow():
            """코인별 최근 1분 매수·매도 체결 금액과 평소(직전 30분 평균) 대비 배수"""
            spike = self.trade_flow.spike_snapshot()
            spike.pop('prices', None)
            return jsonify({'success': True, **self.trade_flow.snapshot(), 'spike': spike})

        # ── 내 조건 (자연어 → 조건 규칙, 사용자 LLM API 사용) ──
        @self.app.route('/api/conditions/generate', methods=['POST'])
        def generate_condition():
            """사용자가 쓴 조건 문장을 조건 규칙(JSON)으로. 보내는 것은 문장뿐"""
            from gui.condition_llm import generate_condition as make, LLMError
            text = (request.get_json() or {}).get('text', '')
            try:
                spec = make(self.config.get('llm_provider', 'claude'), self.config.get('llm_api_key', ''),
                            self.config.get('llm_model', ''), text)
                return jsonify({'success': True, 'condition': spec})
            except LLMError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                logger.error(f"조건 만들기 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': f'조건 만들기 실패: {e}'}), 500

        @self.app.route('/api/llm/test', methods=['POST'])
        def test_llm():
            """AI 조건 도우미 키 확인 (아주 짧은 요청 한 번). 결과는 연결 상태로 기억"""
            status = self._check_llm(force=True)
            if status['state'] == 'ok':
                return jsonify({'success': True, 'model': status['model']})
            return jsonify({'success': False, 'error': status['message']}), 400

        @self.app.route('/api/llm/models', methods=['GET'])
        def llm_models():
            """설정 화면 모델 고르기: Gemini는 저장된 키로 쓸 수 있는 모델 목록을 구글에 물어봄 (글을 만들지 않아 한도를 쓰지 않음)"""
            from gui.condition_llm import gemini_choices, LLMError
            if request.args.get('provider', self.config.get('llm_provider')) != 'gemini':
                return jsonify({'success': True, 'models': []})
            key = self.config.get('llm_api_key', '') or ''
            if not key:
                return jsonify({'success': False, 'error': 'API 키를 먼저 넣어 주십시오', 'models': []})
            try:
                return jsonify({'success': True, 'models': gemini_choices(key)})
            except LLMError as e:
                return jsonify({'success': False, 'error': str(e), 'models': []})
            except Exception as e:
                return jsonify({'success': False, 'error': f'모델 목록을 받지 못했습니다: {e}', 'models': []})

        @self.app.route('/api/llm/status', methods=['GET'])
        def llm_status():
            """AI 조건 도우미 연결 상태 (설정 메뉴 표시용). 키·제공사·모델이 바뀌었을 때만 한 번 새로 확인"""
            return jsonify({'success': True, **self._check_llm(force=False)})

        @self.app.route('/api/conditions', methods=['GET', 'POST', 'DELETE'])
        def custom_conditions():
            """내 조건 목록 조회/저장(추가·수정)/삭제 → data/custom_conditions.json (최대 30개)"""
            import uuid
            from gui.condition_llm import sanitize_saved, validate_condition
            path = self.user_data_dir / 'data' / 'custom_conditions.json'
            try:
                saved = sanitize_saved(json.loads(path.read_text(encoding='utf-8')))
            except Exception:
                saved = []
            if request.method == 'GET':
                return jsonify({'success': True, 'conditions': saved})
            body = request.get_json() or {}
            if request.method == 'DELETE':
                saved = [c for c in saved if c['id'] != body.get('id')]
            else:
                spec, error = validate_condition(body.get('condition') or {})
                if error:
                    return jsonify({'success': False, 'error': error}), 400
                incoming = body.get('condition') or {}
                spec['id'] = str(incoming.get('id') or '')[:40] or f'my_{uuid.uuid4().hex[:10]}'
                spec['role'] = incoming.get('role') if incoming.get('role') in ('buy', 'sell', 'both', '') else ''
                spec['source_text'] = str(incoming.get('source_text') or '')[:500]
                index = next((i for i, c in enumerate(saved) if c['id'] == spec['id']), None)
                if index is None:
                    if len(saved) >= 30:
                        return jsonify({'success': False, 'error': '내 조건은 최대 30개까지 저장할 수 있습니다.'}), 400
                    saved.append(spec)
                else:
                    saved[index] = spec
            self._save_json(path, saved)
            return jsonify({'success': True, 'conditions': saved})

        @self.app.route('/api/control', methods=['POST'])
        def control_api():
            """봇 제어 (시작/중지/재시작)"""
            try:
                body = request.get_json() or {}
                action = body.get('action')

                if action == 'start':
                    # 업비트 모드: 계정 권한이 있어야 시작 (없으면 LICENSE_REQUIRED → 화면이 안내 창을 띄움)
                    refusal = self._license_refusal()
                    if refusal:
                        return refusal
                    # 업비트 모드: 화면의 시작 확인 창(위험 고지 동의)을 거친 요청만 시작
                    if is_live(self.app_mode()) and any(not self.bots.is_running(b['id']) and not b.get('example')
                                                        for b in self.bots.bots_in(self.app_mode())):   # 예시 봇은 실제 계좌 모드에서 안 켜짐
                        if not body.get('confirm_live'):
                            return jsonify({'success': False, 'error_code': 'CONFIRM_LIVE',
                                            'error': '실전 매매 시작 확인이 필요합니다.',
                                            'summary': self._live_start_summary()}), 400
                        if not self.config.get('live_risk_consent_at'):
                            self.config['live_risk_consent_at'] = datetime.now().isoformat(timespec='seconds')
                            self._save_config_internal(self.config)
                            logger.info(f"실전 매매 위험 고지 동의: {self.config['live_risk_consent_at']}")

                    result = self.on_start() if self.on_start else None
                    if result is None:
                        results = self.bots.start_all()
                        if results and not any(ok for _, ok, _ in results):
                            result = (False, ' / '.join(m for _, _, m in results))
                    if isinstance(result, tuple) and not result[0]:
                        return jsonify({'success': False, 'error': result[1]}), 400
                    self.is_running = self.bots.running_count() > 0
                    self.bot_status = "🟢 실행 중" if self.is_running else "⚪ 중지됨"
                    return jsonify({'success': True, 'message': '봇이 시작되었습니다'})

                elif action == 'stop':
                    self.bots.stop_all(mode=self.app_mode())      # 지금 보는 모드의 봇만
                    self.bot_status = "⚪ 중지됨"
                    self.is_running = False
                    return jsonify({'success': True, 'message': '봇이 중지되었습니다'})

                elif action == 'restart':
                    # 멈춰 있는 봇을 재시작으로 새로 켜지 않도록 (재시작 = 멈춤 → 시작이라 실거래가 시작될 수 있음)
                    if not self.is_running:
                        return jsonify({'success': False, 'error': '봇이 실행 중이 아닙니다. 시작 버튼으로 켜 주십시오.'}), 400
                    # 계정 권한이 끝난 채로 재시작하면 업비트 봇이 멈춘 뒤 다시 켜지지 않음 → 멈추기 전에 알림
                    # (실행 중인 봇은 그대로 두어 팔기·손절이 계속되게)
                    for lm in LIVE_MODES:
                        if self.bots.running_count(lm) and self.bots.live_blocked(lm):
                            return jsonify({'success': False, 'error_code': 'LICENSE_REQUIRED', 'license': self.license.status(),
                                            'error': f'지금 재시작하면 {self._ex_name(lm)} 봇을 다시 켤 수 없어서 그대로 두었습니다 (팔기·손절은 계속합니다). '
                                                     + self.bots.live_blocked(lm)}), 403
                    result = self.on_restart() if self.on_restart else None
                    if isinstance(result, tuple) and not result[0]:
                        self.bot_status = "⚪ 중지됨"
                        self.is_running = False
                        return jsonify({'success': False, 'error': f'재시작 중 다시 켜지 못했습니다: {result[1]}'}), 400
                    return jsonify({'success': True, 'message': '봇이 재시작되었습니다'})

                return jsonify({'success': False, 'error': '알 수 없는 액션'}), 400

            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── 업비트 ⇄ 시뮬레이션 모드 ──
        @self.app.route('/api/app-mode', methods=['GET', 'POST'])
        def app_mode_api():
            """지금 보는 모드 조회·바꾸기. 봇은 모드마다 따로 실행되므로 바꿔도 멈추지 않음"""
            if request.method == 'POST':
                mode = (request.get_json() or {}).get('mode')
                if mode not in ('upbit', 'bithumb', 'sim'):
                    return jsonify({'success': False, 'error': '모드는 upbit · bithumb · sim 입니다'}), 400
                self.config['app_mode'] = mode
                self._save_config_internal(self.config)
                self._upbit_status_cache = None
            mode = self.app_mode()
            return jsonify({'success': True, 'mode': mode, 'sim_account': self.sim_account.summary(),
                            'running': {m: self.bots.running_count(m) for m in ('upbit', 'bithumb', 'sim')},
                            'exchange': self._exchange_info(mode)})

        @self.app.route('/api/sim-account', methods=['GET', 'POST'])
        def sim_account_api():
            """가상 계좌 요약 / 시뮬레이션 전체 초기화 ({'action': 'reset', 'initial_krw': 10000000})
            실행 중인 시뮬레이션 봇은 멈춘 뒤 → 가상 계좌(원화·코인·주문) · 봇마다의 시뮬레이션 기록 · 수익률 기록을 처음으로.
            봇과 설정·규칙은 그대로. 지운 기록은 버리지 않고 보관 폴더로"""
            if request.method == 'POST':
                body = request.get_json() or {}
                if body.get('action') != 'reset':
                    return jsonify({'success': False, 'error': '알 수 없는 동작'}), 400
                try:
                    krw = float(body.get('initial_krw') or self.config.get('sim_initial_krw') or 10_000_000)
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'error': '초기자본 숫자가 올바르지 않습니다'}), 400
                if not 10_000 <= krw <= 100_000_000_000:
                    return jsonify({'success': False, 'error': '초기자본은 1만 원 ~ 1,000억 원 사이로 정해 주십시오'}), 400
                # 실행 중인 시뮬레이션 봇부터 멈춤 (멈춘 채로 둠 — 처음부터 [시작])
                stopped = [b['name'] for b in self.bots.bots() if self.bots.is_running(b['id'], 'sim')]
                for b in self.bots.bots():
                    if self.bots.is_running(b['id'], 'sim'):
                        self.bots.stop(b['id'], 'sim')
                self.config['sim_initial_krw'] = krw
                self._save_config_internal(self.config)
                self.sim_account.reset(krw)                                # 걸어 둔 시뮬레이션 지정가도 같이 사라짐
                self.chart_orders['sim'].clear()                           # 시뮬레이션 감시 주문도
                self.trade_stores['sim'].set_meta('account_since', int(time.time()))
                # 보유코인 분석의 수익률 기록(시뮬레이션)도 처음부터
                try:
                    self.pnl_history_sim.clear()
                    snap = self._get_pnl_snapshot_path('sim')
                    if snap.exists():
                        snap.unlink()
                except Exception as e:
                    logger.warning(f"시뮬레이션 수익률 기록 지우기 실패: {e}")
                # 봇들의 시뮬레이션 기록도 처음부터 (보관 폴더로 옮김)
                keep = self.bots.data_root / '_reset' / f"sim_all_{datetime.now():%Y%m%d%H%M%S}"
                for b in self.bots.bots():
                    self.bots.engines.pop((b['id'], 'sim'), None)
                    self.trade_stores['sim'].mark_reset(b['id'])          # 봇 거래 기록도 지금부터 (봇 미적용 계산 · 성적이 예전 매수를 넣지 않게)
                    d = self.bots.data_dir(b['id'], 'sim')
                    if d.exists() and any(d.iterdir()):
                        keep.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(d), str(keep / b['id']))
                note = f" · 실행 중이던 봇 {len(stopped)}개({', '.join(stopped[:3])}{' 외' if len(stopped) > 3 else ''})는 멈췄습니다 — [시작]을 누르면 처음부터 합니다" if stopped else ''
                return jsonify({'success': True, 'message': f'시뮬레이션을 {krw:,.0f}원으로 새로 시작했습니다 (예전 기록은 보관했습니다){note}',
                                'sim_account': self.sim_account.summary()})
            return jsonify({'success': True, 'sim_account': self.sim_account.summary()})

        # ── 여러 봇 ──
        @self.app.route('/api/bots', methods=['GET', 'POST'])
        def bots_api():
            if request.method == 'GET':
                try:                                          # 계좌 총자산: 코인은 지금 시세로 (봇이 꺼져 있어도) — 목록 계산보다 먼저
                    prices = {m: float(t['trade_price']) for m, t in self.mfeed()._krw_tickers().items() if t.get('trade_price')}
                except Exception:
                    prices = None
                account = self.bots.account_snapshot(prices)
                bots = self.bots.list()
                try:                                          # 전체 봇 보드 맨 위: 봇 전체 보유 합산
                    board = self.bots.board_holdings(bots, prices)
                    if account and account.get('krw') is not None:   # 매수 가능 합계는 계좌에 실제로 있는 원화보다 많을 수 없음
                        board['cash'] = min(board['cash'], float(account['krw']))
                except Exception as e:
                    logger.warning(f"봇 합산 보유 계산 실패: {e}")
                    board = None
                try:                                          # 전체 봇 보드 모니터링: 상태 · 오늘 손익 · 최근 활동
                    monitor = self.bots.board_monitor(bots, today_unrealized=self._today_unrealized(bots),
                                                      krw=(account or {}).get('krw'))
                except Exception as e:
                    logger.warning(f"봇 모니터링 계산 실패: {e}")
                    monitor = None
                return jsonify({'success': True, 'bots': bots, 'max': 10,
                                'account': account, 'coin_share': self.bots.coin_share(), 'board': board, 'monitor': monitor,
                                'per_bot_krw': self.bots.sim_per_bot() if self.bots.per_bot() else 0})   # 시뮬레이션: 봇마다 정해진 돈                          # 전체 봇 보드: 계좌 총자산 · 원화 · 코인
            ok, msg, bot = self.bots.create(request.get_json() or {})      # 지금 모드에만 생김
            return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg, 'bot': bot}), (200 if ok else 400)

        # 봇 관리: 계좌 총자산 · 맡기기 코인 값은 전체 시세(2초 캐시)로 — 봇이 꺼져 있어도 지금 값
        self.bots.price_source = lambda ex='upbit': {m: float(t['trade_price']) for m, t in self.feed(ex)._krw_tickers().items() if t.get('trade_price')}
        self.bots.preview_market = lambda held, ex='upbit': self.feed(ex)._preview_market(held)      # 적용 전 미리 보기: 가진 코인 시세만

        @self.app.route('/api/bots/coin-share', methods=['POST'])
        def bots_coin_share_api():
            """봇 경쟁: 같은 코인을 여러 봇이 함께 사고팔기 (지금 모드)"""
            ok, msg = self.bots.set_coin_share(bool((request.get_json() or {}).get('on')))
            return jsonify({'success': ok, 'message': msg})

        @self.app.route('/api/bots/<bot_id>/copy-mode', methods=['POST'])
        def bot_copy_mode_api(bot_id):
            """다른 모드의 매매 규칙 · 안전장치를 지금 모드로 가져오기"""
            ok, msg = self.bots.copy_mode_rules(bot_id, (request.get_json() or {}).get('from', ''))
            return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)

        @self.app.route('/api/bots/<bot_id>/pnl/history')
        def bot_pnl_history_api(bot_id):
            """봇 성과 탭 수익률 변화: 보유코인 분석의 /api/pnl/history 와 같은 모양 (이 봇이 가진 코인만)"""
            try:
                mode = self.app_mode()
                hist = self._bot_pnl_history(bot_id, mode)
                interval, currency = request.args.get('interval', '1m'), request.args.get('currency', 'ALL')
                points = hist.query(interval, currency)
                hold = []
                if currency in (None, '', 'ALL'):
                    points, hold = self._bot_total_curve(bot_id, mode, interval)    # 판 손익 포함 · 같은 돈 기준
                    self._bot_hold_backfill(bot_id, mode, hist)
                elif points:
                    # 한 코인: 봇(판 돈 포함) vs 안 팔고 들고 있었으면 — 이 코인 거래 기록 + 같은 간격 봉으로
                    try:
                        points, hold = self._coin_vs_hold(bot_id, mode, currency, interval, points)
                    except Exception as e:
                        logger.warning(f"코인 들고만 있었으면 계산 실패 ({currency}): {e}")
                return jsonify({'success': True, 'points': points, 'hold': hold, 'stats': hist.stats()})
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400

        @self.app.route('/api/bots/<bot_id>/adopt', methods=['GET', 'POST'])
        def bot_adopt_api(bot_id):
            """팔기만 하는 봇: 계좌 보유 코인을 봇에 맡기기 · 풀기 (지금 모드 계좌)"""
            try:
                prices = {m: float(t['trade_price']) for m, t in self.mfeed()._krw_tickers().items() if t.get('trade_price')}
            except Exception:
                prices = None
            if request.method == 'GET':
                ok, msg, rows = self.bots.adoptable(bot_id, prices)
                return jsonify({'success': ok, 'error': None if ok else msg, 'coins': rows})
            ok, msg = self.bots.set_adopted(bot_id, (request.get_json() or {}).get('currencies') or [], prices)
            return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)

        @self.app.route('/api/bots/<bot_id>/adopt/cleanup', methods=['GET', 'POST'])
        def bot_adopt_cleanup_api(bot_id):
            """맡긴 코인 정리: GET = 손절선을 넘은 코인 미리 보기 · POST = 고른 코인 팔기 (화면에서 한 번 더 확인한 뒤)"""
            if request.method == 'GET':
                try:
                    prices = {m: float(t['trade_price']) for m, t in self.mfeed()._krw_tickers().items() if t.get('trade_price')}
                except Exception:
                    prices = None
                return jsonify({'success': True, **self.bots.adopt_cleanup_list(bot_id, prices)})
            ok, msg = self.bots.adopt_cleanup(bot_id, (request.get_json() or {}).get('currencies') or [])
            return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)

        @self.app.route('/api/bots/<bot_id>/apply-preview', methods=['POST'])
        def bot_apply_preview(bot_id):
            """바꾼 규칙 · 안전장치를 적용하기 전에: 새 값 때문에 곧바로 팔리게 될 보유 코인 · 새로 들어간 '시작할 때 사기' 조건"""
            try:
                out = self.bots.apply_preview(bot_id, request.get_json() or {}, request.args.get('mode'))
                return jsonify({'success': True, **out})
            except Exception as e:
                logger.error(f"적용 미리 보기 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/bots/<bot_id>', methods=['GET', 'PUT', 'DELETE'])
        def bot_api(bot_id):
            if request.method == 'GET':
                d = self.bots.detail(bot_id)
                return (jsonify({'success': True, **d}) if d else (jsonify({'success': False, 'error': '봇을 찾지 못했습니다'}), 404))
            if request.method == 'DELETE':
                ok, msg = self.bots.delete(bot_id)
            else:
                mode = request.args.get('mode')                    # ?mode=sim|upbit 이면 그 모드 것 (없으면 지금 모드)
                ok, msg, _ = self.bots.update(bot_id, request.get_json() or {}, mode if mode in ('sim', 'upbit', 'bithumb') else None)
            return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)

        @self.app.route('/api/bots/compare', methods=['GET'])
        def bots_compare_api():
            """전체 봇 보드 · 봇별 평가수익률 비교: 봇마다 1분 기록을 간격별로 (interval=1h|1d|1w)"""
            interval = request.args.get('interval', '1h')
            if interval not in ('1m', '10m', '15m', '30m', '1h', '4h', '1d', '1w', '1M'):
                return jsonify({'success': False, 'error': '간격이 올바르지 않습니다'}), 400
            mode = self.app_mode()
            out, raw, raw_hold = [], [], []
            for b in self.bots.bots_in(mode):
                if b.get('example') and mode != 'sim':
                    continue
                try:
                    hist = self._bot_pnl_history(b['id'], mode)
                    pts, hold = self._bot_total_curve(b['id'], mode, interval)    # 판 손익 포함 · 봇 미적용은 같은 돈 기준
                    self._bot_hold_backfill(b['id'], mode, hist)
                except Exception:
                    pts, hold = [], []
                if pts:
                    out.append({'id': b['id'], 'name': b.get('name', ''), 'points': [{'ts': p['ts'], 'rate': p['rate']} for p in pts],
                                'hold': [{'ts': p['ts'], 'rate': p['rate']} for p in hold]})
                    raw.append(pts)
                    raw_hold.append(hold)

            def combine(series):
                """봇들을 합친 수익률: 시각마다 (손익 합 / 운용 한도 합). 봇마다 첫 기록부터는 빈 구간을 바로 앞 값으로 채움"""
                times = sorted({p['ts'] for s in series for p in s})
                byts = [{p['ts']: p for p in s} for s in series]
                last = [None] * len(series)
                res = []
                for t in times:
                    buy = value = 0.0
                    for i, m in enumerate(byts):
                        if t in m:
                            last[i] = m[t]
                        if last[i] is not None:
                            buy += float(last[i]['base'] or 0)
                            value += float(last[i]['pnl'] or 0)
                    if buy > 0:
                        res.append({'ts': t, 'rate': value / buy * 100})
                return res
            # 모든 봇 (봇 적용) · 봇 미적용 (모든 봇이 산 코인을 안 팔고 들고 있었으면)
            total = {'points': combine(raw), 'hold': combine([h for h in raw_hold if h])} if raw else None
            return jsonify({'success': True, 'bots': out, 'total': total, 'interval': interval})

        @self.app.route('/api/bots/spike-active', methods=['GET'])
        def bots_spike_active_api():
            """지금 모드에서 '일부 팔고 다시 사기'가 진행 중인 코인 (봇 이름·판 가격·다시 살 가격)"""
            return jsonify({'success': True, 'items': self.bots.spike_active()})

        @self.app.route('/api/bots/<bot_id>/trades', methods=['GET'])
        def bot_trades_api(bot_id):
            """이 봇의 거래 기록 (symbol=KRW-XXX 이면 그 코인만) — 매매 내역 차트용, 오래된 순"""
            if not self.bots.get(bot_id):
                return jsonify({'success': False, 'error': '봇을 찾지 못했습니다'}), 404
            sym = request.args.get('symbol', '') or None
            items = self.bots.all_trades(bot_id, symbol=sym)       # 초기화 이후 전부 (저장소)
            return jsonify({'success': True, 'trades': sorted(items, key=lambda t: str(t.get('time', '')))})

        @self.app.route('/api/bots/<bot_id>/<action>', methods=['POST'])
        def bot_action_api(bot_id, action):
            body = request.get_json() or {}
            bot = self.bots.get(bot_id)
            if not bot:
                return jsonify({'success': False, 'error': '봇을 찾지 못했습니다'}), 404
            if action == 'copy':
                ok, msg, new = self.bots.copy(bot_id)
                return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg, 'bot': new}), (200 if ok else 400)
            if action == 'send':                                     # 다른 모드로 보내기 (규칙 · 안전장치 복사)
                ok, msg = self.bots.send_to(bot_id, str(body.get('to') or ''))
                return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)
            if action == 'stop':
                ok, msg = self.bots.stop(bot_id)
            elif action == 'reset':
                ok, msg = self.bots.reset(bot_id)
            elif action == 'start':
                if bot.get('example') and self.app_mode() != 'sim':   # 예시 봇: 확인 창·계정 확인 전에 바로 안내
                    from bot_core.bot_manager import EXAMPLE_ONLY_SIM
                    return jsonify({'success': False, 'error_code': 'EXAMPLE_SIM_ONLY', 'error': EXAMPLE_ONLY_SIM}), 400
                refusal = self._license_refusal(bot_id=bot_id)   # 실제 모드: 요금제(거래소 · 실제 봇 한도)가 있어야 시작
                if refusal:
                    return refusal
                if is_live(self.app_mode()):
                    if not body.get('confirm_live'):
                        return jsonify({'success': False, 'error_code': 'CONFIRM_LIVE', 'error': '실전 매매 시작 확인이 필요합니다.',
                                        'summary': self._live_start_summary(bot_id)}), 400
                    if not self.config.get('live_risk_consent_at'):
                        self.config['live_risk_consent_at'] = datetime.now().isoformat(timespec='seconds')
                        self._save_config_internal(self.config)
                ok, msg = self.bots.start(bot_id)
            else:
                return jsonify({'success': False, 'error': '알 수 없는 동작'}), 400
            self.is_running = self.bots.running_count() > 0
            self.bot_status = "🟢 실행 중" if self.is_running else "⚪ 중지됨"
            return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)

        # ── '순간 급등 · 일부 팔고 다시 사기' 직접 검증하기 ──
        @self.app.route('/api/spike-verify', methods=['POST'])
        def spike_verify_start():
            body = request.get_json() or {}
            market = str(body.get('market') or '').upper()
            if not market.startswith('KRW-'):
                market = f'KRW-{market}'
            days = int(body.get('days') or 30)
            if days not in (7, 14, 30, 60, 90):
                return jsonify({'success': False, 'error': '기간은 7·14·30·60·90일 중에서 골라 주십시오'}), 400
            from bot_core.spike_rebuy import DEFAULTS
            rule = {k: body['rule'][k] for k in ('mult', 'rise', 'rise_1h', 'portion') if isinstance(body.get('rule'), dict) and k in body['rule']}
            with self._verify_lock:
                if any(j['stage'] in ('fetch', 'calc') for j in self._verify_jobs.values()):
                    return jsonify({'success': False, 'error': '이미 검증 중입니다. 끝난 뒤 다시 눌러 주십시오'}), 409
                job_id = uuid.uuid4().hex[:10]
                self._verify_jobs = {job_id: {'stage': 'fetch', 'progress': 0.0, 'market': market, 'days': days}}
            threading.Thread(target=self._run_verify, args=(job_id, market, days, {**DEFAULTS, **rule}),
                             name='SpikeVerify', daemon=True).start()
            return jsonify({'success': True, 'job': job_id})

        @self.app.route('/api/spike-verify/<job_id>', methods=['GET'])
        def spike_verify_status(job_id):
            job = self._verify_jobs.get(job_id)
            if not job:
                return jsonify({'success': False, 'error': '검증 작업을 찾지 못했습니다'}), 404
            return jsonify({'success': True, **job})

        # ── 봇 백테스트: 코인 · 봉 · 기간 → 필요한 캔들만 받아(이 PC에 저장) 봇 규칙을 다시 돌려 봄 ──
        @self.app.route('/api/bots/<bot_id>/backtest/check', methods=['GET'])
        def bot_backtest_check(bot_id):
            from bot_core import backtest as bt
            bot = self._bt_bot_now(bot_id)
            if not bot:
                return jsonify({'success': False, 'error': '봇을 찾지 못했습니다'}), 404
            p = bt.plan(bot, self._custom_conditions())
            limits = {u: min(d, bt.MINUTE_MAX_DAYS) if p['minute'] else d for u, d in self.BT_LIMIT_DAYS.items()}
            return jsonify({'success': True, 'skipped': p['skipped'], 'notes': p['notes'], 'buy': [c['title'] for _, c in p['buy']],
                            'sell': [c['title'] for _, c in p['sell']], 'partial': p['partial'], 'limits': limits,
                            'budget': bot.get('budget') or 0})

        @self.app.route('/api/bots/<bot_id>/backtest', methods=['POST'])
        def bot_backtest_start(bot_id):
            from bot_core import backtest as bt
            bot = self._bt_bot_now(bot_id)
            if not bot:
                return jsonify({'success': False, 'error': '봇을 찾지 못했습니다'}), 404
            body = request.get_json() or {}
            markets, err = self._bt_markets([body.get('market')])
            if err:
                return jsonify({'success': False, 'error': err}), 400
            market = markets[0]
            args, err = self._bt_args(body, bot)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            unit, start, end, krw, costs = args
            with self._bt_lock:
                if any(j['stage'] in ('fetch', 'calc') for j in self._bt_jobs.values()):
                    return jsonify({'success': False, 'error': '이미 백테스트 중입니다. 끝난 뒤 다시 눌러 주십시오'}), 409
                job_id = uuid.uuid4().hex[:10]
                self._bt_jobs = {job_id: {'stage': 'fetch', 'progress': 0.0, 'text': '준비 중…'}}
            threading.Thread(target=self._run_backtest, args=(job_id, bot, market, unit, start, end, krw, costs),
                             name='Backtest', daemon=True).start()
            return jsonify({'success': True, 'job': job_id})

        # ── 백테스트 메뉴: 저장하지 않은 규칙(대상 코인 · 매수 · 매도 · 위험 관리)을 여러 코인으로 ──
        @self.app.route('/api/backtest/spec/check', methods=['POST'])
        def backtest_spec_check():
            from bot_core import backtest as bt
            bot = self._bt_spec_bot((request.get_json() or {}).get('spec'))
            p = bt.plan(bot, self._custom_conditions())
            cap = min([self.BT_LIMIT_DAYS.get(bt.TF_UNIT[tf], 9999) for tf in p['tfs'] if tf in bt.TF_UNIT] or [9999])
            limits = {u: min(d, cap, bt.MINUTE_MAX_DAYS if p['minute'] else 9999) for u, d in self.BT_LIMIT_DAYS.items()}
            return jsonify({'success': True, 'skipped': p['skipped'], 'notes': [n for n in p['notes'] if n.get('title') != '매매대상'],
                            'buy': [c['title'] for _, c in p['buy']], 'sell': [c['title'] for _, c in p['sell']],
                            'partial': p['partial'], 'limits': limits})

        @self.app.route('/api/backtest/spec/run', methods=['POST'])
        def backtest_spec_run():
            body = request.get_json() or {}
            bot = self._bt_spec_bot(body.get('spec'))
            markets, err = self._bt_markets(body.get('coins') or [], self.BT_MULTI_MAX)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            args, err = self._bt_args(body, bot)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            unit, start, end, krw, costs = args
            with self._bt_lock:
                if any(j['stage'] in ('fetch', 'calc') for j in self._bt_jobs.values()):
                    return jsonify({'success': False, 'error': '이미 백테스트 중입니다. 끝난 뒤 다시 눌러 주십시오'}), 409
                job_id = uuid.uuid4().hex[:10]
                self._bt_jobs = {job_id: {'stage': 'fetch', 'progress': 0.0, 'text': '준비 중…'}}
            threading.Thread(target=self._run_backtest_multi, args=(job_id, bot, markets, unit, start, end, krw, costs),
                             name='BacktestMulti', daemon=True).start()
            return jsonify({'success': True, 'job': job_id})

        @self.app.route('/api/backtest/<job_id>', methods=['GET'])
        def bot_backtest_status(job_id):
            job = self._bt_jobs.get(job_id)
            if not job:
                return jsonify({'success': False, 'error': '백테스트 작업을 찾지 못했습니다'}), 404
            return jsonify({'success': True, **job})

        # ── 보유코인 분석 '다른 코인 비교': 보고 있는 구간에 다른 코인을 샀다면 ──
        @self.app.route('/api/compare/scan', methods=['POST'])
        def compare_scan_start():
            """원화 코인 전부의 구간 수익률 · 구간 최대 하락폭 (코인 고르기 창). 같은 구간은 10분 기억"""
            from gui import coin_compare as cc
            body = request.get_json() or {}
            interval = str(body.get('interval') or '')
            try:
                start = int(body.get('start'))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': '구간 시작 시각이 올바르지 않습니다'}), 400
            if interval not in cc.UNIT_OF or not cc.start_ok(start, time.time()):
                return jsonify({'success': False, 'error': '간격 또는 시작 시각이 올바르지 않습니다'}), 400
            return jsonify({'success': True, 'job': self._cmp_scan_job(interval, start)})

        @self.app.route('/api/compare/job/<job_id>', methods=['GET'])
        def compare_job_status(job_id):
            job = self._cmp_jobs.get(job_id)
            if not job:
                return jsonify({'success': False, 'error': '작업을 찾지 못했습니다'}), 404
            return jsonify({'success': True, **job})

        @self.app.route('/api/compare/bot', methods=['POST'])
        def compare_bot_start():
            """고른 코인을 구간 처음에 같은 금액씩 산 채로 출발해 봇 규칙을 적용했다면 vs 그냥 들고 있었다면 (코인마다 백테스트)"""
            from bot_core import backtest as bt
            from gui import coin_compare as cc
            body = request.get_json() or {}
            bot = self.bots.get(str(body.get('bot_id') or ''))
            if not bot:
                return jsonify({'success': False, 'error': '봇을 찾지 못했습니다'}), 404
            interval = str(body.get('interval') or '')
            coins = [str(c).upper().replace('KRW-', '') for c in (body.get('coins') or [])]
            coins = [c for c in dict.fromkeys(coins) if re.fullmatch(r'[A-Z0-9]{1,15}', c)]
            try:
                start = int(body.get('start'))
            except (TypeError, ValueError):
                start = 0
            if interval not in cc.UNIT_OF or not cc.start_ok(start, time.time()):
                return jsonify({'success': False, 'error': '간격 또는 시작 시각이 올바르지 않습니다'}), 400
            if not coins:
                return jsonify({'success': False, 'error': "먼저 '다른 코인 비교'에서 코인을 골라 주십시오"}), 400
            if len(coins) > cc.BOT_MAX:
                return jsonify({'success': False, 'error': f'봇 적용 비교는 코인 {cc.BOT_MAX}개까지 계산할 수 있습니다'}), 400
            unit = cc.UNIT_OF[interval]
            p = bt.plan(bot, self._custom_conditions())
            limit = min(self.BT_LIMIT_DAYS[unit], bt.MINUTE_MAX_DAYS) if p['minute'] else self.BT_LIMIT_DAYS[unit]
            now = int(time.time())
            clipped = start < now - limit * 86400
            start = max(start, now - limit * 86400)
            with self._bt_lock:
                if any(j['stage'] in ('fetch', 'calc') for j in self._cmp_jobs.values() if j.get('kind') == 'bot'):
                    return jsonify({'success': False, 'error': '이미 계산 중입니다. 끝난 뒤 다시 눌러 주십시오'}), 409
                job_id = uuid.uuid4().hex[:10]
                self._cmp_jobs[job_id] = {'kind': 'bot', 'stage': 'fetch', 'progress': 0.0, 'text': '준비 중…'}
            threading.Thread(target=self._run_cmp_bot, args=(job_id, bot, coins, unit, start, now, clipped, limit),
                             name='CompareBot', daemon=True).start()
            return jsonify({'success': True, 'job': job_id})

        @self.socketio.on('connect')
        def handle_connect():
            """WebSocket 연결"""
            logger.info('클라이언트 연결됨')
            emit('status', {'status': self.bot_status, 'is_running': self.is_running})

        @self.socketio.on('disconnect')
        def handle_disconnect():
            """WebSocket 연결 해제"""
            logger.info('클라이언트 연결 해제됨')

        # ── 연결 테스트 API ─────────────────────────────

        @self.app.route('/api/test-connection', methods=['POST'])
        def test_connection():
            """거래소 API 연결 테스트"""
            try:
                data = request.get_json() or {}
                ex = 'bithumb' if data.get('exchange') == 'bithumb' else 'upbit'
                saved_key, saved_secret = api_keys(ex, self.config)
                api_key = (data.get('api_key', '') or saved_key).strip()
                api_secret = (data.get('api_secret', '') or saved_secret).strip()

                if not api_key or not api_secret:
                    return jsonify({'success': False, 'error': 'API 키와 Secret 키를 입력해 주십시오.'})

                if ex == 'bithumb':
                    return jsonify(self._test_bithumb(api_key, api_secret))
                import pyupbit
                upbit = pyupbit.Upbit(api_key, api_secret)
                balances = upbit.get_balances()

                if balances is not None:
                    # KRW 잔고 찾기
                    krw_balance = 0
                    for b in balances:
                        if b.get('currency') == 'KRW':
                            krw_balance = float(b.get('balance', 0))
                            break
                    return jsonify({
                        'success': True,
                        'message': f'연결 성공! KRW 잔고: {krw_balance:,.0f}원'
                    })
                else:
                    return jsonify({'success': False, 'error': 'API 응답 없음. 키를 확인해 주십시오.'})

            except Exception as e:
                # 업비트 거절 사유(IP 미등록, 키 만료 등)를 그대로 보여준다 (utils/pyupbit_patch.py)
                logger.warning(f"API 연결 테스트 실패: {e}")
                return jsonify({'success': False, 'error': f'연결 실패: {e}'})

        # ── 보유 자산 API ──────────────────────────────

        @self.app.route('/api/holdings', methods=['GET'])
        def get_holdings():
            """보유 자산 조회 (거래소 실시간) - API 키가 있으면 모드와 무관하게 실제 자산 표시"""
            try:
                return jsonify({'success': True, 'holdings': self._fetch_holdings()})
            except HoldingsUnavailable as e:
                return jsonify({'success': False, 'error': str(e)})
            except Exception as e:
                logger.error(f"보유 자산 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/pnl/history', methods=['GET'])
        def get_pnl_history():
            """1분 수익률 기록을 간격별로 묶어 반환 (interval: 1m 15m 30m 1h 4h 1d 1w 1M 1y, currency: ALL 또는 코인)"""
            try:
                interval = request.args.get('interval', '1m')
                currency = request.args.get('currency', 'ALL')
                hist = self._pnl_hist()
                points = hist.query(interval, currency)
                out = {'success': True, 'points': points, 'stats': hist.stats()}
                # 겹쳐 볼 보유 코인들의 수익률 (최대 5개)
                coins = [c.strip().upper() for c in request.args.get('coins', '').split(',') if c.strip()][:5]
                if coins:
                    out['coins'] = {c: hist.query(interval, c) for c in coins}
                # 다른 코인 비교: 고른 코인마다 점의 시각 값 (화면에서 그래프 맨 왼쪽 = 0 으로 묶음 선 · 코인 선)
                cmp = [c.strip().upper() for c in request.args.get('cmp', '').split(',') if re.fullmatch(r'[A-Za-z0-9]{1,15}', c.strip())]
                if cmp and points:
                    out['cmp'] = self._cmp_prices(cmp, interval, points)
                bench = request.args.get('bench', '')
                if (bench.startswith('KRW-') or bench in ('MARKET', 'TOP10')) and points:
                    # 비교선 출발점 = 첫 기록 때 (그래프 왼쪽 끝이 아니라) — 1분봉 · 10분봉 · 1시간봉 어느 것으로 봐도 같은 시각은 같은 값
                    first = hist.query('1y', currency, limit=10 ** 6)[0]
                    out['bench'] = self._bench_prices(bench, interval, [p['at'] for p in points], anchor_at=first['at'])
                    if bench == 'MARKET':
                        p0 = self._bench_prices('MARKET', '1m', [first['at']])[0]
                    elif bench == 'TOP10':
                        p0 = 1.0 if any(out['bench']) else None          # 코인마다 첫 기록 때 값 대비 평균 → 출발점은 1
                    else:
                        p0 = self._price_at(bench, first['at'])
                    if p0:
                        out['bench_anchor'] = {'ts': first['at'], 'rate': first['rate'], 'price': p0}
                    if bench == 'TOP10':
                        out['bench_coins'] = [m.replace('KRW-', '') for m in self._top_markets(10)]
                return jsonify(out)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                logger.error(f"수익률 기록 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/holdings/period-lows', methods=['GET'])
        def get_period_lows():
            """코인별 기간 최저가·종가 추이 (보유코인·전체코인 분석, 하루 캐시) - 자세한 내용은 _fetch_period_lows"""
            markets = [m for m in request.args.get('markets', '').split(',') if m.startswith('KRW-')][:400]
            return jsonify({'success': True, 'lows': self.mfeed()._fetch_period_lows(markets)})

        @self.app.route('/api/upbit-status', methods=['GET'])
        def get_upbit_status():
            """상단 연결 램프용 업비트 연결 상태 (30초 캐시, 설정 저장 시 초기화)

            반환: {'state': 'ok' | 'no_key' | 'error', 'message': 이유}
            업비트 잔고 조회(자산조회 권한)가 성공하면 연결된 것으로 본다.
            """
            mode = self.app_mode()
            if mode == 'sim':
                return jsonify({'state': 'ok', 'message': '시뮬레이션 모드: 가상 계좌로 사고팝니다 (실제 계좌와 섞이지 않습니다).'})
            cached = self._upbit_status_cache
            if cached and time.time() - cached[0] < 30 and cached[2] == mode:
                return jsonify(cached[1])
            name = self._ex_name(mode)
            api_key, api_secret = api_keys(mode, self.config)
            if not api_key or not api_secret:
                status = {'state': 'no_key', 'message': f'API 키가 없습니다. 설정 탭에서 {name} API 키를 입력해 주십시오.'}
            else:
                try:
                    balances = self._account(mode).get_balances()
                    status = ({'state': 'ok', 'message': f'{name} API로 잔고 조회에 성공했습니다.'} if balances is not None
                              else {'state': 'error', 'message': f'{name} 응답이 없습니다.'})
                except Exception as e:
                    # 업비트 거절 사유(IP 미등록, 키 오류 등)는 utils/pyupbit_patch.py가 한글로 바꿔 줌
                    status = {'state': 'error', 'message': str(e)}
            self._upbit_status_cache = (time.time(), status, mode)
            return jsonify(status)

        @self.app.route('/api/candles', methods=['GET'])
        def get_candles():
            """한 코인의 캔들 200개 (오래된 순) - 봇 현황 차트. 같은 코인·단위·끝 시각은 15초 동안 다시 받지 않음
            to(선택): 이 시각 전까지의 캔들 (UTC 'YYYY-MM-DDTHH:MM:SSZ') — 지난 거래를 볼 때"""
            market = request.args.get('market', '')
            unit = request.args.get('unit', '15')
            to = request.args.get('to') or None
            if to and not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', to):
                return jsonify({'success': False, 'error': '시각 형식이 올바르지 않습니다'}), 400
            units = {'1': 'minutes/1', '5': 'minutes/5', '10': 'minutes/10', '15': 'minutes/15', '30': 'minutes/30',
                     '60': 'minutes/60', '240': 'minutes/240', 'day': 'days', 'week': 'weeks', 'month': 'months'}
            if not re.fullmatch(r'KRW-[A-Z0-9]{1,15}', market) or unit not in units:
                return jsonify({'success': False, 'error': '코인 또는 단위가 올바르지 않습니다'}), 400
            feed = self.mfeed()                       # 지금 모드 거래소 캔들 (빗썸 모드 = 빗썸)
            key = (market, unit, to)
            hit = feed._chart_candle_cache.get(key)
            if hit and time.time() - hit[0] < (600 if to else 15):
                return jsonify({'success': True, 'candles': hit[1]})
            try:
                raw = feed._get_candles(units[unit], market, 200, to, priority=True)   # 화면 차트: 앞쪽 줄
            except Exception as e:
                logger.warning(f"캔들 불러오기 실패 {market} {unit}: {e}")
                return jsonify({'success': False, 'error': f'{self._ex_name()}에서 캔들을 받지 못했습니다'}), 502
            out = []
            for c in reversed(raw or []):
                t = c.get('candle_date_time_utc')
                if not t:
                    continue
                ts = int(datetime.strptime(t, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
                close = c.get('trade_price')
                out.append([ts, c.get('opening_price', close), c.get('high_price', close), c.get('low_price', close), close,
                            c.get('candle_acc_trade_price')])        # 6번째: 거래대금 (캔들 창 패턴 표시용)
            cache = feed._chart_candle_cache
            if len(cache) > 300:                      # 지난 거래 차트마다 다른 'to' 묶음이 쌓이므로 10분 지난 것은 비움 (2026-10-17)
                old = time.time() - 600
                for k in [k for k, v in cache.items() if v[0] < old]:
                    cache.pop(k, None)
            cache[key] = (time.time(), out)
            return jsonify({'success': True, 'candles': out})

        @self.app.route('/api/tickers', methods=['GET'])
        def get_tickers():
            """차트 분석 실시간: 고른 코인들의 현재가 · 전일 대비 % · 24시간 거래대금 (원화 전체 시세 2초 캐시를 그대로 씀)"""
            want = [m for m in request.args.get('markets', '').split(',') if re.fullmatch(r'KRW-[A-Z0-9]{1,15}', m)][:60]
            feed = self.mfeed()
            try:
                tickers = feed._krw_tickers()
            except Exception as e:
                logger.warning(f"차트 분석 시세 실패: {e}")
                return jsonify({'success': False, 'error': f'{self._ex_name()} 시세를 받지 못했습니다'}), 502
            # 업비트 · 시뮬레이션: 실시간 체결 연결의 마지막 체결가(거의 바로)로 덮어씀 — 업비트에 따로 묻지 않음
            live = self.trade_flow.last_prices(want) if feed is self else {}
            out = {}
            for m in want:
                t = tickers.get(m)
                if t and t.get('trade_price'):
                    price = float(live.get(m) or t['trade_price'])
                    prev = t.get('prev_closing_price')
                    chg = (price / float(prev) - 1) * 100 if prev else (t.get('signed_change_rate') or 0) * 100
                    out[m] = [price, chg, t.get('acc_trade_price_24h')]
            return jsonify({'success': True, 'tickers': out, 'at': int(time.time())})

        @self.app.route('/api/signal-stats', methods=['GET'])
        def get_signal_stats():
            """조건 · 패턴 신뢰도 요약 (신호 수 · 1·3·7일 뒤 맞은 % · 같은 때 아무 코인 · 평균 등락 · 최근 신호)"""
            try:
                return jsonify({'success': True, **self._signal_stats_summary()})
            except Exception as e:
                logger.warning(f"신뢰도 요약 실패: {e}")
                return jsonify({'success': False, 'error': str(e), 'status': dict(self.signal_stats.status)}), 500

        @self.app.route('/api/signal-stats/refresh', methods=['POST'])
        def refresh_signal_stats():
            """지금 바로 갱신 (이미 도는 중이면 그대로)"""
            if not self.signal_stats.status.get('running'):
                threading.Thread(target=self._signal_stats_update, name='SignalStatsNow', daemon=True).start()
            return jsonify({'success': True, 'status': dict(self.signal_stats.status)})

        @self.app.route('/api/tf-bars', methods=['GET'])
        def get_tf_bars():
            """조건 계산용 4시간·1시간봉 (tf=4h|1h) — 자세한 내용은 _fetch_tf_bars"""
            tf = request.args.get('tf', '')
            if tf not in self.TF_UNIT:
                return jsonify({'success': False, 'error': 'tf 는 ' + ' · '.join(self.TF_UNIT)}), 400
            markets = [m for m in request.args.get('markets', '').split(',') if m.startswith('KRW-')][:400]
            return jsonify({'success': True, 'tf': tf, 'bars': self.mfeed()._fetch_tf_bars(markets, tf)})

        @self.app.route('/api/holdings/hourly', methods=['GET'])
        def get_hourly():
            """코인별 최근 24시간 1시간봉 (10분 캐시) - 자세한 내용은 _fetch_hourly"""
            markets = [m for m in request.args.get('markets', '').split(',') if m.startswith('KRW-')][:400]
            return jsonify({'success': True, 'hourly': self.mfeed()._fetch_hourly(markets)})

        @self.app.route('/api/markets', methods=['GET'])
        def get_markets():
            """전체코인 분석 탭: 업비트 원화 마켓 전체 시세 + 유의·주의·공지 (API 키 필요 없음)"""
            try:
                coins, global_notices = self.mfeed()._build_market_coins()
                return jsonify({'success': True, 'coins': coins, 'global_notices': global_notices, 'exchange': exchange_of(self.app_mode())})
            except Exception as e:
                logger.error(f"전체 코인 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/trade-history/page', methods=['GET'])
        def get_trade_history_page():
            """거래 내역 한 쪽 (최신순 limit건): days · kind(매수|매도) · coins(심볼,..) · q(심볼 일부) · before(다음 쪽 커서)
            → {rows(봇 · 이유 포함), total(조건에 맞는 전체 수), next(다음 쪽 커서 또는 null), updated_at}"""
            try:
                a = request.args
                days = max(1, min(int(a.get('days', 7)), 3660))
                limit = max(50, min(int(a.get('limit', 500)), 2000))
                before = a.get('before') or None
                if before and not re.fullmatch(r'\d+:\d+', before):
                    return jsonify({'success': False, 'error': '쪽 커서가 올바르지 않습니다'}), 400
                self._sync_exchange(days, recent=not before)
                rows, total, nxt = self._trade_store_now().fills_page(
                    since_ts=self._trades_since(days), kind=a.get('kind') or None,
                    currencies=self._coin_args(a.get('coins')), q=(a.get('q') or '').strip() or None, before=before, limit=limit,
                    bot=(a.get('bot') or '').strip() or None)
                return jsonify({'success': True, 'rows': self._with_reasons(rows), 'total': total, 'next': nxt,
                                'with_reasons': True, 'updated_at': int(time.time())})
            except HoldingsUnavailable as e:
                return jsonify({'success': False, 'error': str(e)})
            except Exception as e:
                logger.error(f"거래내역 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/trade-history/clear', methods=['POST'])
        def clear_sim_trade_history():
            """시뮬레이션 거래 내역 지우기: 지금부터만 보이게 (기록 파일 · 봇 성적 · 수익률 그래프 · 가상 계좌는 그대로)"""
            if self.app_mode() != 'sim':
                return jsonify({'success': False, 'error': '업비트 거래 내역은 실제 거래라 지울 수 없습니다'}), 400
            self.trade_stores['sim'].set_meta('history_since', int(time.time()))
            self.__dict__.pop('_realized_cache', None)
            return jsonify({'success': True, 'message': '시뮬레이션 거래 내역을 지웠습니다 — 지금부터 새로 쌓입니다'})

        @self.app.route('/api/trade-history/coin', methods=['GET'])
        def get_coin_trade_history():
            """한 코인의 매수 · 매도 (최근 days일, 봇 · 이유 포함, 최신순) — 코인 그래프 창 · 거래 차트 창용"""
            try:
                cur = (request.args.get('currency') or '').upper()
                days = request.args.get('days')
                days = max(1, min(int(days if days else int(request.args.get('chunks', 5)) * 7), 3660))
                self._sync_exchange(days)
                rows = self._trade_store_now().fills_all(since_ts=self._trades_since(days), currencies=[cur])
                return jsonify({'success': True, 'rows': self._with_reasons(rows), 'days': days})
            except HoldingsUnavailable as e:
                return jsonify({'success': False, 'error': str(e)})
            except Exception as e:
                logger.error(f"코인 거래내역 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/market-breadth', methods=['GET'])
        def get_market_breadth():
            """전체코인 분석 '시장 현황': 상승 코인 비율 · 평균 등락률 추이 (interval=1m~1M, 구간 마지막 값)"""
            interval = request.args.get('interval', '1m')
            if interval not in ('1m', '10m', '15m', '30m', '1h', '4h', '1d', '1w', '1M'):
                return jsonify({'success': False, 'error': '간격이 올바르지 않습니다'}), 400
            try:
                return jsonify({'success': True, 'points': self.market_breadth.query(interval), 'stats': self.market_breadth.stats()})
            except Exception as e:
                logger.warning(f"시장 분위기 조회 실패: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/holdings/realized', methods=['GET'])
        def get_holdings_realized():
            """보유코인 분석 맨 위: 누적 수수료 · 판 손익 (업비트 '투자손익'처럼) — 시뮬레이션은 가상 계좌를 연 때부터,
            업비트는 받아 둔 거래 기록부터 (30초 기억)"""
            mode = self.app_mode()
            hit = getattr(self, '_realized_cache', {}).get(mode)
            if hit and time.time() - hit[0] < 30:
                return jsonify({'success': True, **hit[1]})
            try:
                since = int(self.trade_stores['sim'].get_meta('account_since', 0) or 0) if mode == 'sim' else 0
                out = self._trade_store_now().realized_summary(since)
                out['mode'] = mode
                self.__dict__.setdefault('_realized_cache', {})[mode] = (time.time(), out)
                return jsonify({'success': True, **out})
            except Exception as e:
                logger.warning(f"판 손익 · 수수료 계산 실패: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/trade-history/csv', methods=['GET'])
        def get_trade_history_csv():
            """조건에 맞는 거래 내역 전부를 CSV로 (엑셀에서 한글이 깨지지 않게 BOM) — 입금 · 출금 포함"""
            import csv
            import io
            try:
                a = request.args
                days = max(1, min(int(a.get('days', 7)), 3660))
                kind = a.get('kind') or 'ALL'
                coins = self._coin_args(a.get('coins'))
                q = (a.get('q') or '').strip() or None
                bot = (a.get('bot') or '').strip() or None
                self._sync_exchange(days)
                rows = [] if kind in ('입금', '출금') else self._with_reasons(self._trade_store_now().fills_all(
                    since_ts=self._trades_since(days), kind=kind if kind in ('매수', '매도') else None, currencies=coins, q=q, bot=bot))
                if kind in ('ALL', '입금', '출금') and not bot:              # 봇을 고르면 입금 · 출금은 뺌
                    try:
                        tr = self._fetch_transfers(days)['rows']
                    except Exception:
                        tr = []
                    want = set(coins or [])
                    tr = [r for r in tr if (kind == 'ALL' or r['kind'] == kind)
                          and (coins is None and not q or r.get('currency') in want or (q and q.upper() in str(r.get('currency'))))]
                    rows = sorted(rows + tr, key=lambda r: str(r.get('time') or ''), reverse=True)
                from datetime import timedelta, timezone as tz
                kst = tz(timedelta(hours=9))

                def when(v):                                  # 화면과 같은 한국 시각 'YYYY.MM.DD HH:MM'
                    try:
                        d = datetime.fromisoformat(str(v))
                        return (d if d.tzinfo else d.astimezone()).astimezone(kst).strftime('%Y.%m.%d %H:%M')
                    except Exception:
                        return v or ''
                buf = io.StringIO()
                w = csv.writer(buf, lineterminator='\r\n')
                w.writerow(['체결시간', '코인', '마켓', '종류', '봇', '이유', '산 이유', '거래수량', '거래단가', '거래금액', '수수료', '정산금액', '주문시간'])
                for r in rows:
                    trade = r.get('kind') in ('매수', '매도')
                    w.writerow([when(r.get('time')), r.get('currency'), r.get('market') or '', r.get('kind'), r.get('bot') or '',
                                r.get('reason') or ('직접 주문' if trade else ''), (r.get('buy_reason') or '') if r.get('kind') == '매도' else '',
                                r.get('volume'), r.get('price') or '', r.get('amount') if r.get('amount') is not None else '',
                                r.get('fee'), r.get('settle'), when(r.get('order_time')) if r.get('order_time') else ''])
                name = f"거래내역_{'시뮬레이션' if self.app_mode() == 'sim' else '업비트'}_{datetime.now():%Y%m%d_%H%M}.csv"
                from urllib.parse import quote
                return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                                headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}"})
            except HoldingsUnavailable as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                logger.error(f"거래내역 CSV 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/transfer-history', methods=['GET'])
        def get_transfer_history():
            """업비트 입금·출금 내역 (최근 days일)"""
            try:
                days = max(1, min(int(request.args.get('days', 30)), 186))
                return jsonify({'success': True, **self._fetch_transfers(days)})
            except HoldingsUnavailable as e:
                return jsonify({'success': False, 'error': str(e)})
            except Exception as e:
                logger.error(f"거래내역 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/orders/batch', methods=['POST'])
        def batch_orders():
            """여러 코인 한꺼번에 시장가 주문 (선택 매수/매도). 실제 업비트 계좌

            요청: {'orders': [{'currency': 'BTC', 'side': 'buy', 'amount': 50000},
                              {'currency': 'ETH', 'side': 'sell', 'ratio': 1} 또는 'quantity': 0.1, ...]}
            안전장치:
              - 한 번에 최대 30개, 같은 코인 중복 불가, 일괄 주문은 동시에 하나만
              - 주문 전에 잔고를 한 번 조회: 매수 합계(수수료 포함)가 원화를 넘으면 전체 거절
              - 코인마다 단건 주문과 같은 검사 (원화 마켓 여부, 최소 5,000원, 보유 수량 이내)
              - 업비트 주문 요청 제한(초당 8회) 안쪽으로 0.15초 간격으로 차례로 주문
            반환: {'results': [{'currency', 'side', 'success', 'message'}...]}
            """
            import re

            orders = (request.get_json() or {}).get('orders') or []
            if not isinstance(orders, list) or not orders:
                return jsonify({'success': False, 'error': '주문할 코인을 골라 주십시오.'}), 400
            if len(orders) > 30:
                return jsonify({'success': False, 'error': '한 번에 최대 30개까지 주문할 수 있습니다.'}), 400
            currencies = [str(o.get('currency', '')).upper() for o in orders]
            if len(set(currencies)) != len(currencies):
                return jsonify({'success': False, 'error': '같은 코인이 두 번 들어 있습니다.'}), 400
            if any(not re.fullmatch(r'[A-Z0-9]{1,15}', c) for c in currencies):
                return jsonify({'success': False, 'error': '잘못된 코인이 있습니다.'}), 400
            if any(o.get('side') not in ('buy', 'sell') for o in orders):
                return jsonify({'success': False, 'error': '매수·매도 구분이 잘못됐습니다.'}), 400

            # 계정 잠금: 업비트 모드에서 '사는' 주문만 막음. 파는 주문은 권한이 없어도 그대로 보냄
            buy_blocked = self.bots.live_blocked()
            if buy_blocked and all(o['side'] == 'buy' for o in orders):
                return self._license_refusal()

            try:
                upbit = self._account()
            except HoldingsUnavailable:
                return jsonify({'success': False, 'error': 'API 키가 설정되지 않았습니다.'}), 400
            if not self._batch_lock.acquire(blocking=False):
                return jsonify({'success': False, 'error': '다른 일괄 주문을 처리하고 있습니다. 끝난 뒤 다시 시도해 주십시오.'}), 409
            try:
                import pyupbit
                krw_markets = self._krw_market_set()
                balances = {b['currency']: float(b.get('balance') or 0) for b in upbit.get_balances()}
                krw = balances.get('KRW', 0.0)

                buy_total = 0 if buy_blocked else sum(float(o.get('amount') or 0) for o in orders if o['side'] == 'buy')
                if buy_total * (1 + self._fee()) > krw + 1e-6:
                    return jsonify({'success': False,
                                    'error': f'매수 합계 {buy_total:,.0f}원(수수료 포함)이 주문 가능한 원화 {krw:,.0f}원보다 많습니다.'}), 400

                prices = {}
                sell_markets = [f'KRW-{c}' for o, c in zip(orders, currencies) if o['side'] == 'sell' and f'KRW-{c}' in krw_markets]
                if sell_markets:
                    prices = self._prices_of(sell_markets)

                results = []
                for i, (o, currency) in enumerate(zip(orders, currencies)):
                    symbol = f'KRW-{currency}'
                    side = o['side']
                    item = {'currency': currency, 'side': side, 'success': False}
                    results.append(item)
                    if side == 'buy' and buy_blocked:         # 섞인 주문: 사는 것만 건너뛰고 파는 것은 보냄
                        item['message'] = buy_blocked
                        item['error_code'] = 'LICENSE_REQUIRED'
                        continue
                    if symbol not in krw_markets:
                        item['message'] = '원화 마켓에서 거래할 수 없는 코인입니다'
                        continue
                    with self._sell_lock:
                        if currency in self._selling:
                            item['message'] = '이 코인의 다른 주문을 처리하고 있습니다'
                            continue
                        self._selling.add(currency)
                    try:
                        if i:
                            time.sleep(0.15)  # 업비트 주문 요청 제한 여유
                        if side == 'buy':
                            amount = float(o.get('amount') or 0)
                            if not math.isfinite(amount) or amount < 5000:
                                item['message'] = '최소 주문금액은 5,000원입니다'
                                continue
                            result = upbit.buy_market_order(symbol, amount)
                            item['amount'] = amount
                        else:
                            available = balances.get(currency, 0.0)
                            if float(o.get('ratio') or 0) >= 1:
                                qty = available
                            else:
                                qty = min(float(o.get('quantity') or 0) or available * float(o.get('ratio') or 0), available)
                            price = float(prices.get(symbol) or 0)
                            if qty <= 0:
                                item['message'] = '주문 가능한 수량이 없습니다'
                                continue
                            if qty * price < 5000:
                                item['message'] = f'최소 주문금액 5,000원 미만입니다 (약 {qty * price:,.0f}원)'
                                continue
                            result = upbit.sell_market_order(symbol, qty)
                            item['quantity'] = qty
                        if not result or (isinstance(result, dict) and 'error' in result):
                            item['message'] = (result or {}).get('error', {}).get('message', '거래소 응답 없음') if isinstance(result, dict) else '거래소 응답 없음'
                            continue
                        item['success'] = True
                        item['message'] = '주문 완료'
                        logger.info(f"일괄 {'매수' if side == 'buy' else '매도'} 주문 완료: {symbol} "
                                    f"{item.get('amount', item.get('quantity'))} 주문번호 {result.get('uuid')}")
                    except Exception as e:
                        item['message'] = str(e)
                        logger.error(f"일괄 주문 오류 ({symbol} {side}): {e}")
                    finally:
                        with self._sell_lock:
                            self._selling.discard(currency)

                ok = sum(1 for r in results if r['success'])
                logger.info(f"일괄 주문: {len(results)}건 중 {ok}건 성공")
                return jsonify({'success': True, 'results': results, 'ok': ok, 'total': len(results)})
            except Exception as e:
                logger.error(f"일괄 주문 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': f'일괄 주문 실패: {e}'}), 500
            finally:
                self._batch_lock.release()

        # ── 작은 코인 정리: 5,000원 미만이라 팔 수 없는 코인을 5,300원어치 더 사서 합친 뒤 전부 팖 (사용자가 눌러서) ──
        @self.app.route('/api/holdings/dust', methods=['GET'])
        def holdings_dust():
            """지금 모드 계좌에서 5,000원어치가 안 되는 코인 (원화 마켓에서 거래되는 것만)"""
            try:
                return jsonify({'success': True, **self._dust_list()})
            except HoldingsUnavailable:
                return jsonify({'success': False, 'error': 'API 키가 설정되지 않았습니다.'}), 400
            except Exception as e:
                logger.error(f"작은 코인 목록 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/holdings/dust-sweep', methods=['POST'])
        def holdings_dust_sweep():
            """{coins: [...]} 를 하나씩: 5,300원어치 더 사고 → 잔고에 들어오면 → 그 코인을 전부 팖. 뒤에서 돌고 진행은 GET 으로"""
            body = request.get_json() or {}
            coins = [str(c).upper() for c in (body.get('coins') or []) if re.fullmatch(r'[A-Z0-9]{1,15}', str(c).upper())]
            if not coins:
                return jsonify({'success': False, 'error': '정리할 코인을 골라 주십시오.'}), 400
            if len(coins) > 60:
                return jsonify({'success': False, 'error': '한 번에 60개까지 정리할 수 있습니다.'}), 400
            if not self._batch_lock.acquire(blocking=False):
                return jsonify({'success': False, 'error': '다른 일괄 주문을 처리하고 있습니다. 끝난 뒤 다시 시도해 주십시오.'}), 409
            mode = self.app_mode()
            job = {'id': uuid.uuid4().hex[:10], 'mode': mode, 'total': len(coins), 'done': 0, 'items': [], 'finished': False}
            self._dust_job = job

            def run():
                try:
                    self._dust_sweep(job, coins)
                except Exception as e:
                    logger.error(f"작은 코인 정리 오류: {e}", exc_info=True)
                    job['error'] = str(e)
                finally:
                    job['finished'] = True
                    self._batch_lock.release()
            _shared_pool('background', 2).submit(run)
            return jsonify({'success': True, 'job': job['id']})

        @self.app.route('/api/holdings/dust-sweep', methods=['GET'])
        def holdings_dust_sweep_status():
            job = getattr(self, '_dust_job', None)
            return jsonify({'success': True, 'job': job})

        @self.app.route('/api/markets/buy', methods=['POST'])
        def buy_market():
            """전체코인 분석의 매수 버튼: 원화 금액만큼 시장가 매수 (실제 업비트 계좌)

            사용자가 직접 확인 창에서 누르는 주문이라 봇 모드와 무관.
            요청: {'currency': 'BTC', 'amount': 50000}
            안전장치: 심볼 형식, 원화 마켓 여부(상장폐지 차단), 최소 5,000원,
                      업비트에서 방금 조회한 주문 가능 원화(수수료 포함) 이내, 같은 코인 중복 주문 방지
            """
            import re

            data = request.get_json() or {}
            currency = str(data.get('currency', '')).upper()
            if not re.fullmatch(r'[A-Z0-9]{1,15}', currency):
                return jsonify({'success': False, 'error': '잘못된 코인입니다.'}), 400
            symbol = f'KRW-{currency}'
            try:
                amount = float(data.get('amount', 0) or 0)
            except (TypeError, ValueError):
                amount = 0
            if not math.isfinite(amount) or amount < 5000:
                return jsonify({'success': False, 'error': '최소 주문금액은 5,000원입니다.'}), 400
            refusal = self._license_refusal()              # 업비트 모드에서 사는 주문: 계정 권한 필요 (팔기는 막지 않음)
            if refusal:
                return refusal

            with self._sell_lock:
                if currency in self._selling:
                    return jsonify({'success': False, 'error': '이미 이 코인의 주문을 처리하고 있습니다. 잠시 후 다시 확인해 주십시오.'}), 409
                self._selling.add(currency)
            try:
                try:
                    upbit = self._account()
                except HoldingsUnavailable:
                    return jsonify({'success': False, 'error': 'API 키가 설정되지 않았습니다.'}), 400

                import pyupbit
                if symbol not in self._krw_market_set():
                    return jsonify({'success': False, 'error': '원화 마켓에서 거래할 수 없는 코인입니다.'}), 400

                krw = next((float(b['balance']) for b in upbit.get_balances() if b.get('currency') == 'KRW'), 0.0)
                if amount * (1 + self._fee()) > krw + 1e-6:
                    return jsonify({'success': False,
                                    'error': f'주문 가능한 원화({krw:,.0f}원)보다 많습니다. (수수료 {self._fee() * 100:g}% 포함)'}), 400

                logger.info(f"직접 매수 요청: {symbol} {amount:,.0f}원 (주문 가능 원화 {krw:,.0f}원)")
                result = upbit.buy_market_order(symbol, amount)
                if not result:
                    return jsonify({'success': False, 'error': '매수 실패: 거래소 응답 없음'}), 500
                if isinstance(result, dict) and 'error' in result:
                    return jsonify({'success': False, 'error': f"매수 실패: {result['error'].get('message', '알 수 없는 오류')}"}), 400
                logger.info(f"직접 매수 주문 완료: {symbol} {amount:,.0f}원 주문번호 {result.get('uuid')}")
                return jsonify({'success': True, 'message': f'{currency} {amount:,.0f}원 시장가 매수 주문 완료', 'order': result})
            except Exception as e:
                # 업비트 거절 사유(권한 없음 등)는 utils/pyupbit_patch.py가 한글 메시지로 바꿔 줌
                logger.error(f"직접 매수 오류 ({symbol}): {e}", exc_info=True)
                return jsonify({'success': False, 'error': f'매수 실패: {e}'}), 500
            finally:
                with self._sell_lock:
                    self._selling.discard(currency)

        @self.app.route('/api/krw-balance', methods=['GET'])
        def get_krw_balance():
            """매수 확인 창용 주문 가능 원화"""
            try:
                try:
                    upbit = self._account()
                except HoldingsUnavailable:
                    return jsonify({'success': False, 'error': 'API 키가 설정되지 않았습니다.'})
                krw = next((float(b['balance']) for b in upbit.get_balances() if b.get('currency') == 'KRW'), 0.0)
                return jsonify({'success': True, 'krw': krw})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/holdings/sell', methods=['POST'])
        def sell_holding():
            """보유 코인 시장가 매도 (보유 현황 탭의 매도 버튼)

            계정 잠금(LICENSE_REQUIRED)을 걸지 않습니다: 권한이 없거나 끝나도 가진 코인은 언제든 팔 수 있어야 합니다.

            사용자가 직접 확인 창에서 누르는 주문이라 봇 모드(시뮬레이션/실전)와 무관하게 실제 계좌에서 판다.
            요청: {'currency': 'STEEM', 'quantity': 198.77} 또는 {'currency': 'STEEM', 'ratio': 1}
              ratio=1(100%)이면 업비트에서 방금 조회한 주문 가능 수량 전부를 판다 (소수점 오차로 남는 잔량 방지)
            안전장치: 심볼 형식, 상장폐지 여부, 실제 주문 가능 수량, 최소 주문금액 5,000원, 같은 코인 중복 주문 방지
            """
            import re

            data = request.get_json() or {}
            currency = str(data.get('currency', '')).upper()
            if not re.fullmatch(r'[A-Z0-9]{1,15}', currency):
                return jsonify({'success': False, 'error': '잘못된 코인입니다.'}), 400
            symbol = f'KRW-{currency}'

            # 같은 코인 주문이 처리 중이면 거절 (버튼 두 번 누름 등)
            with self._sell_lock:
                if currency in self._selling:
                    return jsonify({'success': False, 'error': '이미 매도 주문을 처리하고 있습니다. 잠시 후 다시 확인해 주십시오.'}), 409
                self._selling.add(currency)
            try:
                try:
                    upbit = self._account()
                except HoldingsUnavailable:
                    return jsonify({'success': False, 'error': 'API 키가 설정되지 않았습니다.'}), 400

                import pyupbit
                if symbol not in self._krw_market_set():
                    return jsonify({'success': False, 'error': '원화 마켓에서 거래할 수 없는 코인입니다 (상장폐지 등).'}), 400

                # 실제 주문 가능 수량 = 잔고 - 주문에 묶인 수량
                balance = next((b for b in upbit.get_balances() if b.get('currency') == currency), None)
                available = float(balance['balance']) if balance else 0.0
                if available <= 0:
                    return jsonify({'success': False, 'error': '주문 가능한 수량이 없습니다.'}), 400

                if float(data.get('ratio', 0) or 0) >= 1:
                    quantity = available
                else:
                    quantity = float(data.get('quantity', 0) or 0)
                    if quantity <= 0:
                        return jsonify({'success': False, 'error': '매도 수량을 입력해 주십시오.'}), 400
                    if quantity > available * (1 + 1e-9):
                        return jsonify({'success': False,
                                        'error': f'주문 가능 수량({available:g})보다 많습니다.'}), 400
                    quantity = min(quantity, available)

                price = self._prices_of([symbol]).get(symbol) or 0
                if price * quantity < 5000:
                    return jsonify({'success': False,
                                    'error': f'최소 주문금액은 5,000원입니다 (현재 약 {price * quantity:,.0f}원).'}), 400

                logger.info(f"직접 매도 요청: {symbol} 수량 {quantity:g} (주문 가능 {available:g}, 현재가 {price:g})")
                result = upbit.sell_market_order(symbol, quantity)
                if not result:
                    return jsonify({'success': False, 'error': '매도 실패: 거래소 응답 없음'}), 500
                if isinstance(result, dict) and 'error' in result:
                    message = result['error'].get('message', '알 수 없는 오류')
                    return jsonify({'success': False, 'error': f'매도 실패: {message}'}), 400

                logger.info(f"직접 매도 주문 완료: {symbol} 수량 {quantity:g} 주문번호 {result.get('uuid')}")
                return jsonify({'success': True, 'message': f'{currency} {quantity:g}개 시장가 매도 주문 완료',
                                'quantity': quantity, 'order': result})

            except Exception as e:
                # 업비트 거절 사유(권한 없음 등)는 utils/pyupbit_patch.py가 한글 메시지로 바꿔 줌
                logger.error(f"직접 매도 오류 ({symbol}): {e}", exc_info=True)
                return jsonify({'success': False, 'error': f'매도 실패: {e}'}), 500
            finally:
                with self._sell_lock:
                    self._selling.discard(currency)

        # ── 차트에서 걸어 두는 주문 (bot_core/chart_orders.py) ──
        @self.app.route('/api/chart-orders', methods=['GET'])
        def chart_orders_list():
            """지금 모드에서 걸어 둔 주문: 업비트 대기 주문(업비트 앱에서 건 것 포함) · 감시 주문 · 시뮬레이션 지정가"""
            from bot_core import chart_orders as co
            market = self._chart_market(request.args.get('market'))
            if not market:
                return jsonify({'success': False, 'error': '잘못된 코인입니다.'}), 400
            mode = self.app_mode()
            orders, notes = [], []
            if is_live(mode):
                name = self._ex_name(mode)
                try:
                    orders += [co.upbit_row(o, mode) for o in co.upbit_open_orders(self._account(mode), market)]
                except HoldingsUnavailable:
                    notes.append(f'{name} API 키가 없어 {name} 대기 주문을 읽지 못했습니다.')
                except Exception as e:
                    notes.append(f'{name} 대기 주문을 읽지 못했습니다: {e}')
            else:
                orders += self.sim_account.pending_orders(market)
            orders += self.chart_orders[mode].active(market)              # 지켜보는 중 + 확인 필요
            orders.sort(key=lambda o: -float(o.get('price') or 0))
            krw_free = qty_free = qty_total = None       # 주문 패널의 % 단추 (묶인 것을 뺀 양) · 가로선의 '가진 수량의 몇 %' (묶인 것 포함)
            try:
                rows = {b.get('currency'): b for b in (self._account(mode).get_balances() or [])}
                krw_free = float((rows.get('KRW') or {}).get('balance') or 0)
                c = rows.get(market.split('-')[-1]) or {}
                qty_free = float(c.get('balance') or 0)
                qty_total = qty_free + float(c.get('locked') or 0)
            except Exception:
                pass
            return jsonify({'success': True, 'mode': mode, 'price': self._mode_price(market, mode), 'orders': orders,
                            'recent': self.chart_orders[mode].recent(market, 10), 'notes': notes,
                            'krw_free': krw_free, 'qty_free': qty_free, 'qty_total': qty_total})

        @self.app.route('/api/chart-orders', methods=['POST'])
        def chart_orders_place():
            """차트에서 누른 가격으로 걸어 두기. 종류(지정가 · 감시)는 서버가 현재가와 비교해 정함 (화면이 보낸 종류는 쓰지 않음)

            요청: {market, side: 'bid'|'ask', price, krw(매수) | qty(매도), preview: true(미리 보기만)}
            """
            from bot_core import chart_orders as co
            data = request.get_json() or {}
            market = self._chart_market(data.get('market'))
            side = data.get('side')
            if not market or side not in ('bid', 'ask'):
                return jsonify({'success': False, 'error': '잘못된 주문입니다.'}), 400
            try:
                raw = float(data.get('price') or 0)
            except (TypeError, ValueError):
                raw = 0
            now = self._mode_price(market, self.app_mode())
            if raw <= 0 or not now:
                return jsonify({'success': False, 'error': '가격을 알 수 없습니다. 잠시 뒤 다시 시도해 주십시오.'}), 400
            price = co.snap_price(raw, side, co.tick_size(market, raw))
            kind = 'stop'                                   # 모두 예약 (업비트에는 걸지 않음 — 원화 · 코인이 묶이지 않게)
            dir_ = co.direction(price, now)
            mode = self.app_mode()
            info = {'market': market, 'side': side, 'price': price, 'kind': kind, 'dir': dir_, 'now': now, 'mode': mode}
            if data.get('preview'):
                try:
                    acct = self._account(mode)
                    info['krw_free'] = float(acct.get_balance('KRW') or 0)
                    info['qty_free'] = float(acct.get_balance(market) or 0)
                except Exception:
                    info['krw_free'] = info['qty_free'] = None
                return jsonify({'success': True, **info})
            if side == 'bid':
                refusal = self._license_refusal(feature='live_chart_orders')   # 실제 모드에서 사는 예약 주문: 요금제(프로 이상)
                if refusal:
                    return refusal
            cur = market.split('-')[-1]
            with self._sell_lock:
                if cur in self._selling:
                    return jsonify({'success': False, 'error': '이미 이 코인의 주문을 처리하고 있습니다. 잠시 후 다시 확인해 주십시오.'}), 409
                self._selling.add(cur)
            try:
                try:
                    acct = self._account(mode)
                except HoldingsUnavailable:
                    return jsonify({'success': False, 'error': 'API 키가 설정되지 않았습니다.'}), 400
                import pyupbit
                if market not in self._krw_market_set():
                    return jsonify({'success': False, 'error': '원화 마켓에서 거래할 수 없는 코인입니다.'}), 400
                krw = qty = None
                if side == 'bid':
                    try:
                        krw = float(data.get('krw') or 0)
                    except (TypeError, ValueError):
                        krw = 0
                    if not math.isfinite(krw) or krw < co.MIN_ORDER_KRW:
                        return jsonify({'success': False, 'error': '최소 주문금액은 5,000원입니다.'}), 400
                    free = float(acct.get_balance('KRW') or 0)
                    if krw * (1 + co.FEE) > free + 1e-6:
                        return jsonify({'success': False, 'error': f'주문 가능한 원화({free:,.0f}원)보다 많습니다. (수수료 0.05% 포함)'}), 400
                else:
                    try:
                        qty = float(data.get('qty') or 0)
                    except (TypeError, ValueError):
                        qty = 0
                    free = float(acct.get_balance(market) or 0)
                    if qty <= 0:
                        return jsonify({'success': False, 'error': '매도 수량을 넣어 주십시오.'}), 400
                    if qty > free * (1 + 1e-9):
                        return jsonify({'success': False, 'error': f'주문 가능 수량({free:g})보다 많습니다.'}), 400
                    qty = min(qty, free)
                    if qty * price < co.MIN_ORDER_KRW:
                        return jsonify({'success': False, 'error': f'최소 주문금액은 5,000원입니다 (약 {qty * price:,.0f}원).'}), 400

                word = '매수' if side == 'bid' else '매도'
                o = self.chart_orders[mode].add(market, side, price, krw=krw, qty=qty, dir=dir_)
                msg = (f"{cur} {price:,.8g}원 {word} 예약을 걸어 두었습니다. 가격이 {'내려와' if dir_ == 'down' else '올라'} 닿으면 "
                       f"시장가로 {word}합니다(프로그램이 켜져 있어야 합니다).")
                logger.info(f"차트 예약 ({mode}/{dir_}) {market} {side} {price} krw={krw} qty={qty}")
                return jsonify({'success': True, 'message': msg, 'order': o, 'kind': kind, 'price': price})
            except Exception as e:
                logger.error(f"차트 주문 오류 ({market}): {e}", exc_info=True)
                return jsonify({'success': False, 'error': f'주문하지 못했습니다: {e}'}), 500
            finally:
                with self._sell_lock:
                    self._selling.discard(cur)

        @self.app.route('/api/chart-orders/<order_id>/resolve', methods=['POST'])
        def chart_orders_resolve(order_id):
            """확인 필요(꺼져 있던 동안 지나감 · 매수가 너무 지나침)로 멈춘 예약: run(지금 실행) · rewatch(다시 지켜보기) · cancel"""
            action = (request.get_json() or {}).get('action')
            mode = self.app_mode()
            store = self.chart_orders[mode]
            o = store.get(order_id) if re.fullmatch(r'[A-Za-z0-9-]{1,64}', order_id or '') else None
            if not o or o['state'] != 'hold':
                return jsonify({'success': False, 'error': '이미 처리되었거나 없는 예약입니다.'}), 404
            if action == 'cancel':
                store.finish(order_id, 'cancel', '확인 필요에서 취소했습니다.')
                return jsonify({'success': True, 'message': '예약을 취소했습니다.'})
            if action == 'rewatch':
                now = self._mode_price(o['market'], mode)
                if not now:
                    return jsonify({'success': False, 'error': '지금 가격을 알 수 없습니다. 잠시 뒤 다시 시도해 주십시오.'}), 400
                store.rewatch(order_id, now)
                return jsonify({'success': True, 'message': f"지금부터 다시 지켜보며, 가격이 {o['price']:,.8g}원에 다시 닿으면 실행합니다."})
            if action == 'run':
                if o['side'] == 'bid':
                    refusal = self._license_refusal(feature='live_chart_orders')
                    if refusal:
                        return refusal
                r = self.chart_order_watcher.run_now(mode, order_id)
                return jsonify({'success': r['ok'], 'message': r['message'], 'error': None if r['ok'] else r['message']}), (200 if r['ok'] else 400)
            return jsonify({'success': False, 'error': '알 수 없는 동작입니다.'}), 400

        @self.app.route('/api/chart-orders/<order_id>', methods=['DELETE'])
        def chart_orders_cancel(order_id):
            """걸어 둔 주문 취소: source = upbit(업비트 지정가) | watch(감시) | sim(시뮬레이션 지정가)"""
            from bot_core import chart_orders as co
            source = request.args.get('source')
            mode = self.app_mode()
            if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', order_id or ''):
                return jsonify({'success': False, 'error': '잘못된 주문입니다.'}), 400
            try:
                if source == 'watch':
                    done = self.chart_orders[mode].finish(order_id, 'cancel', '취소했습니다.')
                    if not done:
                        return jsonify({'success': False, 'error': '이미 실행되었거나 없는 주문입니다.'}), 404
                    return jsonify({'success': True, 'message': '예약 주문을 취소했습니다.'})
                if source == 'sim' and mode == 'sim':
                    if not self.sim_account.cancel_limit(order_id):
                        return jsonify({'success': False, 'error': '이미 체결되었거나 없는 주문입니다.'}), 404
                    return jsonify({'success': True, 'message': '지정가 주문을 취소했습니다(시뮬레이션).'})
                if source in ('upbit', 'bithumb') and source == mode:
                    after = co.upbit_cancel(self._account(mode), order_id)
                    filled = float(after.get('executed_volume') or 0)
                    msg = f'{self._ex_name(mode)} 지정가 주문을 취소했습니다.'
                    if filled > 0:
                        msg = f'취소하기 전에 {filled:g}개가 이미 체결되었고, 나머지를 취소했습니다.'
                    return jsonify({'success': True, 'message': msg, 'filled': filled, 'state': after.get('state')})
                return jsonify({'success': False, 'error': '지금 모드에서 취소할 수 없는 주문입니다.'}), 400
            except HoldingsUnavailable:
                return jsonify({'success': False, 'error': 'API 키가 설정되지 않았습니다.'}), 400
            except Exception as e:
                logger.error(f"차트 주문 취소 오류 ({order_id}): {e}", exc_info=True)
                return jsonify({'success': False, 'error': f'취소하지 못했습니다: {e}'}), 500

        # ── 대상 코인 목록 API ─────────────────────────────

        @self.app.route('/api/coin-list', methods=['GET'])
        def get_coin_list():
            """업비트 KRW 마켓 코인 목록 조회"""
            try:
                import pyupbit
                tickers_info = pyupbit.get_tickers(fiat="KRW", verbose=True)
                coins = []
                if tickers_info:
                    for t in tickers_info:
                        market = t.get('market', '')
                        if not market.startswith('KRW-'):
                            continue
                        symbol = market.replace('KRW-', '')
                        korean_name = t.get('korean_name', symbol)
                        coins.append({
                            'market': market,
                            'symbol': symbol,
                            'korean_name': korean_name
                        })
                    coins.sort(key=lambda c: c['korean_name'])

                # 설정에서 기존 선택된 코인 복원
                selected = self.config.get('target_coins', [])
                # 이전 단일 symbol 호환
                if not selected and self.config.get('symbol'):
                    selected = [self.config['symbol']]

                return jsonify({'success': True, 'coins': coins, 'selected': selected})
            except Exception as e:
                logger.error(f"코인 목록 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── 시스템 로그 API ─────────────────────────────

        @self.app.route('/api/logs', methods=['GET'])
        def get_logs():
            """시스템 로그 조회 (bot.log 파일 읽기)"""
            try:
                lines = request.args.get('lines', 200, type=int)
                level = request.args.get('level', 'all')

                log_path = self.user_data_dir / 'logs' / 'bot.log'
                if not log_path.exists():
                    return jsonify({'success': True, 'logs': []})

                # 파일 끝에서 N줄 읽기
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    all_lines = f.readlines()

                recent = all_lines[-lines:] if len(all_lines) > lines else all_lines

                logs = []
                for line in recent:
                    line = line.strip()
                    if not line:
                        continue

                    # 로그 레벨 판별
                    log_level = 'info'
                    if ' - ERROR - ' in line or ' - CRITICAL - ' in line:
                        log_level = 'error'
                    elif ' - WARNING - ' in line:
                        log_level = 'warning'

                    # 레벨 필터
                    if level != 'all' and log_level != level:
                        continue

                    logs.append({
                        'text': line,
                        'level': log_level
                    })

                # 최신 로그가 위로 오도록 역순 정렬
                logs.reverse()
                return jsonify({'success': True, 'logs': logs})

            except Exception as e:
                logger.error(f"로그 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── 거래 내역 API ──────────────────────────────

        @self.app.route('/api/trades', methods=['GET'])
        def get_trades():
            """거래 내역 조회 (업비트 전체 완료 주문 - 1회 API 호출)"""
            try:
                import time as _time
                _start = _time.time()

                if self.app_mode() == 'sim':
                    names = self._get_coin_korean_names()
                    trades = []
                    for o in self.sim_account.closed_orders()[:100]:
                        cur = o['market'].replace('KRW-', '')
                        vol, avg = float(o['executed_volume']), float(o['avg_price'])
                        trades.append({'date': o['created_at'], 'market': o['market'], 'currency': cur,
                                       'korean_name': names.get(cur, cur), 'side': o['side'],
                                       'side_label': '매수' if o['side'] == 'bid' else '매도', 'volume': vol,
                                       'avg_price': round(avg), 'trade_amount': round(avg * vol), 'fee': round(float(o['paid_fee']), 2)})
                    return jsonify({'success': True, 'trades': trades})

                api_key = self.config.get('api_key', '')
                api_secret = self.config.get('api_secret', '')

                if not api_key or not api_secret:
                    return jsonify({'success': False, 'error': 'NO_API_KEY'})

                import pyupbit
                import requests
                from concurrent.futures import ThreadPoolExecutor

                upbit = pyupbit.Upbit(api_key, api_secret)

                # 한글명 매핑과 주문 조회를 병렬 실행
                def fetch_orders():
                    data = {
                        'state': 'done',
                        'page': 1,
                        'limit': 100,
                        'order_by': 'desc'
                    }
                    headers = upbit._request_headers(data)
                    resp = requests.get(
                        "https://api.upbit.com/v1/orders",
                        params=data,
                        headers=headers,
                        timeout=10
                    )
                    if resp.status_code != 200:
                        logger.warning(f"주문 조회 실패: HTTP {resp.status_code}")
                        return []
                    orders = resp.json()
                    return orders if isinstance(orders, list) else []

                with nullcontext(_shared_pool('orders', 2)) as executor:
                    orders_future = executor.submit(fetch_orders)
                    names_future = executor.submit(self._get_coin_korean_names)
                    all_orders = orders_future.result()
                    coin_names = names_future.result()

                logger.debug(f"거래내역 조회 완료: {len(all_orders)}건, {_time.time()-_start:.1f}초")

                # 응답 데이터 변환
                trades = []
                for o in all_orders:
                    market = o.get('market', '')
                    currency = market.replace('KRW-', '') if market.startswith('KRW-') else market
                    side = o.get('side', '')
                    volume = float(o.get('executed_volume', 0))
                    avg_price = float(o.get('avg_price', 0))
                    fee = float(o.get('paid_fee', 0))

                    if volume <= 0:
                        continue

                    trade_amount = avg_price * volume

                    trades.append({
                        'date': o.get('created_at', ''),
                        'market': market,
                        'currency': currency,
                        'korean_name': coin_names.get(currency, currency),
                        'side': side,
                        'side_label': '매수' if side == 'bid' else '매도',
                        'volume': volume,
                        'avg_price': round(avg_price),
                        'trade_amount': round(trade_amount),
                        'fee': round(fee, 2)
                    })

                return jsonify({'success': True, 'trades': trades})

            except Exception as e:
                logger.error(f"거래 내역 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── 투자 손익 API ──────────────────────────────

        @self.app.route('/api/pnl/snapshots', methods=['GET'])
        def get_pnl_snapshots():
            """투자 손익 스냅샷 조회"""
            try:
                days = request.args.get('days', 30, type=int)
                snapshots = self._load_pnl_snapshots()

                if not snapshots:
                    return jsonify({'success': True, 'data': [], 'empty': True})

                # 기간 필터
                if days > 0:
                    snapshots = snapshots[-days:]

                # 일일 손익 계산
                result = []
                for i, snap in enumerate(snapshots):
                    prev_value = snapshots[i - 1]['total_value'] if i > 0 else snap['total_value']
                    daily_pnl = snap['total_value'] - prev_value
                    daily_rate = (daily_pnl / prev_value * 100) if prev_value > 0 else 0

                    # 첫 날 대비 누적
                    first_value = snapshots[0]['total_value']
                    cumulative_pnl = snap['total_value'] - first_value
                    cumulative_rate = (cumulative_pnl / first_value * 100) if first_value > 0 else 0

                    result.append({
                        'date': snap['date'],
                        'total_value': snap['total_value'],
                        'daily_pnl': round(daily_pnl),
                        'daily_rate': round(daily_rate, 2),
                        'cumulative_pnl': round(cumulative_pnl),
                        'cumulative_rate': round(cumulative_rate, 2)
                    })

                return jsonify({'success': True, 'data': result})

            except Exception as e:
                logger.error(f"투자 손익 조회 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/pnl/record', methods=['POST'])
        def record_pnl_snapshot():
            """현재 자산 스냅샷 즉시 기록"""
            try:
                try:
                    upbit = self._account()
                except HoldingsUnavailable:
                    return jsonify({'success': False, 'error': 'NO_API_KEY'})

                import pyupbit
                balances = upbit.get_balances()
                if balances is None:
                    return jsonify({'success': False, 'error': '거래소 API 응답 없음'})

                total_value = 0
                coin_tickers = []
                coin_balances_map = {}

                for b in balances:
                    currency = b.get('currency', '')
                    balance = float(b.get('balance', 0)) + float(b.get('locked', 0))
                    if currency == 'KRW':
                        total_value += balance
                        continue
                    if balance <= 0:
                        continue
                    ticker = f'KRW-{currency}'
                    coin_tickers.append(ticker)
                    coin_balances_map[ticker] = balance

                # 시세 일괄 조회
                if coin_tickers:
                    try:
                        all_krw = set(pyupbit.get_tickers(fiat="KRW"))
                        valid = [t for t in coin_tickers if t in all_krw]
                        if valid:
                            if len(valid) == 1:
                                p = pyupbit.get_current_price(valid[0])
                                if p: total_value += p * coin_balances_map[valid[0]]
                            else:
                                prices = pyupbit.get_current_price(valid)
                                if isinstance(prices, dict):
                                    for t, p in prices.items():
                                        if p and t in coin_balances_map:
                                            total_value += p * coin_balances_map[t]
                    except Exception as e:
                        logger.warning(f"스냅샷 시세 조회 실패: {e}")

                from datetime import date
                today = date.today().isoformat()

                snapshots = self._load_pnl_snapshots()
                # 오늘 데이터가 이미 있으면 덮어쓰기
                snapshots = [s for s in snapshots if s['date'] != today]
                snapshots.append({
                    'date': today,
                    'total_value': round(total_value)
                })
                snapshots.sort(key=lambda x: x['date'])
                self._save_pnl_snapshots(snapshots)

                return jsonify({'success': True, 'message': f'기록 완료: {round(total_value):,} KRW'})

            except Exception as e:
                logger.error(f"스냅샷 기록 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── 계정 (권한표) ──
        @self.app.route('/api/license/status', methods=['GET'])
        def license_status():
            """계정 상태 (설정의 계정 화면). 하루가 지났으면 뒤에서 조용히 새로 확인"""
            _shared_pool('background', 2).submit(self.license.maybe_refresh)
            return jsonify({'success': True, 'version': __version__, **self.license.status()})

        @self.app.route('/api/license/login', methods=['POST'])
        def license_login():
            """서버에 로그인해 권한표를 받아 옴. 보내는 것: 이메일·비밀번호·설치 ID·버전 (비밀번호는 저장하지 않음)"""
            body = request.get_json() or {}
            ok, msg = self.license.login(body.get('email', ''), body.get('password', ''))
            return jsonify({'version': __version__, **self.license.status(),
                            'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)

        @self.app.route('/api/license/refresh', methods=['POST'])
        def license_refresh():
            """[다시 확인]: 지금 바로 새 권한표를 받아 옴"""
            ok, msg = self.license.refresh()
            return jsonify({'version': __version__, **self.license.status(),
                            'success': ok, 'message': msg, 'error': None if ok else msg}), (200 if ok else 400)

        @self.app.route('/api/license/logout', methods=['POST'])
        def license_logout():
            """로그아웃: 이 PC에 저장한 권한표·토큰을 지움. 실행 중인 업비트 봇은 멈추지 않고 새로 사기만 멈춤 (팔기·손절은 계속)"""
            ok, msg = self.license.logout()
            return jsonify({'version': __version__, **self.license.status(), 'success': ok, 'message': msg})

        # ── 프로그램 업데이트 (utils/update_check.py) ──
        @self.app.route('/api/check-update', methods=['GET'])
        def check_update():
            """서버의 최신 버전과 지금 버전(gui/__init__.py)을 비교"""
            from utils import update_check
            from utils.server_client import ServerUnreachable
            try:
                info = update_check.fetch_latest()
            except ServerUnreachable as e:
                return jsonify({'success': False, 'error': f'서버에 닿지 않습니다. 잠시 뒤 다시 확인해 주십시오. ({e})'}), 502
            except Exception as e:
                logger.error(f"업데이트 확인 오류: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500
            return jsonify({
                'success': True,
                'current_version': __version__,
                'latest_version': str(info.get('version')),
                'update_available': update_check.is_newer(info.get('version'), __version__),
                'downloadable': bool(update_check.installer_url(info) and info.get('sha256')),
                'needs_login': bool(info.get('auth')) and not self.license.logged_in(),   # 회원 전용 파일인데 로그인 안 함
                'can_install': bool(getattr(sys, 'frozen', False)),      # 소스로 실행 중이면 자동 설치를 하지 않음
                'file_size': int(info.get('size') or 0),
                'notes': str(info.get('notes') or ''),
                'released': str(info.get('released') or ''),
            })

        @self.app.route('/api/download-update', methods=['POST'])
        def download_update():
            """최신 설치 파일을 임시 폴더에 받아 sha256 을 확인한 뒤 실행 (확인값이 다르면 실행하지 않음)"""
            from utils import update_check
            from utils.server_client import ServerUnreachable
            import subprocess
            try:
                if not getattr(sys, 'frozen', False):
                    return jsonify({'success': False, 'error': '소스로 실행 중에는 자동 설치를 하지 않습니다.'}), 400
                info = update_check.fetch_latest()
                if not update_check.is_newer(info.get('version'), __version__):
                    return jsonify({'success': False, 'error': '이미 최신 버전입니다.'}), 400
                url = update_check.installer_url(info)
                if not url:
                    return jsonify({'success': False, 'error': '아직 받을 수 있는 설치 파일이 없습니다.'}), 400
                headers = None
                if info.get('auth'):              # 회원만 받는 설치 파일: 로그인 표를 붙여 받음
                    headers = update_check.auth_headers(url, self.license.refresh_token(), self.license.install_id())
                logger.info(f"업데이트 다운로드 시작: {url}")
                installer_path = update_check.download_verified(url, info.get('sha256'), headers=headers)

                # 봇 중지 (프로그램을 끌 때와 같게: 업데이트 뒤 다시 켜면 켜져 있던 봇을 이어서 켬)
                try:
                    self.bots.stop_all(remember=True)
                    logger.info("업데이트를 위해 봇 중지 완료")
                except Exception as stop_err:
                    logger.warning(f"봇 중지 중 오류 (계속 진행): {stop_err}")

                # 인스톨러 실행 후 봇 프로세스 종료
                subprocess.Popen([str(installer_path)])

                # 잠시 후 프로세스 종료 (응답 전송 후)
                def _exit_after_delay():
                    time.sleep(2)
                    logger.info("업데이트 설치를 위해 봇을 종료합니다.")
                    os._exit(0)
                threading.Thread(target=_exit_after_delay, daemon=True).start()

                return jsonify({'success': True, 'message': '봇을 종료하고 설치 프로그램을 실행합니다.'})

            except ServerUnreachable as e:
                return jsonify({'success': False, 'error': f'서버에 닿지 않습니다. ({e})'}), 502
            except update_check.UpdateError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            except Exception as e:
                logger.error(f"업데이트 다운로드 오류: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

    @staticmethod
    def _chart_market(value) -> Optional[str]:
        """'KRW-BTC' 또는 'BTC' → 'KRW-BTC' (모양이 다르면 None)"""
        cur = str(value or '').upper().replace('KRW-', '')
        return f'KRW-{cur}' if re.fullmatch(r'[A-Z0-9]{1,15}', cur) else None

    def _license_refusal(self, mode: Optional[str] = None, bot_id: Optional[str] = None, feature: Optional[str] = None):
        """업비트 · 빗썸 모드에서 봇 시작·새로 사기가 막혀 있으면 화면에 돌려줄 응답 (error_code 'LICENSE_REQUIRED'), 아니면 None.
        CONFIRM_LIVE 와 같은 방식: 화면은 이 코드를 보고 요금제 안내 창을 띄움. 시뮬레이션 모드·잠금이 꺼진 실행에서는 항상 None
        bot_id: 봇 시작이면 실제 봇 한도도 · feature: 요금제 기능 열쇠(예: live_chart_orders)"""
        m = mode or self.app_mode()
        message = (self.bots.live_blocked(m) or (bot_id and self.bots.limit_blocked(bot_id, m))
                   or (feature and is_live(m) and self.license.feature_block(feature)) or None)
        if not message:
            return None
        return jsonify({'success': False, 'error_code': 'LICENSE_REQUIRED', 'error': message,
                        'license': self.license.status()}), 403

    # ── 업비트 ⇄ 시뮬레이션 모드 ──
    def app_mode(self) -> str:
        """지금 보는 모드: 'upbit' · 'bithumb'(실제 계좌) | 'sim'(가상 계좌)
        처음 실행(설정에 app_mode 가 없을 때)은 시뮬레이션 — 실제 돈이 움직이는 모드는 사용자가 직접 골라야 함"""
        m = self.config.get('app_mode', 'sim')
        return m if m in ('upbit', 'bithumb', 'sim') else 'sim'

    def _account(self, mode: Optional[str] = None):
        """모드의 계좌: 업비트 = pyupbit.Upbit, 빗썸 = BithumbAccount, 시뮬레이션 = VirtualAccount (같은 함수 모양).
        그 거래소 키가 없으면 HoldingsUnavailable"""
        m = mode or self.app_mode()
        if m == 'sim':
            return self.sim_account
        api_key, api_secret = api_keys(m, self.config)
        if not api_key or not api_secret:
            raise HoldingsUnavailable('NO_API_KEY')
        if m == 'bithumb':
            from bot_core.exchanges import BithumbAccount
            return BithumbAccount(api_key, api_secret, fee=fee_rate('bithumb', self.config))
        import pyupbit
        return pyupbit.Upbit(api_key, api_secret)

    # ── 거래소별 시세 묶음 (업비트 · 시뮬레이션 = 이 객체, 빗썸 = gui/exchange_feed.py) ──
    API_BASE = 'https://api.upbit.com'
    EX = 'upbit'
    DAY_START_KST = 9            # 일봉이 바뀌는 시각 (업비트 9시 · 빗썸 0시)

    def feed(self, ex: str = 'upbit'):
        if ex == 'upbit':
            return self
        f = self._feeds.get(ex)
        if f is None:
            from gui.exchange_feed import make_feed
            f = self._feeds[ex] = make_feed(self, ex)
        return f

    def mfeed(self):
        """지금 보는 모드의 거래소 시세 (시뮬레이션 = 업비트)"""
        return self.feed(exchange_of(self.app_mode()))

    def _ex_name(self, mode: Optional[str] = None) -> str:
        return exchange_name(mode or self.app_mode())

    DUST_MIN_KRW = 5000          # 업비트 · 빗썸 최소 주문
    DUST_TOPUP_KRW = 5300        # 더 사는 금액 (합친 뒤 값이 조금 내려도 5,000원을 넘게)

    def _dust_list(self) -> Dict:
        acct = self._account()
        markets = self._krw_market_set()
        balances = acct.get_balances() or []
        rows = [b for b in balances if b.get('currency') not in ('KRW', None)]
        syms = [f"KRW-{b['currency']}" for b in rows if f"KRW-{b['currency']}" in markets]
        prices = self._prices_of(syms) if syms else {}
        names = self._get_coin_korean_names() or {}
        krw = next((float(b.get('balance') or 0) for b in balances if b.get('currency') == 'KRW'), 0.0)
        out = []
        for b in rows:
            sym = f"KRW-{b['currency']}"
            qty = float(b.get('balance') or 0)                       # 주문에 묶이지 않은 수량만
            price = float(prices.get(sym) or 0)
            if sym not in markets or qty <= 0 or not price or qty * price >= self.DUST_MIN_KRW:
                continue
            out.append({'currency': b['currency'], 'name': names.get(sym) or names.get(b['currency']) or b['currency'],
                        'qty': qty, 'price': price, 'value': qty * price})
        out.sort(key=lambda r: -r['value'])
        fee = self._fee()
        return {'coins': out, 'krw': krw, 'topup': self.DUST_TOPUP_KRW, 'need_krw': self.DUST_TOPUP_KRW * (1 + fee),
                'fee_each': round(self.DUST_TOPUP_KRW * fee * 2 + 1, 0), 'mode': self.app_mode()}

    def _dust_sweep(self, job: Dict, coins: List[str]):
        """코인마다 차례로: (5,000원 미만이면) 5,300원어치 사기 → 잔고에 들어올 때까지 잠깐 기다림 → 가진 것 전부 팔기"""
        acct = self._account(job['mode'])
        fee = self._fee(job['mode'])

        def free_of(cur):
            for b in acct.get_balances():
                if b.get('currency') == cur:
                    return float(b.get('balance') or 0)
            return 0.0

        def failed(res):
            return (not res) or (isinstance(res, dict) and 'error' in res)

        for i, cur in enumerate(coins):
            sym = f'KRW-{cur}'
            item = {'currency': cur, 'ok': False, 'message': ''}
            job['items'].append(item)
            job['current'] = {'currency': cur, 'step': '확인 중'}
            with self._sell_lock:
                if cur in self._selling:
                    item['message'] = '이 코인의 다른 주문을 처리하고 있습니다'
                    job['done'] += 1
                    continue
                self._selling.add(cur)
            try:
                if i:
                    time.sleep(0.3)
                price = float(self._prices_of([sym], job['mode']).get(sym) or 0)
                qty = free_of(cur)
                if qty <= 0 or not price:
                    item['message'] = '팔 수 있는 수량이 없습니다'
                    continue
                if qty * price < self.DUST_MIN_KRW:
                    krw = free_of('KRW')
                    if krw < self.DUST_TOPUP_KRW * (1 + fee):
                        item['message'] = f'원화가 모자랍니다 ({self.DUST_TOPUP_KRW:,}원 필요)'
                        continue
                    job['current'] = {'currency': cur, 'step': f'{self.DUST_TOPUP_KRW:,}원어치 더 사는 중'}
                    res = acct.buy_market_order(sym, self.DUST_TOPUP_KRW)
                    if failed(res):
                        item['message'] = '더 사기 실패: ' + (str((res or {}).get('error', {}).get('message', '거래소 응답 없음')) if isinstance(res, dict) else '거래소 응답 없음')
                        continue
                    item['bought'] = self.DUST_TOPUP_KRW
                    for _ in range(20):                          # 시장가 체결이 잔고에 보일 때까지 (최대 약 6초)
                        time.sleep(0.3)
                        if free_of(cur) > qty:
                            break
                    qty = free_of(cur)
                    if qty * price < self.DUST_MIN_KRW * 0.98:
                        item['message'] = '더 산 수량이 아직 잔고에 보이지 않습니다 — 잠시 뒤 다시 정리해 주십시오'
                        continue
                    time.sleep(0.2)
                job['current'] = {'currency': cur, 'step': '전부 파는 중'}
                res = acct.sell_market_order(sym, qty)
                if failed(res):
                    item['message'] = '팔기 실패: ' + (str((res or {}).get('error', {}).get('message', '거래소 응답 없음')) if isinstance(res, dict) else '거래소 응답 없음')
                    continue
                item.update({'ok': True, 'sold_qty': qty, 'value': qty * price,
                             'message': f"{'더 사서 합친 뒤 ' if item.get('bought') else ''}전부 팔았습니다 (약 {qty * price:,.0f}원)"})
                logger.info(f"작은 코인 정리: {sym} {item['message']}")
            except Exception as e:
                item['message'] = str(e)
                logger.error(f"작은 코인 정리 오류 ({sym}): {e}")
            finally:
                job['done'] += 1
                job['current'] = None
                with self._sell_lock:
                    self._selling.discard(cur)

    def _fee(self, mode: Optional[str] = None) -> float:
        return fee_rate(mode or self.app_mode(), self.config)

    def _exchange_info(self, mode: Optional[str] = None) -> Dict:
        """화면용: 거래소 이름 · 수수료 · 최소 주문 · 거래 화면 주소"""
        m = mode or self.app_mode()
        info = EXCHANGES[exchange_of(m)]
        return {'id': exchange_of(m), 'name': info['name'], 'fee': self._fee(m), 'min_order': info['min_order'],
                'trade_url': info['trade_url'], 'live': is_live(m), 'day_start_kst': info['day_start_kst']}

    def _krw_market_set(self, mode: Optional[str] = None) -> set:
        """이 모드 거래소의 원화 마켓 (10분 캐시)"""
        return {m['market'] for m in self.feed(exchange_of(mode or self.app_mode()))._get_krw_markets()}

    def _prices_of(self, markets, mode: Optional[str] = None) -> Dict[str, float]:
        """이 모드 거래소의 현재가 (원화 전체 시세 2초 캐시)"""
        t = self.feed(exchange_of(mode or self.app_mode()))._krw_tickers()
        return {m: float(t[m]['trade_price']) for m in markets if m in t and t[m].get('trade_price')}

    def _mode_price(self, market: str, mode: Optional[str] = None) -> Optional[float]:
        """차트 예약 · 주문 패널의 현재가: 빗썸 모드는 빗썸 시세, 나머지는 업비트 시세(가상 계좌 체결 가격과 같게)"""
        if exchange_of(mode or self.app_mode()) == 'bithumb':
            try:
                return self._prices_of([market], 'bithumb').get(market)
            except Exception:
                return None
        return self._sim_price(market)

    def _pnl_hist(self, mode: Optional[str] = None):
        m = mode or self.app_mode()
        return {'sim': self.pnl_history_sim, 'bithumb': self.pnl_history_bithumb}.get(m, self.pnl_history)

    def _period_day_key(self) -> str:
        """기간 시세 캐시의 '오늘': 이 거래소 일봉이 바뀌는 시각 기준 (업비트 = UTC 날짜, 빗썸 = KST 날짜)"""
        from datetime import timezone
        return (datetime.now(timezone.utc) + timedelta(hours=9 - self.DAY_START_KST)).date().isoformat()

    def _test_bithumb(self, api_key: str, api_secret: str) -> Dict:
        """빗썸 연결 확인: 잔고 + 이 계좌의 실제 수수료 (처음이면 설정 수수료로 저장)"""
        from bot_core.exchanges import BithumbAccount
        acct = BithumbAccount(api_key, api_secret)
        balances = acct.get_balances()
        krw = next((float(b.get('balance') or 0) for b in balances if b.get('currency') == 'KRW'), 0.0)
        note = ''
        try:
            fee = float((acct.order_chance('KRW-BTC') or {}).get('bid_fee'))
            note = f' · 내 수수료 {fee * 100:g}%'
            if self.config.get('bithumb_fee_pct') in (None, ''):
                self.config['bithumb_fee_pct'] = round(fee * 100, 4)
                self._save_config_internal(self.config)
                note += ' (설정에 넣었습니다)'
        except Exception:
            pass
        return {'success': True, 'message': f'연결 성공! KRW 잔고: {krw:,.0f}원{note}', 'fee_pct': self.config.get('bithumb_fee_pct')}

    PRICE_TTL = 5            # 가상 계좌 체결 가격: 이보다 오래된 시세면 새로 받음 (초)
    PRICE_MIN_GAP = 3        # 전체 시세를 새로 받는 최소 간격 (초) — 그사이 요청은 조금 전 시세를 씀
    PRICE_BACKOFF = 15       # 업비트가 요청 한도(429)를 알리면 이만큼 쉼 (초)

    def _sim_price(self, market: str) -> Optional[float]:
        """가상 계좌 체결·평가 가격: 방금 받은 시세(5초 안) → 없으면 원화 코인 전체 시세를 한 번에 새로 받음
        (코인마다 따로 묻지 않음: 수백 개를 가져도 요청은 100개당 하나. 새로 못 받으면 조금 전 시세)"""
        hit = self._price_cache.get(market)
        if hit and time.time() - hit[0] < self.PRICE_TTL:
            return hit[1]
        self._refresh_prices()
        hit = self._price_cache.get(market)
        return hit[1] if hit else None

    def _refresh_prices(self):
        """원화 코인 전체 현재가를 100개씩 묶어 받아 _price_cache 에 (최소 간격·429 쉬기, 동시에 한 곳만)"""
        import requests
        lock = self.__dict__.setdefault('_price_refresh_lock', threading.Lock())
        with lock:
            now = time.time()
            if now < getattr(self, '_price_next_fetch', 0):
                return
            self._price_next_fetch = now + self.PRICE_MIN_GAP
        try:
            codes = [m['market'] for m in self._get_krw_markets()]
            for i in range(0, len(codes), 100):
                self._pacer.wait('ticker')
                resp = requests.get('https://api.upbit.com/v1/ticker', params={'markets': ','.join(codes[i:i + 100])}, timeout=5)
                self._pacer.note('ticker', resp.status_code, getattr(resp, 'headers', None))
                if resp.status_code == 429:
                    self._price_next_fetch = time.time() + self.PRICE_BACKOFF
                    raise RuntimeError('업비트 요청 한도(429) — 잠시 쉬고 다시 받습니다')
                resp.raise_for_status()
                got = time.time()
                for t in resp.json():
                    if t.get('trade_price'):
                        self._price_cache[t['market']] = (got, float(t['trade_price']))
        except Exception as e:
            if time.time() - getattr(self, '_price_warned_at', 0) > 60:      # 같은 경고는 1분에 한 번만
                self._price_warned_at = time.time()
                logger.warning(f"가상 계좌 시세 조회 실패 (조금 전 시세로 계산): {e}")

    def _fetch_holdings(self, mode: Optional[str] = None) -> Dict:
        """보유 자산 계산 (/api/holdings 응답과 1분 수익률 기록이 함께 사용) — 업비트 계좌 또는 가상 계좌

        Raises:
            HoldingsUnavailable: (업비트 모드) API 키가 없거나 거래소 응답이 없을 때
        """
        m_ = mode or self.app_mode()
        feed = self.feed(exchange_of(m_))                 # 빗썸 모드 = 빗썸 시세 · 유의 종목
        upbit = self._account(m_)
        balances = upbit.get_balances()

        if balances is None:
            raise HoldingsUnavailable('거래소 API 응답이 없습니다.')

        # 코인 한글명 매핑 (빗썸에만 있는 코인은 빗썸 이름으로)
        coin_names = self._get_coin_korean_names()
        if feed is not self:
            try:
                coin_names = {**coin_names, **{x['market'][4:]: x['korean_name'] for x in feed._get_krw_markets()}}
            except Exception:
                pass

        # KRW 분리 및 코인 목록 추출
        coin_balances = []
        krw_balance = 0

        for b in balances:
            currency = b.get('currency', '')
            balance = float(b.get('balance', 0))
            locked = float(b.get('locked', 0))
            total_balance = balance + locked

            if currency == 'KRW':
                krw_balance = total_balance
                continue

            if total_balance <= 0:
                continue

            coin_balances.append({
                'currency': currency,
                'balance': total_balance,
                'locked': locked,
                'avg_buy_price': float(b.get('avg_buy_price', 0))
            })

        # 현재가 · 24시간 거래대금 · 등락률: 원화 마켓 전체 시세 한 번(2초 캐시)에서 — 업비트에 따로 묻지 않음
        try:
            tickers = feed._krw_tickers()
        except Exception as e:
            logger.warning(f"시세 조회 실패: {e}")
            tickers = {}
        try:                                              # 원화 마켓에 없으면 상장폐지로 봄 (마켓 목록 10분 캐시)
            all_krw_tickers = {m['market'] for m in feed._get_krw_markets()}
        except Exception:
            all_krw_tickers = set(tickers)
        valid_tickers = [f'KRW-{c["currency"]}' for c in coin_balances if f'KRW-{c["currency"]}' in tickers]
        current_prices = {m: tickers[m].get('trade_price') for m in valid_tickers if tickers[m].get('trade_price') is not None}
        trade_values = {m: tickers[m].get('acc_trade_price_24h') for m in valid_tickers}
        change_rates = {m: tickers[m]['signed_change_rate'] * 100 for m in valid_tickers if tickers[m].get('signed_change_rate') is not None}
        today_lows = {m: tickers[m].get('low_price') for m in valid_tickers}
        prev_closes = {m: tickers[m].get('prev_closing_price') for m in valid_tickers}

        market_events = feed._get_market_events()
        notices = feed._get_upbit_notices()

        assets = []
        total_buy_amount = 0
        total_current_value = 0

        for c in coin_balances:
            currency = c['currency']
            total_balance = c['balance']
            avg_buy_price = c['avg_buy_price']
            ticker = f'KRW-{currency}'

            # 상폐 여부 확인: KRW 마켓에 없으면 상폐
            is_delisted = ticker not in all_krw_tickers
            current_price = current_prices.get(ticker)

            if current_price is None:
                # 상폐 코인은 현재가 0원, 그 외는 매수평균가로 대체
                current_price = 0 if is_delisted else avg_buy_price

            buy_amount = avg_buy_price * total_balance
            current_value = current_price * total_balance
            pnl = current_value - buy_amount
            pnl_rate = ((current_price - avg_buy_price) / avg_buy_price * 100) if avg_buy_price > 0 else 0

            # 업비트 '보유자산' 화면과 같게: 상장폐지(KRW 마켓 없음) 코인은 합계에서 제외
            if not is_delisted:
                total_buy_amount += buy_amount
                total_current_value += current_value

            assets.append({
                'currency': currency,
                'korean_name': coin_names.get(currency, currency),
                'balance': total_balance,
                'locked': c['locked'],
                'avg_buy_price': avg_buy_price,
                'current_price': current_price,
                'buy_amount': buy_amount,
                'current_value': current_value,
                'pnl': pnl,
                'pnl_rate': pnl_rate,
                'trade_value_24h': trade_values.get(ticker),
                'change_rate': change_rates.get(ticker),
                'today_low': today_lows.get(ticker),
                'prev_close': prev_closes.get(ticker),
                'warning': ticker in market_events['warning'],
                'cautions': market_events['caution'].get(ticker, []),
                'notices': notices['by_symbol'].get(currency, []),
                'is_delisted': is_delisted
            })

        # 매수일: 지금 보유분을 처음 산 때 · 마지막으로 산 때 (거래 내역 체결 + 봇 거래)
        try:
            times = self._buy_times(m_, [a['currency'] for a in assets])
        except Exception as e:
            logger.warning(f"매수일 계산 실패: {e}")
            times = {}
        for a in assets:
            t = times.get(a['currency']) or {}
            a['first_buy_ts'], a['last_buy_ts'], a['buy_count'] = t.get('first'), t.get('last'), t.get('n')

        # 평가금액 기준 내림차순 정렬
        assets.sort(key=lambda x: x['current_value'], reverse=True)

        return {
            'krw_balance': krw_balance,
            'total_buy_amount': total_buy_amount,
            'total_value': total_current_value,
            'delisted_buy_amount': sum(a['buy_amount'] for a in assets if a['is_delisted']),
            'assets': assets,
            'global_notices': notices['global'],
        }

    def _buy_times(self, mode: str, currencies: list) -> Dict:
        """코인마다 지금 보유분을 처음 · 마지막으로 산 때. 실제 계좌 모드는 최근 7일 체결을 10분마다 뒤에서 받아 둠
        (거래 내역 탭을 열지 않아도 직접 산 것이 보이게 · 봇 매매는 봇 기록으로 바로 보임)"""
        store = self.trade_stores.get(mode)
        if store is None:
            return {}
        if is_live(mode) and time.time() - self.__dict__.get('_buytime_sync_at', 0) > 600:
            self._buytime_sync_at = time.time()

            def sync():
                try:
                    if mode == 'upbit':
                        self._sync_upbit(7)
                    elif mode == 'bithumb':
                        self._sync_bithumb(7)
                except Exception as e:
                    logger.warning(f"매수일용 체결 받기 실패: {e}")
            _shared_pool('background', 2).submit(sync)          # 새 스레드를 만들지 않음 (gui/watchdog.py)
        since = None
        if mode == 'sim':
            try:                                          # 시뮬레이션 계좌를 새로 만든 때부터 (그 전 계좌의 매매는 빼고)
                since = int(datetime.fromisoformat(str(self.sim_account.state.get('created_at'))).timestamp())
            except Exception:
                since = None
        return store.buy_times(currencies, since)

    # 업비트 주의 종목 사유 (market/all?isDetails=true 의 market_event.caution)
    CAUTION_REASONS = {
        'PRICE_FLUCTUATIONS': '가격 급등락',
        'TRADING_VOLUME_SOARING': '거래량 급등',
        'DEPOSIT_AMOUNT_SOARING': '입금량 급등',
        'GLOBAL_PRICE_DIFFERENCES': '글로벌 시세 차이',
        'CONCENTRATION_OF_SMALL_ACCOUNTS': '소수 계정 거래 집중',
    }

    def _get_market_events(self) -> Dict:
        """업비트 투자유의·주의 종목 (10분 캐시)

        반환: {'warning': {'KRW-EGLD', ...}, 'caution': {'KRW-STEEM': ['입금량 급등'], ...}}
        조회에 실패하면 빈 값 (배지만 안 보이고 나머지는 정상 동작)
        """
        cached = getattr(self, '_market_events_cache', None)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        events = {'warning': set(), 'caution': {}}
        lock = self.__dict__.setdefault('_market_events_lock', threading.Lock())
        if not lock.acquire(blocking=False):             # 다른 곳에서 받는 중이면 지난 값으로 (한꺼번에 몰리지 않게)
            return cached[1] if cached else events
        try:
            import requests
            self._pacer.wait('market')
            resp = requests.get(f'{self.API_BASE}/v1/market/all', params={'isDetails': 'true'}, timeout=5)
            self._pacer.note('market', resp.status_code, getattr(resp, 'headers', None))
            if resp.status_code == 429 and cached:           # 지난 값으로 계속, 1분 뒤 다시
                self._market_events_cache = (cached[0] + 60, cached[1])
                return cached[1]
            resp.raise_for_status()
            for m in resp.json():
                event = m.get('market_event') or {}
                if event.get('warning') or m.get('market_warning') == 'CAUTION':     # 빗썸은 market_warning 칸
                    events['warning'].add(m['market'])
                reasons = [self.CAUTION_REASONS.get(k, k) for k, v in (event.get('caution') or {}).items() if v]
                if reasons:
                    events['caution'][m['market']] = reasons
            self._market_events_cache = (time.time(), events)
        except Exception as e:
            logger.warning(f"유의·주의 종목 조회 실패: {e}")
            if cached:
                return cached[1]
        finally:
            lock.release()
        return events

    def _fetch_period_lows(self, markets: list) -> Dict:
        """코인별 기간 최저가 (오늘은 제외 · 하루 동안 캐시)

        반환: {market: {'d1': 어제, 'w1': 지난 6일, 'm1': 지난 29일, 'h6': 지난 26주, 'y1': 지난 52주,
                        'day_closes': [...], 'week_closes': [...]}}
        오늘 최저가는 시세(ticker)의 low_price로 화면에서 합쳐 각 기간 최저가를 만든다.
        업비트 일봉은 UTC 0시(= KST 9시)에 바뀌므로 UTC 날짜가 같으면 캐시를 그대로 쓴다.
        코인마다 일봉 31개 + 주봉 52개를 조회한다. 요청 하나가 0.3초 이상 걸리므로 여러 개를
        동시에 보내되, 캔들 API 요청 수 제한(초당 10회)을 넘지 않도록 시작 간격을 0.11초 이상 둔다.
        (화면 요청과 백그라운드 미리 받기가 겹쳐도 넘지 않도록 간격 관리는 인스턴스 전체에서 공유)
        """
        import requests
        from concurrent.futures import ThreadPoolExecutor
        from datetime import timezone

        # 캐시 키에 항목 버전을 붙여, 항목이 늘면(h6 추가 등) 그날 캐시도 새로 받게 함
        day_key = self._period_day_key() + '/v8'   # v8: 아주 긴 종가 칸(199일, EMA 120 용) 추가
        cache = self._period_low_cache
        lows = {m: cache[m][1] for m in markets if m in cache and cache[m][0] == day_key}
        todo = [m for m in markets if m not in lows]
        if not todo:
            return lows

        # 다른 요청(화면·백그라운드 미리 받기)이 이미 받고 있는 코인은 다시 요청하지 않고 기다림
        with self._period_low_lock:
            waits = {m: self._period_low_inflight[m] for m in todo if m in self._period_low_inflight}
            todo = [m for m in todo if m not in waits]
            for m in todo:
                self._period_low_inflight[m] = threading.Event()

        get_candles = self._get_candles

        def min_low(candles):
            values = [c['low_price'] for c in candles if c.get('low_price')]
            return min(values) if values else None

        def fetch(market):
            try:
                days = get_candles('days', market, 200)    # 2026-10: 199일 (긴 기간 칸 120 + EMA 120 용 199, 요청 수는 그대로 1번)
                weeks = get_candles('weeks', market, 104)  # 2년 (조건 검색의 2년 최고가용)
                past = days[1:]  # 오늘(진행 중) 제외
                return market, {
                    'd1': min_low(past[:1]),
                    'w1': min_low(past[:6]),
                    'm1': min_low(past[:29]),
                    'h6': min_low(weeks[:26]),
                    'y1': min_low(weeks[:52]),
                    # 조건 검색용: 주별 고가 최근 2년 (오래된 순, 이번 주 포함) → 화면에서 1년·2년 최고가 계산
                    'week_highs': [c['high_price'] for c in reversed(weeks) if c.get('high_price')],
                    # 추이 그래프용 종가 (오래된 순, 진행 중인 봉 제외 → 화면에서 현재가를 끝에 붙임)
                    'day_closes': [c['trade_price'] for c in reversed(past[:30])],
                    # 조건 검색용: 일별 고가·거래대금 (오래된 순, 오늘 제외)
                    'day_highs': [c['high_price'] for c in reversed(past[:30])],
                    'day_values': [c.get('candle_acc_trade_price') for c in reversed(past[:30])],
                    'week_closes': [c['trade_price'] for c in reversed(weeks[1:104])],
                    # 일별 저가 (지난 30일) · 긴 기간 120일 (오래된 순, 오늘 제외) — 기존 30일 칸은 그대로 (RSI 등 값이 바뀌지 않게)
                    'day_lows': [c['low_price'] for c in reversed(past[:30])],
                    **self._long_fields(past),
                }
            except Exception as e:
                logger.warning(f"기간 최저가 조회 실패 ({market}): {e}")
                return market, None

        def save():
            with self._period_low_lock:
                data = {m: list(v) for m, v in cache.items() if v[0] == day_key}  # 지난 날짜는 버림
            self._save_json(self._period_low_path, data)

        try:
            with nullcontext(_shared_pool('period_lows', 6)) as pool:
                for i, (market, result) in enumerate(pool.map(fetch, todo), 1):
                    with self._period_low_lock:
                        if result:
                            cache[market] = (day_key, result)
                            lows[market] = result
                        event = self._period_low_inflight.pop(market, None)
                    if event:
                        event.set()
                    if i % 40 == 0:
                        save()  # 도중에 꺼져도 받은 만큼은 남게
        finally:
            with self._period_low_lock:
                for m in todo:
                    event = self._period_low_inflight.pop(m, None)
                    if event:
                        event.set()
        if todo:
            save()

        for market, event in waits.items():
            event.wait(timeout=120)
            hit = cache.get(market)
            if hit and hit[0] == day_key:
                lows[market] = hit[1]
        return lows

    def _get_minute_bars(self, market: str, days: int, on_progress=None) -> list:
        """1분봉 days일치 (오래된 순 [{t,o,h,l,c,v}], t = UTC 'YYYY-MM-DDTHH:MM:SS'), 거래 없던 1분은 직전 가격·거래대금 0으로 채움.
        요청 간격은 _get_candles 와 같이 인스턴스 전체로 지킴. 받은 날짜별로 저장해 같은 날 다시 받지 않음."""
        import requests
        from datetime import timezone
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        cache_dir = self.user_data_dir / 'data' / 'verify_cache'
        cache = cache_dir / f"{market}_{days}d_{end:%Y%m%d%H}.json"
        start = end - timedelta(days=days)
        # 같은 시간대에 받아 둔 같은 코인(같거나 더 긴 기간)이 있으면 잘라 씀
        for d in sorted({days, 7, 14, 30, 60, 90}):
            if d < days:
                continue
            f = cache_dir / f"{market}_{d}d_{end:%Y%m%d%H}.json"
            try:
                bars = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            cut = start.strftime('%Y-%m-%dT%H:%M:%S')
            return [b for b in bars if b['t'] >= cut]
        session = requests.Session()                  # 연결을 다시 써서 요청마다 새로 접속하지 않음
        total_pages = days * 1440 // 200 + 1
        rows, to, page = {}, end, 0
        while to > start:
            for attempt in range(4):
                self._pacer.wait('candles')
                resp = session.get('https://api.upbit.com/v1/candles/minutes/1',
                                    params={'market': market, 'count': 200, 'to': to.strftime('%Y-%m-%dT%H:%M:%SZ')}, timeout=10)
                self._pacer.note('candles', resp.status_code, getattr(resp, 'headers', None))
                if resp.status_code == 429 and attempt < 3:
                    continue
                resp.raise_for_status()
                break
            chunk = resp.json()
            page += 1
            if on_progress:
                on_progress(min(page / total_pages, 1.0))
            if not chunk:
                break
            for c in chunk:
                rows[c['candle_date_time_utc']] = {'t': c['candle_date_time_utc'], 'o': c['opening_price'], 'h': c['high_price'],
                                                   'l': c['low_price'], 'c': c['trade_price'], 'v': c['candle_acc_trade_price']}
            oldest = datetime.fromisoformat(chunk[-1]['candle_date_time_utc']).replace(tzinfo=timezone.utc)
            if oldest >= to:
                break
            to = oldest
        keys = sorted(k for k in rows if k >= start.strftime('%Y-%m-%dT%H:%M:%S'))
        bars = []
        for k in keys:
            b = rows[k]
            if bars:
                prev = datetime.fromisoformat(bars[-1]['t'])
                gap = int((datetime.fromisoformat(k) - prev).total_seconds() // 60)
                for m in range(1, min(gap, 1440)):
                    c = bars[-1]['c']
                    bars.append({'t': (prev + timedelta(minutes=m)).strftime('%Y-%m-%dT%H:%M:%S'), 'o': c, 'h': c, 'l': c, 'c': c, 'v': 0.0})
            bars.append(b)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            for old in cache.parent.glob(f'{market}_*d_*.json'):
                if old.name != cache.name and not old.name.endswith(f'_{end:%Y%m%d%H}.json'):
                    old.unlink()                    # 지난 시간대 것은 지움
            cache.write_text(json.dumps(bars), encoding='utf-8')
        except Exception:
            pass
        return bars

    # 봇 백테스트 봉 단위별 최대 기간(일) — 1분봉 14일 ≈ 100번 요청(10초 남짓)
    BT_LIMIT_DAYS = {'1': 14, '5': 60, '10': 90, '15': 180, '30': 270, '60': 365, '240': 730, 'day': 1095, 'week': 1095, 'month': 3650}

    def _custom_conditions(self) -> List[Dict]:
        from gui.condition_llm import sanitize_saved
        try:
            path = self.user_data_dir / 'data' / 'custom_conditions.json'
            return sanitize_saved(json.loads(path.read_text(encoding='utf-8')))
        except Exception:
            return []

    BT_MULTI_MAX = 20            # 백테스트 메뉴: 한 번에 돌리는 코인 수

    def _bt_markets(self, coins, most: int = 1):
        """['btc', 'KRW-ETH'] → ['KRW-BTC', 'KRW-ETH'] (중복 빼고 most 개까지) · 틀리면 (None, 까닭)"""
        out = []
        for c in coins if isinstance(coins, (list, tuple)) else []:
            m = str(c or '').strip().upper()
            m = m if m.startswith('KRW-') else f'KRW-{m}'
            if not re.fullmatch(r'KRW-[A-Z0-9]{1,15}', m):
                return None, '코인 또는 봉 단위가 올바르지 않습니다'
            if m not in out:
                out.append(m)
        if not out:
            return None, '백테스트할 코인을 골라 주십시오'
        if len(out) > most:
            return None, f'코인은 한 번에 {most}개까지 돌릴 수 있습니다'
        return out, None

    def _bt_args(self, body: Dict, bot: Dict):
        """봉 · 기간 · 시작 금액 · 수수료 · 미끄럼 검사 → ((unit, start, end, krw, costs), None) 또는 (None, 까닭)
        costs = {'fee': 비율, 'slip': 비율} — 화면에서 %로 받음 (fee_pct · slip_pct, 없으면 기본값)"""
        from bot_core import backtest as bt
        unit = str(body.get('unit') or '60')
        if unit not in self.BT_LIMIT_DAYS:
            return None, '코인 또는 봉 단위가 올바르지 않습니다'
        try:
            kst = timezone(timedelta(hours=9))
            start = int(datetime.strptime(str(body.get('start')), '%Y-%m-%d').replace(tzinfo=kst).timestamp())
            end = int(datetime.strptime(str(body.get('end')), '%Y-%m-%d').replace(tzinfo=kst).timestamp()) + 86400
        except ValueError:
            return None, '날짜를 YYYY-MM-DD 로 골라 주십시오'
        end = min(end, int(time.time()))
        if end - start < 86400 / 4:
            return None, '기간이 너무 짧습니다'
        p = bt.plan(bot, self._custom_conditions())
        limit = self.BT_LIMIT_DAYS[unit]
        short = min((bt.TF_UNIT[tf] for tf in p['tfs'] if tf in bt.TF_UNIT), key=lambda u: self.BT_LIMIT_DAYS.get(u, 9999), default=None)
        if short and self.BT_LIMIT_DAYS.get(short, limit) < limit:
            limit = self.BT_LIMIT_DAYS[short]
        if p['minute']:
            limit = min(limit, bt.MINUTE_MAX_DAYS)
        if end - start > limit * 86400 + 86400:
            why = ' (급등 일부 팔기를 1분봉으로 돌리는 봇)' if p['minute'] else ' (짧은 계산 봉을 쓰는 규칙 포함)' if short and self.BT_LIMIT_DAYS.get(short, 0) < self.BT_LIMIT_DAYS[unit] else ''
            return None, f"이 규칙{why}은 최대 {limit}일까지 백테스트할 수 있습니다"
        if not p['buy']:
            return None, '백테스트로 돌릴 수 있는 매수 조건이 없습니다'
        try:
            krw = float(body.get('krw') or 0) or None
        except (TypeError, ValueError):
            krw = None
        costs = {}
        for key, name in (('fee_pct', 'fee'), ('slip_pct', 'slip')):
            if body.get(key) is None or body.get(key) == '':
                continue
            try:
                v = float(body.get(key))
            except (TypeError, ValueError):
                return None, '수수료 · 미끄럼은 숫자(%)로 넣어 주십시오'
            if not 0 <= v <= 5:
                return None, '수수료 · 미끄럼은 0 ~ 5% 사이로 넣어 주십시오'
            costs[name] = v / 100
        if 'fee' not in costs:                       # 수수료를 안 보냈으면 이 모드 거래소 수수료 (빗썸 기본 0.25% · 설정 값, 2026-10-15)
            costs['fee'] = fee_rate(self.app_mode(), self.config)
        return (unit, start, end, krw, costs), None

    @staticmethod
    def _bt_spec_bot(spec) -> Dict:
        """백테스트 메뉴의 규칙(저장하지 않음) → 백테스트가 읽는 봇 모양 {name, trade_rules, risk_config}"""
        from bot_core.bot_manager import DEFAULT_RISK
        spec = spec if isinstance(spec, dict) else {}
        raw = spec.get('trade_rules') if isinstance(spec.get('trade_rules'), dict) else {}
        rules = {}
        for k in ('target', 'buy', 'sell'):
            rules[k] = [{'id': str(r['id']), 'params': r.get('params') if isinstance(r.get('params'), dict) else {},
                         **{x: r[x] for x in ('portion', 'dips', 'sell_mode') if x in r}}      # 급등 일부 팔기 · 다시 사기 설정
                        for r in (raw.get(k) if isinstance(raw.get(k), list) else []) if isinstance(r, dict) and r.get('id')]
        for k in ('buy_match', 'sell_match', 'target_match'):
            if raw.get(k) in ('any', 'all'):
                rules[k] = raw[k]
        rules['exclude'] = [str(x) for x in raw.get('exclude') or [] if isinstance(x, str)][:200]
        risk = {}
        for k, v in (spec.get('risk_config') or {}).items() if isinstance(spec.get('risk_config'), dict) else []:
            if k in ('position_size_percent', 'stop_loss_percent', 'take_profit_percent', 'trailing_stop_percent',
                     'daily_loss_limit_percent', 'max_positions', 'order_krw', 'max_order_krw'):
                try:
                    risk[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    pass
        if 'max_positions' in risk:
            risk['max_positions'] = max(1, int(risk['max_positions']))
        # 하루 손실 한도는 이 화면에 칸이 없으니 기본 10% 를 몰래 넣지 않음 (0 = 안 씀, 2026-10-15)
        return {'name': (str(spec.get('name') or '').strip() or '백테스트')[:30], 'trade_rules': rules,
                'risk_config': {**DEFAULT_RISK, 'daily_loss_limit_percent': 0, **risk}}

    def _run_backtest_multi(self, job_id: str, bot: Dict, markets: List[str], unit: str, start: int, end: int, krw, costs=None):
        """백테스트 메뉴: 코인마다 차례로 (코인 하나가 실패해도 나머지는 계속) → {'coins': [{market, result | error}]}"""
        job = self._bt_jobs[job_id]
        out, n = [], len(markets)
        for i, market in enumerate(markets):
            try:
                r = self._bt_compute(job, bot, market, unit, start, end, krw, i / n, (i + 1) / n, f"[{i + 1}/{n}] {market.replace('KRW-', '')} · ", costs)
                out.append({'market': market, 'result': r})
            except Exception as e:
                logger.warning(f"백테스트 실패 ({market} {unit}): {e}", exc_info=True)
                out.append({'market': market, 'error': str(e)[:200]})
        job.update(stage='done', progress=1.0, text='끝났습니다', result={'coins': out, 'unit': unit, 'start': start, 'end': end})

    def _bt_bot_now(self, bot_id: str) -> Optional[Dict]:
        """봇 백테스트에 넘길 봇 = 지금 모드의 규칙 · 안전장치 · 예산을 맨 위 칸에 채운 복사본
        (봇을 고치면 by_mode 만 바뀌고 맨 위 칸은 만들 때 값이라, 그대로 넘기면 고치기 전 규칙으로 돌았음 — 2026-10-17)"""
        bot = self.bots.get(bot_id)
        if not bot:
            return None
        return {**bot, 'trade_rules': self.bots.rules_of(bot), 'risk_config': self.bots.risk_of(bot)}

    def _run_backtest(self, job_id: str, bot: Dict, market: str, unit: str, start: int, end: int, krw, costs=None):
        """백테스트 작업: 필요한 캔들 중 이 PC에 없는 구간만 받기(진행률) → 봇 규칙 다시 돌리기 → 결과"""
        job = self._bt_jobs[job_id]
        try:
            r = self._bt_compute(job, bot, market, unit, start, end, krw, costs=costs)
            job.update(stage='done', progress=1.0, text='끝났습니다', result=r)
        except Exception as e:
            logger.warning(f"백테스트 실패 ({market} {unit}): {e}", exc_info=True)
            job.update(stage='error', error=str(e)[:200])

    def _bt_compute(self, job: Dict, bot: Dict, market: str, unit: str, start: int, end: int, krw,
                    lo: float = 0.0, hi: float = 1.0, label: str = '', costs: Optional[Dict] = None) -> Dict:
        """코인 하나: 필요한 캔들 받기(진행률 lo~hi) → 규칙대로 계산 → 결과 (실패하면 예외)"""
        from bot_core import backtest as bt
        from bot_core.candle_store import CandleStore
        span = hi - lo
        if self._candle_store is None:
            self._candle_store = CandleStore(self.user_data_dir / 'data' / 'candles.db')
        store = self._candle_store
        custom = self._custom_conditions()
        need = bt.needed(bot, custom, unit, start, end)
        total = sum(store.pages_needed(market, u, a, b) for u, a, b in need)
        done = [0]

        def fetch(path, mkt, count, to):
            # 백테스트는 오래 받으므로 다른 요청(봇·화면)에 자리를 조금 남기고, 요청이 많다(429)고 하면 쉬었다가 다시
            import requests
            for attempt in range(8):
                time.sleep(0.05)
                try:
                    return self._get_candles(path, mkt, count, to)
                except requests.HTTPError as e:
                    if getattr(e.response, 'status_code', None) != 429 or attempt == 7:
                        raise
                    job.update(text=f"{label}업비트 요청이 많아 잠깐 쉬었다가 이어 받습니다… ({done[0]:,}/{max(total, done[0]):,}번째)")
                    time.sleep(2 + attempt * 2)

        def page(_got, _tot):
            done[0] += 1
            job.update(progress=round(lo + span * min(done[0] / max(total, 1), 1) * 0.9, 3),
                       text=f"{label}업비트에서 캔들 받는 중 {done[0]:,}/{max(total, done[0]):,}번째 · 받은 캔들은 이 PC에 저장해 다음엔 다시 받지 않습니다")
        job.update(stage='fetch', progress=round(lo, 3),
                   text=f"{label}업비트에서 캔들 받는 중 0/{total:,}번째" if total else f'{label}필요한 캔들이 이미 이 PC에 있습니다 — 바로 계산합니다')
        candles = {u: store.get_range(market, u, a, b, fetch, on_progress=page) for u, a, b in need}
        job.update(stage='calc', progress=round(lo + span * 0.92, 3), text=f'{label}규칙대로 계산 중…')
        r = bt.run(bot, market, unit, start, end, candles, custom, krw,
                   facts=lambda cur, day: self.coin_facts.facts(cur, day), **(costs or {}))
        r.update(bot_name=bot.get('name') or '', pages_fetched=done[0])
        return r

    # ── 보유코인 분석 '다른 코인 비교' · '고른 코인에 봇을 적용했다면' ──
    def _cstore(self):
        from bot_core.candle_store import CandleStore
        if self._candle_store is None:
            self._candle_store = CandleStore(self.user_data_dir / 'data' / 'candles.db')
        return self._candle_store

    def _paced_fetch(self, path, mkt, count, to):
        """캔들 받기: 요청이 많다(429)고 하면 쉬었다가 다시 (다른 요청에 자리를 조금 남김)"""
        import requests
        for attempt in range(8):
            time.sleep(0.05)
            try:
                return self._get_candles(path, mkt, count, to)
            except requests.HTTPError as e:
                if getattr(e.response, 'status_code', None) != 429 or attempt == 7:
                    raise
                time.sleep(2 + attempt * 2)

    def _cmp_rows(self, market: str, unit: str, start: int, max_age: float = 30) -> list:
        """비교 코인 봉 (start 조금 앞 ~ 지금) — 이 PC 캔들 저장소 + 30초 기억 (1분마다 그래프를 다시 그려도 요청이 몰리지 않게)"""
        from bot_core.candle_store import UNITS
        sec = UNITS[unit][1]
        cache = self.__dict__.setdefault('_cmp_rows_cache', {})
        hit = cache.get((market, unit))
        if hit and time.time() - hit[0] < max_age and hit[1] <= start - sec:
            return [r for r in hit[2] if r[0] >= start - 2 * sec]
        a = start - 2 * sec
        rows = self._cstore().get_range(market, unit, a, int(time.time()) + sec, self._paced_fetch)
        cache[(market, unit)] = (time.time(), a, rows)
        return rows

    def _cmp_prices(self, coins: list, interval: str, points: list) -> Dict:
        """{코인: [점마다 그 시각의 값]} — 맨 끝 '지금' 점(live)은 지금 시세"""
        from gui import coin_compare as cc
        from bot_core.candle_store import UNITS
        unit = cc.UNIT_OF[interval]
        sec = UNITS[unit][1]
        start = int(points[0]['at'])
        try:
            tickers = self._krw_tickers()
        except Exception:
            tickers = {}
        out = {}
        for c in coins[:cc.CMP_MAX]:
            m = f'KRW-{c}'
            try:
                rows = self._cmp_rows(m, unit, start)
            except Exception as e:
                logger.warning(f"비교 코인 봉 받기 실패 ({m}): {e}")
                continue
            now_px = float((tickers.get(m) or {}).get('trade_price') or 0) or None
            vals = []
            for i, p in enumerate(points):
                if p.get('live') or (i == len(points) - 1 and time.time() - p['at'] < 120):
                    vals.append(now_px or cc.value_at(rows, sec, p['at']))
                else:
                    vals.append(cc.value_at(rows, sec, p['at']))
            out[c] = vals
        return out

    def _cmp_scan_job(self, interval: str, start: int) -> str:
        """코인 고르기 창 값 작업 (같은 구간을 10분 안에 다시 열면 바로 끝난 작업)"""
        from gui import coin_compare as cc
        from bot_core.candle_store import UNITS
        sec = UNITS[cc.UNIT_OF[interval]][1]
        key = (interval, start // sec * sec)          # 같은 그래프 구간 (봉은 _run_cmp_scan 에서 구간 길이로 고름)
        with self._bt_lock:
            for jid, j in self._cmp_jobs.items():
                if j.get('kind') == 'scan' and j.get('key') == key and (j['stage'] in ('fetch', 'calc') or time.time() - j.get('at', 0) < 600):
                    return jid
            for jid in [k for k, j in self._cmp_jobs.items() if j.get('kind') == 'scan']:
                if self._cmp_jobs[jid]['stage'] not in ('fetch', 'calc'):
                    del self._cmp_jobs[jid]          # 지난 구간 결과는 버림
            job_id = uuid.uuid4().hex[:10]
            self._cmp_jobs[job_id] = {'kind': 'scan', 'key': key, 'stage': 'fetch', 'progress': 0.0, 'text': '코인 목록 받는 중', 'at': time.time()}
        threading.Thread(target=self._run_cmp_scan, args=(job_id, interval, start), name='CompareScan', daemon=True).start()
        return job_id

    def _run_cmp_scan(self, job_id: str, interval: str, start: int):
        from gui import coin_compare as cc
        from bot_core.candle_store import UNITS
        job = self._cmp_jobs[job_id]
        try:
            unit = cc.scan_unit(interval, start, time.time())
            sec = UNITS[unit][1]
            markets = self._get_krw_markets()
            tickers = self._krw_tickers()
            rows_out = []
            t0 = time.time()
            for i, mk in enumerate(markets):
                m = mk['market']
                t = tickers.get(m) or {}
                try:
                    st = cc.scan_stats(self._cmp_rows(m, unit, start, max_age=300), sec, start, t.get('trade_price'))   # 고르기 창은 5분 기억
                except Exception as e:
                    logger.warning(f"비교 코인 값 계산 실패 ({m}): {e}")
                    st = None
                if st:
                    rows_out.append({'c': m.replace('KRW-', ''), 'name': mk.get('korean_name') or m, 'ret': round(st['ret'], 3),
                                     'mdd': round(st['mdd'], 3), 'late': st['late'], 'value24h': t.get('acc_trade_price_24h') or 0})
                n = i + 1
                left = (time.time() - t0) / n * (len(markets) - n)
                job.update(progress=round(n / len(markets), 3),
                           text=f"코인마다 구간 값 계산 중 · {n}/{len(markets)}개 · 약 {int(left)}초 남음")
            job.update(stage='done', progress=1.0, text='끝났습니다', at=time.time(),
                       result={'interval': interval, 'start': start, 'coins': rows_out, 'top10': [m.replace('KRW-', '') for m in self._top_markets(10)]})
        except Exception as e:
            logger.warning(f"다른 코인 비교 값 계산 실패: {e}", exc_info=True)
            job.update(stage='error', error=str(e)[:200], at=0)

    def _run_cmp_bot(self, job_id: str, bot: Dict, coins: list, unit: str, start: int, end: int, clipped: bool, limit_days: int):
        """코인마다: 필요한 캔들 받기(진행률) → 구간 처음에 산 채로 봇 규칙 백테스트 → 같은 시각끼리 더함"""
        from bot_core import backtest as bt
        from gui import coin_compare as cc
        job = self._cmp_jobs[job_id]
        try:
            store = self._cstore()
            custom = self._custom_conditions()
            need = bt.needed(bot, custom, unit, start, end)
            total = sum(store.pages_needed(f'KRW-{c}', u, a, b) for c in coins for u, a, b in need)
            done = [0]

            def page(_g, _t):
                done[0] += 1
                job.update(progress=round(min(done[0] / max(total, 1), 1) * 0.9, 3),
                           text=f"업비트에서 캔들 받는 중 {done[0]:,}/{max(total, done[0]):,}번째 · 받은 캔들은 이 PC에 저장해 다음엔 다시 받지 않습니다")
            job.update(text=f"업비트에서 캔들 받는 중 0/{total:,}번째" if total else '필요한 캔들이 이미 이 PC에 있습니다 — 바로 계산합니다')
            results, per, failed = [], [], []
            names = {m['market']: m.get('korean_name') for m in self._get_krw_markets()}
            for i, c in enumerate(coins):
                m = f'KRW-{c}'
                try:
                    candles = {u: store.get_range(m, u, a, b, self._paced_fetch, on_progress=page) for u, a, b in need}
                    job.update(stage='calc', text=f"봇 규칙대로 계산 중 · {i + 1}/{len(coins)}개")
                    r = bt.run(bot, m, unit, start, end, candles, custom, cc.START_KRW,
                               facts=lambda cur, day: self.coin_facts.facts(cur, day), start_held=True)
                except Exception as e:
                    failed.append({'c': c, 'why': str(e)[:120]})
                    continue
                results.append(r)
                last = r['curve'][-1] if r['curve'] else {'bot': cc.START_KRW, 'hold': cc.START_KRW}
                k = r['kpis']
                per.append({'c': c, 'name': names.get(m) or c, 'bot': (last['bot'] / cc.START_KRW - 1) * 100,
                            'hold': (last['hold'] / cc.START_KRW - 1) * 100, 'buys': max(0, k['buys'] - 1), 'sells': k['sells'],
                            'open': k['open'], 'trades': [t for t in r['trades'][1:]][-30:]})
            if not results:
                raise RuntimeError('계산할 수 있는 코인이 없습니다 (' + ', '.join(f"{f['c']}: {f['why']}" for f in failed[:3]) + ')')
            p = bt.plan(bot, custom)
            job.update(stage='done', progress=1.0, text='끝났습니다', result={
                'bot_name': bot.get('name') or '', 'unit': unit, 'start': start, 'end': end, 'clipped': clipped, 'limit_days': limit_days,
                'curve': cc.combine_curves(results), 'coins': per, 'failed': failed,
                'skipped': p['skipped'], 'notes': p['notes'], 'start_krw': cc.START_KRW})
        except Exception as e:
            logger.warning(f"봇 적용 비교 실패: {e}", exc_info=True)
            job.update(stage='error', error=str(e)[:200])

    def _run_verify(self, job_id: str, market: str, days: int, rule: Dict):
        """직접 검증하기 작업: 1분봉 받기(진행률) → 봇과 같은 계산 → 결과(가치 비교·한 건씩·캔들)"""
        from bot_core import spike_verify as sv
        job = self._verify_jobs[job_id]
        try:
            job.update(stage='fetch', progress=0.0)
            bars = self._get_minute_bars(market, days, on_progress=lambda f: job.update(progress=round(f * 0.95, 3)))
            if len(bars) < 200:
                raise RuntimeError('1분봉을 충분히 받지 못했습니다')
            job.update(stage='calc', progress=0.96)
            r = sv.simulate(bars, rule)
            events = []
            for e in r['events']:
                ch = sv.candles_around(bars, e)
                events.append({'market': market, 'signal_t': e['signal_t'], 'sell_t': e['sell_t'], 'sell_price': e['sell_price'],
                               'peak': e['peak'], 'mult': e['mult'], 'rise': e['rise'], 'rise_1h': e['rise_1h'], 'how': e['how'],
                               'coin_change': e.get('coin_change'), 'open': e.get('open', False),
                               'buys': [{'t': b['t'], 'price': b['price'], 'why': b['why'], 'parts': b.get('parts', 1)} for b in e['buys']],
                               'minutes': ch['minutes'], 'candles': [[c['t'], c['o'], c['h'], c['l'], c['c']] for c in ch['candles']]})
            job.update(stage='done', progress=1.0, result={
                'market': market, 'days': days, 'from': bars[0]['t'], 'to': bars[-1]['t'],
                'stats': sv.summarize(r['events']), 'events': events,
                'diag': sv.diagnose(bars, rule) if not events else None, 'rule': {k: rule[k] for k in ('mult', 'rise', 'rise_1h')},
                'curve': [{'t': x['t'], 'rule': round(x['rule']), 'hold': round(x['hold'])} for x in r['curve']],
                'rule_end': r['rule_end'], 'hold_end': r['hold_end'], 'start_value': r['start_value']})
        except Exception as e:
            logger.warning(f"직접 검증 실패 ({market}): {e}")
            job.update(stage='error', error=str(e)[:200])

    def _get_candles(self, unit: str, market: str, count: int, to: str = None, priority: bool = False) -> list:
        """업비트 캔들 (최신순: [0]이 진행 중인 봉). unit 예: 'days', 'weeks', 'minutes/60'

        캔들 API 요청 수 제한(초당 10회)을 넘지 않도록 요청 시작 간격을 둔다 (뒤쪽 줄 0.13초 · 앞쪽 줄 0.4초).
        여러 스레드(화면 요청, 백그라운드 미리 받기)가 동시에 불러도 간격은 인스턴스 전체에서 공유.
        priority=True: 사람이 기다리는 차트 요청 — 뒤쪽 줄을 기다리지 않고 앞쪽 줄로
        """
        import requests
        for attempt in range(4):
            self._pacer.wait('candles', priority=priority)
            params = {'market': market, 'count': count}
            if to:
                params['to'] = to                    # 이 시각 전까지 (UTC, 예: 2026-09-29T02:00:00Z)
            # 연결 재사용: 스레드마다 세션 하나 (캔들마다 새 TLS 연결을 맺으면 수백 개를 받을 때 CPU 를 많이 씀)
            tl = self.__dict__.setdefault('_candle_tls', threading.local())
            if not hasattr(tl, 'session'):
                tl.session = requests.Session()
            resp = tl.session.get(f'{self.API_BASE}/v1/candles/{unit}', params=params, timeout=5)
            self._pacer.note('candles', resp.status_code, getattr(resp, 'headers', None))
            if resp.status_code == 429 and attempt < 3:
                continue                                 # 조절기가 그룹 전체를 쉬게 했으니 다음 wait 에서 기다렸다가 다시
            resp.raise_for_status()
            return resp.json()
        return []

    def _fetch_hourly(self, markets: list) -> Dict:
        """코인별 최근 24시간 1시간봉 (전일저점·24시간 추이, 전일대비 추이용, 10분 캐시)

        반환: {market: {'t': [봉 시작 unix초], 'c': [종가], 'l': [저가]}} 오래된 순 24개 (마지막은 진행 중인 봉)
        화면에서 마지막 값을 현재가로 바꿔 쓴다.

        2026-10-15: 봇 13개가 확인할 때마다 캐시가 끝난 코인 수백 개를 저마다 한꺼번에 받으러 가 업비트 캔들 요청 한도(429)를
        늘 채움 → 차트 캔들이 줄 서서 느렸음. 이제는
          - 캐시가 오래됐어도 값이 있으면 그 값을 바로 쓰고, 새로 받기는 뒤에서 한 번만 (여러 봇이 불러도 하나)
          - 처음 받는 코인만 그 자리에서 받되, 같은 코인을 두 곳에서 한꺼번에 받지 않음
        """
        from datetime import timezone

        cache = self._hourly_cache
        now = time.time()
        result = {m: cache[m][1] for m in markets if m in cache and now - cache[m][0] < 600}
        if len(result) == len(markets):
            return result
        stale = [m for m in markets if m not in result and m in cache]
        missing = [m for m in markets if m not in cache]

        def fetch(market):
            try:
                candles = list(reversed(self._get_candles('minutes/60', market, 24)))
                return market, {
                    't': [int(datetime.fromisoformat(c['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp())
                          for c in candles],
                    'c': [c['trade_price'] for c in candles],
                    'l': [c['low_price'] for c in candles],
                }
            except Exception as e:
                logger.warning(f"1시간봉 조회 실패 ({market}): {e}")
                return market, None

        def fetch_many(todo):
            with nullcontext(_shared_pool('hourly', 6)) as pool:
                for market, data in pool.map(fetch, todo):
                    if data:
                        cache[market] = (time.time(), data)

        lock = self.__dict__.setdefault('_hourly_lock', threading.Lock())
        if stale:                                       # 오래된 값으로 먼저 · 새로 받기는 뒤에서 하나만
            for m in stale:
                result[m] = cache[m][1]
            bg = self.__dict__.setdefault('_hourly_bg_lock', threading.Lock())
            if bg.acquire(blocking=False):
                def refresh(todo=list(stale)):
                    try:
                        with lock:
                            fetch_many([m for m in todo if time.time() - cache.get(m, (0, None))[0] >= 600])
                    finally:
                        bg.release()
                _shared_pool('background', 2).submit(refresh)
        if missing:                                     # 처음 받는 코인: 그 자리에서 (같은 코인을 두 번 받지 않게 잠그고 다시 봄)
            with lock:
                fetch_many([m for m in missing if m not in cache])
            for m in missing:
                if m in cache:
                    result[m] = cache[m][1]
        return result

    # 조건 계산용 봉 (일봉은 기간 시세 lows 를 씀): 업비트 캔들 단위 · 캐시 초 (짧은 봉은 자주, 긴 봉은 드물게 새로)
    TF_UNIT = {'1m': ('minutes/1', 120), '5m': ('minutes/5', 240), '10m': ('minutes/10', 300), '15m': ('minutes/15', 450), '30m': ('minutes/30', 600),
               '1h': ('minutes/60', 600), '4h': ('minutes/240', 1800), 'w': ('weeks', 21600), 'M': ('months', 43200)}

    @staticmethod
    def _long_fields(newest_first: list) -> Dict:
        """끝난 봉(최신순) → 긴 기간 칸 120개 (오래된 순). 고가 · 저가 · 종가 · 거래대금
        + 아주 긴 종가 칸(day_closes_xl, 199개) — EMA 120 같은 긴 지수 평균용 (같은 한 번 요청 안에서)"""
        rows = list(reversed(newest_first[:120]))
        xl = list(reversed(newest_first[:199]))
        return {'day_closes_xl': [c['trade_price'] for c in xl],
                'day_closes_long': [c['trade_price'] for c in rows], 'day_highs_long': [c.get('high_price', c['trade_price']) for c in rows],
                'day_lows_long': [c.get('low_price', c['trade_price']) for c in rows], 'day_values_long': [c.get('candle_acc_trade_price') for c in rows]}    # 조건 시간 봉: (업비트 봉, 캐시 초)

    def _fetch_tf_bars(self, markets: list, tf: str) -> Dict:
        """조건 계산용 4시간·1시간봉 — 일봉 기간 시세와 같은 이름의 칸으로 (조건 계산 코드를 그대로 쓰게)

        반환: {market: {'day_closes', 'day_highs', 'day_values'}} 오래된 순 30개, 진행 중인 봉은 뺌
        (화면·봇이 끝에 현재가를 붙여 계산). 캐시: 4시간봉 30분, 1시간봉 10분
        """
        from concurrent.futures import ThreadPoolExecutor
        unit, ttl = self.TF_UNIT[tf]
        cache = self._tf_cache[tf]

        def fresh():
            now = time.time()
            return {m: cache[m][1] for m in markets if m in cache and now - cache[m][0] < ttl}

        result = fresh()
        if len(result) == len(markets):
            return result
        with self._tf_locks[tf]:                       # 같은 봉을 두 곳에서 한꺼번에 받지 않게
            result = fresh()
            todo = [m for m in markets if m not in result]

            def fetch(market):
                try:
                    raw = self._get_candles(unit, market, 200)[1:]          # 199봉 (EMA 120 용, 요청은 한 번)
                    past = list(reversed(raw[:30]))
                    return market, {'day_closes': [c['trade_price'] for c in past],
                                    'day_highs': [c['high_price'] for c in past],
                                    'day_values': [c.get('candle_acc_trade_price') for c in past],
                                    'day_lows': [c.get('low_price', c['trade_price']) for c in past],
                                    **self._long_fields(raw)}
                except Exception as e:
                    logger.warning(f"{tf} 봉 조회 실패 ({market}): {e}")
                    return market, None

            with nullcontext(_shared_pool('tf_bars', 6)) as pool:
                for market, data in pool.map(fetch, todo):
                    if data:
                        cache[market] = (time.time(), data)
                        result[market] = data
        return result

    def _tfs_in_use(self) -> set:
        """봇 매매 규칙에 쓰인 시간 봉 (일봉 말고)"""
        used = set()
        for b in self.config.get('bots') or []:
          for m in ('sim', 'upbit', 'bithumb'):            # 모든 모드 규칙
            tr = self.bots.rules_of(b, m)
            for side in ('buy', 'sell'):
                rules = tr.get(side) or []
                for r in (rules if isinstance(rules, list) else [rules]):
                    tf = ((r or {}).get('params') or {}).get('tf')
                    if tf in self.TF_UNIT:
                        used.add(tf)
        return used

    def _signal_stats_summary(self) -> Dict:
        cached = self._signal_summary
        upd = float(self.signal_stats.meta('updated') or 0)
        if not cached or cached[0] < upd:
            cached = (upd or time.time(), self.signal_stats.summary())
            self._signal_summary = cached
        out = dict(cached[1])
        out['status'] = dict(self.signal_stats.status)
        return out

    def _signal_stats_update(self):
        import os
        try:
            markets = [m['market'] for m in self._get_krw_markets()]
            started = time.time()
            r = self.signal_stats.update(markets, self._get_candles, self._custom_conditions(), workers=max(1, min(4, (os.cpu_count() or 2) - 1)))
            self._signal_summary = None
            logger.info(f"신뢰도 기록 갱신: {r}, {time.time() - started:.0f}초")
            return 'new_signals' in r
        except Exception as e:
            logger.warning(f"신뢰도 기록 갱신 실패: {e}")
            return False

    def _signal_stats_loop(self):
        """백그라운드: 하루 한 번 (업비트 일봉이 바뀐 뒤, KST 9시 5분 이후) 신뢰도 기록 갱신"""
        from datetime import datetime, timezone
        time.sleep(90)                       # 시작 직후 화면 · 시세 요청이 먼저
        while True:
            try:
                now = datetime.now(timezone.utc)
                day = now.strftime('%Y-%m-%d')
                if self.signal_stats.meta('updated_day') != day and (now.hour > 0 or now.minute >= 5):
                    if self._signal_stats_update():          # 잘 끝났을 때만 오늘 한 것으로 (실패하면 30분 뒤 다시)
                        with self.signal_stats._db() as db:
                            self.signal_stats._set_meta(db, 'updated_day', day)
            except Exception as e:
                logger.warning(f"신뢰도 기록 확인 실패: {e}")
            time.sleep(1800)

    def _prewarm_period_lows(self):
        """백그라운드: 전체 원화 코인의 기간 최저가를 미리 받아 둠 (전체코인 분석 탭을 바로 보이게)

        기간 시세는 하루 캐시라 실제 조회는 업비트 날짜가 바뀐 뒤 한 번, 1시간봉은 10분마다 새로 받는다.
        """
        time.sleep(20)  # 시작 직후 화면 요청이 먼저 가도록
        while True:
            try:
                markets = [m['market'] for m in self._get_krw_markets()]
                started = time.time()
                self._fetch_period_lows(markets)   # 하루 캐시: 날짜가 바뀐 뒤에만 실제 조회
                self._fetch_hourly(markets)        # 10분 캐시
                for tf in self._tfs_in_use():      # 봇 규칙에 쓰인 4시간·1시간봉
                    self._fetch_tf_bars(markets, tf)
                if time.time() - started > 5:
                    logger.info(f"전체 코인 시세 미리 받기: {len(markets)}개, {time.time() - started:.0f}초")
            except Exception as e:
                logger.warning(f"전체 코인 시세 미리 받기 실패: {e}")
            time.sleep(600)

    def _upbit_private_get(self, path: str, params: Dict):
        """업비트 개인 API GET (JWT 서명). 반환: (HTTP 상태, JSON)

        서명용 query_hash는 업비트 규칙대로 '인코딩하지 않은' 쿼리 문자열로 만든다
        (배열 파라미터 states[] 등은 doseq로 펼침).
        """
        import hashlib
        import uuid
        from urllib.parse import unquote, urlencode

        import jwt
        import requests

        api_key = self.config.get('api_key', '')
        api_secret = self.config.get('api_secret', '')
        if not api_key or not api_secret:
            raise HoldingsUnavailable('NO_API_KEY')
        payload = {'access_key': api_key, 'nonce': str(uuid.uuid4())}
        if params:
            query = unquote(urlencode(params, doseq=True))
            payload.update({'query_hash': hashlib.sha512(query.encode()).hexdigest(), 'query_hash_alg': 'SHA512'})
        token = jwt.encode(payload, api_secret, algorithm='HS256')
        resp = requests.get(f'https://api.upbit.com{path}', params=params,
                            headers={'Authorization': f'Bearer {token}'}, timeout=10)
        return resp.status_code, resp.json()

    # 거래내역 구간: 0번 = 오늘(UTC 0시) 6일 전 ~ 지금, k번 = 그 이전 7일씩. 1번 이상은 모두 지난 구간이라 바뀌지 않음
    TRADE_CHUNK_DAYS = 7

    def _trade_chunk_range(self, chunk: int):
        from datetime import timedelta, timezone
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        first_start = today - timedelta(days=self.TRADE_CHUNK_DAYS - 1)
        if chunk == 0:
            return first_start, now
        end = first_start - timedelta(days=self.TRADE_CHUNK_DAYS * (chunk - 1))
        return end - timedelta(days=self.TRADE_CHUNK_DAYS), end

    def _fetch_trade_chunk(self, chunk: int) -> Dict:
        """거래내역 한 구간(7일)의 매수·매도 체결 (업비트 '거래내역' 화면처럼 체결 한 건마다 한 줄)

        - 주문 목록(/v1/orders/closed)은 한 번에 7일까지만 조회돼서 구간을 7일로 나눈다
        - 지난 구간(1번 이상)은 결과를 파일에 저장해 두고 다시 쓴다 (업비트에 다시 묻지 않음)
        - 체결 목록은 주문마다 /v1/order로 받는다. 끝난 주문은 바뀌지 않으니 주문번호별로 파일에 저장
        반환: {'rows': [...최신순], 'start': ISO, 'end': ISO, 'cached': bool}
        """
        start, end = self._trade_chunk_range(chunk)
        store = self.trade_stores['upbit']
        key = start.strftime('%Y-%m-%d')

        fmt = '%Y-%m-%dT%H:%M:%SZ'
        status, data = self._upbit_private_get('/v1/orders/closed', {
            'states[]': ['done', 'cancel'], 'start_time': start.strftime(fmt),
            'end_time': end.strftime(fmt), 'limit': 1000})
        if status != 200:
            raise RuntimeError(f"주문 조회 실패: {data.get('error', {}).get('message', status)}")
        orders = [o for o in data if float(o.get('executed_volume') or 0) > 0]  # 완료 + 일부 체결 후 취소

        # 주문별 체결 목록 (아직 없는 것만, 5개씩 동시에 · 초당 약 20회로 제한 안쪽)
        from concurrent.futures import ThreadPoolExecutor

        def fetch_trades(order_uuid):
            time.sleep(0.25)
            st, detail = self._upbit_private_get('/v1/order', {'uuid': order_uuid})
            return order_uuid, (detail.get('trades') or []) if st == 200 else None

        have = store.get_order_fills([o['uuid'] for o in orders])
        todo = [o['uuid'] for o in orders if o['uuid'] not in have]
        got = {}
        with nullcontext(_shared_pool('order_fills', 5)) as pool:
            for order_uuid, trades in pool.map(fetch_trades, todo):
                if trades is not None:
                    got[order_uuid] = trades
        store.put_order_fills(got)
        have.update(got)

        rows = self._order_fill_rows(orders, have)
        complete = all(have.get(o['uuid']) is not None for o in orders)  # 체결 목록을 못 받은 주문이 있으면 이 구간은 저장하지 않음
        rows.sort(key=lambda r: r['time'], reverse=True)
        store.add_fills(rows)
        if chunk > 0 and complete:                         # 지난 구간은 다 받았으면 다시 묻지 않음
            synced = set(store.get_meta('synced_chunks', []) or [])
            synced.add(key)
            store.set_meta('synced_chunks', sorted(synced))
        return {'rows': rows, 'start': start.isoformat(), 'end': end.isoformat(), 'cached': False}

    @staticmethod
    def _order_fill_rows(orders: List[Dict], have: Dict) -> List[Dict]:
        """끝난 주문 + 주문별 체결 목록 → 거래 내역 줄 (체결 한 건마다 한 줄, 업비트 · 빗썸 같은 모양)"""
        rows = []
        for o in orders:
            trades = have.get(o['uuid'])
            if trades is None:
                continue
            # 수수료는 부동소수 오차 없이 십진수로 계산 (업비트가 준 문자열 값 그대로)
            funds_total = Decimal(o.get('executed_funds') or '0') or sum((Decimal(t['funds']) for t in trades), Decimal('0')) or Decimal('1')
            fee_total = Decimal(o.get('paid_fee') or '0')
            side = '매수' if o['side'] == 'bid' else '매도'
            for t in trades:
                funds_dec = Decimal(t['funds'])
                funds = float(funds_dec)
                # 업비트처럼 체결마다 수수료를 소수 둘째 자리에서 내림 (주문 수수료 × 체결 금액 비율)
                fee = float((fee_total * funds_dec / funds_total).quantize(Decimal('0.01'), rounding=ROUND_DOWN))
                rows.append({
                    'time': t['created_at'], 'order_time': o['created_at'],
                    'currency': o['market'].split('-')[1], 'market': o['market'].split('-')[0],
                    'kind': side, 'volume': float(t['volume']), 'price': float(t['price']), 'order_id': o['uuid'],
                    'amount': funds, 'fee': fee,
                    # 업비트 정산금액 규칙: 매수는 (거래금액+수수료) 원 단위 올림, 매도는 (거래금액-수수료) 내림
                    'settle': (math.ceil(funds + fee - 1e-6) if side == '매수' else math.floor(funds - fee + 1e-6)),
                })
        rows.sort(key=lambda r: r['time'], reverse=True)
        return rows

    def _sync_upbit(self, days: int, recent: bool = True):
        """업비트 체결을 저장소에: 최근 7일은 새로(20초 안에 받았으면 건너뜀), 그 전은 기간 안에서 아직 안 받은 구간만"""
        with self._upbit_sync_lock:
            if recent and time.time() - self._upbit_sync_at > 20:
                self._fetch_trade_chunk(0)
                self._upbit_sync_at = time.time()
            synced = set(self.trade_stores['upbit'].get_meta('synced_chunks', []) or [])
            for k in range(1, 1 + max(0, -(-(days - self.TRADE_CHUNK_DAYS) // self.TRADE_CHUNK_DAYS))):
                if self._trade_chunk_range(k)[0].strftime('%Y-%m-%d') not in synced:
                    self._fetch_trade_chunk(k)

    def _sync_exchange(self, days: int, recent: bool = True):
        """거래 내역 탭: 지금 모드 거래소의 체결을 저장소에 (시뮬레이션은 할 일 없음)"""
        m = self.app_mode()
        if m == 'upbit':
            self._sync_upbit(days, recent=recent)
        elif m == 'bithumb':
            self._sync_bithumb(days, recent=recent)

    def _sync_bithumb(self, days: int, recent: bool = True):
        """빗썸 끝난 주문(체결 · 일부 체결 후 취소)을 저장소에 — 새 것부터 쪽마다 받다가 기간보다 오래되면 멈춤 (20초 안에 받았으면 건너뜀)"""
        with self._bithumb_sync_lock:
            if not recent and self._bithumb_sync_at:
                return
            if time.time() - self._bithumb_sync_at < 20:
                return
            acct = self._account('bithumb')
            store = self.trade_stores['bithumb']
            since = datetime.now().astimezone() - timedelta(days=days)
            orders = []
            for state in ('done', 'cancel'):
                for page in range(1, 21):
                    got = acct.closed_orders(state=state, page=page, limit=100) or []
                    old = False
                    for o in got:
                        try:
                            at = datetime.fromisoformat(str(o.get('created_at')))
                        except ValueError:
                            continue
                        if at < since:
                            old = True
                            break
                        if float(o.get('executed_volume') or 0) > 0:
                            orders.append(o)
                    if old or len(got) < 100:
                        break
                    time.sleep(0.1)
            have = store.get_order_fills([o['uuid'] for o in orders])
            got = {}
            for o in orders:
                if o['uuid'] in have:
                    continue
                try:
                    got[o['uuid']] = (acct.get_order(o['uuid']) or {}).get('trades') or []
                except Exception as e:
                    logger.warning(f"빗썸 체결 조회 실패 ({o['uuid']}): {e}")
                time.sleep(0.1)
            store.put_order_fills(got)
            have.update(got)
            store.add_fills(self._order_fill_rows(orders, have))
            self._bithumb_sync_at = time.time()

    def _trade_store_now(self):
        return self.trade_stores[self.app_mode() if self.app_mode() in self.trade_stores else 'sim']

    def _trades_since(self, days: int) -> int:
        """거래 내역 기간의 시작 (unix 초) — 시뮬레이션은 가상 계좌를 처음부터 한 때보다 앞은 빼고"""
        since = int(time.time()) - int(days) * 86400
        if self.app_mode() == 'sim':
            st = self.trade_stores['sim']
            since = max(since, int(st.get_meta('account_since', 0) or 0), int(st.get_meta('history_since', 0) or 0))   # 거래 내역 지우기 이후만
        return since

    @staticmethod
    def _coin_args(text: Optional[str]):
        """'BTC,ETH' → ['BTC','ETH'] (없으면 None = 모든 코인)"""
        if text is None:
            return None
        return [c for c in (x.strip().upper() for x in text.split(',')) if re.fullmatch(r'[A-Z0-9]{1,15}', c)]

    def _with_reasons(self, rows: List[Dict]) -> List[Dict]:
        """매수 · 매도 줄마다 어느 봇이 왜 (지금 봇 이름으로) — 저장소의 봇 기록과 짝지음"""
        try:
            names = {b['id']: b.get('name') or '' for b in self.bots.bots()}
            return self._trade_store_now().reasons_for(rows, names)
        except Exception as e:
            logger.warning(f"봇 거래 기록 짝짓기 실패: {e}")
            return rows

    def _fetch_transfers(self, days: int) -> Dict:
        """입금·출금 내역 (API 키에 '입출금 조회' 권한이 있어야 함. 없으면 notes에 'NO_DEPOSIT_SCOPE')
        시뮬레이션: 가상 계좌를 연(초기화한) 때 초기자본 입금 한 줄"""
        if self.app_mode() == 'sim':
            st = self.sim_account.state
            amount = float(st.get('initial_krw') or 0)
            when = datetime.fromisoformat(st.get('created_at')).astimezone().isoformat(timespec='seconds')
            if datetime.fromisoformat(when).timestamp() < int(self.trade_stores['sim'].get_meta('history_since', 0) or 0):
                return {'rows': [], 'notes': []}                  # 거래 내역을 지운 뒤면 처음 입금 줄도 안 보임
            return {'rows': [{'time': when, 'order_time': None, 'currency': 'KRW', 'market': None, 'kind': '입금',
                              'volume': amount, 'price': 1.0, 'amount': amount, 'fee': 0.0, 'settle': amount,
                              'state': '가상 자금'}], 'notes': []}
        from datetime import timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows, notes = [], []
        for path, kind in (('/v1/deposits', '입금'), ('/v1/withdraws', '출금')):
            for page in range(1, 11):  # 최신순 100개씩, 기간을 벗어나면 멈춤
                status, data = self._upbit_private_get(path, {'limit': 100, 'page': page, 'order_by': 'desc'})
                if status == 401 and isinstance(data, dict) and data.get('error', {}).get('name') == 'out_of_scope':
                    if 'NO_DEPOSIT_SCOPE' not in notes:
                        notes.append('NO_DEPOSIT_SCOPE')
                    break
                if status != 200 or not data:
                    break
                older = False
                for d in data:
                    when = d.get('done_at') or d.get('created_at')
                    if not when or datetime.fromisoformat(when) < since:
                        older = True
                        continue
                    amount, fee = float(d.get('amount') or 0), float(d.get('fee') or 0)
                    rows.append({
                        'time': when, 'order_time': None, 'currency': d.get('currency'), 'market': None,
                        'kind': kind, 'volume': amount, 'price': 1.0 if d.get('currency') == 'KRW' else None,
                        'amount': amount if d.get('currency') == 'KRW' else None, 'fee': fee,
                        'settle': amount - fee if kind == '출금' else amount, 'state': d.get('state'),
                    })
                if older or len(data) < 100:
                    break
        rows.sort(key=lambda r: r['time'], reverse=True)
        return {'rows': rows, 'notes': notes}

    def _prewarm_trades(self):
        """백그라운드: 봇이 켜지면 최근 한 달(구간 0~4)의 거래내역을 미리 받아 둠 → 거래 내역 탭이 바로 뜨게"""
        time.sleep(30)
        if not (self.config.get('api_key') and self.config.get('api_secret')):
            return
        try:
            self._sync_upbit(30)
        except Exception as e:
            logger.warning(f"거래내역 미리 받기 실패: {e}")

    @staticmethod
    def _save_json(path: Path, data):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            logger.warning(f"파일 저장 실패 ({path.name}): {e}")

    LLM_STATUS_TTL = 86400      # 연결됨 결과는 하루 동안 기억 (다시 켜도 확인 요청을 보내지 않음)

    def _check_llm(self, force: bool = False) -> Dict:
        """AI 연결 상태 {state: ok|error|no_key, provider, model, message}

        자동 확인(force=False): 같은 키·설정이면 기억한 결과를 씀 (연결됨은 파일에 저장해 다시 켜도 하루 동안).
          새로 확인할 때도 모델 정보만 조회해서(check_key) 무료 요청 한도를 쓰지 않음
        [AI 연결 테스트](force=True): 실제로 아주 짧은 글 요청 한 번 (test_connection, 재시도 없음)
        """
        import hashlib
        from gui.condition_llm import test_connection, check_key, LLMError
        provider = self.config.get('llm_provider', 'claude')
        key = self.config.get('llm_api_key', '') or ''
        model = self.config.get('llm_model', '') or ''
        if not key:
            return {'state': 'no_key', 'provider': provider, 'model': '', 'message': 'AI 키가 없습니다'}
        sig = hashlib.sha256(f'{provider}|{model}|{key}'.encode()).hexdigest()
        path = self.user_data_dir / 'data' / 'llm_status.json'
        cached = getattr(self, '_llm_status', None)
        if cached and cached['sig'] == sig and not force:
            return cached['status']
        if not force:
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('sig') == sig and saved['status'].get('state') == 'ok' \
                        and time.time() - float(saved.get('at', 0)) < self.LLM_STATUS_TTL:
                    self._llm_status = {'sig': sig, 'status': saved['status']}
                    return saved['status']
            except Exception:
                pass
        try:
            used = (test_connection if force else check_key)(provider, key, model)
            status = {'state': 'ok', 'provider': provider, 'model': used, 'message': '연결됨'}
        except LLMError as e:
            status = {'state': 'error', 'provider': provider, 'model': model, 'message': str(e)}
        except Exception as e:
            status = {'state': 'error', 'provider': provider, 'model': model, 'message': f'확인 실패: {e}'}
        status['checked_at'] = datetime.now().isoformat(timespec='seconds')
        self._llm_status = {'sig': sig, 'status': status}
        self._save_json(path, {'sig': sig, 'status': status, 'at': time.time()})
        return status

    def _build_market_coins(self):
        """원화 마켓 전체 시세 + 유의·주의·공지 (전체코인 분석 화면과 봇이 같이 씀)

        반환: (coins, global_notices) — coins 항목: currency, korean_name, current_price, change_rate(%),
        today_low, prev_close, 52주 고저, trade_value_24h, warning, cautions, notices
        """
        markets = self._get_krw_markets()
        tickers = self._krw_tickers()                    # 2초 캐시 (보유 현황 · 봇과 함께 씀)
        events = self._get_market_events()
        notices = self._get_upbit_notices()
        coins = []
        for m in markets:
            t = tickers.get(m['market'])
            if not t:
                continue
            currency = m['market'].replace('KRW-', '')
            coins.append({
                'currency': currency,
                'korean_name': m['korean_name'],
                'current_price': t.get('trade_price'),
                'change_rate': (t.get('signed_change_rate') or 0) * 100,
                'today_low': t.get('low_price'),
                'prev_close': t.get('prev_closing_price'),
                'low_52w': t.get('lowest_52_week_price'),
                'low_52w_date': t.get('lowest_52_week_date'),
                'high_52w': t.get('highest_52_week_price'),
                'high_52w_date': t.get('highest_52_week_date'),
                'trade_value_24h': t.get('acc_trade_price_24h'),
                'warning': m['market'] in events['warning'],
                'cautions': events['caution'].get(m['market'], []),
                'notices': notices['by_symbol'].get(currency, []),
                **self.coin_facts.facts(currency),           # listed_days · on_binance (모르면 None)
            })
        return coins, notices['global']

    def get_market_data(self) -> Dict:
        """봇이 조건을 계산할 때 쓰는 시장 데이터 한 묶음 (화면의 조건 검색과 같은 데이터)

        coins: 시세(+1분 매수 flow_*), lows: 일봉·주봉 기간 시세(하루 캐시), hourly: 1시간봉(10분 캐시),
        flow_ready: 1분 매수 평소값 준비 여부, custom: 내 조건 목록
        """
        from gui.condition_llm import sanitize_saved
        coins, _ = self._build_market_coins()
        markets = [f"KRW-{c['currency']}" for c in coins]
        lows = self._fetch_period_lows(markets)
        # '거래대금 상위' 조건용 순위 · '비트코인보다 강한 코인' 조건용 BTC 종가(+지금 가격) — 화면(JS)과 같은 방식
        ranked = sorted((c for c in coins if c.get('trade_value_24h')), key=lambda c: c['trade_value_24h'], reverse=True)
        for i, c in enumerate(ranked, 1):
            c['value_rank'] = i
        btc = next((c for c in coins if c['currency'] == 'BTC'), None)
        btc_closes = (lows.get('KRW-BTC') or {}).get('day_closes')
        if btc and btc_closes and btc.get('current_price'):
            series = [*btc_closes, btc['current_price']]
            lows = {m: {**v, 'btc_closes': series} for m, v in lows.items()}
        try:
            hourly = self._fetch_hourly(markets)
        except Exception as e:
            logger.warning(f"1시간봉 조회 실패 (전일저점은 일봉 기준으로): {e}")
            hourly = {}
        flow = self.trade_flow.snapshot()
        spike = self.trade_flow.spike_snapshot()
        for c in coins:
            f = flow['coins'].get(f"KRW-{c['currency']}")
            c['flow_buy'] = f['buy_1m'] if f else 0
            c['flow_mult'] = f['mult'] if f else None
            c['flow_ratio'] = f['buy_ratio'] if f else None
            self._apply_spike(c, spike)
        try:
            path = self.user_data_dir / 'data' / 'custom_conditions.json'
            custom = sanitize_saved(json.loads(path.read_text(encoding='utf-8')))
        except Exception:
            custom = []
        lows_tf = {}
        for tf in self._tfs_in_use():            # 봇 규칙에 쓰인 시간 봉: 일봉 기간 시세에 봉 칸만 바꿔 끼움
            try:
                bars = self._fetch_tf_bars(markets, tf)
                lows_tf[tf] = {m: {**(lows.get(m) or {}), **b} for m, b in bars.items()}
            except Exception as e:
                logger.warning(f"{tf} 봉 조회 실패: {e}")
        return {'coins': coins, 'lows': lows, 'lows_tf': lows_tf, 'hourly': hourly, 'flow_ready': flow.get('ready', False),
                'spike_ready': spike.get('ready', False), 'custom': custom}

    def _preview_market(self, markets: List[str]) -> Dict:
        """적용 전 미리 보기용 시장 데이터: get_market_data 와 같은 모양이지만 고른 코인(봇이 가진 코인)만"""
        from gui.condition_llm import sanitize_saved
        want = set(markets)
        coins, _ = self._build_market_coins()
        coins = [c for c in coins if f"KRW-{c['currency']}" in want]
        lows = self._fetch_period_lows(markets) if markets else {}
        try:
            hourly = self._fetch_hourly(markets) if markets else {}
        except Exception:
            hourly = {}
        try:
            custom = sanitize_saved(json.loads((self.user_data_dir / 'data' / 'custom_conditions.json').read_text(encoding='utf-8')))
        except Exception:
            custom = []
        lows_tf = {}
        for tf in self._tfs_in_use():
            try:
                bars = self._fetch_tf_bars(markets, tf)
                lows_tf[tf] = {m: {**(lows.get(m) or {}), **b} for m, b in bars.items()}
            except Exception:
                pass
        return {'coins': coins, 'lows': lows, 'lows_tf': lows_tf, 'hourly': hourly, 'custom': custom}

    @staticmethod
    def _apply_spike(c: Dict, spike: Dict):
        """'순간 급등' 조건용 1분 값 (없으면 None — 켠 뒤 1시간 전이거나 그 1분에 체결이 없음)"""
        s = (spike.get('coins') or {}).get(f"KRW-{c['currency']}")
        c['spike_value'] = s['value'] if s else None
        c['spike_avg'] = s['avg60'] if s else None
        c['spike_open'] = s['open'] if s else None
        c['spike_close'] = s['close'] if s else None
        c['spike_close_1h'] = s['close_1h'] if s else None
        c['spike_value15'] = s.get('value15') if s else None          # '급등 직전 모습': 직전 15분 거래대금
        c['spike_close_last'] = s.get('close_last') if s else None    #                  15분 안 마지막 끝가

    def _krw_tickers(self, max_age: float = 2.0) -> Dict[str, Dict]:
        """원화 마켓 전체 시세 {market: 업비트 ticker} — 2초 캐시, 동시에 부르면 한 번만 받음 (연결도 재사용)"""
        import requests
        lock = self.__dict__.setdefault('_ticker_lock', threading.Lock())
        with lock:
            cached = getattr(self, '_ticker_cache', None)
            if cached and time.time() - cached[0] < max_age:
                return cached[1]
            session = self.__dict__.setdefault('_ticker_session', requests.Session())
            try:
                codes = [m['market'] for m in self._get_krw_markets()]
                tickers = {}
                for i in range(0, len(codes), 150):          # 150개씩 (주소 길이 안쪽)
                    tickers.update(self._ticker_chunk(session, codes[i:i + 150]))
            except Exception as e:
                if cached and time.time() - cached[0] < 60:  # 1분 안의 지난 시세로 계속 (봇 확인이 통째로 실패하지 않게, 2026-10-17)
                    logger.warning(f"전체 시세를 새로 받지 못해 조금 전 시세를 씁니다: {e}")
                    return cached[1]
                raise
            self._ticker_cache = (time.time(), tickers)
            now = time.time()
            for code, t in tickers.items():                  # 가상 계좌 체결 가격으로도 씀
                if t.get('trade_price'):
                    self._price_cache[code] = (now, float(t['trade_price']))
            return tickers

    def _ticker_chunk(self, session, codes: list) -> Dict[str, Dict]:
        """코인 묶음 시세. 업비트는 묶음 안에 없는 마켓이 하나라도 있으면 통째로 404 (상장폐지 직후 10분 기억한 마켓 목록에 남아 있을 때)
        → 마켓 목록을 새로 받아 지금 있는 것만 다시 묻고, 그래도 404면 반씩 나눠 없는 마켓을 뺌 (2026-10-12 15시 몇 분 동안 봇 · 화면이 멈췄던 일)"""
        import requests
        self._pacer.wait('ticker')
        resp = session.get(f'{self.API_BASE}/v1/ticker', params={'markets': ','.join(codes)}, timeout=5)
        self._pacer.note('ticker', resp.status_code, getattr(resp, 'headers', None))
        if resp.status_code != 404:
            resp.raise_for_status()
            return {t['market']: t for t in resp.json()}
        if not getattr(self, '_ticker_404_refreshed', False):
            self._ticker_404_refreshed = True
            try:
                self._krw_markets_cache = None
                live = {m['market'] for m in self._get_krw_markets()}
                codes = [c for c in codes if c in live]
            finally:
                self._ticker_404_refreshed = False
            if not codes:
                return {}
            self._pacer.wait('ticker')
            resp = session.get(f'{self.API_BASE}/v1/ticker', params={'markets': ','.join(codes)}, timeout=5)
            self._pacer.note('ticker', resp.status_code, getattr(resp, 'headers', None))
            if resp.status_code != 404:
                resp.raise_for_status()
                return {t['market']: t for t in resp.json()}
        if len(codes) == 1:
            logger.warning(f"시세가 없는 마켓을 뺍니다: {codes[0]}")
            return {}
        half = len(codes) // 2
        out = self._ticker_chunk(session, codes[:half])
        out.update(self._ticker_chunk(session, codes[half:]))
        return out

    def _get_krw_markets(self) -> list:
        """업비트 원화 마켓 목록 [{'market': 'KRW-BTC', 'korean_name': '비트코인'}, ...] (10분 캐시)
        한 번에 하나만 받고(전에는 캐시가 끝나는 순간 봇 13개가 한꺼번에 받으러 가 429), 못 받으면 지난 목록을 그대로 씀 (2026-10-17)"""
        cached = getattr(self, '_krw_markets_cache', None)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        import requests
        with self._krw_markets_lock:
            cached = getattr(self, '_krw_markets_cache', None)
            if cached and time.time() - cached[0] < 600:
                return cached[1]
            try:
                self._pacer.wait('market')
                resp = requests.get(f'{self.API_BASE}/v1/market/all', timeout=5)
                self._pacer.note('market', resp.status_code, getattr(resp, 'headers', None))
                resp.raise_for_status()
                markets = [{'market': m['market'], 'korean_name': m.get('korean_name', m['market'])}
                           for m in resp.json() if m['market'].startswith('KRW-')]
            except Exception as e:
                if cached:                               # 지난 목록으로 계속 (봇 확인이 통째로 실패하지 않게), 1분 뒤 다시
                    logger.warning(f"마켓 목록을 새로 받지 못해 지난 목록을 씁니다: {e}")
                    self._krw_markets_cache = (cached[0] + 60, cached[1])
                    return cached[1]
                raise
            self._krw_markets_cache = (time.time(), markets)
            return markets

    # ── 봇별 1분 수익률 기록: 보유코인 분석과 같은 시각(매 분 + 2초) · 같은 전체 시세 · 같은 계산 (평가손익 ÷ 총 매수) ──
    def _bot_total_curve(self, bot_id: str, mode: str, interval: str, limit: int = 200) -> tuple:
        """봇 수익률 선 두 개 (운용 한도 B 대비 %, 점마다 pnl · base 도 줌)
        - 봇 적용: (그때까지 판 손익 + 그때 평가손익) ÷ B
        - 봇 미적용: 봇이 산 코인을 안 팔고 들고 있었을 때의 수익률 × 같은 돈(그때까지 산 돈, 최대 B) ÷ B"""
        import bisect
        hist = self._bot_pnl_history(bot_id, mode)
        pts = hist.query(interval, limit=limit)
        hold = hist.query(interval, limit=limit, table='pnl_hold')
        try:                                       # 카드 · 보드와 같은 기준: 손익 전 금액 (summary.rate_base)
            _s = self.bots.summary(bot_id, mode)
            B = float(_s.get('rate_base') or _s.get('budget_now') or 0)
        except Exception:
            B = 0.0
        if B <= 0:
            B = max([float(p['buy'] or 0) for p in pts] + [1.0])
        st = self.trade_stores.get(mode)
        real = st.realized_points(bot_id) if st is not None else []
        rts = [t for t, _ in real]
        cum, acc = [], 0.0
        for _, v in real:
            acc += v
            cum.append(acc)
        realized_at = lambda at: cum[bisect.bisect_right(rts, at + 59) - 1] if rts and bisect.bisect_right(rts, at + 59) > 0 else 0.0
        out = []
        for p in pts:
            pnl = realized_at(int(p['at'])) + float(p['value'] or 0) - float(p['buy'] or 0)
            out.append({**p, 'pnl': pnl, 'base': B, 'rate': pnl / B * 100})
        # 아직 한 번도 판 적이 없는 시각은 '봇 미적용 = 봇 적용' 그대로 (산 코인을 그대로 들고 있는 것과 같으니까)
        # — 지난 구간을 봉 종가로 다시 채운 값과 실시간 기록이 조금 어긋나 두 선이 달라 보이지 않게. 맡긴 코인이 있으면 둘이 달라서 하지 않음
        try:
            adopted = any(p.get('adopted') for p in (self.bots._ledger_of(bot_id, mode) or {}).values())
        except Exception:
            adopted = True
        first_sell = rts[0] if rts else None
        bot_at = {p['ts']: p for p in out}
        hout = []
        for p in hold:
            same = bot_at.get(p['ts'])
            if same is not None and not adopted and (first_sell is None or int(same['at']) + 59 < first_sell):
                hout.append({**p, 'pnl': same['pnl'], 'base': B, 'rate': same['rate']})
                continue
            pnl = float(p['rate'] or 0) / 100 * min(float(p['buy'] or 0), B)
            hout.append({**p, 'pnl': pnl, 'base': B, 'rate': pnl / B * 100})
        return out, hout

    def _bot_pnl_history(self, bot_id: str, mode: str):
        cache = self.__dict__.setdefault('_bot_pnl_hist', {})
        key = (bot_id, mode)
        if key not in cache:
            cache[key] = PnlHistory(self.bots.data_dir(bot_id, mode) / 'pnl_history.db')
        return cache[key]

    def _today_unrealized(self, summaries: List[Dict]) -> Dict[str, float]:
        """봇마다 오늘 평가손익 변화 = 지금 평가손익 − 오늘 0시 직전 기록의 평가손익 (봇 1분 기록, 1분 기억)"""
        hit = getattr(self, '_today_unreal_cache', None)
        mode = self.app_mode()
        if hit and hit[2] == mode and time.time() - hit[0] < 60:
            base = hit[1]
        else:
            midnight = int(datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            base = {}
            for s in summaries:
                try:
                    base[s['id']] = self._bot_pnl_history(s['id'], mode).unrealized_at(midnight)
                except Exception:
                    base[s['id']] = None
            self._today_unreal_cache = (time.time(), base, mode)
        return {s['id']: float(s.get('unrealized') or 0) - base[s['id']] for s in summaries if base.get(s['id']) is not None}

    def _bot_holdings(self, bot_id: str, mode: str, prices: Dict[str, float]) -> Dict:
        """/api/holdings 와 같은 형식: 이 봇 장부의 코인만. 상장폐지(시세 목록에 없음) 코인은 0원으로 넣음 (봇 성적 = 손실, 2026-10-15)"""
        assets, buy, value = [], 0.0, 0.0
        full = bool(prices) and len(prices) >= 50
        for sym, p in self.bots._ledger_of(bot_id, mode).items():
            price = prices.get(sym)
            if not price:
                if not full:                         # 시세를 아직 다 못 받았으면 건너뜀 (잠깐 빈 것을 상장폐지로 보지 않게)
                    continue
                price = 0.0
            b, v = p['entry_price'] * p['qty'], price * p['qty']
            assets.append({'currency': sym.replace('KRW-', ''), 'buy_amount': b, 'current_value': v,
                           'pnl_rate': (v / b - 1) * 100 if b else 0})
            buy, value = buy + b, value + v
        return {'total_buy_amount': buy, 'total_value': value, 'assets': assets}

    # ── 비교 코인 (보유코인 분석 평가수익률 추이에 겹쳐 그림): 그 시각의 코인 값 ──
    BENCH_UNITS = {'1m': ('minutes/1', 60), '10m': ('minutes/10', 600), '15m': ('minutes/15', 900), '30m': ('minutes/30', 1800),
                   '1h': ('minutes/60', 3600), '4h': ('minutes/240', 14400), '1d': ('minutes/240', 14400),
                   '1w': ('days', 86400), '1M': ('days', 86400), '1y': ('days', 86400)}

    def _price_at(self, market: str, ts: int):
        """ts 시각의 코인 값 = 그때까지 끝난 1분봉 종가 (지난 시각이라 바뀌지 않으므로 계속 기억)"""
        cache = self.__dict__.setdefault('_price_at_cache', {})
        key = (market, int(ts) // 60 * 60)
        if key not in cache:
            try:
                cs = self._get_candles('minutes/1', market, 1, to=datetime.fromtimestamp(key[1], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), priority=True)
            except Exception:
                return None                                     # 받기 실패: 기억하지 않고 다음에 다시
            cache[key] = float(cs[0]['trade_price']) if cs else None
        return cache[key]

    def _bench_prices(self, market: str, interval: str, ats: list, anchor_at: int = None) -> list:
        """ats(기록 시각)마다 market 값: 그 시각까지 끝난 봉의 종가 (진행 중인 봉은 지금 값). 봉은 30초 캐시
        anchor_at: 대형 코인 10개를 이 시각(첫 기록)에 같은 금액씩 산 것으로 — 없으면 그래프 처음"""
        import bisect
        unit, step = self.BENCH_UNITS.get(interval, ('minutes/60', 3600))
        if market == 'TOP10' and anchor_at:
            out = [[] for _ in ats]
            for m in self._top_markets(10):
                base = self._price_at(m, anchor_at)
                if not base:
                    continue                                    # 그때 없던 코인은 뺌 (간격과 상관없이 같은 코인들)
                for k, p in enumerate(self._bench_prices(m, interval, ats)):
                    if p:
                        out[k].append(p / base)
            return [sum(rs) / len(rs) if len(rs) >= 5 else None for rs in out]
        if market == 'TOP10':                                   # 대형 코인 10개를 그래프 처음에 같은 금액씩 사서 들고 있었다면
            per = [self._bench_prices(m, interval, ats) for m in self._top_markets(10)]
            out = []
            for k in range(len(ats)):
                # 코인마다 그래프에서 값이 처음 나온 때 대비 (값이 없는 코인은 그 점에서 빼고 나머지로 평균)
                rs = []
                for p in per:
                    k0 = next((j for j, x in enumerate(p) if x), None)
                    if k0 is not None and k >= k0 and p[k]:
                        rs.append(p[k] / p[k0])
                out.append(sum(rs) / len(rs) if len(rs) >= 5 else None)
            return out
        if market == 'MARKET':                                  # 시장 전체 평균 지수 (원화 코인 전부를 같은 금액씩)
            self._market_index_backfill()
            arr = self.market_breadth.index_series()
            out = []
            for at in ats:
                k = bisect.bisect_right(arr, (int(at) + 60, float('inf'))) - 1
                out.append(arr[k][1] if k >= 0 and int(at) - arr[k][0] < 2 * step + 3600 else None)
            return out
        cache = self.__dict__.setdefault('_bench_cache', {})
        key = (market, unit)
        hit = cache.get(key)
        if not hit or time.time() - hit[0] > 30:
            try:
                cs = self._get_candles(unit, market, 200, priority=True)
            except Exception:
                cs = []
            now = time.time()
            arr = sorted((min(int(datetime.fromisoformat(c['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()) + step, int(now)),
                          float(c['trade_price'])) for c in cs)
            hit = (time.time(), arr)
            cache[key] = hit
        arr = hit[1]
        out = []
        for at in ats:
            # 그 시각까지 끝난 봉 (1분봉이든 10분봉이든 at 시각의 값 — 1분 뒤 봉을 잡지 않게 +59) · 진행 중인 봉은 끝 = 지금이라 맨 끝 점에 잡힘
            k = bisect.bisect_right(arr, (int(at) + 59, float("inf"))) - 1
            # 그 분에 거래가 없던 코인(1분봉이 비는 때)은 바로 앞 값 그대로 — 빼면 대형 10개 평균이 1분봉에서만 출렁임
            out.append(arr[k][1] if k >= 0 and arr and int(at) - arr[k][0] < max(2 * step, 3600) else None)
        return out

    # 대형 코인 10개 (시가총액 상위 · 업비트 원화 마켓에 있는 것) — 업비트에는 시가총액이 없어 대표 코인으로 정함
    TOP_COINS = ('KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL', 'KRW-DOGE', 'KRW-ADA', 'KRW-TRX', 'KRW-LINK', 'KRW-AVAX', 'KRW-SUI')

    def _top_markets(self, n: int = 10) -> list:
        """대형 코인 n개 (지금 원화 마켓에 있는 것만)"""
        try:
            listed = set(self._krw_tickers())
        except Exception:
            listed = set()
        return [m for m in self.TOP_COINS if not listed or m in listed][:n]

    def _market_index_backfill(self):
        """시장 분위기 기록을 시작하기 전 기간의 시장 전체 평균을 업비트 봉(15분 · 1시간 · 일)으로 한 번 채움 (뒤에서, 켤 때마다 한 번)"""
        if getattr(self, '_mkt_backfill_started', False):
            return
        self._mkt_backfill_started = True

        def run():
            from datetime import timezone
            try:
                mb = self.market_breadth
                first_live = mb.stats().get('first_ts') or int(time.time())
                markets = [m for m in self._krw_tickers().keys() if m.startswith('KRW-')]
                now = int(time.time())
                for unit, step in (('days', 86400), ('minutes/60', 3600), ('minutes/15', 900)):
                    since, until = now - 200 * step, first_live
                    if until - since < 2 * step or mb.eq_hist_count(since, until) >= 0.9 * (until - since) / step:
                        continue
                    closes = {}
                    for m in markets:
                        try:
                            cs = self._get_candles(unit, m, 200)
                        except Exception:
                            continue
                        closes[m] = {int(datetime.fromisoformat(c['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()) + step: float(c['trade_price'])
                                     for c in cs}
                    n = mb.backfill_eq(closes, step, since, until)
                    logger.info(f"시장 전체 평균 지난 기간 채움: {unit} {n}개")
            except Exception as e:
                logger.warning(f"시장 전체 평균 채우기 실패: {e}")
                self._mkt_backfill_started = False

        threading.Thread(target=run, daemon=True, name='market-index-backfill').start()

    # ── 들고만 있었으면: 봇이 산 코인을 산 수량 그대로 팔지 않고 지금까지 들고 있었을 때 ──
    COIN_HOLD_UNITS = {'1m': 'minutes/1', '10m': 'minutes/10', '15m': 'minutes/15', '30m': 'minutes/30', '1h': 'minutes/60', '4h': 'minutes/240',
                       '1d': 'days', '1w': 'weeks', '1M': 'months', '1y': 'months'}

    def _coin_vs_hold(self, bot_id: str, mode: str, currency: str, interval: str, points: list) -> tuple:
        """봇 성과 그래프에서 코인 하나를 골랐을 때 두 선 (점 시각마다):
        - 봇: (그때까지 판 돈 + 남은 수량 × 그때 값) ÷ 그때까지 산 돈 — 일부 팔고 다시 산 것까지 모두 넣은 이 코인 결과
        - 안 팔고 들고 있었으면: 조건으로 산 것(세트의 '다시 사기'는 판 돈으로 산 것이라 뺌)을 그대로 들고 있었을 때
        값은 그래프 간격과 같은 업비트 봉의 종가 (진행 중인 봉 = 지금 값)"""
        import bisect
        from datetime import timezone
        sym = f'KRW-{currency}'
        trades = self.trade_stores[mode].bot_trades(bot_id, symbol=sym, newest_first=False)
        if not trades:
            return points, []
        cs = self._get_candles(self.COIN_HOLD_UNITS.get(interval, 'minutes/1'), sym, 200, priority=True)
        bars = sorted((int(datetime.fromisoformat(c['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()), float(c['trade_price']))
                      for c in cs)
        starts = [b[0] for b in bars]

        def price_at(t):
            k = bisect.bisect_right(starts, t) - 1
            return bars[k][1] if k >= 0 else None

        bot_pts, hold_pts = [], []
        for p in points:
            at = int(p['at'])
            px = price_at(at)
            done = [t for t in trades if int(t['ts']) <= at + 59]
            buys = [t for t in done if t['side'] == 'buy']
            cost = sum(float(t.get('total') or 0) for t in buys)
            if not px or cost <= 0:
                bot_pts.append(p)
                continue
            got = sum(float(t.get('total') or 0) for t in done if t['side'] == 'sell')
            qty = sum(float(t.get('qty') or 0) for t in buys) - sum(float(t.get('qty') or 0) for t in done if t['side'] == 'sell')
            value = got + max(0.0, qty) * px
            bot_pts.append({**p, 'buy': cost, 'value': value, 'rate': (value / cost - 1) * 100})
            lots = [t for t in buys if '다시 사' not in (t.get('reason') or '')]
            hc = sum(float(t.get('total') or 0) for t in lots)
            if hc > 0:
                hv = sum(float(t.get('qty') or 0) for t in lots) * px
                hold_pts.append({'ts': p['ts'], 'at': at, 'buy': hc, 'value': hv, 'rate': (hv / hc - 1) * 100})
        return bot_pts, hold_pts

    def _bot_hold_lots(self, bot_id: str, mode: str) -> list:
        """[(산 시각 ts, 심볼, 수량, 쓴 돈)] — 초기화 이후 이 봇의 매수 기록 (판 것도 그대로 들고 있다고 봄)"""
        st = self.trade_stores.get(mode)
        if st is None:
            return []
        try:
            rows = st.bot_trades(bot_id, newest_first=False)
        except Exception:
            return []
        # 쓴 돈 = 봇 기록의 정산 금액 (수수료 포함) — 봇 장부의 산 금액과 같은 기준이라, 판 적이 없으면 봇 적용 선과 똑같이 나옴
        lots = [(int(r['ts']), r['symbol'], float(r.get('qty') or 0), float(r.get('total') or 0))
                for r in rows if r.get('side') == 'buy' and r.get('qty')]
        # 맡긴 코인: 맡긴 순간 수량 · 가격으로 '들고만 있었다면'에도 넣음 — 봇 적용 선에는 들어가는데 비교선에 빠져
        # '차이'에 맡긴 코인 값 변동이 섞이던 것 (2026-10-15). 기록 전에 맡긴 코인은 지금 장부의 맡긴 값으로
        try:
            d = self.bots.data_dir(bot_id, mode)
            seen = set()
            for a in self.bots._read_json(d / 'adopted_lots.json', []):
                lots.append((int(a['ts']), a['symbol'], float(a['qty']), float(a['cost'])))
                seen.add(a['symbol'])
            for sym, p in (self.bots._read_json(d / 'bot_positions.json', {}) or {}).items():
                if p.get('adopted') and sym not in seen:
                    try:
                        ts = int(datetime.fromisoformat(str(p.get('opened_at'))).timestamp())
                    except (TypeError, ValueError):
                        ts = 0
                    lots.append((ts, sym, float(p['qty']), float(p['qty']) * float(p['entry_price'])))
        except Exception as e:
            logger.debug(f"맡긴 코인 비교선 넣기 실패: {e}")
        return sorted(lots)

    @staticmethod
    def _hold_value(lots: list, prices: Dict[str, float], upto: Optional[int] = None) -> tuple:
        """(쓴 돈, 지금 평가) — 상장폐지(시세 목록에 없음) 코인은 0원 (봇 적용 선과 같게, 2026-10-15)"""
        buy = value = 0.0
        full = bool(prices) and len(prices) >= 50
        for ts, sym, qty, cost in lots:
            if upto is not None and ts > upto:
                continue
            price = prices.get(sym)
            if not price:
                if not full:
                    continue
                price = 0.0
            buy += cost
            value += qty * price
        return buy, value

    # 지난 기간 채우기에 쓰는 봉: (업비트 단위, 봉 길이 초) — 그래프 간격마다 그 간격으로 점이 찍히게 (200개씩 받음)
    HOLD_TIERS = (('minutes/1', 60), ('minutes/15', 900), ('minutes/30', 1800), ('minutes/60', 3600),
                  ('minutes/240', 14400), ('days', 86400))

    def _bot_hold_backfill(self, bot_id: str, mode: str, hist):
        """지난 기간의 '들고만 있었으면'을 그래프 간격에 맞는 촘촘함으로 채움 (뒤에서, 프로그램이 켜져 있는 동안 봇 · 모드마다 한 번)
        - 봉마다 받을 수 있는 범위(200개) 안에서, 그 간격으로 아직 점이 모자란 구간만 받음
          (1분봉 ≈ 최근 3시간, 15분봉 ≈ 50시간, 30분봉 ≈ 4일, 1시간봉 ≈ 8일, 4시간봉 ≈ 33일, 일봉 ≈ 200일)
        - 어느 시각의 코인 값 = 받은 봉 중 그 시각 전에 끝난 가장 가까운 봉의 종가 (촘촘한 봉 우선)
        - 이미 있는 분 기록(매분 실제 기록)은 그대로 둠"""
        done = self.__dict__.setdefault('_hold_backfilled', set())
        key = (bot_id, mode)
        if key in done:
            return
        done.add(key)
        lots = self._bot_hold_lots(bot_id, mode)
        if not lots:
            return

        def run():
            import bisect
            from datetime import timezone
            try:
                first_buy = min(t for t, *_ in lots)
                now = int(time.time()) // 60 * 60
                syms = sorted({s for _, s, *_ in lots})
                tiers = []                                       # 받을 봉: (단위, 길이, 시작, 끝)
                for unit, step in self.HOLD_TIERS:
                    lo = max(first_buy, now - 200 * step)
                    hi = now - step
                    if hi - lo < 2 * step:
                        continue
                    if hist.hold_count(lo, hi) >= 0.9 * (hi - lo) / step:
                        continue                                 # 이 간격으로 이미 충분히 채워짐
                    tiers.append((unit, step, lo, hi))
                if not tiers:
                    return
                # 코인마다 [(봉이 끝난 시각, 종가)] — 촘촘한 봉이 같은 시각을 덮어씀
                prices: Dict[str, list] = {}
                for sym in syms:
                    ends: Dict[int, float] = {}
                    for unit, step, lo, hi in sorted(tiers, key=lambda x: -x[1]):     # 굵은 봉 먼저, 촘촘한 봉이 덮어씀
                        try:
                            cs = self._get_candles(unit, sym, 200)
                        except Exception:
                            continue
                        for c in cs:
                            st = int(datetime.fromisoformat(c['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp())
                            ends[st + step] = float(c['trade_price'])
                    prices[sym] = sorted(ends.items())

                def price_at(sym, t):
                    arr = prices.get(sym)
                    if not arr:
                        return None
                    k = bisect.bisect_right(arr, (t, float('inf'))) - 1
                    return arr[k][1] if k >= 0 else None

                stamps = set()
                for unit, step, lo, hi in tiers:
                    t = lo // step * step + step
                    while t <= hi + step:
                        stamps.add(t - 60)                       # 그 봉의 마지막 분 (봉 종가)
                        t += step
                rows = []
                for t in sorted(stamps):
                    if t < first_buy or t >= now:
                        continue
                    px = {sym: price_at(sym, t + 60) for sym in syms}
                    b, v = self._hold_value(lots, {k: x for k, x in px.items() if x}, upto=t)
                    if b > 0:
                        rows.append((t, b, v))
                hist.record_hold_many(rows)
                logger.info(f"들고만 있었으면 지난 기간 채움: {bot_id}/{mode} {len(rows)}개 ({', '.join(u for u, *_ in tiers)})")
            except Exception as e:
                logger.warning(f"들고만 있었으면 채우기 실패 ({bot_id}/{mode}): {e}")
                done.discard(key)

        threading.Thread(target=run, daemon=True, name=f'hold-backfill-{bot_id}').start()

    SIM_TRADES_KEEP_DAYS = 365       # 시뮬레이션 거래 기록 보관 기간 (업비트 실제 거래는 전부 보관)

    def _bot_pnl_loop(self):
        logger.info("봇별 수익률 기록 시작 (1분 간격)")
        last_prune, last_error = 0.0, None
        while True:
            try:
                tickers = self._krw_tickers()
                try:                                        # 시장 분위기 (전체코인 분석 '시장 현황' 추이)
                    self.market_breadth.record(tickers)
                    if time.time() - last_prune > 3600:
                        self.market_breadth.prune()
                except Exception as e:
                    logger.debug(f"시장 분위기 기록 실패: {e}")
                prices = {m: float(t['trade_price']) for m, t in tickers.items() if t.get('trade_price')}
                prune = time.time() - last_prune > 3600
                ex_prices = {'upbit': prices}               # 빗썸 봇은 빗썸 시세 (빗썸 봇이 있을 때만 받음)
                for b in self.bots.bots():                     # 멈춘 봇 장부를 실제 계좌와 맞춤 (멈춘 동안 직접 판 코인)
                    for mm in self.bots.modes_of(b):
                        try:
                            self.bots.reconcile_stopped(b['id'], mm)
                        except Exception as e:
                            logger.debug(f"멈춘 봇 장부 맞추기 오류 ({b['id']}/{mm}): {e}")
                for b in self.bots.bots():
                    for mode in ('sim', 'upbit', 'bithumb'):
                        if mode == 'bithumb' and not (self.bots._ledger_of(b['id'], mode) or self._bot_hold_lots(b['id'], mode)):
                            continue
                        if mode == 'bithumb' and 'bithumb' not in ex_prices:
                            try:
                                ex_prices['bithumb'] = {m: float(t['trade_price']) for m, t in self.feed('bithumb')._krw_tickers().items() if t.get('trade_price')}
                            except Exception as e:
                                logger.debug(f"빗썸 시세 실패 (봇 수익률 기록): {e}")
                                ex_prices['bithumb'] = {}
                        prices = ex_prices[exchange_of(mode)]
                        held = self._bot_hold_lots(b['id'], mode)       # 들고만 있었으면: 봇이 산 적이 있으면 계속
                        if not self.bots._ledger_of(b['id'], mode) and not held:
                            continue
                        hist = self._bot_pnl_history(b['id'], mode)
                        if self.bots._ledger_of(b['id'], mode):
                            hist.record(self._bot_holdings(b['id'], mode, prices))
                        hb, hv = self._hold_value(held, prices)
                        if hb > 0:
                            hist.record_hold(hb, hv)
                        if prune:
                            hist.prune_coins()
                if prune:
                    last_prune = time.time()
                    if time.time() - getattr(self, '_store_pruned_at', 0) > 86400:
                        self._store_pruned_at = time.time()
                        n = self.trade_stores['sim'].prune(self.SIM_TRADES_KEEP_DAYS)
                        if n:
                            logger.info(f"시뮬레이션 거래 기록 정리: {self.SIM_TRADES_KEEP_DAYS}일 넘은 {n}건 지움")
                last_error = None
            except Exception as e:
                if str(e) != last_error:
                    logger.warning(f"봇 수익률 기록 실패: {e}")
                    last_error = str(e)
            now = time.time()
            time.sleep(60 - now % 60 + 2)                  # 매 분 + 2초 (보유코인 분석 기록과 같은 시각)

    def _live_mode(self) -> str:
        """실전 확인 창이 보는 실제 계좌 모드 (지금 모드가 빗썸이면 빗썸, 아니면 업비트)"""
        return self.app_mode() if is_live(self.app_mode()) else 'upbit'

    def _live_budget_info(self, bot: Dict) -> Dict:
        """실전 시작 확인 창: 배분 비율 · 그 거래소 기준 운용 한도 · 원화 잔고 (못 읽으면 빈 값)"""
        lm = self._live_mode()
        out = {'budget_pct': self.bots.pct_of(bot, lm), 'budget_now': None, 'account_total': None, 'krw': None, 'exchange': self._ex_name(lm)}
        try:
            if bot.get('id'):
                s = self.bots.summary(bot['id'], lm)
                out['budget_now'] = s.get('budget_now')
                out['account_total'] = s.get('account_total')
            if is_live(self.app_mode()):
                snap = self.bots.account_snapshot() or {}
                out['krw'] = snap.get('krw')
                out['account_total'] = out['account_total'] or snap.get('total')
        except Exception as e:
            logger.warning(f"실전 시작 요약 (운용 한도) 실패: {e}")
        return out

    def _live_start_summary(self, bot_id: str = None) -> Dict:
        """실전 매매 시작 확인 창에 보여줄 설정 요약 (매수·매도 조건, 금액, 안전장치). bot_id 없으면 첫 실전 봇"""
        from bot_core import conditions as cd
        from gui.condition_llm import sanitize_saved
        lm = self._live_mode()
        bot = self.bots.get(bot_id) if bot_id else next((b for b in self.bots.bots_in(lm) if not b.get('example')), None)
        bot = bot or {}
        risk = self.bots.risk_of(bot, lm) if bot else (self.config.get('risk_config', {}) or {})     # 실전 확인 창: 그 거래소 모드 규칙
        rules = self.bots.rules_of(bot, lm) if bot else (self.config.get('trade_rules') or {})
        try:
            custom = sanitize_saved(json.loads((self.user_data_dir / 'data' / 'custom_conditions.json').read_text(encoding='utf-8')))
        except Exception:
            custom = []

        from bot_core.bot_engine import dca_settings, rule_list, rule_match, seed_coin_list

        def titles(side):
            names = [c['title'] for c in (cd.resolve(r['id'], custom) for r in rule_list(rules, side)) if c]
            joined = (' 그리고 ' if rule_match(rules, side) == 'all' else ' 또는 ').join(f"'{n}'" for n in names)
            return joined + (' (모두 만족)' if rule_match(rules, side) == 'all' and len(names) > 1 else '')

        # '코인 성격' 조건: '그리고'로 묶지 않으면 시작하자마자 지금 맞는 코인을 삼 (확인 창에 따로 알림)
        buys = rule_list(rules, 'buy')
        start_buy = [] if (rules.get('start_wait') or (rule_match(rules, 'buy') == 'all' and len(buys) > 1)) else [
            c['title'] for c in (cd.resolve(r['id'], custom) for r in buys) if c and c.get('start_buy')]
        return {
            'buy_condition': titles('buy') or '(고르지 않음)',
            'start_buy': start_buy,
            'sell_condition': titles('sell') or '없음 (손절·익절·트레일링으로만)',
            'exclude': rules.get('exclude') or [],
            'position_size_percent': risk.get('position_size_percent', 10),
            'max_order_krw': risk.get('max_order_krw', 0),
            'order_krw': risk.get('order_krw', 0),
            'stop_loss_percent': risk.get('stop_loss_percent', 5),
            'take_profit_percent': risk.get('take_profit_percent', 10),
            'trailing_stop_percent': risk.get('trailing_stop_percent', 0),
            'daily_loss_limit_percent': risk.get('daily_loss_limit_percent', 10),
            'max_positions': risk.get('max_positions', 3),
            'consented_before': bool(self.config.get('live_risk_consent_at')),
            'bot_name': bot.get('name', ''),
            'budget': bot.get('budget', 0),
            **self._live_budget_info(bot),
            # 처음에 사 둘 코인: 시작하면 바로 예산을 나눠 삼 (이미 산 봇이면 다시 사지 않음)
            'seed_coins': seed_coin_list(bot.get('seed_coins')),
            'dca': dca_settings(bot.get('dca')),
            'seeded': bool(bot.get('id')) and (self.bots.data_dir(bot['id'], lm) / 'seeded.json').exists(),
        }

    def _get_upbit_notices(self) -> Dict:
        """업비트 공지사항에서 코인별 경고·알림 추출 (30분 캐시)

        업비트 웹사이트가 쓰는 공지 API(비공식)라 형식이 바뀌면 빈 값을 돌려준다 (배지만 안 보임).
        최근 공지 120개(약 한 달)를 보고 제목으로 분류한다:
          delisting  거래지원 종료 예정 (종료 일시가 아직 안 지난 것)
          suspend    입출금 일시 중단 중 ('완료'·'재개'·'취소'가 붙지 않은 것)
          warning    거래 유의 종목 지정 공지 (배지는 market_event로, 여기선 공지 링크용)
          info       그 밖의 최근 30일 코인 관련 공지 (에어드랍, 유통량 변경 등)
        반환: {'by_symbol': {'ICX': [notice, ...]}, 'global': [점검 등 전체 대상 진행 중 공지]}
        """
        import re
        import requests

        cached = getattr(self, '_notices_cache', None)
        if cached and time.time() - cached[0] < 1800:
            return cached[1]

        result = {'by_symbol': {}, 'global': []}
        try:
            headers = {'Origin': 'https://upbit.com', 'Referer': 'https://upbit.com/service_center/notice',
                       'User-Agent': 'Mozilla/5.0'}
            raw = []
            for page in range(1, 5):  # 한 번에 최대 30개
                resp = requests.get('https://api-manager.upbit.com/api/v1/announcements',
                                    params={'os': 'web', 'page': page, 'per_page': 30, 'category': 'all'},
                                    headers=headers, timeout=5)
                resp.raise_for_status()
                raw.extend(resp.json()['data']['notices'])

            now = datetime.now()
            # 공지는 최신순. 코인별로 더 최근에 '재개·완료·해제' 공지가 나왔으면 그보다 오래된 중단·유의 공지는 끝난 것
            resolved = {'suspend': set(), 'warning': set()}
            for n in raw:
                title, category = n.get('title', ''), n.get('category', '')
                if category in ('이벤트', 'NFT'):
                    continue
                listed = datetime.fromisoformat(n['listed_at'][:19])
                notice = {'id': n['id'], 'title': title, 'date': n['listed_at'][:10],
                          'url': f"https://upbit.com/service_center/notice?id={n['id']}"}
                finished = any(w in title for w in ('완료', '재개', '취소', '해제'))
                symbols = set(re.findall(r'\(([A-Z0-9]{2,12})\)', title))
                if finished and category == '입출금':
                    resolved['suspend'] |= symbols
                if finished and '유의 종목' in title:
                    resolved['warning'] |= symbols

                if category == '거래' and '거래지원 종료' in title and '취소' not in title:
                    # 제목의 "(10/19 15:00)"에서 종료 일시 추출 → 아직 안 지났을 때만
                    m = re.search(r'\((\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})', title)
                    if m:
                        month, day, hour, minute = map(int, m.groups())
                        year = listed.year + (1 if month < listed.month else 0)
                        ends_at = datetime(year, month, day, hour, minute)
                        if ends_at <= now:
                            continue
                        notice['ends_at'] = ends_at.strftime('%Y-%m-%d %H:%M')
                    notice['type'] = 'delisting'
                elif category in ('입출금', '점검') and '중단' in title and not finished:
                    symbols -= resolved['suspend']
                    if not symbols and '입출금' not in title:
                        continue  # 신분증 확인 중단 등 코인과 무관한 점검
                    notice['type'] = 'suspend'
                elif category == '거래' and '유의 종목 지정' in title and not finished:
                    symbols -= resolved['warning']
                    notice['type'] = 'warning'
                elif (now - listed).days <= 30 and not finished and category in ('거래', '입출금', '디지털 자산'):
                    notice['type'] = 'info'
                else:
                    continue

                if symbols:
                    for sym in symbols:
                        result['by_symbol'].setdefault(sym, []).append(notice)
                elif notice['type'] == 'suspend' and not re.search(r'\([^)]*[A-Z]{2,}[^)]*\)', title):
                    result['global'].append(notice)  # 전체 대상 입출금 점검, 네트워크 계열 중단 등

            self._notices_cache = (time.time(), result)
        except Exception as e:
            logger.warning(f"업비트 공지 조회 실패: {e}")
            if cached:
                return cached[1]
        return result

    def _get_pnl_snapshot_path(self, mode: Optional[str] = None) -> Path:
        """스냅샷 파일 경로 (모드별)"""
        return self.user_data_dir / 'data' / ('pnl_snapshots_sim.json' if (mode or self.app_mode()) == 'sim' else 'pnl_snapshots.json')

    def _load_pnl_snapshots(self, mode: Optional[str] = None) -> list:
        """스냅샷 로드"""
        path = self._get_pnl_snapshot_path(mode)
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_pnl_snapshots(self, snapshots: list, mode: Optional[str] = None):
        """스냅샷 저장"""
        path = self._get_pnl_snapshot_path(mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(snapshots, f, ensure_ascii=False, indent=2)

    def _auto_record_daily_snapshot(self):
        """자동 일일 스냅샷 기록 (봇 시작 시 오늘 스냅샷이 없으면 기록)"""
        try:
            api_key = self.config.get('api_key', '')
            api_secret = self.config.get('api_secret', '')
            if not api_key or not api_secret:
                return

            from datetime import date
            today = date.today().isoformat()
            snapshots = self._load_pnl_snapshots('upbit')     # 업비트 실제 계좌 기록

            # 오늘 이미 기록됨
            if any(s['date'] == today for s in snapshots):
                return

            import pyupbit
            upbit = pyupbit.Upbit(api_key, api_secret)
            balances = upbit.get_balances()
            if balances is None:
                return

            total_value = 0
            coin_tickers = []
            coin_balances_map = {}

            for b in balances:
                currency = b.get('currency', '')
                balance = float(b.get('balance', 0)) + float(b.get('locked', 0))
                if currency == 'KRW':
                    total_value += balance
                    continue
                if balance <= 0:
                    continue
                ticker = f'KRW-{currency}'
                coin_tickers.append(ticker)
                coin_balances_map[ticker] = balance

            if coin_tickers:
                try:
                    all_krw = set(pyupbit.get_tickers(fiat="KRW"))
                    valid = [t for t in coin_tickers if t in all_krw]
                    if valid:
                        if len(valid) == 1:
                            p = pyupbit.get_current_price(valid[0])
                            if p: total_value += p * coin_balances_map[valid[0]]
                        else:
                            prices = pyupbit.get_current_price(valid)
                            if isinstance(prices, dict):
                                for t, p in prices.items():
                                    if p and t in coin_balances_map:
                                        total_value += p * coin_balances_map[t]
                except Exception:
                    pass

            snapshots.append({
                'date': today,
                'total_value': round(total_value)
            })
            self._save_pnl_snapshots(snapshots, 'upbit')
            logger.info(f"일일 자산 스냅샷 기록: {today} = {round(total_value):,} KRW")

        except Exception as e:
            logger.warning(f"자동 스냅샷 기록 실패: {e}")

    def _get_simulation_holdings(self) -> Dict:
        """시뮬레이션 모드의 보유 자산 반환"""
        if not self.bot_engine:
            return {
                'success': True,
                'holdings': {
                    'krw_balance': self.config.get('simulation_config', {}).get('initial_balance', 10000000),
                    'total_buy_amount': 0,
                    'total_value': 0,
                    'assets': []
                }
            }

        try:
            status = self.bot_engine.get_status()
            sim_data = status.get('simulation', {})
            positions = sim_data.get('positions', {})

            assets = []
            total_buy = 0
            total_value = 0

            coin_names = self._get_coin_korean_names()

            for symbol, pos in positions.items():
                currency = symbol.replace('KRW-', '')
                entry_price = pos.get('entry_price', 0)
                current_price = pos.get('current_price', entry_price)
                quantity = pos.get('quantity', 0)
                buy_amount = entry_price * quantity
                current_value = current_price * quantity
                pnl = current_value - buy_amount
                pnl_rate = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

                total_buy += buy_amount
                total_value += current_value

                assets.append({
                    'currency': currency,
                    'korean_name': coin_names.get(currency, currency),
                    'balance': quantity,
                    'locked': 0,
                    'avg_buy_price': entry_price,
                    'current_price': current_price,
                    'buy_amount': buy_amount,
                    'current_value': current_value,
                    'pnl': pnl,
                    'pnl_rate': pnl_rate
                })

            assets.sort(key=lambda x: x['current_value'], reverse=True)

            krw_balance = sim_data.get('available_balance', sim_data.get('current_balance', 0))

            return {
                'success': True,
                'holdings': {
                    'krw_balance': krw_balance,
                    'total_buy_amount': total_buy,
                    'total_value': total_value,
                    'assets': assets
                }
            }
        except Exception as e:
            logger.error(f"시뮬레이션 보유 자산 조회 오류: {e}")
            return {'success': False, 'error': str(e)}

    def _get_coin_korean_names(self) -> Dict[str, str]:
        """코인 한글명 매핑 (캐시)"""
        if hasattr(self, '_coin_names_cache') and self._coin_names_cache:
            return self._coin_names_cache

        names = {
            'BTC': '비트코인', 'ETH': '이더리움', 'XRP': '엑스알피(리플)',
            'SOL': '솔라나', 'DOGE': '도지코인', 'ADA': '에이다',
            'AVAX': '아발란체', 'DOT': '폴카닷', 'MATIC': '폴리곤',
            'LINK': '체인링크', 'ATOM': '코스모스', 'UNI': '유니스왑',
            'ETC': '이더리움클래식', 'AAVE': '에이브', 'SAND': '샌드박스',
            'AXS': '엑시인피니티', 'MANA': '디센트럴랜드', 'ENJ': '엔진코인',
            'CHZ': '칠리즈', 'BAT': '베이직어텐션토큰', 'ZIL': '질리카',
            'SXP': '솔라', 'CVC': '시빅', 'ORBS': '오브스',
            'META': '메타디움', 'AERGO': '아르고', 'NEAR': '니어프로토콜',
            'BCH': '비트코인캐시', 'SUI': '수이', 'APT': '앱토스',
            'OP': '옵티미즘', 'ARB': '아비트럼', 'SEI': '세이',
            'SHIB': '시바이누', 'TRX': '트론', 'EOS': '이오스',
            'XLM': '스텔라루멘', 'HBAR': '헤데라', 'VET': '비체인',
            'ALGO': '알고랜드', 'FTM': '팬텀', 'KAVA': '카바',
            'BTT': '비트토렌트', 'STORJ': '스토리지', 'ANKR': '앵커',
            'NEO': '네오', 'IOTA': '아이오타', 'WAVES': '웨이브',
            'ICX': '아이콘', 'QTUM': '퀀텀', 'ZRX': '제로엑스',
            'SNT': '스테이터스네트워크토큰', 'POWR': '파워렛저',
            'STMX': '스톰엑스', 'HUNT': '헌트', 'KNC': '카이버네트워크',
            'POLY': '폴리매쓰', 'GLM': '골렘', 'IOST': '아이오에스티',
            'ONT': '온톨로지', 'GRS': '그로스톨코인', 'SC': '시아코인',
            'STEEM': '스팀', 'ARDR': '아더', 'LSK': '리스크',
            'MTL': '메탈', 'OMG': '오미세고', 'CBK': '코박토큰',
        }

        # 업비트 API에서 추가 조회 시도
        try:
            import pyupbit
            tickers_info = pyupbit.get_tickers(fiat="KRW", verbose=True)
            if tickers_info:
                for t in tickers_info:
                    market = t.get('market', '')
                    korean = t.get('korean_name', '')
                    if market.startswith('KRW-') and korean:
                        currency = market.replace('KRW-', '')
                        names[currency] = korean
        except Exception:
            pass

        self._coin_names_cache = names
        return names

    def set_bot_engine(self, bot_engine):
        """봇 엔진 설정"""
        self.bot_engine = bot_engine

    def update_status(self, status: str):
        """상태 업데이트"""
        self.bot_status = status
        self.socketio.emit('status_update', {'status': status})

    def emit_activity(self, message: str, level: str = 'info', icon: str = None):
        """실시간 활동 메시지를 UI에 전송

        Args:
            message: 사용자에게 보여줄 메시지
            level: 'info', 'success', 'warning', 'signal'
            icon: Bootstrap Icons 클래스명 (없으면 level에 따라 자동 선택)
        """
        default_icons = {
            'info': 'bi-info-circle',
            'success': 'bi-check-circle-fill',
            'warning': 'bi-exclamation-triangle',
            'signal': 'bi-lightning-fill'
        }
        self.socketio.emit('bot_activity', {
            'message': message,
            'level': level,
            'icon': icon or default_icons.get(level, 'bi-info-circle'),
            'time': datetime.now().strftime('%H:%M:%S')
        })

    def start_server(self, open_browser: bool = True):
        """웹 서버 시작"""
        if self.server_thread and self.server_thread.is_alive():
            logger.warning("웹 서버가 이미 실행 중입니다")
            return

        # 자동 일일 스냅샷 기록
        try:
            threading.Thread(target=self._auto_record_daily_snapshot, daemon=True).start()
        except Exception:
            pass

        # 1분 수익률 기록 (프로그램이 켜져 있는 동안)
        self.pnl_recorder.start()
        self.pnl_recorder_sim.start()
        self.pnl_recorder_bithumb.start()        # 빗썸 키가 없으면 기록 없이 조용히 (같은 오류는 한 번만 남김)
        threading.Thread(target=self._bot_pnl_loop, name='BotPnlRecorder', daemon=True).start()   # 봇마다 같은 방식으로

        # 전체코인 분석용 기간 시세 미리 받기
        threading.Thread(target=self._prewarm_period_lows, name='PeriodLowsPrewarm', daemon=True).start()
        # 거래 내역 탭용 최근 한 달 미리 받기
        threading.Thread(target=self._prewarm_trades, name='TradesPrewarm', daemon=True).start()
        # 실시간 체결 수집 시작 (1분 매수 급증 감시)
        self.trade_flow.start()
        # 조건 · 패턴 신뢰도 기록 (하루 한 번)
        threading.Thread(target=self._signal_stats_loop, name='SignalStats', daemon=True).start()
        # 차트에서 걸어 둔 감시 주문 · 시뮬레이션 지정가 지켜보기 (2초마다)
        self.chart_order_watcher.start()
        self.coin_facts.start()
        # 멈춤 막기 · 알아채기 (gui/watchdog.py): 웹 요청은 미리 만든 일꾼들이 처리 · 30초마다 감시
        from gui.watchdog import Watchdog, install_request_pool
        install_request_pool()
        self.watchdog = Watchdog(lambda: self.bots.engines, self.user_data_dir / 'logs',
                                 notify=lambda msg: self.emit_activity(msg, 'warning', 'bi-exclamation-octagon-fill'),
                                 bot_name=lambda bid: (self.bots.get(bid) or {}).get('name', bid))
        self.watchdog.start()
        # 계정 권한표: 로그인해 둔 경우 하루에 한 번 새로 받아 옴 (서버에 닿지 않아도 봇은 그대로)
        self.license.start_auto_refresh()

        def run_server():
            try:
                logger.info(f"웹 대시보드 서버 시작: http://localhost:{self.port}")
                if open_browser:
                    # 브라우저 자동 열기 (약간의 딜레이)
                    def open_browser_delayed():
                        time.sleep(1)
                        webbrowser.open(f'http://localhost:{self.port}')

                    threading.Thread(target=open_browser_delayed, daemon=True).start()

                self.socketio.run(self.app, host='127.0.0.1', port=self.port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
            except Exception as e:
                logger.error(f"웹 서버 실행 오류: {e}")

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

        logger.info("웹 서버 스레드 시작됨")

    def stop_server(self):
        """웹 서버 중지"""
        # Flask-SocketIO 서버는 별도 프로세스이므로 강제 종료 필요
        logger.info("웹 서버 중지 요청")
        # 실제로는 프로세스 종료가 필요하지만, 여기서는 스레드만 중지

