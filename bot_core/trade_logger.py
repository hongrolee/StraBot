"""
거래 기록 모듈
- 거래 내역 로깅
- 파일 및 데이터베이스 저장
"""

import logging
import json
import sqlite3
from typing import Dict, List, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class TradeLogger:
    """거래 기록 관리자"""
    
    def __init__(self, log_dir: str = "logs", db_path: Optional[str] = None):
        """
        Args:
            log_dir: 로그 디렉토리
            db_path: 데이터베이스 경로 (선택)
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.db_path = db_path or str(self.log_dir / "trades.db")
        self.init_database()
        
        logger.info(f"거래 기록 관리자 초기화: {log_dir}")
    
    def init_database(self):
        """데이터베이스 초기화"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    fee REAL DEFAULT 0,
                    total_amount REAL NOT NULL,
                    profit REAL,
                    mode TEXT,
                    strategy_name TEXT,
                    notes TEXT
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON trades(timestamp)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_symbol ON trades(symbol)
            """)
            
            conn.commit()
            conn.close()
            
            logger.info("데이터베이스 초기화 완료")
            
        except Exception as e:
            logger.error(f"데이터베이스 초기화 오류: {e}")
    
    def log_trade(self, trade_data: Dict, mode: str = "simulation", strategy_name: str = ""):
        """
        거래 기록
        
        Args:
            trade_data: 거래 데이터
                {
                    'timestamp': datetime,
                    'symbol': str,
                    'side': 'buy' or 'sell',
                    'price': float,
                    'quantity': float,
                    'fee': float,
                    'total_amount': float,
                    'profit': float (optional)
                }
            mode: 거래 모드 ('live' or 'simulation')
            strategy_name: 전략 이름
        """
        try:
            # 파일 로그
            log_file = self.log_dir / f"trades_{datetime.now().strftime('%Y%m%d')}.log"
            with open(log_file, 'a', encoding='utf-8') as f:
                log_entry = {
                    'timestamp': trade_data.get('timestamp', datetime.now()).isoformat(),
                    'mode': mode,
                    'strategy': strategy_name,
                    **trade_data
                }
                f.write(json.dumps(log_entry, ensure_ascii=False, default=str) + '\n')   # 시각(datetime)도 글자로
            
            # 데이터베이스 저장
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO trades 
                (timestamp, symbol, side, price, quantity, fee, total_amount, profit, mode, strategy_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data.get('timestamp', datetime.now()).isoformat(),
                trade_data.get('symbol', ''),
                trade_data.get('side', ''),
                trade_data.get('price', 0.0),
                trade_data.get('quantity', 0.0),
                trade_data.get('fee', 0.0),
                trade_data.get('total_amount', 0.0),
                trade_data.get('profit'),
                mode,
                strategy_name
            ))
            
            conn.commit()
            conn.close()
            
            logger.info(f"거래 기록 완료: {trade_data.get('symbol')} {trade_data.get('side')}")
            
        except Exception as e:
            logger.error(f"거래 기록 오류: {e}")
    
    def get_trade_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """
        거래 내역 조회
        
        Args:
            symbol: 거래 심볼 (필터링, 선택)
            limit: 조회 개수
        
        Returns:
            거래 내역 리스트
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if symbol:
                cursor.execute("""
                    SELECT * FROM trades 
                    WHERE symbol = ? 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (symbol, limit))
            else:
                cursor.execute("""
                    SELECT * FROM trades 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (limit,))
            
            rows = cursor.fetchall()
            conn.close()
            
            # 컬럼 이름 매핑
            columns = ['id', 'timestamp', 'symbol', 'side', 'price', 'quantity', 
                      'fee', 'total_amount', 'profit', 'mode', 'strategy_name', 'notes']
            
            result = []
            for row in rows:
                trade = dict(zip(columns, row))
                result.append(trade)
            
            return result
            
        except Exception as e:
            logger.error(f"거래 내역 조회 오류: {e}")
            return []
    
    def get_statistics(self, symbol: Optional[str] = None) -> Dict:
        """
        거래 통계 조회
        
        Args:
            symbol: 거래 심볼 (필터링, 선택)
        
        Returns:
            통계 딕셔너리
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if symbol:
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN side = 'buy' THEN 1 ELSE 0 END) as buy_count,
                        SUM(CASE WHEN side = 'sell' THEN 1 ELSE 0 END) as sell_count,
                        SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) as win_count,
                        SUM(CASE WHEN profit < 0 THEN 1 ELSE 0 END) as loss_count,
                        SUM(profit) as total_profit,
                        AVG(profit) as avg_profit
                    FROM trades
                    WHERE symbol = ? AND profit IS NOT NULL
                """, (symbol,))
            else:
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN side = 'buy' THEN 1 ELSE 0 END) as buy_count,
                        SUM(CASE WHEN side = 'sell' THEN 1 ELSE 0 END) as sell_count,
                        SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) as win_count,
                        SUM(CASE WHEN profit < 0 THEN 1 ELSE 0 END) as loss_count,
                        SUM(profit) as total_profit,
                        AVG(profit) as avg_profit
                    FROM trades
                    WHERE profit IS NOT NULL
                """)
            
            row = cursor.fetchone()
            conn.close()
            
            if row and row[0] > 0:
                total_trades, buy_count, sell_count, win_count, loss_count, total_profit, avg_profit = row
                win_rate = (win_count / (win_count + loss_count) * 100) if (win_count + loss_count) > 0 else 0
                
                return {
                    'total_trades': total_trades,
                    'buy_count': buy_count,
                    'sell_count': sell_count,
                    'winning_trades': win_count,
                    'losing_trades': loss_count,
                    'win_rate': win_rate,
                    'total_profit': total_profit or 0.0,
                    'avg_profit': avg_profit or 0.0
                }
            else:
                return {
                    'total_trades': 0,
                    'buy_count': 0,
                    'sell_count': 0,
                    'winning_trades': 0,
                    'losing_trades': 0,
                    'win_rate': 0.0,
                    'total_profit': 0.0,
                    'avg_profit': 0.0
                }
                
        except Exception as e:
            logger.error(f"통계 조회 오류: {e}")
            return {}

