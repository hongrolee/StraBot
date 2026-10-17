"""
여러 봇 관리 — 사용자가 '봇 만들기'로 만든 봇들을 저장·시작·중지하고 봇별 성적을 계산

- 봇 설정은 전역 설정의 config['bots'] 목록에 저장 (업비트·AI 키 등 연결 설정은 모든 봇 공용)
    {id, name, budget, trade_rules, risk_config, seed_top, seed_coins, created_at, was_running: {'upbit': bool, 'sim': bool}}
- 봇은 두 모드가 함께 씀. 실행·기록은 모드마다 따로:
    업비트 모드 = 실제 업비트 계좌, 시뮬레이션 모드 = 가상 계좌(VirtualAccount) — 봇 코드는 두 모드가 같음
    기록 폴더 data/bots/<id>/<upbit|sim>/ (장부·급등 진행·거래 기록·수익 추이)
- 예산 = 그 봇이 코인에 넣어 둘 수 있는 최대 금액 (0 = 제한 없음), 두 모드 공통
- 한 모드 안에서 한 코인은 한 봇만 가짐 (계좌가 하나라 봇끼리 몫이 섞이지 않게)
- 처음 켤 때 예전 설정(봇 하나 · 봇마다 모드가 있던 구조)을 옮김
시장 데이터·실시간 체결은 대시보드 서버의 것 하나를 모든 봇이 같이 읽는다.
"""
import bisect
import copy
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .bot_engine import BotEngine, add_adopted_legacy, adopted_legacy_total, dca_settings, realized_total, rule_list, rule_match, seed_coin_list, write_json


def rule_match_all(rules: Dict) -> bool:
    return rule_match(rules, 'buy') == 'all'
from .exchange_wrapper import ExchangeWrapper
from .exchanges import is_live, exchange_of, fee_rate
from .spike_rebuy import settings as spike_settings, surge_settings

logger = logging.getLogger(__name__)

MAX_BOTS = 10
SIM_MAX_BOTS = 20           # 시뮬레이션은 규칙 비교용으로 더 많이 (2026-10-13)
BOT_STYLES = ('aggressive', 'balanced', 'defensive')   # 봇 성격 꼬리표: 공격형 · 균형형 · 안정형 (사용자가 붙임, 매매에는 영향 없음)


def max_bots(mode: str) -> int:
    return SIM_MAX_BOTS if mode == 'sim' else MAX_BOTS
MODES = ('upbit', 'bithumb', 'sim')            # 실제 계좌 2곳(업비트 · 빗썸) + 가상 계좌 — 계좌 · 봇 실행 · 기록이 모드마다 따로
MODE_NAME = {'upbit': '업비트', 'bithumb': '빗썸', 'sim': '시뮬레이션'}
SIM_BOT_KRW = 10_000_000          # 시뮬레이션: 봇마다 굴릴 돈 기본값 (config sim_per_bot_krw 를 넣으면 켜짐, 없거나 0 = 예전처럼 계좌를 비율로 나눔)
DEFAULT_RISK = {'position_size_percent': 10.0, 'stop_loss_percent': 5.0, 'take_profit_percent': 10.0,
                'daily_loss_limit_percent': 10.0, 'max_positions': 3, 'risk_level': 'basic'}
DEFAULT_BUDGET = 1_000_000.0
LEGACY_FILES = ('bot_positions.json', 'sim_state.json', 'spike_state.json')
EXAMPLE_ONLY_SIM = ("예시 봇은 시뮬레이션에서만 돌아갑니다. "
                    "[복사해서 내 봇 만들기]로 내 봇을 만든 뒤 업비트 모드에서 쓰십시오.")


def example_bots() -> List[Dict]:
    """처음 설치한 사용자에게 넣어 두는 예시 봇 — 봇을 만드는 법(사는 조건·파는 조건·안전 설정)을 보여 주는 교과서식 규칙.
    수익을 기대하고 고른 규칙이 아니며, 시뮬레이션에서만 시작할 수 있음(start). 성과 숫자는 붙이지 않음."""
    now = datetime.now().isoformat(timespec='seconds')
    return [
        {'id': 'ex1', 'name': '예시 · RSI 낮을 때 사기', 'example': True, 'budget': DEFAULT_BUDGET,
         'trade_rules': {'buy': [{'id': 'rsi_low', 'params': {'rsi': 30}}], 'sell': [], 'exclude': []},
         'risk_config': {**DEFAULT_RISK, 'take_profit_percent': 5.0, 'stop_loss_percent': 3.0},
         'created_at': now, 'was_running': {}},
        {'id': 'ex2', 'name': '예시 · 이동평균 교차', 'example': True, 'budget': DEFAULT_BUDGET,
         'trade_rules': {'buy': [{'id': 'golden_cross', 'params': {'short': 5, 'long': 20, 'within': 3}}],
                         'sell': [{'id': 'dead_cross', 'params': {'short': 5, 'long': 20, 'within': 3}}], 'exclude': []},
         'risk_config': dict(DEFAULT_RISK),
         'created_at': now, 'was_running': {}},
    ]
LIVE_FILES = ('bot_positions.json', 'trades.json', 'equity.json', 'spike_state.json', 'surge_state.json')


def _num(v, default, lo=0.0, hi=1e13):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(max(v, lo), hi)


# 안전장치 값의 허용 범위 (화면 · 조작된 요청 모두 여기서 걸러짐, 2026-10-17): 키 → (최소, 최대)
RISK_RANGES = {'position_size_percent': (0, 100), 'stop_loss_percent': (0, 100), 'take_profit_percent': (0, 100000),
               'trailing_stop_percent': (0, 100), 'daily_loss_limit_percent': (0, 100), 'daily_loss_limit': (0, 1e13),
               'max_order_krw': (0, 1e13), 'order_krw': (0, 1e13)}


def clean_risk(rc: Dict) -> Dict:
    """안전장치 값 정리: 숫자가 아니거나 NaN 이면 버림(이전 값 유지), 범위 밖은 깎음, 최대 보유 수는 1 이상의 정수,
    코인당 금액은 0 아니면 5,000원 이상 (봇이 그렇게 사므로 화면도 같게). 빈 값('' · None)은 그대로 (= 안 씀 · 예전 값)"""
    out = {}
    for k, v in (rc or {}).items():
        if k in RISK_RANGES:
            if v in ('', None):
                out[k] = v
                continue
            n = _num(v, None, *RISK_RANGES[k])
            if n is None:
                continue
            if k == 'order_krw' and 0 < n < 5000:
                n = 5000.0
            out[k] = n
        elif k == 'max_positions':
            n = _num(v, None, 1, 1000)
            if n is not None:
                out[k] = int(n)
        else:
            out[k] = v
    return out


