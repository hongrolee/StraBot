"""
실시간 데이터 수집 모듈
- 업비트 API를 통한 실시간 가격 데이터 수집
- 시뮬레이션 모드에서는 읽기 전용 API 사용
"""

import logging
import time
import pyupbit
from typing import Dict, Optional, List
from datetime import datetime
import pandas as pd

logger = logging.getLogger(__name__)


class DataFetcher:
    """실시간 데이터 수집기"""
    
    def __init__(self, mode: str = "simulation"):
        """
        Args:
            mode: 'live' (실전) or 'simulation' (시뮬레이션)
        """
        self.mode = mode
        self.last_fetch_time = {}
        self.cache = {}
        self.cache_duration = 1.0  # 캐시 유지 시간 (초)
        
        logger.info(f"데이터 수집기 초기화: 모드={mode}")
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        현재가 조회
        
        Args:
            symbol: 거래 심볼 (예: 'KRW-BTC')
        
        Returns:
            현재가 또는 None
        """
        try:
            # 캐시 확인
            cache_key = f"price_{symbol}"
            if cache_key in self.cache:
                cached_data, cached_time = self.cache[cache_key]
                if time.time() - cached_time < self.cache_duration:
                    return cached_data
            
            # API 호출
            price = pyupbit.get_current_price(symbol)
            
            if price is None:
                logger.warning(f"가격 조회 실패: {symbol}")
                return None
            
            # 캐시 저장
            self.cache[cache_key] = (price, time.time())
            self.last_fetch_time[symbol] = datetime.now()
            
            return price
            
        except Exception as e:
            logger.error(f"가격 조회 오류 ({symbol}): {e}")
            return None
    
    def get_ohlcv(self, symbol: str, interval: str = "day", count: int = 200) -> Optional[pd.DataFrame]:
        """
        OHLCV 데이터 조회
        
        Args:
            symbol: 거래 심볼
            interval: 간격 ('day', 'minute1', 'minute5', 'minute60', etc.)
            count: 조회할 데이터 개수
        
        Returns:
            DataFrame 또는 None
        """
        try:
            df = pyupbit.get_ohlcv(symbol, interval=interval, count=count)
            
            if df is None or df.empty:
                logger.warning(f"OHLCV 데이터 조회 실패: {symbol}, {interval}")
                return None
            
            return df
            
        except Exception as e:
            logger.error(f"OHLCV 조회 오류 ({symbol}, {interval}): {e}")
            return None
    
    def get_ticker(self, symbol: str) -> Optional[Dict]:
        """
        티커 정보 조회
        
        Args:
            symbol: 거래 심볼
        
        Returns:
            티커 정보 딕셔너리 또는 None
        """
        try:
            ticker = pyupbit.get_ticker(symbol)
            return ticker
            
        except Exception as e:
            logger.error(f"티커 조회 오류 ({symbol}): {e}")
            return None
    
    def get_balance(self, symbol: str, api_key: Optional[str] = None, api_secret: Optional[str] = None) -> float:
        """
        잔고 조회 (실전 모드만)
        
        Args:
            symbol: 코인 심볼 (예: 'BTC')
            api_key: API 키
            api_secret: API 시크릿
        
        Returns:
            잔고
        """
        if self.mode == "simulation":
            logger.warning("시뮬레이션 모드에서는 잔고 조회 불가")
            return 0.0
        
        if not api_key or not api_secret:
            logger.error("API 키가 필요합니다")
            return 0.0
        
        try:
            upbit = pyupbit.Upbit(api_key, api_secret)
            balance = upbit.get_balance(symbol)
            return balance if balance else 0.0
            
        except Exception as e:
            logger.error(f"잔고 조회 오류 ({symbol}): {e}")
            return 0.0
    
    def clear_cache(self):
        """캐시 초기화"""
        self.cache.clear()
        logger.info("데이터 캐시 초기화 완료")

