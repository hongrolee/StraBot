"""
거래소 API 래퍼
- 업비트 API 통합 관리
- 실전 모드: 실제 주문 실행
- 시뮬레이션 모드: 시뮬레이션 엔진으로 위임
"""

import logging
from typing import Dict, Optional, Tuple
from datetime import datetime
import pyupbit

logger = logging.getLogger(__name__)


class ExchangeWrapper:
    """거래소 API 래퍼"""
    
    def __init__(self, mode: str = "simulation", api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 account=None):
        """
        Args:
            mode: 'live' (실전) or 'simulation' (시뮬레이션)
            api_key: API 키 (실전 모드만 필요)
            api_secret: API 시크릿 (실전 모드만 필요)
            account: 업비트 계좌 대신 쓸 계좌 (예: 시뮬레이션 모드의 VirtualAccount). 주면 실전과 같은 코드로 그 계좌에 주문
        """
        self.mode = mode
        self.api_key = api_key
        self.api_secret = api_secret
        self.upbit = None

        if account is not None:
            ex = getattr(account, 'EXCHANGE', None)          # 빗썸 계좌 등 (가상 계좌는 없음)
            self.mode = 'live' if ex else 'virtual'
            self.upbit = account
            logger.info(f"거래소 연결: {'빗썸' if ex == 'bithumb' else '가상 계좌' if not ex else ex}")
        elif mode == "live":
            if not api_key or not api_secret:
                raise ValueError("실전 모드에서는 API 키와 시크릿이 필요합니다")
            self.upbit = pyupbit.Upbit(api_key, api_secret)
            logger.info("거래소 연결: 실전 모드")
        else:
            logger.info("거래소 연결: 시뮬레이션 모드")
    
    def test_connection(self) -> Tuple[bool, str]:
        """
        연결 테스트
        
        Returns:
            (성공 여부, 메시지)
        """
        if self.mode == "simulation":
            return True, "시뮬레이션 모드 - 연결 테스트 불필요"
        
        try:
            # 잔고 조회로 연결 테스트
            balances = self.upbit.get_balances()
            if balances is not None:
                return True, "연결 성공"
            else:
                return False, "연결 실패: 응답 없음"
                
        except Exception as e:
            logger.error(f"연결 테스트 오류: {e}")
            return False, f"연결 실패: {str(e)}"
    
    def get_balance(self, symbol: str) -> float:
        """
        잔고 조회
        
        Args:
            symbol: 코인 심볼 (예: 'BTC')
        
        Returns:
            잔고
        """
        if self.mode == "simulation":
            return 0.0
        
        try:
            balance = self.upbit.get_balance(symbol)
            return balance if balance else 0.0
            
        except Exception as e:
            logger.error(f"잔고 조회 오류 ({symbol}): {e}")
            return 0.0
    
    def get_krw_balance(self) -> float:
        """KRW 잔고 조회"""
        return self.get_balance("KRW")

    def get_position(self, symbol: str) -> Tuple[float, float]:
        """보유 수량(주문 중 포함)과 평균 매수가. symbol 예: 'KRW-BTC' 또는 'BTC'. 실패하면 (0, 0)"""
        if self.mode == "simulation":
            return 0.0, 0.0
        currency = symbol.split('-')[-1]
        try:
            for b in self.upbit.get_balances() or []:
                if b.get('currency') == currency:
                    qty = float(b.get('balance') or 0) + float(b.get('locked') or 0)
                    return qty, float(b.get('avg_buy_price') or 0)
        except Exception as e:
            logger.error(f"보유 수량 조회 오류 ({symbol}): {e}")
        return 0.0, 0.0
    
    def get_all_positions(self) -> Dict[str, Tuple[float, float]]:
        """모든 코인 보유 수량(주문 중 포함)·평균 매수가를 한 번에 {'BTC': (qty, avg)}. 실패하면 None"""
        if self.mode == "simulation":
            return {}
        try:
            out = {}
            for b in self.upbit.get_balances() or []:
                cur = b.get('currency')
                if not cur or cur == 'KRW':
                    continue
                out[cur] = (float(b.get('balance') or 0) + float(b.get('locked') or 0), float(b.get('avg_buy_price') or 0))
            return out
        except Exception as e:
            logger.error(f"잔고 목록 조회 오류: {e}")
            return None

    def buy_market_order(self, symbol: str, price: float) -> Tuple[bool, str, Optional[Dict]]:
        """
        시장가 매수 주문
        
        Args:
            symbol: 거래 심볼 (예: 'KRW-BTC')
            price: 매수 금액 (원)
        
        Returns:
            (성공 여부, 메시지, 주문 정보)
        """
        if self.mode == "simulation":
            return False, "시뮬레이션 모드에서는 직접 주문 불가 - 시뮬레이션 엔진 사용 필요", None
        
        try:
            result = self.upbit.buy_market_order(symbol, price)
            
            if result is None:
                return False, "주문 실패: 응답 없음", None
            
            if 'error' in result:
                return False, f"주문 실패: {result.get('error', {}).get('message', '알 수 없는 오류')}", None
            
            logger.info(f"매수 주문 완료: {symbol}, 금액: {price:,.0f}원")
            return True, "매수 주문 완료", result
            
        except Exception as e:
            logger.error(f"매수 주문 오류 ({symbol}): {e}")
            return False, f"주문 오류: {str(e)}", None
    
    def sell_market_order(self, symbol: str, quantity: float) -> Tuple[bool, str, Optional[Dict]]:
        """
        시장가 매도 주문
        
        Args:
            symbol: 거래 심볼 (예: 'KRW-BTC')
            quantity: 매도 수량
        
        Returns:
            (성공 여부, 메시지, 주문 정보)
        """
        if self.mode == "simulation":
            return False, "시뮬레이션 모드에서는 직접 주문 불가 - 시뮬레이션 엔진 사용 필요", None
        
        try:
            result = self.upbit.sell_market_order(symbol, quantity)
            
            if result is None:
                return False, "주문 실패: 응답 없음", None
            
            if 'error' in result:
                return False, f"주문 실패: {result.get('error', {}).get('message', '알 수 없는 오류')}", None
            
            logger.info(f"매도 주문 완료: {symbol}, 수량: {quantity}")
            return True, "매도 주문 완료", result
            
        except Exception as e:
            logger.error(f"매도 주문 오류 ({symbol}): {e}")
            return False, f"주문 오류: {str(e)}", None
    
    def get_order(self, uuid: str) -> Optional[Dict]:
        """
        주문 조회
        
        Args:
            uuid: 주문 UUID
        
        Returns:
            주문 정보
        """
        if self.mode == "simulation":
            return None
        
        try:
            order = self.upbit.get_order(uuid)
            return order
            
        except Exception as e:
            logger.error(f"주문 조회 오류 ({uuid}): {e}")
            return None

