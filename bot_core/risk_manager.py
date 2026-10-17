"""
리스크 관리 모듈
- 포지션 크기 제한 (비율 + 1회 최대 주문 금액)
- 손절/익절/트레일링 스톱
- 일일 손실 한도 (그날 시작 자금 대비 %)
- 최대 포지션 수 제한
- 같은 코인 주문 최소 간격 (짧은 시간 매수·매도 반복 방지)
"""

import logging
import time
from typing import Dict, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# 같은 코인에 전략 신호로 다시 주문하기까지 최소 간격(초). 손절·익절 같은 위험 축소 매도는 예외
MIN_ORDER_INTERVAL_SEC = 60


class RiskManager:
    """리스크 관리자"""

    def __init__(self, config: Dict):
        """
        Args:
            config: 리스크 설정 딕셔너리
                {
                    'position_size_percent': float,     # 1회 매수 금액 (원화 잔고 대비 %)
                    'max_order_krw': float,             # 1회 최대 주문 금액 (원, 0이면 제한 없음)
                    'order_krw': float,                 # 코인당 금액 (원, 0이면 비율대로) — 정하면 늘 이 금액씩
                    'stop_loss_percent': float,         # 손절 (%)
                    'take_profit_percent': float,       # 익절 (%)
                    'trailing_stop_percent': float,     # 트레일링 스톱 (%, 0이면 사용 안 함)
                    'daily_loss_limit_percent': float,  # 일일 손실 한도 (그날 시작 자금 대비 %)
                    'daily_loss_limit': float,          # (예전 설정) 일일 손실 한도 (원) - % 설정이 없을 때만 사용
                    'max_positions': int,               # 최대 포지션 수
                    'risk_level': str                   # 'basic', 'advanced', 'minimal'
                }
        """
        self.update_config(config)

        # 일일 손실 추적
        self.daily_loss = 0.0
        self.day_start_balance = None   # 그날 처음 확인한 자금 (한도 % 계산 기준)
        self.last_reset_date = datetime.now().date()

        # 코인별 마지막 주문 시각, 산 뒤 최고가 (트레일링 스톱용)
        self.last_order_time: Dict[str, float] = {}
        self.peak_price: Dict[str, float] = {}

        logger.info(f"리스크 관리자 초기화: 레벨={self.risk_level}, 손실한도={self._limit_text()}")

    def update_config(self, config: Dict):
        """설정만 바꿈 (오늘 손실·주문 시각·최고가 기록은 그대로) — 봇 실행 중 설정을 바꿔도 다음 확인부터 반영"""
        config = config or {}
        self.config = config
        self.position_size_percent = float(config.get('position_size_percent', 10.0))
        self.max_order_krw = float(config.get('max_order_krw', 0) or 0)
        self.order_krw = float(config.get('order_krw', 0) or 0)
        self.stop_loss_percent = float(config.get('stop_loss_percent', 5.0))
        self.take_profit_percent = float(config.get('take_profit_percent', 10.0))
        self.trailing_stop_percent = float(config.get('trailing_stop_percent', 0) or 0)
        # 화면은 %로 저장(daily_loss_limit_percent). 예전 원 단위 설정은 % 설정이 없을 때만 사용
        self.daily_loss_limit_percent = config.get('daily_loss_limit_percent')
        self.daily_loss_limit_krw = float(config.get('daily_loss_limit', 100000.0))
        self.max_positions = int(config.get('max_positions', 3))
        self.risk_level = config.get('risk_level', 'basic')

    # ── 일일 손실 한도 ──
    def reset_daily_loss(self):
        """일일 손실 리셋 (날짜 변경 시)"""
        today = datetime.now().date()
        if today > self.last_reset_date:
            self.daily_loss = 0.0
            self.day_start_balance = None
            self.last_reset_date = today
            logger.info("일일 손실 리셋")

    def note_balance(self, balance: float):
        """그날 처음 본 자금을 한도 계산 기준으로 기억"""
        self.reset_daily_loss()
        if self.day_start_balance is None and balance and balance > 0:
            self.day_start_balance = balance

    @property
    def daily_loss_limit(self) -> float:
        """오늘의 손실 한도 (원)"""
        if self.daily_loss_limit_percent is not None:
            # 0 = 안 씀 (손절 · 익절 0 과 같은 뜻). 전에는 0 이면 한도도 0원이라 처음부터 새로 사기를 막았음 (2026-10-15)
            if float(self.daily_loss_limit_percent or 0) <= 0:
                return float('inf')
            base = self.day_start_balance or 0
            return base * float(self.daily_loss_limit_percent) / 100 if base else float('inf')
        return self.daily_loss_limit_krw

    def _limit_text(self) -> str:
        if self.daily_loss_limit_percent is not None:
            return f"하루 {float(self.daily_loss_limit_percent):g}%"
        return f"하루 {self.daily_loss_limit_krw:,.0f}원"

    # ── 주문 간격 ──
    def order_too_soon(self, symbol: str) -> Tuple[bool, str]:
        """같은 코인에 최근 주문한 지 MIN_ORDER_INTERVAL_SEC가 안 지났으면 True"""
        last = self.last_order_time.get(symbol)
        if last is None:
            return False, ""
        waited = time.time() - last
        if waited < MIN_ORDER_INTERVAL_SEC:
            return True, f"같은 코인 주문 후 {MIN_ORDER_INTERVAL_SEC}초가 지나지 않았습니다 ({waited:.0f}초)"
        return False, ""

    def record_order(self, symbol: str):
        self.last_order_time[symbol] = time.time()

    # ── 매수 가능 여부·크기 ──
    def can_open_position(self, current_positions: int, balance: float, signal_confidence: float = 0.5) -> Tuple[bool, str]:
        """
        새 포지션 개설 가능 여부 확인

        Returns:
            (가능 여부, 메시지)
        """
        self.note_balance(balance)

        if current_positions >= self.max_positions:
            return False, f"최대 보유 코인 수 도달: {current_positions}/{self.max_positions}"

        if abs(self.daily_loss) >= self.daily_loss_limit:
            return False, f"오늘 손실 한도 도달 ({self._limit_text()}): {self.daily_loss:,.0f}원"

        if self.risk_level == 'advanced' and signal_confidence < 0.7:
            return False, f"신호 신뢰도 부족: {signal_confidence:.2f}"

        return True, "포지션 개설 가능"

    def calculate_position_size(self, balance: float, price: float, signal_confidence: float = 0.5) -> Tuple[float, float]:
        """
        포지션 크기 계산 (원화 잔고 × 비율, 1회 최대 주문 금액·잔고 30% 이내, 최소 5,000원)

        Returns:
            (포지션 크기 (원), 수량)
        """
        if self.order_krw > 0:
            # 코인당 금액을 정했으면 늘 그 금액 (켤 때 · 돈이 생겨 더 살 때 · 조건으로 살 때 모두 같은 크기 — 2026-10-12)
            # 돈이 모자라면 사는 쪽에서 기다림. 1회 최대가 있으면 그보다 크게 사지 않음
            size = self.order_krw if self.max_order_krw <= 0 else min(self.order_krw, self.max_order_krw)
            size = max(5000, size)
            return size, size / price
        if self.position_size_percent >= 100 and self.max_positions == 1:
            # 한 코인에 가진 돈 전부 (최대 보유 1개 · 비율 100%): 잔고 30% 제한 없이, 비용 몫만 남김 — 평균선 위에서만 들고 있기 같은 봇
            size = balance * 0.995
            if self.max_order_krw > 0:
                size = min(size, self.max_order_krw)
            size = max(5000, size)
            return size, size / price
        base_size = balance * (self.position_size_percent / 100)
        position_size = base_size * signal_confidence if self.risk_level == 'advanced' else base_size

        max_size = balance * 0.3  # 한 번에 잔고의 30%를 넘지 않음
        if self.max_order_krw > 0:
            max_size = min(max_size, self.max_order_krw)
        position_size = max(5000, min(position_size, max_size))
        return position_size, position_size / price

    # ── 매도 조건 (손절·익절·트레일링) ──
    def check_stop_loss(self, entry_price: float, current_price: float) -> Tuple[bool, str]:
        if self.stop_loss_percent <= 0:                  # 0 = 손절 안 함 (사기만 하는 봇 등)
            return False, "손절 사용 안 함"
        if entry_price == 0:
            return False, "입장 가격이 0입니다"
        loss_rate = ((current_price - entry_price) / entry_price) * 100
        if loss_rate <= -self.stop_loss_percent:
            return True, f"손절 조건 충족: {loss_rate:.2f}%"
        return False, "손절 조건 미충족"

    def check_take_profit(self, entry_price: float, current_price: float) -> Tuple[bool, str]:
        if self.take_profit_percent <= 0:                # 0 = 익절 안 함
            return False, "익절 사용 안 함"
        if entry_price == 0:
            return False, "입장 가격이 0입니다"
        profit_rate = ((current_price - entry_price) / entry_price) * 100
        if profit_rate >= self.take_profit_percent:
            return True, f"익절 조건 충족: {profit_rate:.2f}%"
        return False, "익절 조건 미충족"

    def check_trailing_stop(self, symbol: str, entry_price: float, current_price: float) -> Tuple[bool, str]:
        """산 뒤 최고가에서 trailing_stop_percent 이상 내려오면 True (최고가가 매수가보다 높을 때만: 손실 구간은 손절이 담당)"""
        if self.trailing_stop_percent <= 0 or entry_price <= 0:
            return False, ""
        peak = max(self.peak_price.get(symbol, entry_price), current_price)
        self.peak_price[symbol] = peak
        if peak <= entry_price:
            return False, ""
        drop = (peak - current_price) / peak * 100
        if drop >= self.trailing_stop_percent:
            return True, f"트레일링 스톱: 최고가 {peak:,.0f}원에서 {drop:.2f}% 하락"
        return False, ""

    def clear_position(self, symbol: str):
        """매도 후 최고가 기록 지우기"""
        self.peak_price.pop(symbol, None)

    def update_daily_loss(self, profit: float):
        """일일 손실 업데이트"""
        self.reset_daily_loss()
        if profit < 0:
            self.daily_loss += profit
            logger.info(f"일일 손실 업데이트: {self.daily_loss:,.0f}원 (한도 {self._limit_text()})")

    def get_risk_status(self) -> Dict:
        """리스크 상태 반환"""
        self.reset_daily_loss()
        limit = self.daily_loss_limit
        return {
            'daily_loss': self.daily_loss,
            'daily_loss_limit': None if limit == float('inf') else limit,
            'daily_loss_remaining': None if limit == float('inf') else limit - abs(self.daily_loss),
            'risk_level': self.risk_level,
            'position_size_percent': self.position_size_percent,
            'max_order_krw': self.max_order_krw,
            'order_krw': self.order_krw,
            'stop_loss_percent': self.stop_loss_percent,
            'take_profit_percent': self.take_profit_percent,
            'trailing_stop_percent': self.trailing_stop_percent,
            'max_positions': self.max_positions
        }