class BotManager:
    def __init__(self, config: Dict, save_config: Callable[[Dict], None], get_market_data: Callable[[], Dict],
                 get_flow: Optional[Callable[[], Dict]], data_root: Path, legacy_dir: Optional[Path] = None,
                 on_activity: Optional[Callable] = None, engine_cls=BotEngine,
                 get_mode: Optional[Callable[[], str]] = None, get_account: Optional[Callable] = None,
                 live_block: Optional[Callable[..., Optional[str]]] = None,
                 live_limit: Optional[Callable[[], Optional[int]]] = None,
                 get_mode_account: Optional[Callable[[str], object]] = None,
                 get_trade_store: Optional[Callable[[str], object]] = None,
                 get_market_data_for: Optional[Callable[[str], Dict]] = None):
        """
        Args:
            config: 전역 설정 dict (대시보드와 같은 객체 — 바꾸면 바로 반영)
            save_config: 설정 파일 저장 함수
            data_root: 봇별 폴더를 둘 곳 (data/bots)
            legacy_dir: 예전 봇 파일이 있던 곳 (data) — 처음 한 번 '기본 봇' 폴더로 옮김
            get_mode: 지금 보는 모드 ('upbit' | 'sim') — 모드를 따로 주지 않은 목록·현황·시작은 이 모드
            get_account: 시뮬레이션 모드의 가상 계좌 (VirtualAccount)
            live_block: 업비트(실제 주문) 모드를 지금 쓸 수 없으면 그 이유(문장), 쓸 수 있으면 None
                        (계정 권한표: bot_core/license.py LicenseManager.block_message). 없으면 막지 않음
        """
        self.config = config
        self._save = save_config
        self._get_market_data = get_market_data
        self._get_market_data_for = get_market_data_for     # 거래소별 시장 데이터 (빗썸 봇은 빗썸 시세로 판단) — 없으면 get_market_data
        self._get_flow = get_flow
        self.data_root = Path(data_root)
        self.legacy_dir = Path(legacy_dir) if legacy_dir else None
        self._on_activity = on_activity
        self._engine_cls = engine_cls
        self._get_mode = get_mode or (lambda: self.config.get('app_mode', 'sim'))   # 처음에는 시뮬레이션
        self._get_account = get_account                 # 시뮬레이션 가상 계좌
        self._get_mode_account = get_mode_account       # 모드별 계좌 (업비트 = 실제 계좌) — 계좌 총자산용
        self._live_block = live_block
        self._live_limit = live_limit            # 실제 모드에서 동시에 돌릴 봇 수 (요금제, None = 한도 없음)
        self._get_trade_store = get_trade_store         # 모드별 거래 기록 저장소 (bot_core/trade_store.py) — 봇 거래 전부
        self.engines: Dict[Tuple[str, str], BotEngine] = {}
        self._lock = threading.RLock()
        self._migrated = False                 # 예전 설정 옮기기는 처음 쓸 때 (만들자마자 파일을 건드리지 않게)

    # ── 모드 ──
    def mode(self, mode: Optional[str] = None) -> str:
        m = mode or self._get_mode()
        return m if m in MODES else 'sim'

    # ── 저장된 봇 ──
    def bots(self) -> List[Dict]:
        if not self._migrated:
            self._migrated = True
            self._migrate()
            self._migrate_modes()
            self._migrate_zero_budget()
            self._migrate_pct_only()
            self._migrate_alloc()
            self._migrate_by_mode()
        return self.config.setdefault('bots', [])

    def get(self, bot_id: str) -> Optional[Dict]:
        return next((b for b in self.bots() if b.get('id') == bot_id), None)

    # 봇은 모드마다 따로 만들고 지움: modes = 이 봇이 있는 모드 (예전 봇은 두 모드 모두)
    @staticmethod
    def modes_of(bot: Dict) -> List[str]:
        ms = bot.get('modes')
        return [m for m in MODES if m in ms] if isinstance(ms, list) else ['upbit', 'sim']   # 예전 봇: 업비트 · 시뮬레이션

    def in_mode(self, bot: Dict, mode: Optional[str] = None) -> bool:
        return self.mode(mode) in self.modes_of(bot)

    def bots_in(self, mode: Optional[str] = None) -> List[Dict]:
        """이 모드에 있는 봇만 (목록 · 모두 시작 · 배분 합계 등)"""
        m = self.mode(mode)
        return [b for b in self.bots() if m in self.modes_of(b)]

    def data_dir(self, bot_id: str, mode: Optional[str] = None) -> Path:
        return self.data_root / bot_id / self.mode(mode)

    def store(self, mode: Optional[str] = None):
        """이 모드의 거래 기록 저장소 (없으면 None — 그때는 trades.json 으로)"""
        try:
            return self._get_trade_store(self.mode(mode)) if self._get_trade_store else None
        except Exception:
            return None

    def _migrate_zero_budget(self):
        """0원의 뜻이 '한도 없음' → '새로 사지 않음'으로 바뀜: 예전에 0원(한도 없음)으로 둔 봇은 다른 봇이 쓰고 남은 계좌 %로 옮김"""
        bots = self.config.get('bots') or []
        changed = False
        for b in bots:
            if b.get('example') or b.get('budget_migrated'):
                continue
            b['budget_migrated'] = True
            changed = True
            if float(b.get('budget') or 0) > 0 or float(b.get('budget_pct') or 0) > 0:
                continue
            used = sum(float(o.get('budget_pct') or 0) for o in bots if o is not b)
            if used < 100:
                b['budget_pct'] = 100 - used
                logger.info(f"'{b.get('name')}' 봇: 0원(한도 없음) → 계좌 {b['budget_pct']:g}%로 옮김 (0원은 이제 '새로 사지 않음')")
        if changed:
            self._save(self.config)

    def _migrate_pct_only(self):
        """운용 한도는 계좌 %로만 나눔: 금액(시작 금액 + 판 손익)으로 정한 봇은 지금 계좌에서 차지하는 비율로 한 번 옮김
        (계좌를 못 읽으면 남은 비율) — 봇들 합계 100%를 넘지 않게"""
        bots = self.config.get('bots') or []
        todo = [b for b in bots if not b.get('example') and not b.get('pct_only')]
        if not todo:
            return
        snap = None
        for b in todo:
            b['pct_only'] = True
            budget = float(b.get('budget') or 0)
            if budget <= 0 or float(b.get('budget_pct') or 0) > 0:
                continue
            if snap is None:
                snap = self.account_snapshot() or {}
            total = float(snap.get('total') or 0)
            now = budget + realized_total(self.data_dir(b['id']))
            used = sum(float(o.get('budget_pct') or 0) for o in bots if o is not b)
            left = max(0.0, 100 - used)
            share = min(left, max(1.0, round(now / total * 100))) if total else left
            b['budget_pct'], b['budget'] = share, 0.0
            logger.info(f"'{b.get('name')}' 봇: 금액 {now:,.0f}원 → 계좌 {share:g}% (운용 한도는 이제 계좌 %로만)")
        self._save(self.config)

    # 시뮬레이션은 봇마다 정해진 돈(기본 1,000만 원)으로 따로 굴림 — 여러 봇이 계좌를 나눠 쓰지 않고, 봇 하나하나의 실력을 같은 조건으로 비교 (2026-10-13)
    def sim_per_bot(self) -> float:
        try:
            return float(self.config.get('sim_per_bot_krw', 0) or 0)
        except (TypeError, ValueError):
            return float(SIM_BOT_KRW)

    def per_bot(self, mode: Optional[str] = None) -> bool:
        return self.mode(mode) == 'sim' and self.sim_per_bot() > 0

    def budget_of(self, bot: Dict, mode: Optional[str] = None) -> float:
        """시작 금액: 시뮬레이션은 봇마다 정해진 돈, 실제 계좌는 봇 설정 금액 (비율로 정한 봇은 0)"""
        return self.sim_per_bot() if self.per_bot(mode) else float(bot.get('budget') or 0)

    def _fund_sim(self, n: int = 1):
        """시뮬레이션 봇이 새로 생기면 가상 계좌에 그 봇 몫만큼 더 넣음"""
        if self.per_bot('sim') and self._get_account and n > 0:
            try:
                self._get_account().deposit(self.sim_per_bot() * n)
            except Exception as e:
                logger.warning(f"시뮬레이션 계좌에 봇 몫 넣기 실패: {e}")

    def pct_of(self, bot: Dict, mode: Optional[str] = None) -> float:
        """이 모드의 배분 비율 (시뮬레이션 · 업비트 따로). 예전 한 값(budget_pct)만 있으면 그 값 · 시뮬레이션은 봇마다 정해진 돈이라 0"""
        m = self.mode(mode)
        if self.per_bot(m):
            return 0.0
        alloc = bot.get('alloc')
        if isinstance(alloc, dict) and m in alloc:
            return float(alloc.get(m) or 0)
        return float(bot.get('budget_pct') or 0)

    def rules_of(self, bot: Dict, mode: Optional[str] = None) -> Dict:
        """이 모드의 매매 규칙 (시뮬레이션 · 업비트 따로). 나누기 전 봇은 한 벌"""
        per = (bot.get('by_mode') or {}).get(self.mode(mode)) if isinstance(bot.get('by_mode'), dict) else None
        if isinstance(per, dict) and isinstance(per.get('trade_rules'), dict):
            return per['trade_rules']
        return bot.get('trade_rules') or {}

    def risk_of(self, bot: Dict, mode: Optional[str] = None) -> Dict:
        """이 모드의 안전장치 (1회 매수 % · 최대 보유 · 손절 · 익절 · 하루 손실 한도 …)"""
        per = (bot.get('by_mode') or {}).get(self.mode(mode)) if isinstance(bot.get('by_mode'), dict) else None
        if isinstance(per, dict) and isinstance(per.get('risk_config'), dict):
            return {**DEFAULT_RISK, **per['risk_config']}
        return {**DEFAULT_RISK, **(bot.get('risk_config') or {})}

    def _migrate_by_mode(self):
        """매매 규칙 · 안전장치를 모드마다 따로: 예전 한 벌은 두 모드에 같은 값으로 (지금 동작 그대로)"""
        changed = False
        for b in self.config.get('bots') or []:
            if isinstance(b.get('by_mode'), dict):
                continue
            b['by_mode'] = {m: {'trade_rules': copy.deepcopy(b.get('trade_rules') or {'buy': [], 'sell': [], 'exclude': []}),
                                'risk_config': {**DEFAULT_RISK, **(b.get('risk_config') or {})}} for m in MODES}
            changed = True
        if changed:
            self._save(self.config)

    def copy_mode_rules(self, bot_id: str, src: str, mode: Optional[str] = None) -> Tuple[bool, str]:
        """다른 모드의 매매 규칙 · 안전장치를 지금 모드로 가져오기 (예: 시뮬레이션에서 시험한 규칙을 업비트에)"""
        m = self.mode(mode)
        if src not in MODES or src == m:
            return False, '가져올 모드가 맞지 않습니다'
        with self._lock:
            bot = self.get(bot_id)
            if not bot:
                return False, '봇을 찾지 못했습니다'
            if not self.in_mode(bot, src):
                return False, f"{MODE_NAME[src]} 모드에는 이 봇이 없습니다"
            bm = bot.setdefault('by_mode', {})
            bm[m] = {'trade_rules': copy.deepcopy(self.rules_of(bot, src)), 'risk_config': copy.deepcopy(self.risk_of(bot, src))}
            self._save(self.config)
        return True, f"{MODE_NAME[src]}의 매매 규칙 · 안전장치를 {MODE_NAME[m]}로 가져왔습니다"

    def send_to(self, bot_id: str, dst: str, mode: Optional[str] = None) -> Tuple[bool, str]:
        """지금 모드의 봇을 다른 모드로 보내기 — 매매 규칙 · 안전장치 · 처음에 사 둘 코인을 그대로 복사.
        받는 모드에 이 봇이 없으면 새로 생기고(배분 0%, 멈춘 채), 있으면 그 모드의 규칙 · 안전장치만 바꿈. 기록은 옮기지 않음"""
        src = self.mode(mode)
        if dst not in MODES or dst == src:
            return False, '보낼 모드가 맞지 않습니다'
        with self._lock:
            bot = self.get(bot_id)
            if not bot or not self.in_mode(bot, src):
                return False, '봇을 찾지 못했습니다'
            if bot.get('example') and dst != 'sim':
                return False, EXAMPLE_ONLY_SIM
            had = self.in_mode(bot, dst)
            if not had and len(self.bots_in(dst)) >= max_bots(dst):
                return False, f"{MODE_NAME[dst]} 모드에는 봇이 이미 {max_bots(dst)}개라 더 보낼 수 없습니다"
            bm = bot.setdefault('by_mode', {})
            bm[dst] = {'trade_rules': copy.deepcopy(self.rules_of(bot, src)), 'risk_config': copy.deepcopy(self.risk_of(bot, src))}
            if not had:
                if dst == 'sim':
                    self._fund_sim()
                bot['modes'] = [x for x in MODES if x in self.modes_of(bot) + [dst]]
                alloc = bot.get('alloc') if isinstance(bot.get('alloc'), dict) else {x: float(bot.get('budget_pct') or 0) for x in MODES}
                alloc[dst] = 0.0                  # 배분은 받는 모드의 전체 봇 보드에서 정함 (합계 100% 를 넘지 않게)
                bot['alloc'] = alloc
                self._was_running(bot)[dst] = False
            self._save(self.config)
        if had:
            return True, f"{MODE_NAME[dst]} 모드의 '{bot['name']}' 봇 규칙 · 안전장치를 {MODE_NAME[src]} 것으로 바꿨습니다" + (
                ' — 실행 중이라 다음 확인부터 반영됩니다' if self.is_running(bot_id, dst) else '')
        return True, f"'{bot['name']}' 봇을 {MODE_NAME[dst]} 모드로 보냈습니다 — {MODE_NAME[dst]} 모드로 바꾸면 보입니다 (배분 0%, 멈춘 채)"

    def _migrate_alloc(self):
        """배분 비율을 모드마다 따로: 예전 한 값은 두 모드에 같은 값으로 (지금 동작 그대로)"""
        changed = False
        for b in self.config.get('bots') or []:
            if b.get('example') or isinstance(b.get('alloc'), dict):
                continue
            v = float(b.get('budget_pct') or 0)
            b['alloc'] = {m: v for m in MODES}
            changed = True
        if changed:
            self._save(self.config)

    def _new_id(self) -> str:
        while True:
            bid = 'b' + uuid.uuid4().hex[:6]
            if not self.get(bid):
                return bid

    def _migrate(self):
        """config['bots']가 없을 때 한 번만: 예전 설정(봇 하나)이 있으면 '기본 봇'으로 옮기고,
        처음 설치한 사용자(예전 규칙 없음)면 예시 봇을 넣어 둠"""
        if isinstance(self.config.get('bots'), list):
            return
        old_rules = self.config.get('trade_rules') or {}
        if self.config.get('mode') != 'live' and not (rule_list(old_rules, 'buy') or rule_list(old_rules, 'sell')):
            self.config['bots'] = example_bots()
            logger.info('처음 사용: 예시 봇 2개를 넣어 두었습니다 (시뮬레이션 전용)')
            self._save(self.config)
            return
        live = self.config.get('mode') == 'live'
        bot = {'id': 'b1', 'name': '기본 봇', 'budget': 0.0 if live else DEFAULT_BUDGET,
               'trade_rules': copy.deepcopy(self.config.get('trade_rules') or {'buy': [], 'sell': [], 'exclude': []}),
               'risk_config': {**DEFAULT_RISK, **(self.config.get('risk_config') or {})},
               'created_at': datetime.now().isoformat(timespec='seconds'), 'was_running': {}}
        self.config['bots'] = [bot]
        if self.legacy_dir and live:                 # 실전이던 봇 장부만 옮김 (예전 가상 잔고는 새 가상 계좌와 섞지 않음)
            src = self.legacy_dir / 'bot_positions.json'
            dest = self.data_dir('b1', 'upbit')
            if src.exists():
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / src.name)
                src.rename(src.with_name(src.name + '.bak'))
        logger.info("예전 봇 설정을 '기본 봇'으로 옮겼습니다")
        self._save(self.config)

    def _migrate_modes(self):
        """봇마다 모드가 있던 구조 → 두 모드 공용 봇. 예전 실전 봇 기록은 upbit/ 로, 예전 시뮬 봇 기록은 _archive_sim/ 에 보관"""
        old = [b for b in self.config.get('bots') or [] if 'mode' in b or not isinstance(b.get('was_running', {}), dict)]
        if not old:
            return
        if self.data_root.exists() and any(self.data_root.iterdir()):
            backup = self.data_root.parent / f"bots_backup_{datetime.now():%Y%m%d%H%M%S}"
            shutil.copytree(self.data_root, backup)
            logger.info(f"봇 기록을 옮기기 전에 백업했습니다: {backup}")
        for b in old:
            live = b.pop('mode', 'simulation') == 'live'
            was = b.get('was_running')
            b['was_running'] = was if isinstance(was, dict) else ({'upbit' if live else 'sim': bool(was)})
            d = self.data_root / b['id']
            files = [p for p in d.iterdir() if p.is_file()] if d.exists() else []
            if not files:
                continue
            if live:
                dest = d / 'upbit'
                dest.mkdir(parents=True, exist_ok=True)
                for p in files:
                    shutil.move(str(p), str(dest / p.name))
            else:
                keep = self.data_root / '_archive_sim' / f"{b['id']}_{datetime.now():%Y%m%d%H%M%S}"
                keep.mkdir(parents=True, exist_ok=True)
                for p in files:
                    shutil.move(str(p), str(keep / p.name))
        logger.info("봇을 업비트·시뮬레이션 두 모드 공용으로 옮겼습니다")
        self._save(self.config)

    # ── 만들기·고치기·복제·삭제 ──
    def _clean(self, spec: Dict, base: Optional[Dict] = None, mode: Optional[str] = None) -> Dict:
        b = copy.deepcopy(base or {})
        if 'name' in spec:
            b['name'] = (str(spec.get('name') or '').strip() or '이름 없는 봇')[:30]
        if 'budget' in spec:
            b['budget'] = _num(spec.get('budget'), DEFAULT_BUDGET, 0)
        if isinstance(spec.get('alloc'), dict):     # 모드별 배분 비율을 한꺼번에 (봇 복사)
            b['alloc'] = {m: _num(spec['alloc'].get(m), 0, 0, 100) for m in MODES}
        if 'budget_pct' in spec:                  # 계좌 총자산의 몇 % — 지금 모드에만 (시뮬레이션 · 업비트 따로)
            alloc = dict(b.get('alloc') or {}) if isinstance(b.get('alloc'), dict) else {m: float(b.get('budget_pct') or 0) for m in MODES}
            alloc[self.mode(mode)] = _num(spec.get('budget_pct'), 0, 0, 100)
            b['alloc'] = alloc
        if isinstance(spec.get('by_mode'), dict):    # 봇 복사: 모드별 규칙 그대로
            b['by_mode'] = copy.deepcopy(spec['by_mode'])
        new_bot = base is None
        targets = MODES if new_bot else (self.mode(mode),)       # 새 봇은 두 모드 같은 규칙으로 시작, 고칠 때는 지금 모드만
        if not isinstance(b.get('by_mode'), dict):
            b['by_mode'] = {m: {'trade_rules': copy.deepcopy(b.get('trade_rules') or {'buy': [], 'sell': [], 'exclude': []}),
                                'risk_config': {**DEFAULT_RISK, **(b.get('risk_config') or {})}} for m in MODES}
        if 'trade_rules' in spec and isinstance(spec['trade_rules'], dict):
            for m in targets:
                b['by_mode'].setdefault(m, {})['trade_rules'] = copy.deepcopy(spec['trade_rules'])
            if new_bot:
                b['trade_rules'] = copy.deepcopy(spec['trade_rules'])
        if 'risk_config' in spec and isinstance(spec['risk_config'], dict):
            rc = clean_risk(spec['risk_config'])
            for m in targets:
                per = b['by_mode'].setdefault(m, {})
                per['risk_config'] = {**DEFAULT_RISK, **(per.get('risk_config') or b.get('risk_config') or {}), **rc}
            if new_bot:
                b['risk_config'] = {**DEFAULT_RISK, **(b.get('risk_config') or {}), **rc}
        if 'seed_top' in spec:                    # 시뮬레이션 시험용: 시작하면 거래 상위 N개를 한꺼번에 삼 (0 = 안 씀)
            b['seed_top'] = int(_num(spec.get('seed_top'), 0, -1, SEED_MAX))      # -1 = 전체
        if 'seed_coins' in spec:                  # 처음에 사 둘 코인 (두 모드): 시작하면 예산을 나눠 한 번만 삼
            b['seed_coins'] = seed_coin_list(spec.get('seed_coins'))
        if 'dca' in spec:                         # 나눠 사기: 정한 코인을 N일마다 같은 금액씩 (None · 틀린 값 = 안 씀)
            v = dca_settings(spec.get('dca'))
            if v:
                b['dca'] = v
            else:
                b.pop('dca', None)
        if 'style' in spec:                       # 성격 꼬리표 (없음 = '')
            v = str(spec.get('style') or '')
            if v in BOT_STYLES:
                b['style'] = v
            else:
                b.pop('style', None)
        if b.get('seed_coins'):                   # 둘 중 하나만: 고른 코인이 있으면 '거래 상위 N개'는 안 씀
            b['seed_top'] = 0
        b.pop('mode', None)
        if not (base or {}).get('example'):      # 예시 표시는 처음 넣어 둔 봇에만 — 화면 입력으로 켜거나 끌 수 없음
            b.pop('example', None)
        b.setdefault('name', '새 봇')
        b.setdefault('budget', DEFAULT_BUDGET)
        b['budget_migrated'] = True               # 0원 = 새로 사지 않음 (예전 '한도 없음' 봇 옮기기 대상 아님)
        b['pct_only'] = True                      # 계좌 %로만 나누던 뒤에 만든 봇 (금액 → % 옮기기 대상 아님)
        b.setdefault('trade_rules', {'buy': [], 'sell': [], 'exclude': []})
        b.setdefault('risk_config', dict(DEFAULT_RISK))
        if not isinstance(b.get('was_running'), dict):
            b['was_running'] = {}
        return b

    def create(self, spec: Dict, mode: Optional[str] = None) -> Tuple[bool, str, Optional[Dict]]:
        """새 봇은 지금 모드에만 생김 (다른 모드로는 [보내기])"""
        m = self.mode(mode)
        with self._lock:
            if len(self.bots_in(m)) >= max_bots(m):
                return False, f"봇은 {max_bots(m)}개까지 만들 수 있습니다", None
            bot = self._clean(spec)
            bot.update({'id': self._new_id(), 'created_at': datetime.now().isoformat(timespec='seconds'), 'was_running': {}, 'modes': [m]})
            self.bots().append(bot)
            self._save(self.config)
            if m == 'sim':
                self._fund_sim()
            return True, f"'{bot['name']}' 봇을 만들었습니다", bot

    def update(self, bot_id: str, spec: Dict, mode: Optional[str] = None) -> Tuple[bool, str, Optional[Dict]]:
        """mode 를 주면 그 모드 것을 고침 (안 주면 지금 보는 모드)"""
        m = self.mode(mode)
        with self._lock:
            bot = self.get(bot_id)
            if not bot:
                return False, '봇을 찾지 못했습니다', None
            running = self.running_modes(bot_id)
            new = self._clean(spec, bot, m)
            pct = self.pct_of(new, m)
            if pct > 0:                           # 봇들의 비율 합계는 (이 모드에서) 100% 까지
                others = sum(self.pct_of(o, m) for o in self.bots_in(m) if o['id'] != bot_id)
                if others + pct > 100 + 1e-9:
                    return False, f"다른 봇과 합쳐 100%를 넘습니다 — 다른 봇이 {others:g}%를 쓰고 있어 이 봇은 {max(0, 100 - others):g}%까지 됩니다", None
            bot.clear()
            bot.update(new)
            self._save(self.config)
            return True, '저장했습니다' + (' — 실행 중인 봇에도 다음 확인부터 반영됩니다' if running else ''), bot

    def copy(self, bot_id: str) -> Tuple[bool, str, Optional[Dict]]:
        bot = self.get(bot_id)
        if not bot:
            return False, '봇을 찾지 못했습니다', None
        spec = {k: copy.deepcopy(bot[k]) for k in ('budget', 'trade_rules', 'risk_config', 'seed_top', 'seed_coins', 'by_mode', 'style', 'dca') if k in bot}
        if bot.get('budget_pct') or bot.get('alloc'):   # 비율은 합계 100% 를 넘을 수 있어 복사하지 않음 → 두 모드 0% (전체 봇 보드에서 나눔)
            spec['budget'] = 0
            spec['alloc'] = {m: 0.0 for m in MODES}
        # 예시를 복사하면 사용자의 봇 (예시 표시 없음 — 고친 뒤 업비트 모드에서도 쓸 수 있음)
        spec['name'] = ('내 봇 (예시에서 복사)' if bot.get('example') else f"{bot['name']} 복사본")[:30]
        return self.create(spec)

    def delete(self, bot_id: str, mode: Optional[str] = None) -> Tuple[bool, str]:
        """지금 모드에서 지우기. 다른 모드에도 있는 봇이면 이 모드 것만 (설정 · 기록), 다른 모드의 봇 · 기록은 그대로"""
        m = self.mode(mode)
        with self._lock:
            bot = self.get(bot_id)
            if not bot or not self.in_mode(bot, m):
                return False, '봇을 찾지 못했습니다'
            if self.is_running(bot_id, m):
                return False, f"{MODE_NAME[m]} 모드에서 실행 중인 봇은 지울 수 없습니다. 먼저 멈춰 주십시오."
            rest = [x for x in self.modes_of(bot) if x != m]
            if rest:
                self.engines.pop((bot_id, m), None)
                d = self.data_dir(bot_id, m)
                if d.exists():                     # 이 모드 기록은 버리지 않고 옮겨 둠
                    trash = self.data_root / '_deleted' / f"{bot_id}_{m}_{datetime.now():%Y%m%d%H%M%S}"
                    trash.parent.mkdir(parents=True, exist_ok=True)
                    self._move_dir(d, trash)
                bot['modes'] = rest
                (bot.get('by_mode') or {}).pop(m, None)
                if isinstance(bot.get('alloc'), dict):
                    bot['alloc'][m] = 0.0
                self._was_running(bot)[m] = False
                self._save(self.config)
                return True, f"'{bot['name']}' 봇을 {MODE_NAME[m]} 모드에서 지웠습니다 — {'·'.join(MODE_NAME[x] for x in rest)}의 봇과 기록은 그대로입니다"
            running = self.running_modes(bot_id)
            if running:
                return False, f"{'·'.join(MODE_NAME[x] for x in running)} 모드에서 실행 중인 봇은 지울 수 없습니다. 먼저 멈춰 주십시오."
            self.bots().remove(bot)
            for m in MODES:
                self.engines.pop((bot_id, m), None)
            d = self.data_root / bot_id
            if d.exists():                         # 지운 봇의 기록은 버리지 않고 옮겨 둠
                trash = self.data_root / '_deleted' / f"{bot_id}_{datetime.now():%Y%m%d%H%M%S}"
                trash.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(trash))
            self._save(self.config)
            return True, f"'{bot['name']}' 봇을 지웠습니다"

    @staticmethod
    def _move_dir(src: Path, dst: Path, tries: int = 6):
        """폴더 옮기기 (윈도: 다른 곳이 파일을 잠깐 쓰는 중이면 실패 → 잠시 뒤 다시). 끝내 안 되면 파일마다 옮겨 원래 폴더를 비움"""
        for i in range(tries):
            try:
                shutil.move(str(src), str(dst))
                return
            except PermissionError:
                time.sleep(0.5)
                if dst.exists() and src.exists():              # 반쯤 복사된 것은 지우고 다시
                    shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        for f in list(src.iterdir()):
            for _ in range(tries):
                try:
                    shutil.move(str(f), str(dst / f.name))
                    break
                except PermissionError:
                    time.sleep(0.5)
            else:
                logger.warning(f"초기화: {f.name} 을(를) 옮기지 못해 지웁니다")
                try:
                    (shutil.rmtree if f.is_dir() else os.remove)(f)
                except Exception as e:
                    logger.warning(f"초기화: {f.name} 지우기 실패: {e}")

    def reset(self, bot_id: str, mode: Optional[str] = None) -> Tuple[bool, str]:
        """시뮬레이션 모드에서 이 봇을 처음부터: 봇이 가진 가상 코인을 팔고 거래 기록·수익 추이를 지움 (규칙·안전장치는 그대로).
        기록은 data/bots/_reset/<id>_<시각>/ 으로 옮겨 둠. 실행 중이면 멈췄다가 다시 시작"""
        m = self.mode(mode)
        bot = self.get(bot_id)
        if not bot:
            return False, '봇을 찾지 못했습니다'
        if m != 'sim':
            return False, '업비트 모드의 기록은 실제 거래라 지울 수 없습니다'
        was_running = self.is_running(bot_id, m)
        if was_running:
            self.stop(bot_id, m)
        sold = 0
        with self._lock:
            self.engines.pop((bot_id, m), None)
            d = self.data_dir(bot_id, m)
            ledger = self._read_json(d / 'bot_positions.json', {})
            st0 = self.store(m)
            if ledger and self._get_account:
                ex = ExchangeWrapper(account=self._get_account())
                have = ex.get_all_positions() or {}
                for sym, p in ledger.items():
                    qty = min(float(p.get('qty') or 0), (have.get(sym.replace('KRW-', '')) or (0, 0))[0])
                    if qty <= 0:
                        continue
                    ok, _msg, o = ex.sell_market_order(sym, qty)
                    if not ok:
                        try:                                    # 5,000원 미만이라 못 팔면 가상 계좌에서 지움 (영영 남아 총자산에 끼던 것, 2026-10-17)
                            self._get_account().drop_dust(sym)
                        except Exception as de:
                            logger.debug(f"초기화 먼지 코인 정리 실패 ({sym}): {de}")
                        continue
                    sold += 1
                    if st0 is not None and o:                  # 거래 내역에 '직접 주문'이 아니라 이 봇의 '초기화로 정리'로 보이게
                        try:
                            px, fee = float(o.get('avg_price') or 0), float(o.get('paid_fee') or 0)
                            gross = float(o.get('trade_amount') or px * qty)
                            entry = float(p.get('entry_price') or 0)
                            st0.add_bot_trade(bot_id, bot['name'], {
                                'time': o.get('created_at') or datetime.now().astimezone().isoformat(timespec='seconds'),
                                'symbol': sym, 'side': 'sell', 'price': px, 'qty': qty, 'fee': fee, 'total': gross - fee,
                                'profit': (gross - fee - entry * qty) if entry else None, 'reason': '모두 팔고 새로 시작',
                                'buy_reason': p.get('reason') or '', 'order_id': o.get('uuid')})
                        except Exception as e:
                            logger.warning(f"초기화 매도 기록 실패 ({sym}): {e}")
            if d.exists() and any(d.iterdir()):
                keep = self.data_root / '_reset' / f"{bot_id}_{datetime.now():%Y%m%d%H%M%S}"
                keep.parent.mkdir(parents=True, exist_ok=True)
                self._move_dir(d, keep)
            st = self.store(m)
            if st is not None:                     # 저장소의 기록은 남기되 이 봇 화면에서는 지금부터만 (방금 '초기화로 정리'한 매도도 빼게 1초 뒤부터)
                st.mark_reset(bot_id, int(time.time()) + 1)
        note = f" (가진 코인 {sold}개는 팔았습니다)" if sold else ''
        if was_running:
            ok, msg = self.start(bot_id, m)
            if not ok:
                return False, f"기록은 지웠지만 다시 시작하지 못했습니다: {msg}"
            return True, f"'{bot['name']}' 봇의 시뮬레이션 기록을 지우고 새로 시작했습니다{note}"
        return True, f"'{bot['name']}' 봇의 시뮬레이션 기록을 지웠습니다{note}. [시작]을 누르면 처음부터 시작합니다"

    # ── 실행 ──
    def bot_config(self, bot_id: str, mode: Optional[str] = None) -> Dict:
        """엔진이 읽는 설정 = 전역(키 등) + 그 봇의 규칙·안전장치·예산 + 모드"""
        m = self.mode(mode)
        bot = self.get(bot_id) or {}
        cfg = {k: v for k, v in self.config.items() if k != 'bots'}
        cfg.update({'mode': 'live' if is_live(m) else 'sim', 'account_mode': m, 'fee_rate': fee_rate(m, self.config),
                    'trade_rules': self.rules_of(bot, m), 'risk_config': self.risk_of(bot, m),
                    'budget': self.budget_of(bot, m), 'budget_pct': self.pct_of(bot, m), 'bot_id': bot_id, 'bot_name': bot.get('name', ''),
                    'seed_top': seed_top(bot) if m == 'sim' else 0, 'seed_coins': seed_coin_list(bot.get('seed_coins')),
                    'dca': dca_settings(bot.get('dca'))})
        return cfg

    def is_running(self, bot_id: str, mode: Optional[str] = None) -> bool:
        e = self.engines.get((bot_id, self.mode(mode)))
        return bool(e and e.is_running)

    def running_modes(self, bot_id: str) -> List[str]:
        return [m for m in MODES if self.is_running(bot_id, m)]

    def coin_share(self, mode: Optional[str] = None) -> bool:
        """봇 경쟁: 같은 코인을 여러 봇이 함께 사고팔 수 있게 (모드마다, 기본 꺼짐 = 한 코인은 한 봇만) · 시뮬레이션 봇마다 따로 굴릴 때는 늘 켬"""
        if self.per_bot(mode):
            return True
        return bool((self.config.get('coin_share') or {}).get(self.mode(mode)))

    def set_coin_share(self, on: bool, mode: Optional[str] = None) -> Tuple[bool, str]:
        m = self.mode(mode)
        self.config.setdefault('coin_share', {})[m] = bool(on)
        self._save(self.config)
        return True, (f"{MODE_NAME[m]}: 같은 코인을 여러 봇이 함께 사고팝니다 (봇 경쟁)" if on
                      else f"{MODE_NAME[m]}: 한 코인은 한 봇만 삽니다 (이미 여러 봇이 가진 코인은 각자 팔 때까지 그대로)")

    def reconcile_stopped(self, bot_id: str, mode: Optional[str] = None, now: Optional[float] = None) -> int:
        """멈춘 봇의 장부를 실제 계좌와 맞춤 (실행 중이면 엔진이 몇 초마다 하므로 할 일 없음).
        멈춘 동안 사용자가 직접 판 코인: 다 팔렸으면 장부에서 빼고, 줄었으면 수량을 줄임.
        다른 봇이 같은 코인을 파는 아주 짧은 틈에 잘못 지우지 않게, 줄어든 것을 5초 넘게 떨어진 두 번에 걸쳐 봤을 때만 바꿈.
        돌려주는 값: 바꾼 코인 수"""
        m = self.mode(mode)
        if self.is_running(bot_id, m):
            return 0
        path = self.data_dir(bot_id, m) / 'bot_positions.json'
        ledger = self._read_json(path, {})
        if not ledger:
            return 0
        get = (lambda: self._get_mode_account(m)) if (self._get_mode_account and m != 'sim') else (self._get_account if m == 'sim' else None)
        if not get:
            return 0
        try:
            actual = ExchangeWrapper(account=get()).get_all_positions()
        except Exception as e:
            logger.debug(f"멈춘 봇 장부 맞추기: 계좌를 읽지 못함 ({e})")
            return 0
        if actual is None:
            return 0
        now = time.time() if now is None else now
        seen = self.__dict__.setdefault('_stopped_shrink_seen', {})
        others = self._others_qty(bot_id, m)
        changed, gone = 0, []
        for sym in list(ledger):
            entry = ledger[sym] or {}
            mine = max(0.0, (actual.get(sym.replace('KRW-', '')) or (0.0, 0.0))[0] - float(others.get(sym) or 0))
            key = (bot_id, m, sym)
            if mine >= float(entry.get('qty') or 0) - 1e-12:
                seen.pop(key, None)
                continue
            first = seen.get(key)
            if first is None or now - first < 5:
                seen.setdefault(key, now)
                continue
            seen.pop(key, None)
            changed += 1
            if mine <= 1e-12:
                del ledger[sym]
                gone.append(sym)
            else:
                entry['qty'] = mine
        if changed:
            try:
                write_json(path, ledger, ensure_ascii=False, indent=1)
            except Exception as e:
                logger.warning(f"멈춘 봇 장부 저장 실패: {e}")
                return 0
            if gone:                                       # 켤 때 사기 · 성격 조건으로 곧바로 다시 사지 않게 (엔진과 같은 기억)
                ms = self.data_dir(bot_id, m) / 'manual_sold.json'
                items = self._read_json(ms, {})
                stamp = datetime.now().isoformat(timespec='seconds')
                items.update({s: stamp for s in gone})
                try:
                    ms.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
                except Exception:
                    pass
            name = (self.get(bot_id) or {}).get('name', bot_id)
            logger.info(f"멈춘 봇 장부 맞춤: '{name}'({MODE_NAME[m]}) {changed}개 (계좌에서 다 팔림 {len(gone)}개)")
        return changed

    def _others_qty(self, bot_id: str, mode: str) -> Dict[str, float]:
        """같은 모드의 다른 봇들이 가진 수량 합 {심볼: 수량} — 장부 맞출 때 내 몫 계산용"""
        out: Dict[str, float] = {}
        for b in self.bots():
            if b['id'] == bot_id:
                continue
            e = self.engines.get((b['id'], mode))
            led = e.ledger if (e and e.is_running) else self._read_json(self.data_dir(b['id'], mode) / 'bot_positions.json', {})
            for s, p in led.items():
                out[s] = out.get(s, 0.0) + float((p or {}).get('qty') or 0)
        return out

    def _taken_by_others(self, bot_id: str, mode: str) -> set:
        """같은 모드의 다른 봇이 가진 코인 (한 계좌에서 한 코인은 한 봇만) — 봇 경쟁을 켜면 막지 않음"""
        if self.coin_share(mode):
            return set()
        taken = set()
        for b in self.bots():
            if b['id'] == bot_id:
                continue
            taken |= set(self._ledger_of(b['id'], mode))     # 멈춘 엔진의 옛 장부가 아니라 파일 (2026-10-17)
        return taken

    def _owners(self, bot_id: str, mode: str) -> Dict[str, str]:
        """같은 모드에서 다른 봇이 가진 코인 → 그 봇 이름"""
        out = {}
        for b in self.bots():
            if b['id'] == bot_id:
                continue
            e = self.engines.get((b['id'], mode))
            led = e.ledger if (e and e.is_running) else self._read_json(self.data_dir(b['id'], mode) / 'bot_positions.json', {})
            for s in led:
                out[s] = b.get('name', '')
        return out

    def _ledger_of(self, bot_id: str, mode: str) -> Dict:
        e = self.engines.get((bot_id, mode))
        # 멈춘 엔진이 남아 있어도 그 장부는 멈춘 때의 것 → 파일 (멈춘 동안 맞춘 결과 · reconcile_stopped)
        if e and e.is_running:
            with e._lock:                                   # tick 스레드가 파는 순간 dict 가 바뀌어 한 번씩 500 나던 것 (2026-10-17)
                return dict(e.ledger)
        return self._read_json(self.data_dir(bot_id, mode) / 'bot_positions.json', {})

    def adoptable(self, bot_id: str, prices: Optional[Dict[str, float]] = None, mode: Optional[str] = None) -> Tuple[bool, str, List[Dict]]:
        """이 봇에 맡길 수 있는 계좌 보유 코인: 상태 = adopted(이 봇에 맡김) · bought(이 봇이 산 코인) · other(다른 봇) · free"""
        m = self.mode(mode)
        if not self.get(bot_id):
            return False, '봇을 찾지 못했습니다', []
        get = (lambda: self._get_mode_account(m)) if self._get_mode_account else (self._get_account if m == 'sim' else None)
        try:
            pos = ExchangeWrapper(account=get()).get_all_positions() if get else None
        except Exception as e:
            return False, f'계좌를 읽지 못했습니다: {e}', []
        if pos is None:
            return False, '계좌를 읽지 못했습니다', []
        if not prices:
            try:
                prices = self._src_prices(m) or self._prices(m)
            except Exception:
                prices = self._prices(m)
        mine, owners = self._ledger_of(bot_id, m), self._owners(bot_id, m)
        rows = []
        for cur, (qty, avg) in pos.items():
            sym = f'KRW-{cur}'
            price = prices.get(sym)
            if not price:                                  # 원화 마켓에 없는 코인(상장폐지 등)은 맡길 수 없음
                continue
            state = ('adopted' if mine[sym].get('adopted') else 'bought') if sym in mine else ('other' if sym in owners and not self.coin_share(m) else 'free')
            if sym not in mine and sym in owners and self.coin_share(m):     # 봇 경쟁: 다른 봇 몫을 뺀 나머지만 맡길 수 있음
                qty = max(0.0, qty - self._others_qty(bot_id, m).get(sym, 0.0))
            rows.append({'currency': cur, 'symbol': sym, 'qty': qty, 'avg_price': avg, 'price': price, 'value': qty * price,
                         'state': state, 'owner': owners.get(sym, '')})
        rows.sort(key=lambda r: -r['value'])
        return True, '', rows

    def adopt_cleanup_list(self, bot_id: str, prices: Optional[Dict[str, float]] = None, mode: Optional[str] = None) -> Dict:
        """맡긴 코인 중 원래 평균 매수가 대비 손절선(봇 설정)을 넘은 코인 — 정리할지 사용자가 고름 (자동으로 팔지 않음)"""
        m = self.mode(mode)
        bot = self.get(bot_id) or {}
        stop = float(self.risk_of(bot, m).get('stop_loss_percent') or 0)
        e = self.engines.get((bot_id, m))
        prices = prices or (e.last_prices if e else None) or self._prices(m)
        rows = []
        for sym, p in self._ledger_of(bot_id, m).items():
            if not p.get('adopted'):
                continue
            orig = float(p.get('orig_entry') or p.get('entry_price') or 0)
            price = (prices or {}).get(sym)
            if not orig or not price:
                continue
            rate = (price / orig - 1) * 100
            qty = float(p.get('qty') or 0)
            rows.append({'currency': sym.replace('KRW-', ''), 'symbol': sym, 'qty': qty, 'orig': orig, 'price': price, 'rate': rate,
                         'value': qty * price, 'loss': (price - orig) * qty, 'over': stop > 0 and rate <= -stop})
        rows.sort(key=lambda r: r['rate'])
        return {'stop': stop, 'running': bool(e and e.is_running), 'coins': rows}

    def adopt_cleanup(self, bot_id: str, currencies: List[str], mode: Optional[str] = None) -> Tuple[bool, str]:
        """고른 맡긴 코인을 지금 가격으로 팔기 (사용자가 확인한 뒤) — 맡기기 전 손익은 '맡긴 코인 정리'로 따로"""
        m = self.mode(mode)
        e = self.engines.get((bot_id, m))
        if not e or not e.is_running:
            return False, '봇이 켜져 있을 때 정리할 수 있습니다 (봇을 시작한 뒤 다시 눌러 주십시오)'
        want = {str(c).upper() for c in currencies}
        done, failed = [], []
        for cur in want:
            sym = f'KRW-{cur}'
            p = dict(e.ledger.get(sym) or {})
            if not p.get('adopted'):
                continue
            orig = float(p.get('orig_entry') or p.get('entry_price') or 0)
            price = (e.last_prices or {}).get(sym) or 0
            rate = (price / orig - 1) * 100 if orig and price else 0.0
            if e.sell_now(sym, f'맡긴 코인 정리 (원래 평균가 대비 {rate:+.1f}%)'):   # 맡기기 전 몫은 엔진 _sell 이 남김 (2026-10-17)
                done.append(cur)
            else:
                failed.append(cur)
        msg = f"{len(done)}개 정리했습니다" if done else '정리한 코인이 없습니다'
        if failed:
            msg += f" · 못 판 코인: {', '.join(failed)} (5,000원 미만이거나 시세가 없습니다)"
        return bool(done), msg

    def set_adopted(self, bot_id: str, currencies: List[str], prices: Optional[Dict[str, float]] = None,
                    mode: Optional[str] = None) -> Tuple[bool, str]:
        """맡길 코인을 이 목록으로: 새로 고른 코인은 지금 수량 · 평균 매수가로 봇 장부에 넣고, 빠진 맡긴 코인은 장부에서만 뺌(팔지 않음)"""
        m = self.mode(mode)
        ok, msg, rows = self.adoptable(bot_id, prices, m)
        if not ok:
            return False, msg
        want = {str(c).upper() for c in currencies}
        by_cur = {r['currency']: r for r in rows}
        now = datetime.now().isoformat(timespec='seconds')
        add, release, skipped = {}, [], []
        for cur in want:
            r = by_cur.get(cur)
            if not r or r['state'] in ('adopted', 'bought'):
                continue
            if r['state'] == 'other':
                skipped.append(f"{cur}(다른 봇 '{r['owner']}')")
                continue
            if r['value'] < 5000:
                skipped.append(f"{cur}(5,000원 미만)")
                continue
            # 손절 · 익절 기준 = 맡긴 순간 가격 (원래 평균가로 하면 이미 많이 내린 코인이 맡기자마자 손절로 팔림 — 2026-10-08)
            add[r['symbol']] = {'qty': r['qty'], 'entry_price': r['price'], 'orig_entry': r['avg_price'] or r['price'], 'basis': 'adopt_price',
                                'opened_at': now, 'condition': '맡긴 코인', 'adopted': True}
        for r in rows:
            if r['state'] == 'adopted' and r['currency'] not in want:
                release.append(r['symbol'])
        if add:                                    # '봇 미적용' 비교선에도 넣으려고 맡긴 순간 수량 · 가격을 따로 기록 (팔아도 남음, 2026-10-15)
            lp = self.data_dir(bot_id, m) / 'adopted_lots.json'
            lots = self._read_json(lp, [])
            ts = int(datetime.now().timestamp())
            lots += [{'ts': ts, 'symbol': s, 'qty': float(v['qty']), 'cost': float(v['qty']) * float(v['entry_price'])} for s, v in add.items()]
            write_json(lp, lots, ensure_ascii=False)
        e = self.engines.get((bot_id, m))
        if e:
            e.set_adopted(add, release)
        else:
            path = self.data_dir(bot_id, m) / 'bot_positions.json'
            led = self._read_json(path, {})
            for s in release:
                led.pop(s, None)
            led.update(add)
            write_json(path, led, ensure_ascii=False, indent=1)
        parts = [f"{len(add)}개 맡겼습니다"] if add else []
        if release:
            parts.append(f"{len(release)}개 맡기기를 풀었습니다 (팔지 않고 장부에서만 뺌)")
        if skipped:
            parts.append('못 맡긴 코인: ' + ', '.join(skipped))
        return True, ' · '.join(parts) or '바뀐 게 없습니다'

    def _was_running(self, bot: Dict) -> Dict:
        if not isinstance(bot.get('was_running'), dict):
            bot['was_running'] = {}
        return bot['was_running']

    def live_blocked(self, mode: Optional[str] = None) -> Optional[str]:
        """이 모드에서 봇 시작·새로 사기가 막혀 있으면 그 이유, 아니면 None.
        실제 계좌 모드(업비트 · 빗썸)만 해당 (시뮬레이션은 계정 없이 언제나 됨). 화면은 이때 error_code 'LICENSE_REQUIRED' 로 안내
        요금제에 이 거래소가 없으면 막음 (live_block 이 mode 를 받으면 넘김 — 예전처럼 인자 없는 함수도 됨)"""
        m = self.mode(mode)
        if not is_live(m) or not self._live_block:
            return None
        try:
            try:
                return self._live_block(m) or None
            except TypeError:
                return self._live_block() or None
        except Exception as e:                # 확인하다 오류가 나면 시작하지 않는 쪽으로
            logger.warning(f"계정 권한 확인 오류: {e}")
            return '계정 권한을 확인하지 못했습니다. 설정의 계정 화면을 확인해 주십시오.'

    def _live_limit_value(self) -> Optional[int]:
        if not self._live_limit:
            return None
        try:
            return self._live_limit()
        except Exception as e:
            logger.warning(f"실제 봇 한도 확인 오류: {e}")
            return None

    def limit_blocked(self, bot_id: str, mode: Optional[str] = None) -> Optional[str]:
        """실제 모드(업비트 + 빗썸 합쳐)에서 동시에 도는 봇 수가 요금제 한도에 닿아 이 봇을 시작할 수 없으면 안내"""
        m = self.mode(mode)
        limit = self._live_limit_value()
        if not is_live(m) or limit is None:
            return None
        running = [k for k in self.engines if is_live(k[1]) and k != (bot_id, m) and self.is_running(*k)]
        if len(running) < limit:
            return None
        return (f"지금 요금제로는 실제 모드(업비트 · 빗썸 합쳐)에서 봇을 {limit}개까지 함께 돌릴 수 있습니다 "
                f"(지금 {len(running)}개 실행 중). 다른 봇을 멈추거나 요금제를 올려 주십시오.")

    def _over_limit(self, bot_id: str, m: str) -> bool:
        """요금제를 내려 한도보다 많이 돌고 있으면, 늦게 켠 봇부터 새로 사기를 멈춤 (팔기 · 손절은 계속)"""
        limit = self._live_limit_value()
        if limit is None:
            return False
        order = [k for k in self.engines if is_live(k[1]) and self.is_running(*k)]
        return (bot_id, m) in order and order.index((bot_id, m)) >= limit

    def start(self, bot_id: str, mode: Optional[str] = None) -> Tuple[bool, str]:
        m = self.mode(mode)
        with self._lock:
            # 계정 잠금: 업비트 모드 봇 시작의 한 길목 — 화면·트레이·재시작·다시 켜기(resume)가 모두 여기를 지남 (한도 세기도 잠금 안에서, 2026-10-17)
            blocked = self.live_blocked(m) or self.limit_blocked(bot_id, m)
            if blocked:
                return False, blocked
            bot = self.get(bot_id)
            if not bot:
                return False, '봇을 찾지 못했습니다'
            name = bot['name']
            if not self.in_mode(bot, m):
                return False, f"'{name}' 봇은 {MODE_NAME[m]} 모드에 없습니다"
            if bot.get('example') and m != 'sim':
                return False, EXAMPLE_ONLY_SIM
            if self.is_running(bot_id, m):
                return False, f"'{name}' 봇은 {MODE_NAME[m]} 모드에서 이미 실행 중입니다"
            if m == 'sim' and not self._get_account:
                return False, '가상 계좌를 열 수 없습니다'
            engine = self._engine_cls(self.bot_config(bot_id, m), get_config=lambda: self.bot_config(bot_id, m),
                                      get_market_data=lambda: self._market_data(m), data_dir=self.data_dir(bot_id, m),
                                      # 실시간 체결(급등 때 팔고 다시 사기)은 업비트 연결뿐 — 빗썸 봇은 2단계까지 없음
                                      get_flow=self._get_flow if exchange_of(m) == 'upbit' else None,
                                      get_taken=lambda: self._taken_by_others(bot_id, m),
                                      account=self._get_account() if m == 'sim' else None,
                                      # 실행 중에 권한이 끝나면 새로 사기만 멈춤 (팔기·손절은 계속)
                                      can_buy=(lambda: not self.live_blocked(m) and not self._over_limit(bot_id, m)) if is_live(m) else None)
            engine._get_others_qty = lambda: self._others_qty(bot_id, m)     # 같은 코인을 다른 봇도 가질 때 내 몫 계산
            engine._trade_store = self.store(m)                                # 봇 거래를 전부 남길 곳
            engine.set_callbacks(on_activity=lambda msg, l='info', i=None: self._activity(f"[{name}·{MODE_NAME[m]}] {msg}", l, i))
            ok, msg = engine.initialize()
            if not ok:
                return False, f"'{name}' 시작 실패: {msg}"
            ok, msg = engine.start()
            if not ok:
                return False, msg
            self.engines[(bot_id, m)] = engine
            self._was_running(bot)[m] = True
            self._save(self.config)
            return True, f"'{name}' 봇을 {MODE_NAME[m]} 모드에서 시작했습니다"

    def stop(self, bot_id: str, mode: Optional[str] = None, remember: bool = False) -> Tuple[bool, str]:
        """remember=True: 프로그램을 끌 때 — 다음에 켜면 다시 시작하도록 실행 표시를 남김"""
        m = self.mode(mode)
        with self._lock:
            bot = self.get(bot_id)
            e = self.engines.get((bot_id, m))
            if e and e.is_running:
                e.stop()
            if bot and not remember:
                self._was_running(bot)[m] = False
                self._save(self.config)
            return True, f"'{(bot or {}).get('name', bot_id)}' 봇을 멈췄습니다 ({MODE_NAME[m]})"

    def can_start(self, bot: Dict, mode: str) -> bool:
        if bot.get('example') and mode != 'sim':          # 예시 봇은 시뮬레이션에서만 ([모두 시작]에서도 건너뜀)
            return False
        return bool(rule_list(self.rules_of(bot, mode), 'buy') or (mode == 'sim' and seed_top(bot))
                    or seed_coin_list(bot.get('seed_coins')) or dca_settings(bot.get('dca')))

    def start_all(self, only: Optional[List[str]] = None, mode: Optional[str] = None) -> List[Tuple[str, bool, str]]:
        m = self.mode(mode)
        out = []
        for b in list(self.bots_in(m)):
            if only is not None and b['id'] not in only:
                continue
            if self.is_running(b['id'], m) or not self.can_start(b, m):
                continue
            ok, msg = self.start(b['id'], m)
            out.append((b['id'], ok, msg))
        return out

    def stop_all(self, remember: bool = False, mode: Optional[str] = None):
        """mode=None 이면 두 모드 모두"""
        for (bid, m) in list(self.engines):
            if mode is None or m == mode:
                self.stop(bid, m, remember=remember)

    def resume(self) -> List[Tuple[str, bool, str]]:
        """프로그램을 다시 켰을 때 켜져 있던 봇을 모드마다 다시 켬 (업비트 모드는 위험 고지에 동의한 적이 있을 때만)"""
        out = []
        for m in MODES:
            if is_live(m) and not self.config.get('live_risk_consent_at'):
                continue
            ids = [b['id'] for b in self.bots_in(m) if self._was_running(b).get(m)]
            if ids:
                out += self.start_all(only=ids, mode=m)
        return out

    def _market_data(self, mode: str) -> Dict:
        """이 모드 봇이 판단에 쓰는 시장 데이터 — 빗썸 모드는 빗썸 시세 (get_market_data_for), 나머지는 업비트"""
        ex = exchange_of(mode)
        if ex != 'upbit' and self._get_market_data_for:
            return self._get_market_data_for(ex)
        return self._get_market_data()

    def running_count(self, mode: Optional[str] = None) -> int:
        """지금 보는 모드(또는 주어진 모드)에서 실행 중인 봇 수"""
        m = self.mode(mode)
        return sum(1 for (bid, mm) in self.engines if mm == m and self.is_running(bid, m))

    def running_total(self) -> int:
        return sum(1 for (bid, m) in self.engines if self.is_running(bid, m))

    # ── 성적 ──
    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except Exception:
            return default

    def trades(self, bot_id: str, mode: Optional[str] = None) -> List[Dict]:
        """최근 거래 (trades.json, 최근 2000건 · 오래된 순)"""
        return self._read_json(self.data_dir(bot_id, mode) / 'trades.json', [])

    def all_trades(self, bot_id: str, mode: Optional[str] = None, symbol: Optional[str] = None) -> List[Dict]:
        """이 봇의 거래 전부 (저장소, 초기화 이후 · 오래된 순) — 저장소가 없으면 trades.json"""
        st = self.store(mode)
        if st is None:
            items = self.with_avg_cost(self.trades(bot_id, mode))
            return [t for t in items if not symbol or t.get('symbol') == symbol]
        return st.bot_trades(bot_id, symbol=symbol, newest_first=False)

    def _prices(self, mode: Optional[str] = None) -> Dict[str, float]:
        """실행 중인 봇이 마지막으로 받은 시세 — 같은 거래소 봇 것만 (업비트 · 빗썸 값이 섞이지 않게)"""
        ex = exchange_of(self.mode(mode))
        for (bid, m), e in self.engines.items():
            if exchange_of(m) == ex and e.last_prices:
                return e.last_prices
        return {}

    def _src_prices(self, mode: Optional[str] = None) -> Dict[str, float]:
        """화면 쪽 실시간 전체 시세 (그 모드 거래소, 2초 캐시) — price_source(ex) 가 없으면 빈 값"""
        src = getattr(self, 'price_source', None)
        if not src:
            return {}
        ex = exchange_of(self.mode(mode))
        try:
            return src(ex) or {}
        except TypeError:                     # 예전 모양 price_source() (테스트 등): 업비트 시세
            return (src() or {}) if ex == 'upbit' else {}

    def _live_prices(self, mode: Optional[str] = None) -> Dict[str, float]:
        """화면 쪽 실시간 전체 시세 (2초 캐시) — 없으면 빈 값"""
        try:
            return self._src_prices(mode)
        except Exception:
            return {}

    def account_snapshot(self, prices: Optional[Dict[str, float]] = None, mode: Optional[str] = None) -> Optional[Dict]:
        """이 모드(기본 = 지금 모드) 계좌: 총자산 = 원화 + 모든 코인 평가금액 (지금 가격, 없으면 평균 매수가)
        계좌 잔고(원화 · 코인 수량)만 15초 캐시하고, 평가금액은 부를 때마다 받은 시세로 셈
        — 같은 응답 안의 봇 보유 평가(board_holdings)와 같은 시세를 써서 두 숫자가 어긋나지 않게
        (전에는 mode 를 안 받아 시뮬레이션 화면에서 실전 시작 확인 창이 가상 계좌 총자산으로 한도를 보여 줬음, 2026-10-17)"""
        cached = getattr(self, '_acct_cache', None)
        m = self.mode(mode)
        if not prices:                                  # 시세: 화면 쪽 전체 시세(봇이 꺼져 있어도) → 없으면 실행 중 봇 시세
            try:
                prices = self._src_prices(m) or self._prices(m)
            except Exception:
                prices = self._prices(m)

        def value(raw):
            pos, krw = raw
            # 시세가 있으면 그 시세로, 원화 마켓에 없는 코인(상장폐지 등)은 0원 (업비트 보유 현황과 같게) · 시세를 못 받았으면 평균 매수가
            coins = sum(q * ((prices.get(f'KRW-{c}') or 0) if prices else avg) for c, (q, avg) in pos.items())
            return {'total': krw + coins, 'krw': krw, 'coins': coins}

        if cached and cached[2] == m and time.time() - cached[0] < 15:
            return value(cached[1])
        get = (lambda: self._get_mode_account(m)) if self._get_mode_account else (self._get_account if m == 'sim' else None)
        if not get:
            return None
        try:
            ex = ExchangeWrapper(account=get())         # 지금 모드 계좌 (업비트 모드면 업비트 계좌)
            pos = ex.get_all_positions()
            if pos is None:
                return value(cached[1]) if cached else None
            krw = ex.get_krw_balance() or 0
        except Exception as ex_err:
            logger.warning(f"계좌 총자산 조회 실패: {ex_err}")
            return value(cached[1]) if cached else None
        raw = (dict(pos), float(krw))
        snap = value(raw)
        self._acct_cache = (time.time(), raw, m)
        return snap

    def _account_total(self, prices: Dict[str, float], mode: Optional[str] = None) -> Optional[float]:
        snap = self.account_snapshot(prices, mode)
        return snap['total'] if snap else None

    def summary(self, bot_id: str, mode: Optional[str] = None) -> Dict:
        """목록용 한 줄: 실행·수익률·보유 수 등 (멈춘 봇은 저장된 파일로 계산) — 두 모드 같은 계산
        실현 손익(봇 거래 기록) + 평가 손익(봇 장부 × 지금 가격), 수익률은 예산 대비 (예산이 없으면 넣어 둔 돈 대비)"""
        m = self.mode(mode)
        bot = self.get(bot_id) or {}
        budget = self.budget_of(bot, m)
        e = self.engines.get((bot_id, m))
        ledger = self._ledger_of(bot_id, m)          # 멈춘 봇은 파일 (멈춘 동안 직접 판 코인을 맞춘 결과) — 전에는 옛 엔진 장부를 봤음 (2026-10-17)
        prices = self._live_prices(m) or (e.last_prices if e else None) or self._prices(m)   # 화면 숫자는 실시간 전체 시세 (보유코인 분석과 같게)
        trades = self.trades(bot_id, m)
        sells = [t for t in trades if t.get('side') == 'sell' and t.get('profit') is not None]
        realized = realized_total(self.data_dir(bot_id, m)) if (self.data_dir(bot_id, m) / 'realized.json').exists() else sum(t['profit'] for t in sells)
        adopted_loss = adopted_legacy_total(self.data_dir(bot_id, m))    # 맡기기 전부터 있던 손실을 판 몫 (봇 성적에서 뺌)
        cnt = self.trade_counts(bot_id, m)                               # 거래 수 · 승률은 기록 전부로 (저장소)
        n_trades = cnt['buys'] + cnt['sells']
        win_rate = cnt['wins'] / (cnt['wins'] + cnt['losses']) * 100 if (cnt['wins'] + cnt['losses']) else 0.0
        # 상장폐지(시세 목록이 다 있는데 그 코인만 없음) = 0원 — 팔 수 없으니 손실 (2026-10-15, 전에는 산 값으로 쳐 손실이 숨었음)
        full = bool(prices) and len(prices) >= 50
        delisted = [sym for sym in ledger if full and sym not in prices]
        unrealized = sum(((0.0 if sym in delisted else (prices.get(sym) or p['entry_price'])) - p['entry_price']) * p['qty']
                         for sym, p in ledger.items())
        invested = sum(p['entry_price'] * p['qty'] for p in ledger.values())
        pct = self.pct_of(bot, m)
        account_total = None
        if pct > 0:                                                       # 운용 한도 = 계좌 총자산 × 비율 (계좌가 불어나면 같이)
            account_total = (e._account_total() if (e and e.is_running and e.last_prices) else self._account_total(prices, m))   # 멈춘 엔진은 시세가 없음
            budget_now = max(0.0, (account_total or 0) * pct / 100)
            # 수익률 기준 = 손익 전 한도 = (계좌 총자산 − 이 봇의 손익) × 비율 (2026-10-17)
            # — 전에는 budget_now − 손익 이라 한도에 손익의 비율 몫만 들어 있어 벌면 크게(500만/550만) · 잃으면 작게 보였음
            base = max(0.0, ((account_total or 0) - realized - unrealized) * pct / 100) or invested or 0
        else:                                                             # 운용 한도 = 시작 금액 + 판 손익 (복리)
            budget_now = max(0.0, budget + realized) if budget > 0 else 0.0
            base = budget or invested or 0
        own = sum(p['entry_price'] * p['qty'] for p in ledger.values() if not p.get('adopted'))   # 맡긴 코인은 빼고 (엔진 _live_budget_left · _size_base 와 같게)
        cash = max(0.0, budget_now - own)                                # 한도에서 남은 돈 (한도 0 = 새로 사지 않음)
        if m == 'sim' and self._get_account:                             # 시뮬: 가상 계좌 원화보다 많이 쓸 수는 없음
            try:
                krw = self._get_account().get_balance('KRW')
                cash = krw if cash is None else min(cash, krw)
            except Exception:
                pass
        elif is_live(m):                                                 # 업비트 · 빗썸: 계좌 원화보다 많이 쓸 수는 없음
            snap = self.account_snapshot(prices, m)
            if snap:
                cash = min(cash, float(snap.get('krw') or 0))
        # 한 번에 살 금액의 기준 (엔진 _size_base와 같게): min(운용 한도, 봇이 산 금액 + 쓸 수 있는 원화)
        size_base = min(budget_now, own + cash) if budget_now > 0 else 0.0
        return {'id': bot_id, 'name': bot.get('name', ''), 'mode': m, 'modes': self.modes_of(bot), 'budget': budget, 'budget_now': budget_now, 'size_base': size_base,
                'budget_pct': pct, 'account_total': account_total, 'example': bool(bot.get('example')),
                'running': self.is_running(bot_id, m), 'running_modes': self.running_modes(bot_id),
                'buy_rules': len(rule_list(self.rules_of(bot, m), 'buy')),
                'seed_top': seed_top(bot) if m == 'sim' else 0,
                'seed_coins': seed_coin_list(bot.get('seed_coins')),
                'style': bot.get('style') if bot.get('style') in BOT_STYLES else '',
                'dca': dca_settings(bot.get('dca')),
                'seeded': (self.data_dir(bot_id, m) / 'seeded.json').exists(),   # 이미 시작할 때 사기를 했는지
                'spike': spike_settings(self.rules_of(bot, m))['enabled'],
                'surge': surge_settings(self.rules_of(bot, m))['enabled'],
                'buy_on': self.rules_of(bot, m).get('buy_on', True) is not False,
                'sell_on': self.rules_of(bot, m).get('sell_on', True) is not False,
                'equity': invested + unrealized, 'invested': invested, 'realized': realized, 'unrealized': unrealized, 'adopted_loss': adopted_loss,
                'profit': realized + unrealized, 'profit_rate': (realized + unrealized) / base * 100 if base else 0.0,
                # 화면에 보이는 % 는 이것 하나로: 보유코인 분석과 같은 평가수익률 = 평가손익 ÷ 지금 코인을 산 금액
                'hold_rate': unrealized / invested * 100 if invested else 0.0,
                # 봇 카드 · 보드 · 머리줄 · 비교 그래프의 수익률 (모두 이것 하나): (판 손익 + 지금 평가손익) ÷ 손익 전 금액(base)
                # — 전에는 손익이 이미 들어간 운용 한도(budget_now)로 나눠 잃으면 더 크게(−1백만/9백만 = −11.1%), 벌면 더 작게 보였음 (2026-10-15)
                'total_rate': (realized + unrealized) / base * 100 if base else 0.0, 'rate_base': base,
                'win_rate': win_rate,
                'trades': n_trades, 'holding': len(ledger), 'cash': cash, 'delisted': len(delisted)}

    def trade_counts(self, bot_id: str, mode: Optional[str] = None) -> Dict:
        """매수·매도 수, 이긴·진 거래(실현 손익 기준), 오늘 거래 수 — 저장소가 있으면 기록 전부로"""
        st = self.store(mode)
        if st is not None:
            c = st.bot_counts(bot_id)
            return {k: c[k] for k in ('buys', 'sells', 'wins', 'losses', 'today')}
        items = self.trades(bot_id, mode)
        today = datetime.now().strftime('%Y-%m-%d')
        sells = [t for t in items if t.get('side') == 'sell' and t.get('profit') is not None]
        return {'buys': sum(t.get('side') == 'buy' for t in items), 'sells': sum(t.get('side') == 'sell' for t in items),
                'wins': sum(t['profit'] > 0 for t in sells), 'losses': sum(t['profit'] <= 0 for t in sells),
                'today': sum(str(t.get('time', '')).startswith(today) for t in items)}

    def history(self, bot_id: str, mode: Optional[str] = None, points: int = 300) -> List[Dict]:
        """수익 추이 (최근 30일, 5분 간격) — 많으면 고르게 줄여서"""
        items = self._read_json(self.data_dir(bot_id, mode) / 'equity.json', [])
        if len(items) > points:
            step = len(items) / points
            items = [items[int(i * step)] for i in range(points - 1)] + [items[-1]]
        return items

    def list(self, mode: Optional[str] = None) -> List[Dict]:
        return [self.summary(b['id'], mode) for b in self.bots_in(mode)]

    # ── 전체 봇 보드 모니터링 ──
    MIN_ORDER_KRW = 5000

    def board_monitor(self, summaries: List[Dict], mode: Optional[str] = None, today_unrealized: Optional[Dict[str, float]] = None,
                      krw: Optional[float] = None) -> Dict:
        """봇마다 상태(정상 · 주의 · 문제 · 멈춤) · 오늘 손익 · 마지막 거래 · 손실 한도 사용률, 주의 봇 수, 최근 활동(거래 + 경고)
        today_unrealized = {봇 id: 오늘 평가손익 변화} (봇 1분 기록으로 화면 쪽에서 셈, 없으면 판 손익만)"""
        m = self.mode(mode)
        st = self.store(m)
        now = datetime.now()
        kst_midnight = int(datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        blocked = self.live_blocked(m) if is_live(m) else None
        bots: Dict[str, Dict] = {}
        feed: List[Dict] = []
        alerts = 0
        names = {}
        for s in summaries:
            bid = s['id']
            bot = self.get(bid) or {}
            if bot.get('example') and m != 'sim':
                continue
            names[bid] = s.get('name', '')
            e = self.engines.get((bid, m))
            running = bool(s.get('running'))
            today = st.bot_today(bid, kst_midnight) if st is not None else {'realized': 0.0, 'trades': 0, 'last_ts': None}
            level, label, reason = 'ok', '정상', ''
            risk = {}
            if e is not None:
                try:
                    risk = e.risk_manager.get_risk_status() or {}
                except Exception:
                    risk = {}
            if not running:
                level, label, reason = 'off', '멈춤', '실행 중이 아닙니다'
            else:
                interval = float(getattr(e, 'update_interval', 60) or 60) if e else 60
                last = getattr(e, 'last_update_time', None) if e else None
                late = max(3 * interval, 300)
                err = getattr(e, 'last_error', None) if e else None
                warns = [d for d in (getattr(e, 'decisions', None) or [])[:10] if d.get('kind') == 'warning'
                         and (now - datetime.fromisoformat(d['time'])).total_seconds() < 600] if e else []
                started = getattr(e, 'started_at', None) if e else None
                if last is None and started and (now - started).total_seconds() <= late:
                    level, label, reason = 'ok', '시작 중', '첫 확인을 기다리는 중입니다 (시세를 받는 데 1~2분 걸릴 수 있습니다)'
                elif last is None or (now - last).total_seconds() > late:
                    ago = int((now - last).total_seconds() // 60) if last else None
                    level, label, reason = 'error', '응답 없음', f"{ago}분째 확인이 없습니다" if ago is not None else '아직 한 번도 확인하지 못했습니다'
                elif err and (now - err[0]).total_seconds() < 600:
                    level, label, reason = 'error', '오류', err[1]
                elif risk.get('daily_loss_remaining') is not None and risk['daily_loss_remaining'] <= 0:
                    level, label, reason = 'warn', '손실 한도', '오늘 손실 한도에 닿아 새로 사지 않습니다'
                elif s.get('buy_on') is False:
                    level, label, reason = 'ok', '정상', '매수를 꺼 두었습니다 · 가진 코인만 팝니다' if s.get('sell_on', True) else '매수 · 매도를 꺼 두었습니다 · 손절만 합니다'
                elif blocked or (e is not None and getattr(e, '_buy_blocked', False)):
                    level, label, reason = 'warn', '매수 막힘', blocked or '권한이 끝나 새로 사지 않습니다 (팔기 · 손절은 계속)'
                elif not s.get('budget_pct') and not s.get('budget'):
                    level, label, reason = 'warn', '배분 0%', '배분이 0%라 새로 사지 않습니다'
                elif min(float(s.get('cash') or 0), float('inf') if krw is None else float(krw)) < self.MIN_ORDER_KRW and not s.get('holding'):   # 계좌 원화가 모자라도
                    level, label, reason = 'warn', '돈 없음', f"매수 가능 금액이 {self.MIN_ORDER_KRW:,}원보다 적어 살 수 없습니다"
                elif not s.get('buy_rules') and not s.get('holding') and not s.get('seed_top') and not s.get('seed_coins') and not s.get('dca'):
                    level, label, reason = 'warn', '조건 없음', '매수 조건이 없어 아무것도 사지 않습니다'
                elif warns:
                    level, label, reason = 'warn', '경고', warns[0]['message'][:80]
                else:
                    ago = int((now - last).total_seconds())
                    reason = f"{ago}초 전 확인" if ago < 120 else f"{ago // 60}분 전 확인"
            if level in ('warn', 'error'):
                alerts += 1
            lim = risk.get('daily_loss_limit')
            used = (abs(float(risk.get('daily_loss') or 0)) / lim * 100) if lim else None
            unreal = (today_unrealized or {}).get(bid)
            bots[bid] = {'level': level, 'label': label, 'reason': reason,
                         'today_pnl': today['realized'] + (unreal or 0.0), 'today_realized': today['realized'],
                         'today_trades': today['trades'], 'last_trade_ts': today['last_ts'], 'loss_used': used}
            for d in (getattr(e, 'decisions', None) or [])[:20] if (e and running) else []:
                if d.get('kind') == 'warning' and (now - datetime.fromisoformat(d['time'])).total_seconds() < 1800:
                    feed.append({'ts': int(datetime.fromisoformat(d['time']).timestamp()), 'bot_id': bid, 'bot': names[bid], 'kind': 'warning',
                                 'symbol': d.get('symbol') or '', 'message': d.get('message', '')})
        if st is not None and names:
            for t in st.recent_bot_trades(30, list(names)):
                feed.append({'ts': int(datetime.fromisoformat(str(t['time'])).astimezone().timestamp()) if t.get('time') else 0,
                             'bot_id': t['bot_id'], 'bot': names.get(t['bot_id'], t.get('bot_name') or ''), 'kind': t['side'],
                             'symbol': t.get('symbol', ''), 'price': t.get('price'), 'total': t.get('total'), 'profit': t.get('profit'),
                             'message': t.get('reason') or ''})
        feed.sort(key=lambda x: x['ts'], reverse=True)
        return {'bots': bots, 'alerts': alerts, 'feed': feed[:30]}

    def board_holdings(self, summaries: List[Dict], prices: Optional[Dict[str, float]] = None, mode: Optional[str] = None) -> Dict:
        """전체 봇 보드 맨 위: 모든 봇 장부를 합친 보유 현황 (보유코인 분석 요약 줄과 같은 칸)
        같은 코인을 여러 봇이 가지면 한 코인으로 합침 · 시세가 없는 코인(원화 마켓에서 빠짐)은 평가 0원 = 상장폐지
        매수 가능 = 봇마다 매수 가능 금액 합 · 판 손익 = 봇마다 판 손익 합 · 수수료 = 봇 거래 기록의 수수료 합 (초기화 이후)"""
        m = self.mode(mode)
        prices = prices or self._live_prices(m) or self._prices(m)
        st = self.store(m)
        coins: Dict[str, List[float]] = {}
        cash = realized = fees = adopted_loss = 0.0
        for s in summaries:
            b = self.get(s['id']) or {}
            if b.get('example') and m != 'sim':
                continue
            for sym, p in self._ledger_of(s['id'], m).items():
                q = float(p.get('qty') or 0)
                c = coins.setdefault(sym, [0.0, 0.0])
                c[0] += q
                c[1] += q * float(p.get('entry_price') or 0)
            cash += float(s.get('cash') or 0)
            realized += float(s.get('realized') or 0)
            adopted_loss += float(s.get('adopted_loss') or 0)
            if st is not None:
                try:
                    fees += st.bot_fee_sum(s['id'])
                except Exception:
                    pass
        buy = value = 0.0
        up = down = gone = 0
        for sym, (q, cost) in coins.items():
            if q <= 0:
                continue
            if prices and sym not in prices:
                v = 0.0
                gone += 1
            else:
                price = (prices or {}).get(sym) or (cost / q if q else 0)
                v = q * price
                up += v > cost
                down += v < cost
            buy += cost
            value += v
        return {'coins': sum(1 for q, _ in coins.values() if q > 0), 'up': up, 'down': down, 'delisted': gone,
                'buy': buy, 'value': value, 'pnl': value - buy, 'rate': (value - buy) / buy * 100 if buy else 0.0,
                'cash': cash, 'realized': realized, 'fees': fees, 'adopted_loss': adopted_loss}

    @staticmethod
    def _attach_bot_pnl(trades: List[Dict], *_args, **_kw) -> None:
        """거래 순간에 저장한 봇 총 자산(코인 + 매수 가능 금액)과 평가수익률을 그대로 — 기록이라 나중에 바뀌지 않음.
        이 기능 전의 거래는 저장된 값이 없어 비움"""
        for tr in trades:
            snap = tr.get('snap')
            if isinstance(snap, dict) and snap.get('total') is not None:
                tr['bot_total'], tr['bot_coins'], tr['bot_cash'] = snap['total'], snap.get('coins'), snap.get('cash')
                tr['bot_pnl_rate'] = snap.get('rate')

    @staticmethod
    def with_avg_cost(trades: List[Dict]) -> List[Dict]:
        """산 줄마다 산 뒤의 매수평균가(수수료 포함, 업비트 매수평균가와 같은 방식)를 avg_cost 로 붙임 — 판 줄은 평균 그대로, 보유량만 줄임"""
        pos: Dict[str, List[float]] = {}
        out = []
        for t in trades:
            t = dict(t)
            q = float(t.get('qty') or 0)
            st = pos.setdefault(t.get('symbol'), [0.0, 0.0])          # [수량, 들인 돈]
            if t.get('side') == 'buy':
                st[0] += q
                st[1] += float(t.get('total') or 0)
                if st[0] > 0:
                    t['avg_cost'] = st[1] / st[0]
            elif st[0] > 0:
                st[1] -= st[1] * min(1.0, q / st[0])
                st[0] = max(0.0, st[0] - q)
                if st[0] <= 1e-12:
                    st[0] = st[1] = 0.0
            out.append(t)
        return out

    def detail(self, bot_id: str, mode: Optional[str] = None) -> Optional[Dict]:
        m = self.mode(mode)
        bot = self.get(bot_id)
        if not bot:
            return None
        summary = self.summary(bot_id, m)
        st = self.store(m)
        recent = st.bot_trades(bot_id, limit=50) if st is not None else self.with_avg_cost(self.trades(bot_id, m))[-50:][::-1]
        self._attach_bot_pnl(recent, self._read_json(self.data_dir(bot_id, m) / 'equity.json', []),
                             summary['budget'] or summary['invested'],
                             summary['budget'] or (max(0.0, summary['budget_now'] - summary['profit']) if summary.get('budget_pct') else 0))
        out = {'bot': {**copy.deepcopy(bot), 'budget_pct': self.pct_of(bot, m),            # 화면: 지금 모드의 규칙 · 안전장치
                       'trade_rules': copy.deepcopy(self.rules_of(bot, m)), 'risk_config': self.risk_of(bot, m)},
               'mode': m, 'summary': summary,
               'recent_trades': recent,
               'counts': self.trade_counts(bot_id, m), 'history': self.history(bot_id, m)}
        e = self.engines.get((bot_id, m))
        live = self._live_prices(m)
        full = bool(live) and len(live) >= 50                   # 시세 목록이 다 있으면 거기 없는 코인 = 상장폐지 = 0원 (summary 와 같게, 2026-10-17)
        if e and e.is_running:
            out['status'] = e.get_status()
            for p in out['status'].get('positions') or []:      # 보유 코인 현재가 · 손익도 실시간 전체 시세로
                price = live.get(p['symbol'])
                if not price and full:
                    price, p['delisted'] = 0.0, True
                if price or p.get('delisted'):
                    cost = p['entry_price'] * p['quantity']
                    p['current_price'] = price
                    p['profit'] = (price - p['entry_price']) * p['quantity']
                    p['profit_rate'] = p['profit'] / cost * 100 if cost else 0
            return out
        # 멈춘 봇: 저장된 파일로 보유 코인·급등 진행 (최근 판단은 실행 중에만) — 먼저 실제 계좌와 맞춤 (멈춘 동안 직접 판 코인)
        try:
            self.reconcile_stopped(bot_id, m)
        except Exception as e:
            logger.debug(f"멈춘 봇 장부 맞추기 오류: {e}")
        from .spike_rebuy import SpikeRebuy
        prices = live or self._prices(m)
        d = self.data_dir(bot_id, m)
        held = self._read_json(d / 'bot_positions.json', {})
        positions = []
        for sym, p in held.items():
            gone = full and sym not in live
            price = 0.0 if gone else (prices.get(sym) or p['entry_price'])
            cost = p['entry_price'] * p['qty']
            profit = (price - p['entry_price']) * p['qty']
            positions.append({'symbol': sym, 'quantity': p['qty'], 'entry_price': p['entry_price'], 'current_price': price,
                              'profit': profit, 'profit_rate': profit / cost * 100 if cost else 0, 'delisted': gone,
                              'condition': p.get('condition', ''), 'opened_at': p.get('opened_at')})
        spike = SpikeRebuy(d / 'spike_state.json') if (d / 'spike_state.json').exists() else SpikeRebuy()
        surge = SpikeRebuy(d / 'surge_state.json') if (d / 'surge_state.json').exists() else SpikeRebuy()
        last_decisions = []
        if e:                                                  # 멈추기 전 최근 판단은 남은 엔진에서 (보유 코인은 위 파일 것)
            try:
                last_decisions = (e.get_status() or {}).get("decisions") or []
            except Exception:
                last_decisions = []
        out['status'] = {'is_running': False, 'mode': m, 'rules': self.rules_of(bot, m),
                         'positions': positions, 'decisions': last_decisions,
                         'spike': {**spike.status(), 'settings': spike_settings(self.rules_of(bot, m))},
                         'surge': {**surge.status(), 'settings': surge_settings(self.rules_of(bot, m))}}
        return out

    # ── 바꾼 규칙 · 안전장치를 적용하기 전에: 곧바로 일어날 일 미리 보기 ──
    def _exit_reason(self, pos: Dict, risk: Dict, rules: Dict, peaks: Dict, cond_hits: Dict[str, str]) -> Optional[str]:
        """이 값이면 지금 이 코인을 팔지 (봇의 _check_sells 와 같은 순서: 손절 → 익절 → 트레일링 → 매도 조건)"""
        entry, price, sym = float(pos.get('entry_price') or 0), float(pos.get('current_price') or 0), pos['symbol']
        if entry <= 0 or price <= 0:
            return None
        rate = (price / entry - 1) * 100
        sl = float(risk.get('stop_loss_percent') or 0)
        if sl > 0 and rate <= -sl:
            return f'손절 −{sl:g}%'
        if rules.get('sell_on') is False:                  # 매도를 꺼 두면 손절만
            return None
        tp = float(risk.get('take_profit_percent') or 0)
        if tp > 0 and rate >= tp:
            return f'익절 +{tp:g}%'
        tr = float(risk.get('trailing_stop_percent') or 0)
        if tr > 0:
            peak = max(float(peaks.get(sym) or entry), price)
            if peak > entry and (peak - price) / peak * 100 >= tr:
                return f'트레일링 −{tr:g}%'
        if sym in cond_hits:
            return f"매도 조건 '{cond_hits[sym]}'"
        return None

    def _sell_cond_hits(self, rules: Dict, held: List[str], data: Dict) -> Dict[str, str]:
        """매도 조건(일부 팔고 다시 사기 세트는 빼고)에 지금 맞는 보유 코인 → 조건 이름"""
        from . import conditions as cd
        from .bot_engine import rule_match
        from . import spike_rebuy as sr
        coins = [c for c in (data.get('coins') or []) if f"KRW-{c['currency']}" in held]
        if not coins:
            return {}
        results = []
        for rule in rule_list(rules, 'sell'):
            if sr.is_partial_rule(rule):
                continue
            cond = cd.resolve(rule['id'], data.get('custom') or [])
            if not cond or cond['needs_flow']:
                continue
            tf = (rule.get('params') or {}).get('tf') or 'day'
            lows = data.get('lows') if tf == 'day' else (data.get('lows_tf') or {}).get(tf)
            if not lows:
                continue
            hits = cd.run_condition(cond, rule.get('params'), coins, lows, data.get('hourly'), include_stable=True)
            results.append((cond['title'], {f"KRW-{h['currency']}" for h in hits}))
        if not results:
            return {}
        if rule_match(rules, 'sell') == 'all':
            both = set.intersection(*(s for _, s in results))
            return {sym: ' + '.join(t for t, _ in results) for sym in both}
        out: Dict[str, str] = {}
        for title, syms in results:
            for sym in syms:
                out.setdefault(sym, title)
        return out

    def apply_preview(self, bot_id: str, spec: Dict, mode: Optional[str] = None) -> Dict:
        """적용하려는 trade_rules · risk_config 로 바꾸면 '곧바로' 생길 일: 새로 팔리게 되는 보유 코인 · 시작할 때 사기 조건
        (지금 값으로도 이미 팔릴 코인은 이번 변경 때문이 아니므로 빼고, 새 값 때문에 팔리게 되는 것만)"""
        from . import conditions as cd
        m = self.mode(mode)
        bot = self.get(bot_id)
        if not bot:
            return {'sells': [], 'start_buys': [], 'running': False}
        old_rules, old_risk = self.rules_of(bot, m), self.risk_of(bot, m)
        new_rules = spec.get('trade_rules') if isinstance(spec.get('trade_rules'), dict) else old_rules
        new_risk = {**old_risk, **(spec.get('risk_config') or {})} if isinstance(spec.get('risk_config'), dict) else old_risk
        d = self.detail(bot_id, m) or {}
        positions = (d.get('status') or {}).get('positions') or []
        e = self.engines.get((bot_id, m))
        peaks = dict(getattr(getattr(e, 'risk_manager', None), 'peak_price', {}) or {})
        held = [p['symbol'] for p in positions]
        # 매도 조건이 바뀐 때만 조건을 계산 (가진 코인 시세만 받아 빠르게) — 안 바뀌었으면 조건 때문에 새로 팔릴 코인은 없음
        sell_key = lambda r: json.dumps([rule_list(r, 'sell'), r.get('sell_match'), r.get('sell_on')], sort_keys=True, default=str)
        old_hits = new_hits = {}
        if held and sell_key(old_rules) != sell_key(new_rules):
            try:
                get = getattr(self, 'preview_market', None)
                try:
                    data = (get(held, exchange_of(m)) if get else self._market_data(m)) or {}
                except TypeError:             # 예전 모양 preview_market(held)
                    data = get(held) or {}
            except Exception:
                data = {}
            old_hits = self._sell_cond_hits(old_rules, held, data)
            new_hits = self._sell_cond_hits(new_rules, held, data)
        sells = []
        for p in positions:
            why = self._exit_reason(p, new_risk, new_rules, peaks, new_hits)
            if not why or self._exit_reason(p, old_risk, old_rules, peaks, old_hits):
                continue
            entry, price, qty = float(p['entry_price']), float(p['current_price']), float(p['quantity'])
            sells.append({'symbol': p['symbol'], 'rate': (price / entry - 1) * 100 if entry else 0.0, 'value': price * qty,
                          'profit': (price - entry) * qty, 'why': why})
        sells.sort(key=lambda x: x['rate'])
        start_buys = []
        if new_rules.get('buy_on') is not False and not new_rules.get('start_wait')                 and not (rule_match_all(new_rules) and len(rule_list(new_rules, 'buy')) > 1):
            before = {r['id'] for r in rule_list(old_rules, 'buy')}
            for r in rule_list(new_rules, 'buy'):
                cond = cd.resolve(r['id'], [])
                if cond and cond.get('start_buy') and r['id'] not in before:
                    start_buys.append(cond['title'])
        return {'running': self.is_running(bot_id, m), 'mode': m, 'sells': sells, 'start_buys': start_buys,
                'max_positions': int(new_risk.get('max_positions') or 0), 'held': len(positions)}

    def spike_active(self, mode: Optional[str] = None) -> List[Dict]:
        """'순간 급등'·'하루 급등' 일부 팔고 다시 사기를 진행 중인 코인 (모든 봇) — 보유코인 분석 표에 표시용
        kind: 'spike'(순간 급등) | 'surge'(하루 급등)
        phase: 'selling'(급등 감지, 꺾이면 일부 팔기) | 'waiting'(일부 팔았고 다시 사기 기다림)"""
        from .spike_rebuy import SpikeRebuy
        m = self.mode(mode)
        out = []
        for b in self.bots_in(m):
            e = self.engines.get((b['id'], m))
            for kind, fname in (('spike', 'spike_state.json'), ('surge', 'surge_state.json')):
                machine = getattr(e, kind, None) if e is not None else None
                if machine is not None:
                    active = machine.status()['active']
                else:
                    f = self.data_dir(b['id'], m) / fname
                    active = SpikeRebuy(f).status()['active'] if f.exists() else []
                for a in active:
                    out.append({**a, 'kind': kind, 'bot_id': b['id'], 'bot_name': b.get('name', ''), 'running': e is not None})
        return out

    def _activity(self, message, level='info', icon=None):
        if self._on_activity:
            try:
                self._on_activity(message, level, icon)
            except Exception:
                pass


SEED_MAX = 300


def seed_top(bot: Dict) -> int:
    """'시작할 때 거래 상위 N개를 한꺼번에 사기' 개수 (시뮬레이션 모드에서만 씀). 0 = 안 함, -1 = 전체"""
    try:
        v = int((bot or {}).get('seed_top') or 0)
    except (TypeError, ValueError):
        return 0
    return -1 if v < 0 else min(SEED_MAX, v)
