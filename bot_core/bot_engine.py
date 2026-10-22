"""
봇 실행 엔진 — 사용자가 조건 목록에서 고른 조건대로 사고파는 자동매매

- 매수: 매수 조건에 '새로 들어온' 코인만 삼 ([시작] 때 이미 맞는 코인은 기준으로만)
  단 '코인 성격' 조건(start_buy, 예: 급등이 잦던 코인)은 '그리고'로 묶지 않으면 [시작] 때 지금 맞는 코인을
  거래 많은 순으로 최대 보유 수까지 삼 (잘 안 바뀌는 성격이라 새로 들어오기를 기다리면 거의 사지 않으므로)
  · 켠 뒤에도 돈이 생기면(판 돈 · 수익) 맞는 코인 가운데 안 가진 것을 같은 코인당 금액으로 더 삼 (판 코인은 24시간 뒤부터)
- 매도: 봇이 산 코인만 (사용자가 직접 산 코인은 건드리지 않음)
        손절·익절·트레일링(안전장치) 또는 매도 조건에 맞으면 팜
- 대상: 원화 마켓 전체 (스테이블코인·유의 종목·상장폐지 예정·사용자 제외 코인 빼고)
- 모드: 시뮬레이션(가상 돈) / 실전(업비트 시장가 주문)
- 안전장치(RiskManager): 최대 보유 수, 1회 금액(잔고 %·상한), 하루 손실 한도, 같은 코인 주문 간격
- (켜면) 급등 때 일부 팔고 다시 사기: 봇이 산 코인이 급등하면 일부를 팔고 싸지면 다시 삼 (bot_core/spike_rebuy.py)
  실시간 체결로 5초마다 확인
- (켜면) 하루 급등 · 꺾이면 일부 팔고 다시 사기: 24시간 +20% 오른 코인을 (1시간봉, tick 에서) 잡고, 꺾이면 일부 팔고 다시 삼
  같은 틀(SpikeRebuy)을 한 개 더 씀 (surge_state.json). 한 코인은 두 세트 중 하나만 진행

설정은 매번 get_config()로 새로 읽으므로 규칙·안전장치를 바꾸면 다음 확인부터 반영된다.
조건 계산은 bot_core/conditions.py (화면의 조건 검색과 같은 계산).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import conditions as cd
from . import spike_rebuy as sr
from .exchange_wrapper import ExchangeWrapper
from .exchanges import exchange_name
from .risk_manager import RiskManager
from .simulation_engine import SimulationEngine

logger = logging.getLogger(__name__)

FEE = 0.0005                 # 업비트 수수료 0.05%
MIN_ORDER_KRW = 5000         # 업비트 최소 주문금액
TOPUP_KRW = 5300             # 5,000원 미만 코인을 팔 때 먼저 더 사는 금액 (합친 뒤 값이 조금 내려도 5,000원을 넘게)
MAX_BUYS_PER_TICK = 3        # 한 번 확인에 최대 3개까지만 삼 (한꺼번에 몰아 사지 않게)
REFILL_COOLDOWN_H = 24       # '코인 성격' 조건으로 더 살 때: 이 봇이 판 코인은 판 뒤 24시간 동안 다시 사지 않음 (손절 직후 다시 사기 방지)
_SEED_LOCKS: Dict[str, threading.Lock] = {}     # 봇 기록 폴더 → 한꺼번에 사기 잠금
_SEED_LOCKS_GUARD = threading.Lock()
SEED_COINS_MAX = 10          # '처음에 사 둘 코인' 최대 개수


def seed_coin_list(value) -> List[str]:
    """'처음에 사 둘 코인' 목록 정리: ['krw-drv', 'B3', ...] → ['DRV', 'B3'] (영문·숫자만, 중복 빼고 최대 10개)"""
    out: List[str] = []
    for x in (value if isinstance(value, (list, tuple)) else []):
        c = str(x or '').strip().upper().replace('KRW-', '')
        if c and c.isascii() and c.isalnum() and len(c) <= 15 and c not in out:
            out.append(c)
    return out[:SEED_COINS_MAX]


def dca_settings(value) -> Optional[Dict]:
    """나눠 사기 설정 정리: {'coin': 'BTC', 'every_days': 7, 'krw': 190000} → 같은 모양 (틀리면 None = 안 씀)
    정한 코인 하나를 every_days 일마다 krw 원씩 사서 모으기만 함 (팔기는 손절 · 익절 · 매도 조건을 따로 넣을 때만)"""
    if not isinstance(value, dict):
        return None
    coins = seed_coin_list([value.get('coin')])
    try:
        every = float(value.get('every_days') or 0)
        krw = float(value.get('krw') or 0)
    except (TypeError, ValueError):
        return None
    if not coins or every <= 0 or krw < 5000:
        return None
    return {'coin': coins[0], 'every_days': min(every, 365.0), 'krw': krw}
DATA_READY_RATIO = 0.9       # 코인 90% 이상의 일봉을 받았을 때만 판단
SPIKE_STEP_SEC = 5           # 급등 팔고 다시 사기는 5초마다 확인 (실시간 체결)
EQUITY_EVERY_SEC = 300       # 봇 현황 '수익 추이' 그래프용 기록 간격 (5분)
EQUITY_KEEP_SEC = 30 * 86400 # 최근 30일만 보관


def rule_list(rules: Dict, side: str) -> List[Dict]:
    """매수·매도 조건 목록 [{id, params}] — 예전 형식(조건 하나 {id, params})도 받음"""
    v = (rules or {}).get(side)
    if isinstance(v, dict):
        v = [v]
    return [r for r in (v or []) if isinstance(r, dict) and r.get('id')][:5]


def rule_match(rules: Dict, side: str) -> str:
    """칸 안 조건을 묶는 방법: 'any'(또는, 하나라도) / 'all'(그리고, 모두)"""
    return 'all' if (rules or {}).get(f'{side}_match') == 'all' else 'any'



# ── 판 손익 누적 (realized.json): 운용 한도 = 시작 금액 + 판 손익 (복리) ──
def realized_total(data_dir: Path) -> float:
    try:
        return float(json.loads((Path(data_dir) / 'realized.json').read_text(encoding='utf-8'))['total'])
    except Exception:
        pass
    try:
        items = json.loads((Path(data_dir) / 'trades.json').read_text(encoding='utf-8'))
        return sum(float(t.get('profit') or 0) for t in items if t.get('side') == 'sell')
    except Exception:
        return 0.0


def adopted_legacy_total(data_dir: Path) -> float:
    """맡기기 전부터 있던 손실을 판 몫 (원래 평균가 기준으로 맡겼던 때 — 봇 성적에서 빼고 따로 보여 줌)"""
    try:
        return float(json.loads((Path(data_dir) / 'realized.json').read_text(encoding='utf-8')).get('adopted_legacy') or 0)
    except Exception:
        return 0.0


def add_adopted_legacy(data_dir: Path, amount: float) -> None:
    """맡기기 전부터 있던 손익(원래 평균가 → 맡긴 때 가격)을 정리로 확정한 몫 — 봇 성적과 따로"""
    path = Path(data_dir) / 'realized.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = {'total': 0.0}
    data['adopted_legacy'] = float(data.get('adopted_legacy') or 0) + float(amount)
    try:
        write_json(path, data)
    except Exception as e:
        logger.warning(f"맡긴 코인 정리 손익 저장 실패: {e}")


def write_json(path: Path, obj, **dumps_kw) -> None:
    """JSON 을 .tmp 에 다 쓴 뒤 바꿔 넣음 — 쓰는 도중 꺼져도 반쪽 파일이 남지 않게 (2026-10-17, 장부가 비어 손절을 안 하던 틈)"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj, **dumps_kw), encoding='utf-8')
    os.replace(tmp, path)


def add_realized(data_dir: Path, profit: float, items_before: list) -> None:
    path = Path(data_dir) / 'realized.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        total = float(data['total'])
    except Exception:                                   # 처음: 지금까지 기록에서 시작
        data = {}
        total = sum(float(t.get('profit') or 0) for t in items_before if t.get('side') == 'sell')
    try:
        write_json(path, {**data, 'total': total + profit})
    except Exception as e:
        logger.warning(f"판 손익 누적 저장 실패: {e}")


