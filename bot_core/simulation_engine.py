"""
시뮬레이션 엔진
- 가상 자금 관리
- 가상 포지션 관리
- 실제 시장 가격 기반 수익 계산
- 수수료 및 슬리피지 시뮬레이션
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, asdict
import json

logger = logging.getLogger(__name__)


@dataclass
class VirtualPosition:
    """가상 포지션 데이터 클래스"""
    symbol: str
    entry_price: float
    quantity: float
    entry_time: datetime
    current_price: float = 0.0
    buy_fee: float = 0.0          # 매수 수수료 (손익에 반영)
    condition: str = ''           # 이 코인을 산 조건 이름

    @property
    def profit(self) -> float:
        """현재 손익 (매수 수수료 포함, 팔 때 수수료는 제외)"""
        return (self.current_price - self.entry_price) * self.quantity - self.buy_fee

    @property
    def profit_rate(self) -> float:
        """현재 수익률 (%) — 들인 돈(매수가×수량+수수료) 대비"""
        cost = self.entry_price * self.quantity + self.buy_fee
        return self.profit / cost * 100 if cost > 0 else 0.0

    def to_dict(self) -> Dict:
        return {'symbol': self.symbol, 'entry_price': self.entry_price, 'quantity': self.quantity,
                'entry_time': self.entry_time.isoformat(), 'current_price': self.current_price,
                'buy_fee': self.buy_fee, 'condition': self.condition,
                'profit': self.profit, 'profit_rate': self.profit_rate}


@dataclass
class TradeRecord:
    """거래 내역 데이터 클래스"""
    timestamp: datetime
    symbol: str
    side: str  # 'buy' or 'sell'
    price: float
    quantity: float
    fee: float
    total_amount: float
    profit: Optional[float] = None  # 매도 시 수익


class SimulationEngine:
    """시뮬레이션 거래 엔진"""

    def __init__(self, initial_balance: float = 10000000.0, fee_rate: float = 0.0005, slippage_rate: float = 0.001):
        """
        Args:
            initial_balance: 초기 자금 (원)
            fee_rate: 수수료율 (기본 0.05%)
            slippage_rate: 슬리피지율 (기본 0.1%)
        """
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.available_balance = initial_balance
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate

        self.positions: Dict[str, VirtualPosition] = {}  # symbol -> position
        self.trade_history: List[TradeRecord] = []

        # 통계
        self.total_profit = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.max_drawdown = 0.0
        self.peak_balance = initial_balance

        logger.info(f"시뮬레이션 엔진 초기화: 초기 자금 {initial_balance:,.0f}원")

    def reset(self, new_initial_balance: Optional[float] = None):
        """시뮬레이션 리셋"""
        if new_initial_balance is not None:
            self.initial_balance = new_initial_balance

        self.current_balance = self.initial_balance
        self.available_balance = self.initial_balance
        self.positions.clear()
        self.trade_history.clear()
        self.total_profit = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.max_drawdown = 0.0
        self.peak_balance = self.initial_balance

        logger.info(f"시뮬레이션 리셋: 초기 자금 {self.initial_balance:,.0f}원")

    def update_price(self, symbol: str, current_price: float):
        """포지션의 현재가 업데이트"""
        if symbol in self.positions:
            self.positions[symbol].current_price = current_price

    def can_buy(self, symbol: str, price: float, quantity: float) -> Tuple[bool, str]:
        """
        매수 가능 여부 확인

        Returns:
            (가능 여부, 메시지)
        """
        total_cost = price * quantity
        fee = total_cost * self.fee_rate
        slippage = total_cost * self.slippage_rate
        total_required = total_cost + fee + slippage

        if total_required > self.available_balance:
            return False, f"자금 부족: 필요 {total_required:,.0f}원, 사용 가능 {self.available_balance:,.0f}원"

        if symbol in self.positions:
            return False, f"이미 포지션 보유: {symbol}"

        return True, "매수 가능"

    def buy(self, symbol: str, price: float, quantity: float, current_time: Optional[datetime] = None,
            condition: str = '') -> Tuple[bool, str, Optional[TradeRecord]]:
        """
        가상 매수 주문 실행

        Returns:
            (성공 여부, 메시지, 거래 기록)
        """
        if current_time is None:
            current_time = datetime.now()

        # 슬리피지 적용
        execution_price = price * (1 + self.slippage_rate)
        total_cost = execution_price * quantity
        fee = total_cost * self.fee_rate
        total_required = total_cost + fee

        # 자금 확인
        if total_required > self.available_balance:
            return False, f"자금 부족: 필요 {total_required:,.0f}원", None

        # 포지션 확인
        if symbol in self.positions:
            return False, f"이미 포지션 보유: {symbol}", None

        # 자금 차감
        self.available_balance -= total_required
        self.current_balance -= total_required

        # 포지션 생성
        position = VirtualPosition(
            symbol=symbol,
            entry_price=execution_price,
            quantity=quantity,
            entry_time=current_time,
            current_price=execution_price,
            buy_fee=fee,
            condition=condition
        )
        self.positions[symbol] = position

        # 거래 기록
        trade = TradeRecord(
            timestamp=current_time,
            symbol=symbol,
            side='buy',
            price=execution_price,
            quantity=quantity,
            fee=fee,
            total_amount=total_required
        )
        self.trade_history.append(trade)
        self.total_trades += 1

        logger.info(f"매수 완료: {symbol} {quantity}개 @ {execution_price:,.0f}원 (수수료: {fee:,.0f}원)")

        return True, f"매수 완료: {symbol}", trade

    def add_to_position(self, symbol: str, price: float, krw: float,
                        current_time: Optional[datetime] = None) -> Tuple[bool, str, Optional[TradeRecord]]:
        """가진 코인에 krw(수수료 포함)만큼 더 삼 — 매수가는 평균으로 합침 (급등 때 판 몫을 다시 살 때)"""
        if current_time is None:
            current_time = datetime.now()
        if symbol not in self.positions:
            return False, f"포지션 없음: {symbol}", None
        krw = min(krw, self.available_balance)
        execution_price = price * (1 + self.slippage_rate)
        quantity = krw / (execution_price * (1 + self.fee_rate))
        if quantity <= 0:
            return False, "살 금액이 없습니다", None
        cost = execution_price * quantity
        fee = cost * self.fee_rate
        self.available_balance -= cost + fee
        self.current_balance -= cost + fee
        pos = self.positions[symbol]
        total_qty = pos.quantity + quantity
        pos.entry_price = (pos.entry_price * pos.quantity + cost) / total_qty
        pos.quantity = total_qty
        pos.buy_fee += fee
        pos.current_price = execution_price
        trade = TradeRecord(timestamp=current_time, symbol=symbol, side='buy', price=execution_price,
                            quantity=quantity, fee=fee, total_amount=cost + fee)
        self.trade_history.append(trade)
        self.total_trades += 1
        return True, f"추가 매수 완료: {symbol}", trade

    def can_sell(self, symbol: str, quantity: float) -> Tuple[bool, str]:
        """매도 가능 여부 확인"""
        if symbol not in self.positions:
            return False, f"포지션 없음: {symbol}"

        position = self.positions[symbol]
        if quantity > position.quantity:
            return False, f"보유 수량 부족: 보유 {position.quantity}, 요청 {quantity}"

        return True, "매도 가능"

    def sell(self, symbol: str, price: float, quantity: float, current_time: Optional[datetime] = None) -> Tuple[bool, str, Optional[TradeRecord], Optional[float]]:
        """
        가상 매도 주문 실행

        Returns:
            (성공 여부, 메시지, 거래 기록, 수익)
        """
        if current_time is None:
            current_time = datetime.now()

        if symbol not in self.positions:
            return False, f"포지션 없음: {symbol}", None, None

        position = self.positions[symbol]
        if quantity > position.quantity:
            return False, f"보유 수량 부족: 보유 {position.quantity}, 요청 {quantity}", None, None

        # 슬리피지 적용
        execution_price = price * (1 - self.slippage_rate)
        total_revenue = execution_price * quantity
        fee = total_revenue * self.fee_rate
        net_revenue = total_revenue - fee

        # 수익 계산
        buy_fee_part = position.buy_fee * (quantity / position.quantity) if position.quantity else 0.0
        profit = (execution_price - position.entry_price) * quantity - fee - buy_fee_part
        profit_rate = ((execution_price - position.entry_price) / position.entry_price) * 100 if position.entry_price > 0 else 0

        # 자금 추가
        self.available_balance += net_revenue
        self.current_balance += net_revenue
        self.total_profit += profit

        # 통계 업데이트
        if profit > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        # 최대 낙폭 업데이트
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
        else:
            drawdown = ((self.peak_balance - self.current_balance) / self.peak_balance) * 100
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown

        # 포지션 업데이트 또는 제거
        if quantity >= position.quantity:
            # 전량 매도
            del self.positions[symbol]
        else:
            # 부분 매도
            position.quantity -= quantity
            position.buy_fee -= buy_fee_part

        # 거래 기록
        trade = TradeRecord(
            timestamp=current_time,
            symbol=symbol,
            side='sell',
            price=execution_price,
            quantity=quantity,
            fee=fee,
            total_amount=net_revenue,
            profit=profit
        )
        self.trade_history.append(trade)
        self.total_trades += 1

        logger.info(f"매도 완료: {symbol} {quantity}개 @ {execution_price:,.0f}원, 수익: {profit:,.0f}원 ({profit_rate:.2f}%)")

        return True, f"매도 완료: {symbol}", trade, profit

    def equity(self) -> float:
        """지금 가치 = 현금 + 가진 코인 평가금 (현재가 기준)"""
        return self.available_balance + sum(p.current_price * p.quantity for p in self.positions.values())

    def get_statistics(self) -> Dict:
        """통계 정보 반환"""
        equity = self.equity()
        total_profit_rate = (equity - self.initial_balance) / self.initial_balance * 100 if self.initial_balance > 0 else 0
        closed = self.winning_trades + self.losing_trades
        win_rate = self.winning_trades / closed * 100 if closed else 0
        unrealized_profit = sum(pos.profit for pos in self.positions.values())

        return {
            "mode": "simulation",
            "initial_balance": self.initial_balance,
            "current_balance": self.current_balance,
            "available_balance": self.available_balance,
            "equity": equity,
            "total_profit": self.total_profit + unrealized_profit,
            "total_profit_rate": total_profit_rate,
            "unrealized_profit": unrealized_profit,
            "positions": {symbol: pos.to_dict() for symbol, pos in self.positions.items()},
            "trade_history_count": len(self.trade_history),
            "statistics": {
                "win_rate": win_rate,
                "total_trades": self.total_trades,
                "closed_trades": closed,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "max_drawdown": self.max_drawdown,
                "peak_balance": self.peak_balance
            }
        }

    # ── 저장·불러오기 (봇을 다시 켜도 가상 잔고·포지션이 이어지게) ──
    def to_state(self) -> Dict:
        return {
            'initial_balance': self.initial_balance, 'current_balance': self.current_balance,
            'available_balance': self.available_balance, 'total_profit': self.total_profit,
            'total_trades': self.total_trades, 'winning_trades': self.winning_trades, 'losing_trades': self.losing_trades,
            'max_drawdown': self.max_drawdown, 'peak_balance': self.peak_balance,
            'positions': [p.to_dict() for p in self.positions.values()],
        }

    def load_state(self, state: Dict):
        self.initial_balance = float(state.get('initial_balance', self.initial_balance))
        self.current_balance = float(state.get('current_balance', self.initial_balance))
        self.available_balance = float(state.get('available_balance', self.initial_balance))
        self.total_profit = float(state.get('total_profit', 0))
        self.total_trades = int(state.get('total_trades', 0))
        self.winning_trades = int(state.get('winning_trades', 0))
        self.losing_trades = int(state.get('losing_trades', 0))
        self.max_drawdown = float(state.get('max_drawdown', 0))
        self.peak_balance = float(state.get('peak_balance', self.initial_balance))
        self.positions = {}
        for p in state.get('positions', []):
            self.positions[p['symbol']] = VirtualPosition(
                symbol=p['symbol'], entry_price=float(p['entry_price']), quantity=float(p['quantity']),
                entry_time=datetime.fromisoformat(p['entry_time']), current_price=float(p.get('current_price') or p['entry_price']),
                buy_fee=float(p.get('buy_fee', 0)), condition=p.get('condition', ''))

    def get_recent_trades(self, limit: int = 10) -> List[Dict]:
        """최근 거래 내역 반환"""
        recent = self.trade_history[-limit:] if len(self.trade_history) > limit else self.trade_history
        return [asdict(trade) for trade in recent]

