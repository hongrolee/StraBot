"""
주문 관리 모듈
- 주문 실행 및 추적
- 주문 상태 관리
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class Order:
    """주문 데이터 클래스"""
    order_id: str
    symbol: str
    side: str  # 'buy' or 'sell'
    order_type: str  # 'market' or 'limit'
    price: float
    quantity: float
    status: str  # 'pending', 'filled', 'cancelled', 'failed'
    created_at: datetime
    filled_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    filled_quantity: Optional[float] = None


class OrderManager:
    """주문 관리자"""
    
    def __init__(self):
        self.orders: Dict[str, Order] = {}
        self.order_counter = 0
        
        logger.info("주문 관리자 초기화")
    
    def create_order(self, symbol: str, side: str, order_type: str, price: float, quantity: float) -> Order:
        """
        주문 생성
        
        Args:
            symbol: 거래 심볼
            side: 'buy' or 'sell'
            order_type: 'market' or 'limit'
            price: 주문 가격
            quantity: 주문 수량
        
        Returns:
            Order 객체
        """
        self.order_counter += 1
        order_id = f"order_{self.order_counter}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity,
            status='pending',
            created_at=datetime.now()
        )
        
        self.orders[order_id] = order
        logger.info(f"주문 생성: {order_id}, {side} {symbol} {quantity} @ {price:,.0f}")
        
        return order
    
    def update_order(self, order_id: str, status: str, filled_price: Optional[float] = None, filled_quantity: Optional[float] = None):
        """주문 상태 업데이트"""
        if order_id not in self.orders:
            logger.warning(f"주문을 찾을 수 없습니다: {order_id}")
            return
        
        order = self.orders[order_id]
        order.status = status
        
        if status == 'filled':
            order.filled_at = datetime.now()
            if filled_price is not None:
                order.filled_price = filled_price
            if filled_quantity is not None:
                order.filled_quantity = filled_quantity
        
        logger.info(f"주문 업데이트: {order_id}, 상태: {status}")
    
    def get_order(self, order_id: str) -> Optional[Order]:
        """주문 조회"""
        return self.orders.get(order_id)
    
    def get_pending_orders(self) -> List[Order]:
        """대기 중인 주문 목록"""
        return [order for order in self.orders.values() if order.status == 'pending']
    
    def get_order_history(self, limit: int = 50) -> List[Dict]:
        """주문 내역 조회"""
        orders = list(self.orders.values())
        orders.sort(key=lambda x: x.created_at, reverse=True)
        recent = orders[:limit] if len(orders) > limit else orders
        return [asdict(order) for order in recent]
    
    def cancel_order(self, order_id: str) -> bool:
        """주문 취소"""
        if order_id not in self.orders:
            return False
        
        order = self.orders[order_id]
        if order.status != 'pending':
            logger.warning(f"취소할 수 없는 주문: {order_id}, 상태: {order.status}")
            return False
        
        order.status = 'cancelled'
        logger.info(f"주문 취소: {order_id}")
        return True