class BotEngine:
    def __init__(self, config: Dict, get_config: Optional[Callable[[], Dict]] = None,
                 get_market_data: Optional[Callable[[], Dict]] = None, data_dir: Optional[Path] = None,
                 get_flow: Optional[Callable[[], Dict]] = None, get_taken: Optional[Callable[[], set]] = None,
                 account=None, can_buy: Optional[Callable[[], bool]] = None):
        """
        Args:
            config: 시작할 때의 설정 (mode, trade_rules, risk_config, simulation_config, api_key, api_secret)
            get_config: 최신 설정을 돌려주는 함수 (없으면 config 그대로)
            get_market_data: 시장 데이터 함수 → {coins, lows, hourly, flow_ready, custom}
            data_dir: 봇 장부·가상 잔고 저장 폴더
            get_flow: 실시간 체결 요약 함수 (TradeFlowMonitor.spike_snapshot) — 급등 팔고 다시 사기용
            get_taken: 같은 계좌의 다른 봇이 가진 코인 (여러 봇일 때: 한 코인은 한 봇만)
            account: 시뮬레이션 모드의 가상 계좌 (VirtualAccount). 주면 업비트 대신 이 계좌에 실전과 같은 코드로 주문
            can_buy: 새 코인을 사도 되는지 (업비트 모드의 계정 권한, bot_core/license.py). 없으면 언제나 됨.
                     False 여도 팔기·손절·진행 중이던 '다시 사기'는 그대로 함 (_buy_fresh 참고)
        """
        self._can_buy = can_buy
        self._buy_blocked = False              # 권한이 없어 새로 사기를 멈춘 상태인지 (안내는 한 번만)
        self.config = config
        self._get_config = get_config or (lambda: self.config)
        self._get_market_data = get_market_data
        self.account = account
        self.mode = 'sim' if account is not None else config.get('mode', 'simulation')
        self.account_mode = config.get('account_mode') or ('sim' if self.mode != 'live' else 'upbit')   # upbit · bithumb · sim
        try:                                    # 수수료: 거래소마다 (빗썸은 설정에서 정한 값) — 사는 금액 · 손익 계산에 씀
            self.fee = float(config.get('fee_rate') if config.get('fee_rate') is not None else FEE)
        except (TypeError, ValueError):
            self.fee = FEE
        self.update_interval = float(config.get('update_interval', 60.0))
        self.data_dir = Path(data_dir) if data_dir else Path('data')
        self._get_flow = get_flow
        self._get_taken = get_taken
        self._get_others_qty = None          # 같은 계좌 다른 봇이 가진 수량 {심볼: 수량} (봇 경쟁: 같은 코인을 여러 봇이)
        self.last_error = None               # (시각, 메시지) 실행 루프에서 마지막으로 난 오류
        self.last_loop_at = None             # 실행 루프가 마지막으로 한 바퀴 돈 때 (time.monotonic) — 멈춤 감시(gui/watchdog.py)
        self._trade_store = None             # 이 모드의 거래 기록 저장소 (bot_core/trade_store.py) — 봇 거래를 전부 남김
        self.spike = sr.SpikeRebuy(self.data_dir / 'spike_state.json')
        self.surge = sr.SpikeRebuy(self.data_dir / 'surge_state.json', cooldown_sec=sr.SURGE_COOLDOWN_SEC)

        self.is_running = False
        self.engine_thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()          # 상태 조회(화면)와 루프가 겹치지 않게

        self.exchange = None
        self.simulation_engine = None
        self.risk_manager = RiskManager(config.get('risk_config', {}))

        # 실전 모드 봇 장부: 봇이 산 코인과 수량 {'KRW-XRP': {qty, entry_price, opened_at, condition}}
        self.ledger: Dict[str, Dict] = {}
        # 매수 조건마다 이미 맞던 코인 {(조건 id, 숫자): set} — 새로 들어온 코인만 사기 위해 (조건·숫자가 바뀌면 다시 잡음)
        self.baselines: Dict[tuple, set] = {}
        self.last_prices: Dict[str, float] = {}
        self.decisions: List[Dict] = []         # 최근 판단 기록 (화면 표시)
        self.last_update_time = None

        self.on_signal_callback: Optional[Callable] = None
        self.on_trade_callback: Optional[Callable] = None
        self.on_error_callback: Optional[Callable] = None
        self.on_activity_callback: Optional[Callable] = None

        logger.info(f"봇 엔진 초기화: 모드={self.mode}")

    # ── 준비·시작·중지 ──
    @property
    def ledger_path(self) -> Path:
        return self.data_dir / 'bot_positions.json'

    @property
    def sim_path(self) -> Path:
        return self.data_dir / 'sim_state.json'

    def _rules(self) -> Dict:
        return (self._get_config() or {}).get('trade_rules') or {}

    # 매수 · 매도 켜기/끄기 (모드마다 매매 규칙에 저장, 없으면 켜짐)
    def _buy_on(self, rules: Optional[Dict] = None) -> bool:
        return (rules if rules is not None else self._rules()).get('buy_on', True) is not False

    def _sell_on(self, rules: Optional[Dict] = None) -> bool:
        return (rules if rules is not None else self._rules()).get('sell_on', True) is not False

    def _note_every(self, key: str, kind: str, msg: str, every: float = 600):
        """같은 안내는 every초에 한 번만 (매수 · 매도를 꺼 둔 동안 판단 기록이 넘치지 않게)"""
        seen = self.__dict__.setdefault('_notes_at', {})
        if time.time() - seen.get(key, 0) >= every:
            seen[key] = time.time()
            self._decide(kind, msg)

    def _flow_wait_note(self, cond: Dict):
        """실시간 체결이 필요한 조건인데 자료가 없음 — 빗썸은 늘 없음(쓸 수 없다고 1시간에 한 번), 업비트 · 시뮬레이션은 모으는 중 (2026-10-19)"""
        if self.account_mode == 'bithumb':
            self._note_every(f"noflow_{cond['id']}", 'warning',
                             f"'{cond['title']}': 빗썸 모드에는 실시간 체결 정보가 없어 이 조건은 맞지 않습니다 (업비트 · 시뮬레이션 모드에서만 씁니다)",
                             every=3600)
            return
        self._emit_activity(f"'{cond['title']}': 실시간 체결 평소값을 모으는 중입니다 (봇 프로그램을 켠 뒤 "
                            f"{'1시간' if cond.get('ready_key') == 'spike_ready' else '10분'})", 'info', 'bi-hourglass-split')

    def _custom(self, data: Optional[Dict] = None) -> List[Dict]:
        return (data or {}).get('custom') or []

    def initialize(self):
        """(성공 여부, 메시지). 매수 조건이 골라져 있어야 시작할 수 있음"""
        try:
            # 시작할 때 사기: '처음에 사 둘 코인'(고른 코인, 두 모드) 또는 시험용 '거래 상위 N개'(시뮬레이션만)
            self._seed_coins = seed_coin_list(self.config.get('seed_coins'))
            self._seed_n = 0 if self._seed_coins or self.mode == 'live' else int(self.config.get('seed_top') or 0)
            try:                                     # 봇 장부에 코인이 있으면(맡긴 코인 등) 매수 조건 없이 팔기만 할 수 있음
                has_coins = bool(json.loads(self.ledger_path.read_text(encoding='utf-8')))
            except Exception:
                has_coins = False
            if not rule_list(self._rules(), 'buy') and not self._seed_n and not self._seed_coins and not has_coins \
                    and not dca_settings(self.config.get('dca')):
                return False, "매수 조건을 넣거나, 보유 코인을 봇에 맡겨 주십시오 (맡기면 매수 조건 없이 팔기만 합니다)"
            if self._seed_coins and self.mode == 'live' and not self._has_limit():
                return False, "실전에서 '처음에 사 둘 코인'을 쓰려면 ① 기본 봇 정보에서 최대 투입 금액을 정해 주십시오 (그 금액을 코인 수로 나눠 삽니다)"
            seeded = (Path(self.data_dir) / 'seeded.json').exists()

            if self.account is not None:
                # 시뮬레이션 모드: 가상 계좌를 업비트 계좌처럼 끼움 — 아래부터는 실전과 같은 코드
                self.exchange = ExchangeWrapper(account=self.account)
                self._load_ledger()
                self._seed_pending = bool(self._seed_n or self._seed_coins) and not seeded
            elif self.mode == 'live':
                ok, msg = self._connect_live()
                if not ok:
                    return False, msg
                self._load_ledger()
                self._seed_pending = bool(self._seed_coins) and not seeded
            else:
                initial = float((self.config.get('simulation_config') or {}).get('initial_balance', 10000000.0))
                self.simulation_engine = SimulationEngine(initial_balance=initial)
                self._load_sim()
                # 시작할 때 한꺼번에 사기: 이 봇 기록에서 한 번만 ([새로 시작]하면 기록 폴더가 비어 다시 삼)
                self._seed_pending = bool(self._seed_n or self._seed_coins) and not seeded

            logger.info("봇 초기화 완료")
            return True, "봇 초기화 완료"
        except Exception as e:
            logger.error(f"봇 초기화 오류: {e}", exc_info=True)
            return False, f"초기화 오류: {e}"

    def _connect_live(self):
        """실제 계좌 연결 (업비트 · 빗썸) → (성공 여부, 메시지). 테스트는 이것만 가짜 거래소로 바꿈"""
        from .exchanges import api_keys, BithumbAccount
        api_key, api_secret = api_keys(self.account_mode, self.config)
        if not api_key or not api_secret:
            return False, f"실전 모드에서는 {'빗썸' if self.account_mode == 'bithumb' else '업비트'} API 키가 필요합니다"
        if self.account_mode == 'bithumb':
            self.exchange = ExchangeWrapper(account=BithumbAccount(api_key, api_secret, fee=self.fee))
        else:
            self.exchange = ExchangeWrapper(mode='live', api_key=api_key, api_secret=api_secret)
        ok, msg = self.exchange.test_connection()
        if not ok:
            return False, f"거래소 연결 실패: {msg}"
        return True, ''

    def start(self):
        if self.is_running:
            return False, "봇이 이미 실행 중입니다"
        if self.exchange is None and self.simulation_engine is None:
            return False, "봇이 초기화되지 않았습니다. initialize()를 먼저 호출하십시오."
        self.is_running = True
        self.started_at = datetime.now()                     # 전체 봇 보드: 첫 확인 전 '시작 중'
        self._stop_event.clear()
        self.baselines = {}
        self._restore_daily_loss()
        self.engine_thread = threading.Thread(target=self._run_loop, name='BotEngine', daemon=True)
        self.engine_thread.start()
        logger.info("봇 시작")
        return True, "봇 시작 완료"

    def _restore_daily_loss(self):
        """오늘 이 봇이 판 손실을 하루 손실 한도에 다시 채움 — 전에는 다시 켜면 0 이라 같은 날 한도만큼 또 잃을 수 있었음 (2026-10-17)"""
        try:
            items = json.loads((self.data_dir / 'trades.json').read_text(encoding='utf-8'))
        except Exception:
            return
        today = datetime.now().strftime('%Y-%m-%d')
        lost = 0.0
        for t in items:
            if t.get('side') == 'sell' and str(t.get('time') or '')[:10] == today:
                try:
                    p = float(t.get('profit') or 0)
                except (TypeError, ValueError):
                    continue
                if p < 0:
                    lost += p
        if lost < 0:
            self.risk_manager.reset_daily_loss()
            self.risk_manager.daily_loss = lost
            logger.info(f"오늘 판 손실 {lost:,.0f}원을 하루 손실 한도에 다시 채움")

    def stop(self):
        self.is_running = False
        self._stop_event.set()
        self._emit_activity("봇이 중지되었습니다", 'warning', 'bi-stop-circle-fill')
        if self.engine_thread:
            self.engine_thread.join(timeout=10.0)
        logger.info("봇 중지")

    def _run_loop(self):
        self._emit_activity("봇이 시작되었습니다", 'success', 'bi-play-circle-fill')
        next_tick = 0.0
        while self.is_running:
            self.last_loop_at = time.monotonic()
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + self.update_interval
                self._safe(self.tick)
            self._safe(self.spike_tick)          # 급등 팔고 다시 사기: 5초마다
            if self._stop_event.wait(max(0.5, min(SPIKE_STEP_SEC, next_tick - time.monotonic()))):
                break

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:
            logger.error(f"실행 루프 오류: {e}", exc_info=True)
            self.last_error = (datetime.now(), str(e)[:120])     # 전체 봇 보드 상태 점검용
            self._emit_activity(f"오류가 발생했습니다: {str(e)[:60]}", 'warning')
            if self.on_error_callback:
                try:
                    self.on_error_callback(e)
                except Exception:
                    pass

    # ── 한 번 확인 ──
    def tick(self, data: Optional[Dict] = None):
        """시장 데이터를 받아 매도(먼저) → 매수. 테스트에서는 data를 직접 넣을 수 있음"""
        config = self._get_config() or {}
        self.risk_manager.update_config(config.get('risk_config') or {})
        rules = config.get('trade_rules') or {}
        if data is None:
            if not self._get_market_data:
                return
            data = self._get_market_data()          # 오래 걸릴 수 있음 (기간 시세를 새로 받을 때 1분 넘게)
            if not self.is_running:
                return                               # 받는 동안 멈췄으면 아무것도 사고팔지 않음 (새로 켠 봇과 겹치지 않게)
        coins = data.get('coins') or []
        lows = data.get('lows') or {}
        self.last_prices = {f"KRW-{c['currency']}": c.get('current_price') for c in coins if c.get('current_price')}
        self.last_update_time = datetime.now()

        with self._lock:
            self._sync_seed(config)
            if getattr(self, '_seed_pending', False) and coins:
                self._seed_buy(coins, rules)
            self._sync_positions()
            self._rebase_adopted()
            self._check_sells(rules, data)
            self._dca_tick(config, coins)
            self.surge_detect(data)           # 하루 급등: 1시간봉이 있는 여기서 잡고, 파는 건 spike_tick 이 5초마다
            self._record_equity()

        have = sum(1 for c in coins if (lows.get(f"KRW-{c['currency']}") or {}).get('day_highs'))
        if not coins or have < len(coins) * DATA_READY_RATIO:
            self._emit_activity(f"기간 시세를 모으는 중입니다 ({have}/{len(coins)}) — 다 모이면 매수 조건을 확인합니다", 'info', 'bi-hourglass-split')
            return
        with self._lock:
            self._check_buys(rules, data)

    def _allow(self, rules: Dict) -> Dict[str, bool]:
        """스테이블코인 · 유의 종목 · 상장폐지 예정을 살 수 있게 했는지 — 모드마다 따로 (rules['allow'][모드])"""
        mode = self.account_mode if self.mode == 'live' else 'sim'
        per = (rules.get('allow') or {}).get(mode) if isinstance(rules.get('allow'), dict) else None
        if per is None:                                      # 예전: 맨 위 한 값 → 시뮬레이션에만
            per = {k: rules.get(k) for k in ('include_stable', 'allow_warning', 'allow_delist')} if mode == 'sim' else {}
        return {'allow_stable': bool(per.get('include_stable')),
                'allow_warning': bool(per.get('allow_warning')),
                'allow_delist': bool(per.get('allow_delist'))}

    def _universe(self, rules: Dict, coins: List[Dict], data: Optional[Dict] = None) -> List[Dict]:
        """살 수 있는 코인 = 매매대상: 사용자 제외 코인 · 유의 · 상장폐지 예정 빼고 (고르면 시뮬레이션에서 풀 수 있음, 스테이블은 조건 계산에서 뺌)
        + 직접 고른 코인(only)이 있으면 그 코인만 + 매매대상 성격(target)이 있으면 그 성격에 맞는 코인만 ('그리고' 기본 · '또는' 고름)"""
        exclude = {str(x).strip().upper().replace('KRW-', '') for x in (rules.get('exclude') or []) if str(x).strip()}
        allow = {**self._allow(rules), 'allow_stable': True}
        out = [c for c in coins if c.get('current_price') and c['currency'] not in exclude and not cd.blocked_kind(c, **allow)]
        only = {str(x).strip().upper().replace('KRW-', '') for x in (rules.get('only') or []) if str(x).strip()}
        if only:
            out = [c for c in out if c['currency'] in only]
        targets = rule_list(rules, 'target')
        # '지금 보유한 코인' 대상이거나 내 조건이 '보유' 항목을 쓰면 코인마다 held 를 붙임
        # (전에는 대상일 때만 붙여, 내 조건의 '보유 = 예'는 늘 안 맞고 '아니오'는 늘 맞았음 — 2026-10-15)
        if out and (any(t.get('id') == 'held_coins' for t in targets) or self._uses_held(rules, data)):
            out = self._with_held(out)
        if not targets or not out:
            return out
        data = data or {}
        sets = []
        for rule in targets:
            cond = cd.resolve(rule['id'], self._custom(data))
            if not cond:
                continue
            hits = cd.run_condition(cond, rule.get('params'), out, data.get('lows') or {}, data.get('hourly'), include_stable=True)
            sets.append({h['currency'] for h in hits})
        if not sets:
            return out
        keep = set.union(*sets) if (rules.get('target_match') == 'any') else set.intersection(*sets)
        return [c for c in out if c['currency'] in keep]

    def _uses_held(self, rules: Dict, data: Optional[Dict] = None) -> bool:
        """매수 · 매매대상에 쓴 내 조건 중 '보유' 항목(metric held)을 쓰는 것이 있나"""
        ids = {r.get('id') for side in ('buy', 'target') for r in rule_list(rules, side)}
        return any(c.get('id') in ids and any((x or {}).get('metric') == 'held' for x in (c.get('rules') or []))
                   for c in self._custom(data))

    def _with_held(self, coins: List[Dict]) -> List[Dict]:
        """'지금 보유한 코인' 매매대상: 이 계좌(이 모드)에 지금 있는 코인마다 held 를 붙인 복사본.
        금액이 작아도 들고 있으면 보유 (값이 많이 내려 작아진 코인도 봇에 맡길 수 있게).
        잔고를 못 읽으면 아무것도 보유로 보지 않음 (모르면 사지 않는 쪽)"""
        pos = self.exchange.get_all_positions() if self.exchange else None
        held = {cur for cur, (qty, _avg) in (pos or {}).items() if qty > 0}
        return [dict(c, held=c['currency'] in held) for c in coins]

    def _with_bot_state(self, rules: Dict, coins: List[Dict]) -> List[Dict]:
        """이 봇의 기록이 필요한 조건이 있을 때만 코인마다 붙인 복사본:
        Hongro 전략2 = '마지막으로 판 거래가 이익이었나', 수요 구역 사기 = '이 봇이 마지막으로 사고판 시각'(같은 봉 안 되사기 막기)"""
        ids = {r.get('id') for r in rule_list(rules, 'buy')}
        if 'hongro2_buy' in ids:
            last = self._last_sell_profit()
            coins = [dict(c, bot_profit_sold=(last.get(f"KRW-{c['currency']}") or 0) > 0) for c in coins]
        if 'zone_buy' in ids or 'tc_buy' in ids:          # 하루 매매 전략도 '구역 한 번만'에 씀
            ts = self._last_trade_ts()
            coins = [dict(c, bot_last_trade_ts=ts.get(f"KRW-{c['currency']}")) for c in coins]
        return coins

    def _last_trade_ts(self) -> Dict[str, float]:
        """코인마다 이 봇이 마지막으로 사거나 판 시각 (trades.json 의 time = 이 PC 시각, 파일이 바뀔 때만 다시 읽음)"""
        path = self.data_dir / 'trades.json'
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cache = self.__dict__.get('_last_trade_cache')
        if not cache or cache[0] != mtime:
            try:
                items = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                items = []
            out = {}
            for t in items:
                try:
                    at = datetime.fromisoformat(str(t.get('time'))).timestamp()
                except (TypeError, ValueError):
                    continue
                sym = t.get('symbol')
                if sym and at > out.get(sym, 0):
                    out[sym] = at
            cache = (mtime, out)
            self._last_trade_cache = cache
        return cache[1]

    def _last_sell_profit(self) -> Dict[str, float]:
        """코인마다 이 봇이 마지막으로 판 거래의 손익 (trades.json — 파일이 바뀔 때만 다시 읽음)"""
        path = self.data_dir / 'trades.json'
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cache = self.__dict__.get('_last_sell_cache')
        if not cache or cache[0] != mtime:
            try:
                items = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                items = []
            out = {}
            for t in sorted((t for t in items if t.get('side') == 'sell'), key=lambda t: t.get('time') or ''):
                out[t.get('symbol')] = float(t.get('profit') or 0)
            cache = (mtime, out)
            self._last_sell_cache = cache
        return cache[1]

    TF_NAME = {'4h': '4시간봉', '1h': '1시간봉', '15m': '15분봉', '5m': '5분봉'}

    def _lows_for(self, rule: Dict, data: Dict, title: str = '') -> Optional[Dict]:
        """규칙이 고른 시간 봉의 기간 시세 (일봉이면 lows). 4시간·1시간봉을 아직 못 받았으면 None (이번 판은 건너뜀)"""
        tf = (rule.get('params') or {}).get('tf') or 'day'
        if tf == 'day':
            return data.get('lows') or {}
        bars = (data.get('lows_tf') or {}).get(tf)
        coins = data.get('coins') or []
        have = sum(1 for c in coins if (bars or {}).get(f"KRW-{c['currency']}", {}).get('day_highs'))
        if not bars or have < len(coins) * DATA_READY_RATIO:
            self._emit_activity(f"'{title}': {self.TF_NAME.get(tf, tf)} 시세를 모으는 중입니다 — 다 모이면 확인합니다", 'info', 'bi-hourglass-split')
            return None
        htf = (rule.get('params') or {}).get('htf')
        if htf and htf != tf:                      # 두 봉을 함께 보는 규칙(하루 매매 전략): 위 봉을 'htf' 칸으로 붙임
            hb = (data.get('lows') or {}) if htf == 'day' else (data.get('lows_tf') or {}).get(htf)
            if not hb:
                self._emit_activity(f"'{title}': {self.TF_NAME.get(htf, htf)} 시세를 모으는 중입니다 — 다 모이면 확인합니다", 'info', 'bi-hourglass-split')
                return None
            return {m: {**v, 'htf': hb.get(m)} for m, v in bars.items()}
        return bars

    # ── 매도 ──
    def _check_sells(self, rules: Dict, data: Dict):
        positions = self._positions()
        if not positions:
            return
        # 매도 조건: '또는'이면 하나라도, '그리고'면 모두 맞을 때 팜 → {코인: 맞은 조건 이름}
        sell_hits: Dict[str, str] = {}
        held = {sym.replace('KRW-', '') for sym in positions}
        # 산 값 · 산 시각을 붙인 복사본 (매도 조건 — Hongro 전략2 팔기 · 산 지 N일 지남)
        def opened_ts(p):
            try:
                return datetime.fromisoformat(str(p.get('opened_at'))).timestamp() if p.get('opened_at') else None
            except ValueError:
                return None
        coins = [dict(c, bot_entry=float(positions[f"KRW-{c['currency']}"].get('entry_price') or 0) or None,
                      bot_opened_ts=opened_ts(positions[f"KRW-{c['currency']}"]))
                 for c in data.get('coins') or [] if c['currency'] in held]
        results = []                       # [(조건 이름, 맞은 코인 set)]
        ready = True
        for rule in rule_list(rules, 'sell'):
            if sr.is_partial_rule(rule):
                continue                      # 일부 팔고 다시 사기 (순간 급등·하루 급등) → spike_tick 이 맡음
            cond = cd.resolve(rule['id'], self._custom(data))
            if not cond or (cond['needs_flow'] and not data.get(cond.get('ready_key', 'flow_ready'))):
                ready = False
                continue
            lows = self._lows_for(rule, data, cond['title'])
            if lows is None:
                ready = False
                continue
            hits = cd.run_condition(cond, rule.get('params'), coins, lows,
                                    data.get('hourly'), include_stable=True)
            results.append((cond['title'], {f"KRW-{h['currency']}" for h in hits}))
        if rule_match(rules, 'sell') == 'all':
            if ready and results:
                both = set.intersection(*(s for _, s in results))
                name = ' + '.join(t for t, _ in results)
                sell_hits = {sym: name for sym in both}
        else:
            for title, syms in results:
                for sym in syms:
                    sell_hits.setdefault(sym, title)

        sell_on = self._sell_on(rules)
        if not sell_on:
            sell_hits = {}                                # 매도를 꺼 두면 매도 조건 · 익절 · 트레일링은 보지 않고 손절만
        for symbol, pos in list(positions.items()):   # 팔면 장부에서 빠지므로 복사본으로
            price = self.last_prices.get(symbol)
            if not price:
                continue
            reason = None
            if not sell_on:
                hit, msg = self.risk_manager.check_stop_loss(pos['entry_price'], price)
                if hit:
                    self._sell(symbol, price, msg, risk_exit=True)
                continue
            for check in (self.risk_manager.check_stop_loss, self.risk_manager.check_take_profit):
                hit, msg = check(pos['entry_price'], price)
                if hit:
                    reason = msg
                    break
            if not reason:
                hit, msg = self.risk_manager.check_trailing_stop(symbol, pos['entry_price'], price)
                if hit:
                    reason = msg
            risk_exit = bool(reason)
            if not reason and symbol in sell_hits:
                reason = f"매도 조건 '{sell_hits[symbol]}'에 맞음"
            if reason:
                self._sell(symbol, price, reason, risk_exit=risk_exit)

    # ── 매수 ──
    def _check_buys(self, rules: Dict, data: Dict):
        """매수 조건 여러 개: '또는'이면 하나에라도, '그리고'면 모두에 동시에 '새로 들어온' 코인을 삼"""
        if rule_match(rules, 'buy') == 'all' and len(rule_list(rules, 'buy')) > 1:
            return self._check_buys_all(rules, data)
        universe = self._with_bot_state(rules, self._universe(rules, data.get('coins') or [], data))
        fresh: List[tuple] = []          # (코인 정보, 조건 이름)
        start: List[tuple] = []          # 시작할 때 사기 ('코인 성격' 조건): (코인 정보, 조건 이름)
        refill: List[tuple] = []         # 켠 뒤 돈이 생기면 더 사기 ('코인 성격' 조건에 맞는데 안 가진 코인)
        seen = set()
        live_keys = set()
        # '켤 때는 사지 않고 새로 올라올 때만' (trade_rules.start_wait): 코인 성격 조건도 보통 조건처럼 —
        # 켤 때 맞는 코인은 기준만 잡고, 돈이 생겨도 더 사지 않음 (평균선 바로 위에서 켜 곧바로 되파는 일을 피하고 싶을 때, probe50)
        start_wait = bool(rules.get('start_wait'))
        for rule in rule_list(rules, 'buy'):
            cond = cd.resolve(rule['id'], self._custom(data))
            if cond and start_wait and cond.get('start_buy'):
                cond = {**cond, 'start_buy': False}
            if not cond:
                self._emit_activity("매수 조건을 찾지 못했습니다 (지워진 내 조건일 수 있습니다)", 'warning', 'bi-exclamation-triangle')
                continue
            key = (cond['id'], json.dumps(rule.get('params') or {}, sort_keys=True))
            live_keys.add(key)       # 자료가 잠깐 덜 모여 이번 확인을 건너뛰어도 기준은 지우지 않음 (2026-10-17, 전에는 지워져 새로 들어온 코인을 놓치고 시작 사기를 또 했음)
            if cond['needs_flow'] and not data.get(cond.get('ready_key', 'flow_ready')):
                self._flow_wait_note(cond)
                continue
            lows = self._lows_for(rule, data, cond['title'])
            if lows is None:
                continue
            hits = cd.run_condition(cond, rule.get('params'), universe, lows, data.get('hourly'),
                                    include_stable=self._allow(rules)['allow_stable'])
            now = {h['currency'] for h in hits}
            if key not in self.baselines:
                self.baselines[key] = now
                if cond.get('start_buy'):
                    self._decide('info', f"'{cond['title']}': 지금 맞는 {len(now)}개 중 거래 많은 순으로 최대 보유 수까지 사고, "
                                         f"그 뒤로는 새로 맞게 된 코인만 사겠습니다")
                    start += [(h, cond['title']) for h in hits if h['currency'] not in seen]
                    seen.update(h['currency'] for h in hits)
                else:
                    self._decide('info', f"'{cond['title']}' 기준을 잡았습니다: 지금 맞는 {len(now)}개는 사지 않고, 새로 들어오는 코인만 사겠습니다")
                continue
            before = self.baselines[key]
            self.baselines[key] = now
            waiting = getattr(self, '_skipped_taken', {})
            for h in hits:
                cur = h['currency']
                if cur in seen:
                    continue
                if cur not in before:
                    seen.add(cur)
                    fresh.append((h, cond['title']))
                elif cur in waiting:                          # 전에 다른 봇 것이라 건너뛴 코인: 다시 시도
                    seen.add(cur)
                    (start if cond.get('start_buy') else fresh).append((h, cond['title']))
                elif cond.get('start_buy'):
                    seen.add(cur)
                    refill.append((h, cond['title']))
        # 규칙에서 뺀 조건의 기준은 지움 (다시 넣으면 기준부터 새로)
        for key in list(self.baselines):
            if key not in live_keys:
                del self.baselines[key]
        if start:
            start.sort(key=lambda x: x[0].get('trade_value_24h') or 0, reverse=True)
            self._buy_fresh(start, at_start=True)
        self._buy_fresh(fresh)
        if refill:
            self._buy_refill(refill)

    def _dca_tick(self, config: Dict, coins: List[Dict]):
        """나눠 사기: 정한 코인을 every_days 일마다 같은 금액씩 (켠 날 첫 번째) — 마지막 산 때는 dca.json (다시 켜도 기억)
        돈이 모자라면 남은 만큼 (5,000원 아래면 멈추고 들고 있기만)"""
        d = dca_settings(config.get('dca'))
        if not d or not self.exchange:
            return
        path = Path(self.data_dir) / 'dca.json'
        try:
            st = json.loads(path.read_text(encoding='utf-8')) or {}
        except Exception:
            st = {}
        now = datetime.now()
        try:
            if st.get('last') and now < datetime.fromisoformat(st['last']) + timedelta(days=d['every_days']):
                return
        except ValueError:
            pass
        coin = next((c for c in coins if c.get('currency') == d['coin'] and c.get('current_price')), None)
        if not coin or not self._new_buys_allowed():
            return
        krw = min(d['krw'], self._live_budget_left() / (1 + self.fee) * 0.999)
        if krw < 5000:
            self._note_every('dca_cash', 'info', f"나눠 사기: 남은 돈이 없어 {d['coin']}을 더 사지 않습니다 (가진 만큼 들고 있습니다)", 3600)
            return
        n = int(st.get('n') or 0) + 1
        every = f"{d['every_days']:g}일마다"
        if self._order_buy(f"KRW-{d['coin']}", coin, krw, f"나눠 사기 {d['coin']}", f"{every} {d['krw']:,.0f}원씩 나눠 사기 {n}번째"):
            try:
                Path(self.data_dir).mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'last': now.isoformat(timespec='seconds'), 'n': n}), encoding='utf-8')
            except Exception as e:
                logger.warning(f"나눠 사기 기록 저장 실패: {e}")

    def _recently_sold(self, hours: float) -> set:
        """이 봇이 최근 hours 시간 안에 판 코인 (trades.json — 파일이 바뀔 때만 다시 읽음)"""
        path = self.data_dir / 'trades.json'
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return set()
        cache = self.__dict__.get('_sold_cache')
        if not cache or cache[0] != mtime:
            try:
                items = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                items = []
            cache = (mtime, [(t.get('symbol'), t.get('time') or '') for t in items if t.get('side') == 'sell'])
            self._sold_cache = cache
        since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec='seconds')
        manual = {sym for sym, at in self._manual_sold().items() if at >= since}
        return {sym for sym, at in cache[1] if sym and at >= since} | manual

    def _manual_sold(self) -> Dict[str, str]:
        """사용자가 직접 팔아 장부에서 뺀 코인 {symbol: 그때} (manual_sold.json — 다시 켜도 기억)"""
        try:
            return json.loads((self.data_dir / 'manual_sold.json').read_text(encoding='utf-8')) or {}
        except Exception:
            return {}

    def _note_manual_sold(self, symbol: str):
        """직접 판 코인을 기억: '코인 성격' 조건으로 더 사기 · 켤 때 사기에서 REFILL_COOLDOWN_H 동안 빼려고
        (전에는 직접 판 코인을 봇이 몇 초 뒤 '돈이 생겼다'며 다시 샀음)"""
        try:
            items = self._manual_sold()
            items[symbol] = datetime.now().isoformat(timespec='seconds')
            cut = (datetime.now() - timedelta(days=7)).isoformat(timespec='seconds')
            items = {k: v for k, v in items.items() if v >= cut}
            (self.data_dir / 'manual_sold.json').write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            logger.warning(f"직접 판 코인 기억 실패: {e}")

    def _buy_refill(self, cands: List[tuple]):
        """'코인 성격' 조건(매매대상 코인 바로 사기 등): 켠 뒤에도 돈이 생기면 맞는 코인 가운데 안 가진 것을 더 삼
        — 켤 때와 같은 코인당 금액 · 돈이 모자라거나 최대 보유 수가 차면 조용히 기다림
        · 이 봇이 REFILL_COOLDOWN_H 시간 안에 판 코인은 빼고 · 한 번에 3개까지 · 거래 많은 순"""
        pos = self._positions()
        if len(pos) >= self.risk_manager.max_positions:
            return
        sold = self._recently_sold(REFILL_COOLDOWN_H)
        cands = [(h, t) for h, t in cands if f"KRW-{h['currency']}" not in pos and f"KRW-{h['currency']}" not in sold]
        if not cands:
            return
        each, _ = self.risk_manager.calculate_position_size(self._size_base(), 1.0)
        left = self.simulation_engine.available_balance if self.simulation_engine else self._live_budget_left()
        if each < 5000 or (each * (1 + self.fee) > left and not self._last_slot_fit(each, left)):   # 한 코인 살 돈이 생길 때까지 조용히 기다림
            return
        cands.sort(key=lambda x: x[0].get('trade_value_24h') or 0, reverse=True)
        self._buy_fresh(cands, refill=True)

    def _check_buys_all(self, rules: Dict, data: Dict):
        """'그리고': 모든 매수 조건에 동시에 맞는 코인 묶음을 한 조건처럼 보고, 그 묶음에 새로 들어온 코인을 삼"""
        universe = self._with_bot_state(rules, self._universe(rules, data.get('coins') or [], data))
        sets, titles, order, key_parts = [], [], [], []
        for rule in rule_list(rules, 'buy'):
            cond = cd.resolve(rule['id'], self._custom(data))
            if not cond:
                self._emit_activity("매수 조건을 찾지 못했습니다 (지워진 내 조건일 수 있습니다)", 'warning', 'bi-exclamation-triangle')
                return
            if cond['needs_flow'] and not data.get(cond.get('ready_key', 'flow_ready')):
                self._flow_wait_note(cond)
                return
            lows = self._lows_for(rule, data, cond['title'])
            if lows is None:
                return
            hits = cd.run_condition(cond, rule.get('params'), universe, lows, data.get('hourly'),
                                    include_stable=self._allow(rules)['allow_stable'])
            sets.append({h['currency'] for h in hits})
            titles.append(cond['title'])
            if not order:
                order = hits                  # 첫 조건의 정렬 순서로 삼
            key_parts.append((cond['id'], json.dumps(rule.get('params') or {}, sort_keys=True)))
        now = set.intersection(*sets) if sets else set()
        key = ('all',) + tuple(key_parts)
        name = ' + '.join(titles)
        for k in list(self.baselines):
            if k != key:
                del self.baselines[k]
        if key not in self.baselines:
            self.baselines[key] = now
            self._decide('info', f"'{name}' (모두 만족) 기준을 잡았습니다: 지금 맞는 {len(now)}개는 사지 않고, 새로 들어오는 코인만 사겠습니다")
            return
        before = self.baselines[key]
        self.baselines[key] = now
        self._buy_fresh([(h, name) for h in order if h['currency'] in now and h['currency'] not in before])

    def _buy_fresh(self, fresh: List[tuple], at_start: bool = False, refill: bool = False):
        """새로 들어온 코인 사기 (이미 가진 코인 빼고, 최대 보유 수·한 번에 3개까지)
        at_start: '코인 성격' 조건의 시작할 때 사기 — 한 번에 3개 제한 없이 최대 보유 수까지, 넘치는 코인은 조용히 건너뜀
        refill: 켠 뒤 돈이 생겨 더 사기 — 시작할 때와 같은 코인당 금액, 한 번에 3개까지, 돈 · 자리가 모자라면 조용히 멈춤"""
        if not self._new_buys_allowed() or not fresh:
            return

        fresh = [(h, t) for h, t in fresh if f"KRW-{h['currency']}" not in self._positions()]
        if at_start or refill:                        # 이 봇이 팔았거나 사용자가 직접 판 지 24시간이 안 된 코인은 켤 때도 다시 사지 않음
            sold = self._recently_sold(REFILL_COOLDOWN_H)
            fresh = [(h, t) for h, t in fresh if f"KRW-{h['currency']}" not in sold]
        if self.exchange and self._get_taken:
            taken = self._get_taken() or set()
            skipped = [h['currency'] for h, _ in fresh if f"KRW-{h['currency']}" in taken]
            waiting = self.__dict__.setdefault('_skipped_taken', {})
            for h, _ in fresh:                                # 이번에 막히지 않은 코인은 기억에서 뺌 (이제 살 수 있음)
                if h['currency'] not in skipped:
                    waiting.pop(h['currency'], None)
            new_skips = [c for c in skipped if c not in waiting]
            waiting.update({c: True for c in skipped})
            skipped = new_skips                               # 같은 코인을 매번 알리지 않음
            if skipped:
                self._decide('info', f"{', '.join(skipped[:5])}{f' 등 {len(skipped)}개' if len(skipped) > 5 else ''}는 다른 봇이 가진 코인이라 건너뜀 (같은 코인을 함께 사려면 전체 봇 보드에서 '봇 경쟁'을 켜십시오)")
            fresh = [(h, t) for h, t in fresh if f"KRW-{h['currency']}" not in taken]
        if not fresh:
            return
        room = self.risk_manager.max_positions - len(self._positions())
        if at_start or refill:
            fresh = fresh[:max(0, room)]
        if room <= 0 and refill:
            return
        if room <= 0 and at_start:
            self._decide('info', f"최대 보유 코인 수({self.risk_manager.max_positions}개)가 이미 차서 시작할 때 사기는 건너뜀")
            return
        if room <= 0:
            names = ', '.join(h['currency'] for h, _ in fresh[:5]) + (' 외' if len(fresh) > 5 else '')
            self._decide('info', f"새로 들어온 {len(fresh)}개({names})는 건너뜀 — 최대 보유 코인 수({self.risk_manager.max_positions}개)가 찼습니다")
            return
        bought = 0
        start_balance = None
        if at_start or refill:
            # 코인마다 같은 금액: 이 봇이 실제로 굴릴 수 있는 돈(이미 산 금액 + 쓸 수 있는 원화, 운용 한도까지) 기준
            # — 남은 돈 기준이면 다시 켤 때마다 점점 작게 삼, 계좌 총자산 기준이면 직접 가진 코인까지 세어 너무 크게 삼
            start_balance = self._size_base()
            each, _ = self.risk_manager.calculate_position_size(start_balance, 1.0)
        for h, title in fresh:
            if refill:
                left = self.simulation_engine.available_balance if self.simulation_engine else self._live_budget_left()
                if (each * (1 + self.fee) > left and not self._last_slot_fit(each, left)) or bought >= MAX_BUYS_PER_TICK:
                    break
            if at_start:
                left = self.simulation_engine.available_balance if self.simulation_engine else self._live_budget_left()
                if each * (1 + self.fee) > left and not self._last_slot_fit(each, left):
                    self._decide('info', (f"예산을 다 써서 {bought}개만 샀습니다" if bought else f"남은 예산({left:,.0f}원)이 코인당 금액보다 적어 더 사지 않습니다")
                                 + f" (지금 맞는 코인 {len(fresh)}개 중, 코인마다 약 {each:,.0f}원) — "
                                 f"더 많이 담으려면 한 번에 사는 비율을 줄이거나 예산을 늘려 주십시오")
                    break
            if bought >= MAX_BUYS_PER_TICK and not at_start:
                self._decide('info', f"한 번에 {MAX_BUYS_PER_TICK}개까지만 삽니다 — 나머지 {len(fresh) - bought}개는 건너뜀")
                break
            symbol = f"KRW-{h['currency']}"
            if symbol in self._positions():
                continue
            why = f"시작할 때 '{title}'에 맞음" if at_start else f"돈이 생겨 '{title}'에 맞는 코인을 더 삼" if refill else None
            if self._buy(symbol, h, title, why, size_from=start_balance):
                bought += 1

    def _new_buys_allowed(self) -> bool:
        """계정 권한(can_buy)이 끝났으면 '새 코인 사기'만 멈춤 — 실행 중에 권한이 끝났을 때의 길목.

        - 막는 것: 매수 조건에 새로 들어온 코인 사기 (_buy_fresh → _buy)
        - 막지 않는 것: 팔기·손절·익절(_check_sells), 급등 때 일부 팔기(_spike_sell),
          이미 판 몫을 되찾는 '다시 사기'(_spike_rebuy — 봇이 스스로 판 코인을 되돌리는 것이라 진행 중이면 끝까지 함)
        - 조건 기준(baselines)은 막힌 동안에도 계속 갱신 → 권한이 돌아와도 그사이 들어온 코인을 몰아서 사지 않음
        - 안내는 막힐 때 한 번, 풀릴 때 한 번만
        """
        if not self._buy_on():
            self._note_every('buy_off', 'info', "매수를 꺼 두어 새로 사지 않습니다 (가진 코인의 팔기 · 손절은 그대로)")
            return False
        if self._can_buy is None:
            return True
        try:
            ok = bool(self._can_buy())
        except Exception as e:                   # 확인하다 오류가 나면 사지 않는 쪽으로 (팔기는 영향 없음)
            logger.warning(f"계정 권한 확인 오류: {e}")
            ok = False
        if not ok and not self._buy_blocked:
            self._buy_blocked = True
            self._decide('warning', "계정 확인 기간이 끝나 새 코인 사기를 멈췄습니다. 가진 코인의 팔기·손절은 계속합니다. "
                                    "설정의 계정 화면에서 다시 확인하면 이어서 삽니다")
        elif ok and self._buy_blocked:
            self._buy_blocked = False
            self._decide('info', "계정이 다시 확인돼 새 코인 사기를 이어서 합니다")
        return ok

    # ── 예산 (여러 봇: 실전 봇이 코인에 넣어 둘 수 있는 최대 금액, 0 = 제한 없음) ──
    def _budget(self) -> float:
        try:
            return float((self._get_config() or {}).get('budget') or 0)
        except (TypeError, ValueError):
            return 0.0

    def _realized_total(self) -> float:
        """이 봇이 판 손익 합계 (realized.json — trades.json 은 최근 2000건만 남아서 따로 누적, 없으면 기록에서 셈)"""
        return realized_total(Path(self.data_dir))

    def _budget_pct(self) -> float:
        try:
            return float((self._get_config() or {}).get('budget_pct') or 0)
        except (TypeError, ValueError):
            return 0.0

    def _has_limit(self) -> bool:
        return self._budget_pct() > 0 or self._budget() > 0

    def _account_total(self) -> float:
        """계좌 총자산 = 원화 + 모든 코인 평가금액 (지금 가격, 없으면 평균 매수가) — 10초 캐시"""
        cached = getattr(self, '_acct_cache', None)
        if cached and time.time() - cached[0] < 10:
            return cached[1]
        if not self.exchange:
            return 0.0
        krw = self.exchange.get_krw_balance() or 0
        pos = self.exchange.get_all_positions()
        if pos is None:                                   # 조회 실패: 지난 값 (없으면 원화만)
            return cached[1] if cached else krw
        prices = getattr(self, 'last_prices', None) or {}
        total = krw + sum(q * (prices.get(f'KRW-{c}') or avg) for c, (q, avg) in pos.items())
        self._acct_cache = (time.time(), total)
        return total

    def _budget_now(self) -> float:
        """지금 운용 한도 (0 = 새로 사지 않음 — 들고 있는 코인은 계속 팝니다)
        · 비율로 정하면: 계좌 총자산 × 비율 → 계좌가 불어나면 한도도 같이 커짐(복리)
        · 금액으로 정하면: 시작 금액 + 판 손익 → 번 만큼 다시 투자(복리), 잃으면 그만큼 줄어듦"""
        pct = self._budget_pct()
        if pct > 0:
            return max(0.0, self._account_total() * pct / 100)
        budget = self._budget()
        return max(0.0, budget + self._realized_total()) if budget > 0 else 0.0

    def _size_base(self) -> float:
        """한 번에 살 금액(1회 매수 %)의 기준 = min(운용 한도, 이 봇이 산 코인 금액 + 지금 쓸 수 있는 원화)
        계좌 총자산에는 사용자가 직접 가진 코인도 들어 있어, 그대로 쓰면 원화보다 훨씬 큰 금액으로 사게 됨"""
        budget = self._budget_now()
        # 맡긴 코인은 팔기만 맡긴 것이라 사는 크기를 키우지 않게 뺌 (2026-10-08)
        invested = sum(p['entry_price'] * p['qty'] for p in self._positions().values() if not p.get('adopted'))
        cash = self.simulation_engine.available_balance if self.simulation_engine else (self.exchange.get_krw_balance() if self.exchange else 0)
        have = invested + max(0.0, cash or 0)
        return max(0.0, min(budget, have)) if budget > 0 else max(0.0, cash or 0)

    def _live_budget_left(self) -> float:
        """새로 살 수 있는 돈 = min(계좌 원화, 운용 한도 − 이 봇이 산 코인 금액). 한도 0(0원 · 0%) = 새로 사지 않음
        맡긴 코인은 팔기만 맡긴 것이라 한도에서 빼지 않음 (_size_base 와 같게, 2026-10-17 이홍로 님 결정)"""
        krw = self.exchange.get_krw_balance() if self.exchange else 0
        budget = self._budget_now()
        invested = sum(p['entry_price'] * p['qty'] for p in self.ledger.values() if not p.get('adopted'))
        return max(0.0, min(krw, budget - invested))

    # ── 포지션 (봇이 산 코인만) ──
    def _positions(self) -> Dict[str, Dict]:
        if self.simulation_engine:
            return {s: {'qty': p.quantity, 'entry_price': p.entry_price, 'condition': p.condition,
                        'opened_at': p.entry_time.isoformat()} for s, p in self.simulation_engine.positions.items()}
        return self.ledger

    def _sync_positions(self):
        """가격 반영 (시뮬레이션) / 실제 잔고와 장부 맞추기 (실전: 사용자가 직접 팔았으면 장부도 줄임)"""
        if self.simulation_engine:
            for s in list(self.simulation_engine.positions):
                if self.last_prices.get(s):
                    self.simulation_engine.update_price(s, self.last_prices[s])
            return
        if not self.ledger or not self.exchange:
            return
        actual = self.exchange.get_all_positions()
        if actual is None:
            return  # 조회 실패: 장부는 그대로
        changed = False
        others = {}
        if self._get_others_qty:
            try:
                others = self._get_others_qty() or {}
            except Exception:
                others = {}
        shrink_seen = self.__dict__.setdefault('_shrink_seen', {})
        for symbol in list(shrink_seen):                 # 장부에서 사라진 코인의 기억은 지움
            if symbol not in self.ledger:
                shrink_seen.pop(symbol, None)
        for symbol in list(self.ledger):
            # 내 몫 = 계좌 수량 − 같은 코인을 가진 다른 봇들의 몫 (사용자가 직접 판 만큼만 줄임)
            qty = max(0.0, actual.get(symbol.replace('KRW-', ''), (0.0, 0.0))[0] - float(others.get(symbol) or 0))
            entry = self.ledger[symbol]
            price = self.last_prices.get(symbol) or entry['entry_price']
            if qty >= entry['qty'] - 1e-12:
                shrink_seen.pop(symbol, None)
            # 계좌 수량이 장부보다 줄었을 때(직접 팔았을 때)만 장부를 줄이고, 남은 게 5,000원 미만이면 뺌
            # — 값만 내려 5,000원 아래가 된 코인은 그대로 둠 (5,000원어치 산 코인은 수수료 때문에 바로 4,99x원이라,
            #   전에는 사자마자 장부에서 빠져 계좌에만 남고, 봇은 '안 가졌다'며 같은 코인을 또 샀음)
            if qty < entry['qty'] - 1e-12:
                # 한 번 본 것으로는 줄이지 않고, 다음 확인(몇 초 뒤)에도 그대로 줄어 있을 때만 줄임
                # — 다른 봇이 같은 코인을 팔 때 '계좌에서는 빠졌는데 그 봇 장부는 아직' 인 아주 짧은 틈에
                #   내 몫까지 판 것으로 잘못 보고 장부에서 지우던 문제
                seen = self.__dict__.setdefault('_shrink_seen', {})
                first = seen.get(symbol)
                if first is None or time.time() - first < 5:
                    seen.setdefault(symbol, time.time())
                    continue
                seen.pop(symbol, None)
                entry['qty'] = qty
                changed = True
                if qty <= 1e-12:                         # 직접 다 팔았을 때만 뺌 (조금 남은 코인은 봇이 계속 맡음 — 팔 때는 더 사서 합쳐 팖)
                    self._decide('info', f"{symbol} 봇 장부에서 뺐습니다 (계좌에서 다 팔렸음 · {REFILL_COOLDOWN_H}시간 동안 다시 사지 않음)")
                    self._note_manual_sold(symbol)
                    del self.ledger[symbol]
                    self.risk_manager.clear_position(symbol)
        if changed:
            self._save_ledger()

    def _sync_seed(self, config: Dict):
        """아직 시작할 때 사기를 안 했으면 지금 설정을 씀 (켠 뒤에 바꿔도 반영). 이미 샀으면 그대로"""
        if (Path(self.data_dir) / 'seeded.json').exists():
            return
        coins = seed_coin_list(config.get('seed_coins'))
        try:
            n = 0 if coins or self.mode == 'live' else int(config.get('seed_top') or 0)
        except (TypeError, ValueError):
            n = 0
        if self.mode == 'live' and coins and not self._has_limit():
            coins = []                       # 실전: 최대 투입 금액 없이는 사지 않음 (시작할 때도 막음)
        if n != getattr(self, '_seed_n', 0) or coins != getattr(self, '_seed_coins', []):
            self._seed_n, self._seed_coins = n, coins
            self._seed_pending = bool(n or coins)

    def _seed_buy(self, coins: List[Dict], rules: Dict):
        """봇 기록 폴더당 한 번만 (같은 봇 엔진이 잠깐 둘이어도 한 번만 삼)"""
        key = str(Path(self.data_dir).resolve())
        with _SEED_LOCKS_GUARD:
            lock = _SEED_LOCKS.setdefault(key, threading.Lock())
        with lock:
            if (Path(self.data_dir) / 'seeded.json').exists():
                self._seed_pending = False
                return
            return self._seed_buy_once(coins, rules)

    def _seed_buy_once(self, coins: List[Dict], rules: Dict):
        """시작할 때 한 번 사기 (최대 보유 수 제한 없이, 예산을 코인 수로 똑같이 나눠)
        - '처음에 사 둘 코인': 사용자가 고른 코인 (두 모드)
        - 시험용 '한꺼번에 사기': 거래대금 상위 N개(-1 = 전체, 시뮬레이션만)"""
        from .conditions import is_risky_coin, is_stable_coin
        sim = self.simulation_engine
        if not sim and not self.exchange:
            self._seed_pending = False
            return
        if not self._buy_on(rules):
            return                               # 매수를 꺼 두면 시작할 때 사기도 미룸 (켜면 그때 삼)
        if not sim and not self._new_buys_allowed():
            return                               # 계정 권한이 끝났으면 사지 않고 기다림 (다시 확인되면 삼)
        self._seed_pending = False
        skip = {str(x).upper() for x in (rules.get('exclude') or [])}
        if getattr(self, '_seed_coins', None):
            by = {str(c.get('currency', '')).upper(): c for c in coins if c.get('current_price')}
            taken = (self._get_taken() or set()) if (self.exchange and self._get_taken) else set()
            pick, left_out = [], []
            for cur in self._seed_coins:
                c = by.get(cur)
                why = ('업비트 원화 시장에 없음' if not c else cd.blocked_kind(c, **self._allow(rules)) if cd.blocked_kind(c, **self._allow(rules))
                       else '사지 않을 코인에 있음' if cur in skip
                       else '이미 가진 코인' if f"KRW-{cur}" in self._positions()
                       else '다른 봇이 가진 코인' if f"KRW-{cur}" in taken else '')
                if why:
                    left_out.append(f"{cur}({why})")
                else:
                    pick.append(c)
            if left_out:
                self._decide('info', f"처음에 사 둘 코인 중 {', '.join(left_out)}은 사지 않습니다")
            title = f"처음에 사 둘 코인 {len(pick)}개"
        else:
            pool = [c for c in coins if c.get('current_price') and not cd.blocked_kind(c, **self._allow(rules))
                    and c.get('currency', '').upper() not in skip and f"KRW-{c['currency']}" not in self._positions()]
            pool.sort(key=lambda c: c.get('trade_value_24h') or 0, reverse=True)
            pick = pool if self._seed_n < 0 else pool[:self._seed_n]      # -1 = 살 수 있는 코인 전체
            title = f"시작할 때 한꺼번에 사기: {'전체' if self._seed_n < 0 else '거래 상위'} {len(pick)}개"
        if not pick:
            return
        bought = 0
        if sim:
            each = sim.available_balance / len(pick) / (1 + sim.fee_rate + sim.slippage_rate) * 0.999
            for c in pick:
                symbol = f"KRW-{c['currency']}"
                price = c['current_price']
                ok, msg, trade = sim.buy(symbol, price, each / price, condition=title)
                if not ok:
                    continue
                bought += 1
                self.risk_manager.clear_position(symbol)
                self._log_trade(trade.symbol, 'buy', trade.price, trade.quantity, trade.fee, trade.total_amount, None, title)
            self._save_sim()
        else:
            # 예산(없으면 계좌 원화)을 N등분 — 주문은 실전과 같은 코드
            each = self._live_budget_left() / len(pick) / (1 + self.fee + 0.001) * 0.999
            for c in pick:
                if self.engine_thread is not None and not self.is_running:
                    break                            # 사는 도중에 멈춤
                if each >= 5000 and self._order_buy(f"KRW-{c['currency']}", c, each, title, title, quiet=True):
                    bought += 1
        try:
            Path(self.data_dir).mkdir(parents=True, exist_ok=True)
            (Path(self.data_dir) / 'seeded.json').write_text(json.dumps({'time': datetime.now().isoformat(timespec='seconds'),
                                                                        'n': bought}), encoding='utf-8')
        except Exception as e:
            logger.warning(f"한꺼번에 사기 표시 저장 실패: {e}")
        what = '처음에 사 둘 코인' if getattr(self, '_seed_coins', None) else '시작할 때 한꺼번에'
        self._decide('buy', f"{what} {bought}개를 샀습니다 (코인마다 약 {each:,.0f}원{f', {len(pick)}개 중' if bought < len(pick) else ''})")

    def _last_slot_fit(self, each: float, left: float) -> Optional[float]:
        """마지막 한 자리(최대 보유 수 - 1 개를 이미 가짐)는 코인당 금액보다 조금 모자라도 남은 돈으로 삼.
        20% × 5개처럼 비율 × 개수 = 100% 이면 수수료 · 오른 코인 몫 때문에 5번째를 영영 못 사던 것 (2026-10-15).
        코인당 금액을 원으로 정한 봇은 그대로 (늘 같은 금액). 남은 돈이 코인당 금액의 절반도 안 되면 사지 않음"""
        rm = self.risk_manager
        if rm.order_krw > 0 or len(self._positions()) + 1 < rm.max_positions:
            return None
        size = left / (1 + self.fee) * 0.998
        return size if size >= max(5000, each * 0.5) else None

    def _buy(self, symbol: str, coin: Dict, cond_title: str, reason: Optional[str] = None,
             size_from: Optional[float] = None) -> bool:
        """size_from: 금액을 이 잔고 기준으로 정함 (시작할 때 사기: 코인마다 같은 금액). 없으면 지금 잔고 기준"""
        price = coin['current_price']
        too_soon, why = self.risk_manager.order_too_soon(symbol)
        if too_soon:
            self._decide('info', f"{symbol} 매수 보류: {why}")
            return False
        balance = self.simulation_engine.available_balance if self.simulation_engine else self._live_budget_left()
        # 하루 손실 한도의 기준 = 봇이 굴리는 돈 전체(산 코인 금액 + 원화) — 남은 원화로 잡으면 한도가 너무 작아짐 (2026-10-08)
        self.risk_manager.note_balance(self._size_base() or balance)
        ok, msg = self.risk_manager.can_open_position(len(self._positions()), balance)
        if not ok:
            self._decide('info', f"{symbol} 매수 보류: {msg}")
            return False
        # 기준: 시작할 때 사기는 정해 둔 금액, 그 밖에는 봇이 굴릴 수 있는 돈(산 금액 + 원화) — 원화가 줄어도 같은 크기로 삼
        krw, qty = self.risk_manager.calculate_position_size(size_from or self._size_base() or balance, price)
        fit = self._last_slot_fit(krw, balance) if krw * (1 + self.fee) > balance else None
        if fit:
            krw, qty = fit, fit / price
        if krw * (1 + self.fee) > balance:
            why = ('운용 한도가 0이라 새로 사지 않습니다' if self._budget_now() <= 0 else '이 봇의 운용 한도를 다 썼습니다') \
                if not self.simulation_engine else '원화 잔고 부족'
            self._decide('warning', f"{symbol} 매수 보류: {why} ({balance:,.0f}원)")
            return False
        name = f"{coin.get('korean_name', '')} {symbol.replace('KRW-', '')}".strip()
        reason = reason or f"'{cond_title}'에 새로 들어옴"

        if self.simulation_engine:
            ok, msg, trade = self.simulation_engine.buy(symbol, price, qty, condition=cond_title)
            self.risk_manager.record_order(symbol)
            if not ok:
                self._decide('warning', f"{name} 가상 매수 실패: {msg}")
                return False
            self.risk_manager.clear_position(symbol)
            self._decide('buy', f"{name} 가상 매수 {trade.total_amount:,.0f}원 ({reason})", symbol)
            self._log_trade(trade.symbol, 'buy', trade.price, trade.quantity, trade.fee, trade.total_amount, None, cond_title)
            self._save_sim()
            self._trade_callback('buy', trade)
            return True

        return self._order_buy(symbol, coin, krw, cond_title, reason)

    def _order_buy(self, symbol: str, coin: Dict, krw: float, cond_title: str, reason: str, quiet: bool = False) -> bool:
        """계좌(업비트 또는 가상)에 시장가 매수 → 봇 장부·기록. quiet: 한 줄씩 판단 기록을 남기지 않음"""
        price = coin['current_price']
        name = f"{coin.get('korean_name', '')} {symbol.replace('KRW-', '')}".strip()
        pos_all = self.exchange.get_all_positions()
        if pos_all is None:
            # 주문 전 수량을 모르면 사지 않음 — 0 으로 치면 사용자가 가진 코인까지 봇 수량에 들어가 나중에 팔아 버림 (2026-10-17)
            self._decide('warning', f"{name} 매수 보류: 잔고 조회에 실패해 이번에는 사지 않습니다 (다음 확인 때 다시)")
            return False
        before = pos_all.get(symbol.replace('KRW-', ''), (0.0, 0.0))[0]
        ok, msg, order = self.exchange.buy_market_order(symbol, krw)
        self.risk_manager.record_order(symbol)  # 실패해도 곧바로 다시 주문하지 않게
        if not ok:
            self._decide('warning', f"{name} 매수 주문 실패: {msg}")
            return False
        # 봇이 산 수량: 주문이 바로 체결됐으면(가상 계좌) 그 체결 수량 — 같은 코인을 다른 곳에서 동시에 사도 섞이지 않음
        # 아니면(업비트 시장가는 체결이 조금 늦음) 주문 전후 잔고 차이, 그래도 모르면 금액÷가격으로 어림
        try:
            filled = float((order or {}).get('executed_volume') or 0) if (order or {}).get('state') == 'done' else 0.0
        except (TypeError, ValueError, AttributeError):
            filled = 0.0
        after = before
        for k in range(0 if filled else 4):
            if k:
                time.sleep(1)
            after = (self.exchange.get_all_positions() or {}).get(symbol.replace('KRW-', ''), (before, 0.0))[0]
            if after > before:
                break
        got = filled or (after - before if after > before else krw / price)   # 수수료는 따로 떼므로 수량은 krw/price (2026-10-17)
        old = self.ledger.get(symbol)
        if old and float(old.get('qty') or 0) > 0:
            # 이미 가진 코인을 더 샀으면 수량을 더하고 평균 매수가를 다시 셈 (덮어쓰면 전에 산 만큼이 장부에서 사라짐)
            q0 = float(old['qty'])
            qty_all = q0 + got
            old.update({'qty': qty_all, 'entry_price': (q0 * float(old['entry_price']) + krw) / qty_all if qty_all else price})
        else:
            self.ledger[symbol] = {'qty': got, 'entry_price': krw / got if got else price,
                                   'opened_at': datetime.now().isoformat(), 'condition': cond_title}
        self._save_ledger()
        self.risk_manager.clear_position(symbol)
        if not quiet:
            self._decide('buy', f"{name} 매수 {krw:,.0f}원 ({reason})", symbol)
        self._log_trade(symbol, 'buy', price, got, krw * self.fee, krw, None, cond_title, order=order)
        return True

    def _top_up_for_sell(self, symbol: str, price: float, qty: float, cond_title: str) -> Optional[float]:
        """5,000원 미만이라 팔 수 없는 코인: TOPUP_KRW 만큼 더 사서 팔 수 있는 수량을 돌려줌. 못 사면 None (알림은 10분에 한 번)"""
        def note(text):
            waits = self.__dict__.setdefault('_small_sell_note', {})
            if time.time() - waits.get(symbol, 0) > 600:
                waits[symbol] = time.time()
                self._decide('info', text, symbol)
        krw = float(self.exchange.get_krw_balance() or 0) if self.exchange else 0.0
        if krw < TOPUP_KRW * (1 + self.fee):
            note(f"{symbol} 지금 {qty * price:,.0f}원어치라 {exchange_name(self.account_mode)} 최소 주문(5,000원)에 모자랍니다 — 더 사서 합친 뒤 팔아야 하는데 "
                 f"원화가 {TOPUP_KRW:,}원보다 적어 기다립니다")
            return None
        coin = {'current_price': price, 'currency': symbol.replace('KRW-', ''), 'korean_name': ''}
        if not self._order_buy(symbol, coin, TOPUP_KRW, cond_title, '팔기 위해 모자란 만큼 더 사기', quiet=True):
            return None
        self._decide('info', f"{symbol} {qty * price:,.0f}원어치라 그대로는 팔 수 없어 {TOPUP_KRW:,}원어치를 더 사서 합친 뒤 팝니다", symbol)
        pos = self.ledger.get(symbol) or {}
        actual = (self.exchange.get_all_positions() or {}).get(symbol.replace('KRW-', ''), (0.0, 0.0))[0]
        qty = min(float(pos.get('qty') or 0), actual)
        if qty * price < MIN_ORDER_KRW:                 # 산 게 아직 잔고에 안 보임 → 다음 확인 때 팖
            note(f"{symbol} 더 산 수량이 아직 잔고에 보이지 않아 다음 확인 때 팝니다")
            return None
        return qty

    def _sell(self, symbol: str, price: float, reason: str, risk_exit: bool = False):
        if not risk_exit:
            too_soon, why = self.risk_manager.order_too_soon(symbol)
            if too_soon:
                self._decide('info', f"{symbol} 매도 보류: {why}")
                return
        pos = self._positions().get(symbol)
        if not pos:
            return
        cond_title = pos.get('condition', '')
        if self.simulation_engine:
            ok, msg, trade, profit = self.simulation_engine.sell(symbol, price, pos['qty'])
            self.risk_manager.record_order(symbol)
            if not ok:
                self._decide('warning', f"{symbol} 가상 매도 실패: {msg}")
                return
            self.risk_manager.clear_position(symbol)
            if profit:
                self.risk_manager.update_daily_loss(profit)
            self._decide('sell', f"{symbol} 가상 매도 {profit:+,.0f}원 ({reason})", symbol)
            self._log_trade(symbol, 'sell', trade.price, trade.quantity, trade.fee, trade.total_amount, profit, reason, buy_reason=cond_title)
            self._save_sim()
            self._trade_callback('sell', trade)
            return

        pos_all = self.exchange.get_all_positions()
        if pos_all is None:
            # 조회 실패를 '계좌에 없음'으로 보면 장부만 지워 손절 · 익절이 영영 안 걸림 → 다음 확인 때 다시 (알림은 10분에 한 번, 2026-10-17)
            waits = self.__dict__.setdefault('_sell_lookup_note', {})
            if time.time() - waits.get(symbol, 0) > 600:
                waits[symbol] = time.time()
                self._decide('warning', f"{symbol} 매도 보류: 잔고 조회에 실패해 다음 확인 때 다시 봅니다 ({reason})", symbol)
            return
        actual = pos_all.get(symbol.replace('KRW-', ''), (0.0, 0.0))[0]
        qty = min(pos['qty'], actual)     # 봇이 산 만큼만 (사용자 코인은 안 건드림)
        get_free = getattr(self.exchange, 'get_balance', None)
        if qty > 0 and get_free:
            free = float(get_free(symbol) or 0)               # 주문에 묶이지 않은 수량
            if free + 1e-12 < qty:
                # 차트에서 걸어 둔 매도(지정가)가 수량을 묶고 있음 → 그 주문이 팔도록 두고 봇은 건너뜀 (알림은 10분에 한 번)
                waits = self.__dict__.setdefault('_locked_sell_note', {})
                if time.time() - waits.get(symbol, 0) > 600:
                    waits[symbol] = time.time()
                    self._decide('info', f"{symbol} 매도 보류: 걸어 둔 매도 주문으로 묶인 수량이 있어 봇이 팔지 않았습니다 "
                                         f"(팔 수 있는 수량 {free:g} / 봇 수량 {qty:g})")
                return
        if qty * price < MIN_ORDER_KRW:
            if qty <= 0:                  # 계좌에 없음 (직접 팔았음): 장부에서만 뺌
                self.ledger.pop(symbol, None)
                self._save_ledger()
                return
            # 업비트는 5,000원 미만 주문을 받지 않음 → 모자란 만큼 먼저 더 사서(5,300원) 합친 뒤 한 번에 다 팖
            # (산 금액은 장부의 평균 매수가에 들어가 손익에 그대로 반영)
            qty = self._top_up_for_sell(symbol, price, qty, cond_title)
            if qty is None:
                return
        ok, msg, placed = self.exchange.sell_market_order(symbol, qty)
        self.risk_manager.record_order(symbol)
        if not ok:
            self._decide('warning', f"{symbol} 매도 주문 실패: {msg}")
            return
        # 바로 체결된 주문(가상 계좌)은 돌려준 체결값으로 — 가상 계좌는 시세보다 0.1% 싸게 팔므로 시세로 적으면 손익이 부풀려짐 (2026-10-17)
        price, qty, sell_fee = self._fill_of(placed, price, qty)
        profit = (price - pos['entry_price']) * qty - sell_fee - pos['entry_price'] * qty * self.fee
        self.risk_manager.update_daily_loss(profit)
        self.risk_manager.clear_position(symbol)
        self.ledger.pop(symbol, None)
        self._save_ledger()
        if pos.get('adopted') and pos.get('orig_entry'):
            # 맡기기 전부터 있던 손익(원래 평균가 → 맡긴 때 가격)은 봇 성적이 아니라 '맡긴 코인 정리' 몫 (2026-10-17, 전에는 정리 단추로 팔 때만 남김)
            legacy = (float(pos['entry_price']) - float(pos['orig_entry'])) * qty
            if legacy:
                add_adopted_legacy(self.data_dir, legacy)
        self._decide('sell', f"{symbol} 매도 · 예상 손익 {profit:+,.0f}원 ({reason})", symbol)
        self._log_trade(symbol, 'sell', price, qty, sell_fee, price * qty, profit, reason, buy_reason=cond_title, order=placed)

    def _fill_of(self, placed, price: float, qty: float):
        """(판 값, 판 수량, 매도 수수료) — 계좌가 체결값(avg_price · executed_volume · paid_fee)을 돌려줬으면 그 값, 아니면(업비트 시장가는 체결이 늦음) 시세로"""
        if isinstance(placed, dict) and placed.get('state') == 'done':
            try:
                fp = float(placed.get('avg_price') or 0)
                fq = float(placed.get('executed_volume') or 0)
                ff = float(placed.get('paid_fee') or 0)
                if fp > 0 and fq > 0:
                    return fp, fq, (ff if ff > 0 else fp * fq * self.fee)
            except (TypeError, ValueError):
                pass
        return price, qty, price * qty * self.fee

    # ── 급등 때 일부 팔고 다시 사기 (봇이 산 코인만): 순간 급등(spike) · 하루 급등(surge) ──
    KIND_TEXT = {'spike': {'name': '급등', 'sell': '급등 때 일부 팔기', 'rebuy': '급등 뒤 다시 사기'},
                 'surge': {'name': '하루 급등', 'sell': '하루 급등 때 일부 팔기', 'rebuy': '하루 급등 뒤 다시 사기'}}

    def _machine(self, kind: str) -> 'sr.SpikeRebuy':
        return self.surge if kind == 'surge' else self.spike

    def surge_detect(self, data: Dict, now: Optional[float] = None) -> List[tuple]:
        """하루 급등: 봇이 가진 코인 중 24시간 전(1시간봉)보다 크게 오른 코인을 '꺾이기 기다림'으로 등록
        (tick 이 잠금 안에서 부름. 순간 급등을 진행 중인 코인은 건너뜀 — 한 코인은 한 세트만)"""
        s = sr.surge_settings(self._rules())
        if not s['enabled']:
            return []
        if self.account_mode == 'bithumb':
            return []                          # 빗썸: 꺾임을 지켜볼 실시간 가격이 없어 팔지 못하니 감지도 하지 않음 (안내는 spike_tick, 2026-10-19)
        now = now or time.time()
        hourly = data.get('hourly') or {}
        found = []
        for sym in self._positions():
            if sym in self.spike.state or sym in self.surge.state:
                continue
            price = self.last_prices.get(sym)
            info = sr.is_surge(price, cd.close_24h_of(hourly.get(sym), now), s) if price else None
            if info and self.surge.start(sym, price, info, now):
                found.append((sym, info))
                self._decide('info', f"{sym} 하루 급등 감지 (24시간 {info['rise_24h']:+.1f}%) — {s['pullback_wait_h']:g}시간 안에 "
                                     f"최고가에서 {s['pullback']:g}% 꺾이면 {s['portion']:g}%를 팝니다 (안 꺾이면 팔지 않습니다)", sym)
        return found

    def spike_tick(self, snap: Optional[Dict] = None, now: Optional[float] = None):
        """실시간 체결로 급등을 보고, 꺾이면 일부 팔고, 싸지면 나눠 다시 삼. 테스트에서는 snap·now를 넣을 수 있음
        하루 급등(surge)은 감지만 tick 에서 하고, 꺾임·다시 사기는 여기서 같은 실시간 가격으로 봄"""
        rules = self._rules()
        s = sr.settings(rules)
        su = sr.surge_settings(rules)
        if not s['enabled'] and not self.spike.state and not su['enabled'] and not self.surge.state:
            return                               # 꺼져 있어도 진행 중이던 다시 사기는 마저 함
        if snap is None:
            if not self._get_flow:
                if self.account_mode == 'bithumb' and (s['enabled'] or su['enabled']):
                    self._note_every('spike_noflow', 'warning',
                                     "급등 세트(순간 급등 · 하루 급등)는 빗썸 모드에서 동작하지 않습니다 — 실시간 체결 정보가 없어 꺾임을 지켜보지 못합니다 "
                                     "(업비트 · 시뮬레이션 모드에서만 팔고 다시 삽니다)", every=86400)
                return
            snap = self._get_flow() or {}
        prices = snap.get('prices') or {}
        with self._lock:
            positions = self._positions()
            # 봇이 이미 다 판 코인(손절·익절·매도 조건)은 진행을 멈춤 — 다시 사지 않고 현금으로 둠
            for kind in ('spike', 'surge'):
                machine = self._machine(kind)
                for sym in list(machine.state):
                    if sym not in positions:
                        machine.cancel(sym, 'position_closed')
                        self._decide('info', f"{sym} {self.KIND_TEXT[kind]['name']} 뒤 다시 사기를 멈췄습니다 — 봇이 이 코인을 모두 팔았습니다")
            if s['enabled']:
                held = set(positions) - set(self.surge.state)      # 하루 급등을 진행 중인 코인은 빼고
                self._update_spike_watch(snap, held, s)
                for sym, info in self.spike.detect(snap, held, s, now):
                    self._decide('info', f"{sym} 급등 감지 (1분 거래대금 평소의 {info['mult']:.1f}배 · 1분 {info['rise']:+.1f}% · "
                                         f"1시간 {info['rise_1h']:+.1f}%) — 최고가에서 {s['pullback']:g}% 꺾이면 {s['portion']:g}%를 팝니다", sym)
            for kind, settings in (('spike', s), ('surge', su)):
                machine = self._machine(kind)
                for sym in list(machine.state):
                    price = prices.get(sym) or self.last_prices.get(sym)
                    act = machine.step(sym, price, settings, now)
                    if not act:
                        continue
                    if act['do'] == 'sell':
                        if not self._sell_on(rules):
                            self._note_every(f'sell_off_{sym}', 'info', f"{sym} {self.KIND_TEXT[kind]['name']} 꺾임 — 매도를 꺼 두어 팔지 않았습니다")
                            continue
                        self._spike_sell(sym, price, settings, act['why'], now, kind)
                    elif act['do'] == 'cancel':
                        machine.cancel(sym, 'no_pullback', now)
                        self._decide('info', f"{sym} {self.KIND_TEXT[kind]['name']} 지켜보기를 끝냈습니다 — {act['why']}, 팔지 않았습니다", sym)
                    else:
                        if not self._buy_on(rules):
                            self._note_every(f'rebuy_off_{sym}', 'info', f"{sym} 다시 사기 차례지만 매수를 꺼 두어 사지 않았습니다")
                            continue
                        self._spike_rebuy(sym, price, act, now, kind)

    def _update_spike_watch(self, snap: Dict, held: set, s: Dict):
        """급등 지켜보기 (화면용): 1분마다 가진 코인 중 가장 가까운 3개 + 켠 뒤 가장 가까웠던 순간"""
        if not snap.get('ready'):
            self.spike_watch = {'ready': False}
            return
        minute = snap.get('minute')
        if (getattr(self, 'spike_watch', None) or {}).get('minute') == minute:
            return                                   # 같은 1분은 한 번만
        items = sr.watch_list(snap, held, s)
        best = (getattr(self, 'spike_watch', None) or {}).get('best')
        if items and (not best or (items[0]['met'], items[0]['score']) > (best['met'], best['score'])):
            best = {**items[0], 'time': datetime.now().isoformat(timespec='seconds')}
        self.spike_watch = {'ready': True, 'minute': minute, 'items': items, 'best': best, 'held': len(held)}

    def _spike_sell(self, symbol: str, price: float, s: Dict, why: str, now: Optional[float] = None, kind: str = 'spike'):
        machine, text = self._machine(kind), self.KIND_TEXT[kind]
        pos = self._positions().get(symbol)
        if not pos:
            return machine.cancel(symbol, 'position_closed')
        qty = pos['qty'] * s['portion'] / 100
        if qty * price < sr.MIN_ORDER_KRW:
            machine.cancel(symbol, 'too_small')
            return self._decide('info', f"{symbol} {text['name']}이지만 팔 금액이 5,000원 미만이라 건너뛰었습니다", symbol)
        if self.simulation_engine:
            ok, msg, trade, profit = self.simulation_engine.sell(symbol, price, qty)
            if not ok:
                machine.cancel(symbol, 'sell_failed')
                return self._decide('warning', f"{symbol} {text['name']} 가상 매도 실패: {msg}", symbol)
            got, sold_price = trade.total_amount, trade.price
            self._save_sim()
            self._trade_callback('sell', trade)
        else:
            pos_all = self.exchange.get_all_positions()
            if pos_all is None:                           # 조회 실패: 이번엔 건너뜀 (0개로 보고 팔려다 취소되지 않게, 2026-10-17)
                return self._decide('warning', f"{symbol} {text['name']} 매도 보류: 잔고 조회에 실패해 다음에 다시 봅니다", symbol)
            actual = pos_all.get(symbol.replace('KRW-', ''), (0.0, 0.0))[0]
            qty = min(qty, actual)
            ok, msg, placed = self.exchange.sell_market_order(symbol, qty)
            if not ok:
                machine.cancel(symbol, 'sell_failed')
                return self._decide('warning', f"{symbol} {text['name']} 매도 주문 실패: {msg}", symbol)
            sold_price, qty, sell_fee = self._fill_of(placed, price, qty)     # 가상 계좌 체결값 (2026-10-17)
            got = sold_price * qty - sell_fee
            profit = (sold_price - pos['entry_price']) * qty - sell_fee - pos['entry_price'] * qty * self.fee
            self.ledger[symbol]['qty'] = max(0.0, self.ledger[symbol]['qty'] - qty)
            self._save_ledger()
        self.risk_manager.record_order(symbol)
        if profit:
            self.risk_manager.update_daily_loss(profit)
        machine.on_sold(symbol, sold_price, qty, got, s, now)
        levels = ' · '.join(f"{lv:,.8g}원" for lv in machine.state[symbol]['left'])
        self._decide('sell', f"{symbol} {text['name']} 때 {s['portion']:g}% 팜 {got:,.0f}원 ({why}) — 다시 살 가격: {levels}", symbol)
        # 거래 내역 '봇 · 이유' 칸에 판 비율까지 (예: 하루 급등 때 일부 팔기 (30%)) — 봇이 가진 수량 중 몇 %
        self._log_trade(symbol, 'sell', sold_price, qty, sold_price * qty * self.fee, got, profit, f"{text['sell']} ({s['portion']:g}%)", order=locals().get('placed'))

    def _spike_rebuy(self, symbol: str, price: float, act: Dict, now: Optional[float] = None, kind: str = 'spike'):
        machine, text = self._machine(kind), self.KIND_TEXT[kind]
        pos = self._positions().get(symbol)
        if not pos:
            return machine.cancel(symbol, 'position_closed')
        krw = act['krw']
        now = now or time.time()
        past_deadline = now >= float((machine.state.get(symbol) or {}).get('deadline') or 0)

        def give_up(why: str):
            # 되살 수 없으면 접음 — 판 돈은 그대로 두고 그 코인은 보통 보유로 (전에는 5초마다 영원히 보류 경고, 2026-10-17)
            machine.cancel(symbol, 'rebuy_failed', now)
            return self._decide('warning', f"{symbol} 다시 사기 접음: {why} — 판 돈은 그대로 두고 지켜보기를 끝냅니다", symbol)
        if self.simulation_engine:
            ok, msg, trade = self.simulation_engine.add_to_position(symbol, price, krw)
            if not ok:
                return give_up(msg) if past_deadline else self._decide('warning', f"{symbol} 다시 사기 실패: {msg}", symbol)
            qty, paid = trade.quantity, trade.total_amount
            self._save_sim()
            self._trade_callback('buy', trade)
        else:
            bal = self.exchange.get_krw_balance() or 0
            krw = min(krw, bal / (1 + self.fee))          # 주문에는 수수료 몫이 더 필요 (전에는 잔고 그대로라 늘 거절됨)
            if krw < sr.MIN_ORDER_KRW:
                return give_up(f"원화 잔고 부족 ({bal:,.0f}원)")
            pos_all = self.exchange.get_all_positions()
            if pos_all is None:                           # 주문 전 수량을 모르면 사지 않음 (_order_buy 와 같은 이유)
                return self._decide('warning', f"{symbol} 다시 사기 보류: 잔고 조회에 실패해 이번에는 사지 않습니다", symbol)
            before = pos_all.get(symbol.replace('KRW-', ''), (0.0, 0.0))[0]
            ok, msg, placed = self.exchange.buy_market_order(symbol, krw)
            if not ok:
                return give_up(f"주문 실패: {msg}") if past_deadline else self._decide('warning', f"{symbol} 다시 사기 주문 실패: {msg}", symbol)
            after = before
            for _ in range(3):
                time.sleep(1)
                after = (self.exchange.get_all_positions() or {}).get(symbol.replace('KRW-', ''), (before, 0.0))[0]
                if after > before:
                    break
            qty = after - before if after > before else krw / price
            entry = self.ledger[symbol]
            entry['entry_price'] = (entry['entry_price'] * entry['qty'] + krw) / (entry['qty'] + qty)
            entry['qty'] += qty
            self._save_ledger()
            paid = krw
        self.risk_manager.record_order(symbol)
        done = machine.on_rebought(symbol, act['n'], qty, now)
        msg = f"{symbol} {'하루 급등 뒤 ' if kind == 'surge' else ''}다시 삼 {paid:,.0f}원 ({act['why']})"
        if done and done.get('coin_change') is not None:
            msg += f" — 이번 한 번으로 판 몫의 코인이 {done['coin_change']:+.1f}%"
        self._decide('buy', msg, symbol)
        self._log_trade(symbol, 'buy', price, qty, paid * self.fee, paid, None, text['rebuy'], order=locals().get('placed'))

    # ── 수익 추이 기록 (봇 현황 그래프) ──
    @property
    def equity_path(self) -> Path:
        return self.data_dir / 'equity.json'

    def _realized_profit(self) -> float:
        """실전 봇의 실현 손익 합계 (봇 거래 기록의 매도 손익)"""
        try:
            items = json.loads((self.data_dir / 'trades.json').read_text(encoding='utf-8'))
        except Exception:
            return 0.0
        return sum(float(t.get('profit') or 0) for t in items if t.get('side') == 'sell')

    def equity_now(self) -> Optional[Dict]:
        """지금 가치(v)·손익(p): 시뮬레이션 = 가상 잔고 기준, 실전 = 봇이 넣어 둔 코인 평가금 + 실현 손익"""
        if self.simulation_engine:
            v = self.simulation_engine.equity()
            return {'v': v, 'p': v - self.simulation_engine.initial_balance}
        if not self.exchange:
            return None
        invested = sum(p['entry_price'] * p['qty'] for p in self.ledger.values())
        unreal = sum((self._price_or_zero(s, p) - p['entry_price']) * p['qty'] for s, p in self.ledger.items())
        return {'v': invested + unreal, 'p': self._realized_profit() + unreal}

    def _price_or_zero(self, symbol: str, pos: Dict) -> float:
        """지금 가격 — 시세 목록이 다 있는데(50개 이상) 그 코인만 없으면 상장폐지 = 0원, 시세를 아직 못 받았으면 산 값 (summary 와 같게, 2026-10-17)"""
        prices = self.last_prices or {}
        price = prices.get(symbol)
        if price:
            return float(price)
        return 0.0 if len(prices) >= 50 else float(pos['entry_price'])

    def _record_equity(self, now: Optional[float] = None):
        now = now or time.time()
        if now - getattr(self, '_equity_last', 0) < EQUITY_EVERY_SEC:
            return
        point = self.equity_now()
        if point is None:
            return
        self._equity_last = now
        try:
            items = json.loads(self.equity_path.read_text(encoding='utf-8'))
        except Exception:
            items = []
        items = [x for x in items if x.get('t', 0) >= now - EQUITY_KEEP_SEC]
        items.append({'t': int(now), 'v': round(point['v'], 2), 'p': round(point['p'], 2)})
        try:
            self.equity_path.parent.mkdir(parents=True, exist_ok=True)
            self.equity_path.write_text(json.dumps(items), encoding='utf-8')
        except Exception as e:
            logger.warning(f"수익 추이 기록 실패: {e}")

    def sell_now(self, symbol: str, reason: str) -> bool:
        """사용자가 확인한 정리 매도 — 지금 가격으로 바로 (주문 간격 기다리지 않음). 팔렸으면 True"""
        with self._lock:
            price = (self.last_prices or {}).get(symbol)
            if not price or symbol not in self.ledger:
                return False
            self._sell(symbol, price, reason, risk_exit=True)
            return symbol not in self.ledger

    def _rebase_adopted(self):
        """맡긴 코인의 손절 · 익절 기준을 맡긴 때 가격으로 — 예전에 원래 평균가로 맡긴 코인도 지금 가격으로 한 번 바꿈"""
        changed = False
        for sym, p in self.ledger.items():
            if not p.get('adopted') or p.get('basis') == 'adopt_price':
                continue
            price = (self.last_prices or {}).get(sym)
            if not price:
                continue
            p.setdefault('orig_entry', p.get('entry_price'))
            p['entry_price'] = price
            p['basis'] = 'adopt_price'
            changed = True
            self._decide('info', f"{sym} 맡긴 코인의 기준가를 지금 가격({price:,}원)으로 바꿨습니다 — 손절 · 익절은 여기서부터 잽니다")
        if changed:
            self._save_ledger()

    def set_adopted(self, add: Dict[str, Dict], release: List[str]):
        """실행 중에 보유 코인 맡기기 · 풀기 (풀면 장부에서만 빼고 팔지 않음)"""
        with self._lock:
            for s in release:
                if (self.ledger.get(s) or {}).get('adopted'):
                    del self.ledger[s]
                    self.risk_manager.clear_position(s)
            self.ledger.update(add)
            self._save_ledger()

    # ── 저장 ──
    def _load_ledger(self):
        try:
            data = json.loads(self.ledger_path.read_text(encoding='utf-8'))
            self.ledger = {k: v for k, v in data.items() if isinstance(v, dict) and v.get('qty', 0) > 0}
        except Exception:
            self.ledger = {}

    def _save_ledger(self):
        try:
            write_json(self.ledger_path, self.ledger, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"봇 장부 저장 실패: {e}")

    def _load_sim(self):
        try:
            state = json.loads(self.sim_path.read_text(encoding='utf-8'))
            if float(state.get('initial_balance', -1)) == self.simulation_engine.initial_balance:
                self.simulation_engine.load_state(state)
        except Exception:
            pass

    def _save_sim(self):
        try:
            self.sim_path.parent.mkdir(parents=True, exist_ok=True)
            self.sim_path.write_text(json.dumps(self.simulation_engine.to_state(), ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            logger.warning(f"가상 잔고 저장 실패: {e}")

    # ── 상태·기록 ──
    def get_status(self) -> Dict:
        with self._lock:
            positions = []
            for symbol, p in self._positions().items():
                price = self.last_prices.get(symbol) or p['entry_price']
                cost = p['entry_price'] * p['qty']
                profit = (price - p['entry_price']) * p['qty']
                positions.append({'symbol': symbol, 'quantity': p['qty'], 'entry_price': p['entry_price'],
                                  'current_price': price, 'profit': profit,
                                  'profit_rate': profit / cost * 100 if cost else 0,
                                  'condition': p.get('condition', ''), 'opened_at': p.get('opened_at'), 'adopted': bool(p.get('adopted'))})
            status = {
                'is_running': self.is_running,
                'mode': self.mode,
                'rules': self._rules(),
                'positions': positions,
                'decisions': self.decisions[:30],
                'last_update_time': self.last_update_time.isoformat() if self.last_update_time else None,
                'risk': self.risk_manager.get_risk_status(),
                'buy_blocked': self._buy_blocked,      # 계정 권한이 끝나 새로 사기를 멈춘 상태
                'spike': {**self.spike.status(), 'settings': sr.settings(self._rules()),
                          'watch': getattr(self, 'spike_watch', None) if self.is_running else None},
                'surge': {**self.surge.status(), 'settings': sr.surge_settings(self._rules())},
            }
            if self.simulation_engine:
                status['simulation'] = self.simulation_engine.get_statistics()
            return status

    def set_callbacks(self, on_signal: Optional[Callable] = None, on_trade: Optional[Callable] = None,
                      on_error: Optional[Callable] = None, on_activity: Optional[Callable] = None):
        self.on_signal_callback = on_signal
        self.on_trade_callback = on_trade
        self.on_error_callback = on_error
        self.on_activity_callback = on_activity

    def _decide(self, kind: str, message: str, symbol: str = ''):
        """판단 기록 (화면의 '최근 판단') + 활동 메시지"""
        self.decisions.insert(0, {'time': datetime.now().isoformat(timespec='seconds'), 'kind': kind,
                                  'message': message, 'symbol': symbol})
        del self.decisions[50:]
        logger.info(f"[봇] {message}")
        icon = {'buy': 'bi-bag-check-fill', 'sell': 'bi-cash-coin', 'warning': 'bi-exclamation-triangle'}.get(kind, 'bi-info-circle')
        level = {'buy': 'success', 'sell': 'success', 'warning': 'warning'}.get(kind, 'info')
        self._emit_activity(message, level, icon)

    def _asset_snapshot(self, profit_now: Optional[float] = None) -> Optional[Dict]:
        """지금 이 봇의 총 자산 = 가진 코인 평가액 + 매수 가능 금액.
        rate = 봇 수익률과 같은 기준: (그때까지 판 손익 + 평가손익) ÷ 손익 전 금액 (2026-10-15, 전에는 평가손익 ÷ 산 금액만)
        profit_now = 지금 막 판 거래의 손익 (아직 realized.json 에 더하기 전)"""
        try:
            pos = self._positions()
            coins = sum(p['qty'] * self._price_or_zero(s, p) for s, p in pos.items())
            cost = sum(p['qty'] * p['entry_price'] for p in pos.values())
            cash = self.simulation_engine.available_balance if self.simulation_engine else self._live_budget_left()
            pnl = self._realized_total() + float(profit_now or 0) + (coins - cost)
            pct = self._budget_pct()
            if pct > 0 and not self.simulation_engine:
                # 비율 봇: 손익 전 한도 = (계좌 총자산 − 이 봇의 손익) × 비율 (2026-10-17, 전에는 한도에 손익의 비율 몫만 들어 벌면 크게 · 잃으면 작게 보였음)
                base = max(0.0, (self._account_total() - pnl) * pct / 100)
            else:
                base = (coins + cash) - pnl                      # 손익이 생기기 전 금액
            return {'total': round(coins + cash), 'coins': round(coins), 'cash': round(cash),
                    'rate': round(pnl / base * 100, 4) if base > 0 else 0.0,
                    'hold_rate': round((coins - cost) / cost * 100, 4) if cost else 0.0}
        except Exception as e:
            logger.debug(f"총 자산 기록 실패: {e}")
            return None

    def _log_trade(self, symbol, side, price, qty, fee, total, profit, cond_title, buy_reason=None, order=None):
        """cond_title = 이 거래의 이유 (살 때: 매수 조건, 팔 때: 손절·익절·매도 조건 등 실제 판 이유)
        buy_reason = 팔 때 그 코인을 샀던 조건 (봇별 기록에 따로 남김)
        order = 계좌가 돌려준 주문 (주문번호를 남겨 거래 내역 탭의 줄과 짝지음)"""
        cfg = self._get_config() or {}
        bot_name = cfg.get('bot_name') or ''
        # 봇별 거래 기록: trades.json = 최근 2000건(엔진 · 요약용 빠른 기록), 거래 기록 저장소 = 전부
        try:
            path = self.data_dir / 'trades.json'
            try:
                items = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                items = []
            item = {'time': datetime.now().isoformat(timespec='seconds'), 'symbol': symbol, 'side': side,
                    'price': price, 'qty': qty, 'fee': fee, 'total': total, 'profit': profit, 'reason': cond_title}
            if buy_reason:
                item['buy_reason'] = buy_reason
            if isinstance(order, dict) and order.get('uuid'):
                item['order_id'] = order['uuid']
            snap = self._asset_snapshot(profit)
            if snap:
                item['snap'] = snap                              # 그 순간의 봇 총 자산 (기록이라 그대로)
            if profit is not None:                                # 판 손익 누적 (운용 한도 = 시작 금액 + 판 손익)
                add_realized(path.parent, float(profit), items)
            items.append(item)
            write_json(path, items[-2000:], ensure_ascii=False)
        except Exception as e:
            logger.warning(f"봇 거래 기록 저장 실패: {e}")
            return
        if self._trade_store is not None and cfg.get('bot_id'):
            try:
                self._trade_store.add_bot_trade(cfg['bot_id'], bot_name, item)
            except Exception as e:
                logger.warning(f"거래 기록 저장소에 쓰기 실패: {e}")

    def _trade_callback(self, side, trade):
        if self.on_trade_callback:
            try:
                self.on_trade_callback(side, trade)
            except Exception as e:
                logger.error(f"거래 콜백 오류: {e}")

    def _emit_activity(self, message: str, level: str = 'info', icon: str = None):
        if self.on_activity_callback:
            try:
                self.on_activity_callback(message, level, icon)
            except Exception:
                pass
