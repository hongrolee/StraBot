"""
상태 보기 창
- 실시간 수익 현황
- 현재 포지션 목록
- 최근 거래 내역
- 성과 지표
"""

import logging
import tkinter as tk
from tkinter import ttk
from typing import Dict, Optional, Callable
import threading
import time

logger = logging.getLogger(__name__)


class StatusWindow:
    """상태 보기 창"""
    
    def __init__(self, on_refresh: Optional[Callable] = None):
        """
        Args:
            on_refresh: 새로고침 콜백 함수
        """
        self.root = tk.Toplevel()
        self.root.title("봇 상태 보기")
        self.root.geometry("700x600")
        
        self.on_refresh = on_refresh
        self.auto_refresh = True
        self.refresh_interval = 5.0  # 초
        
        self._create_widgets()
        self._start_auto_refresh()
        
        logger.info("상태 창 초기화")
    
    def _create_widgets(self):
        """위젯 생성"""
        # 모드 표시
        mode_frame = ttk.Frame(self.root)
        mode_frame.pack(fill=tk.X, padx=10, pady=5)
        self.mode_label = ttk.Label(mode_frame, text="모드: 🎮 시뮬레이션", font=('Arial', 10, 'bold'))
        self.mode_label.pack(side=tk.LEFT)
        
        # 수익 현황 프레임
        profit_frame = ttk.LabelFrame(self.root, text="📊 수익 현황", padding=10)
        profit_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.profit_labels = {}
        profit_items = [
            ('initial_balance', '초기 자금:'),
            ('current_balance', '현재 자금:'),
            ('total_profit_rate', '총 수익률:'),
            ('total_profit', '총 수익:'),
            ('today_profit', '오늘 수익:'),
            ('win_rate', '승률:'),
            ('max_drawdown', '최대 낙폭:')
        ]
        
        for i, (key, label) in enumerate(profit_items):
            row = i // 2
            col = (i % 2) * 2
            ttk.Label(profit_frame, text=label).grid(row=row, column=col, sticky=tk.W, padx=5, pady=2)
            value_label = ttk.Label(profit_frame, text="N/A", font=('Arial', 9))
            value_label.grid(row=row, column=col+1, sticky=tk.W, padx=5, pady=2)
            self.profit_labels[key] = value_label
        
        # 현재 포지션 프레임
        position_frame = ttk.LabelFrame(self.root, text="💼 현재 포지션", padding=10)
        position_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # 포지션 트리뷰
        columns = ('symbol', 'entry_price', 'current_price', 'profit', 'profit_rate')
        self.position_tree = ttk.Treeview(position_frame, columns=columns, show='headings', height=5)
        
        for col in columns:
            self.position_tree.heading(col, text=col.replace('_', ' ').title())
            self.position_tree.column(col, width=120)
        
        scrollbar = ttk.Scrollbar(position_frame, orient=tk.VERTICAL, command=self.position_tree.yview)
        self.position_tree.configure(yscrollcommand=scrollbar.set)
        
        self.position_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 최근 거래 내역 프레임
        trade_frame = ttk.LabelFrame(self.root, text="📋 최근 거래 내역", padding=10)
        trade_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # 거래 내역 트리뷰
        trade_columns = ('time', 'side', 'symbol', 'price', 'quantity', 'profit')
        self.trade_tree = ttk.Treeview(trade_frame, columns=trade_columns, show='headings', height=8)
        
        for col in trade_columns:
            self.trade_tree.heading(col, text=col.title())
            self.trade_tree.column(col, width=100)
        
        trade_scrollbar = ttk.Scrollbar(trade_frame, orient=tk.VERTICAL, command=self.trade_tree.yview)
        self.trade_tree.configure(yscrollcommand=trade_scrollbar.set)
        
        self.trade_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        trade_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 버튼 프레임
        button_frame = ttk.Frame(self.root)
        button_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Button(button_frame, text="🔄 새로고침", command=self.refresh).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="📊 상세 보기", command=self._show_details).pack(side=tk.LEFT, padx=5)
    
    def update_data(self, status_data: Dict):
        """데이터 업데이트"""
        try:
            # 모드 업데이트
            mode = status_data.get('mode', 'simulation')
            mode_text = f"모드: {'🎮 시뮬레이션' if mode == 'simulation' else '💰 실전'}"
            self.mode_label.config(text=mode_text)
            
            # 시뮬레이션 데이터
            if 'simulation' in status_data:
                sim_data = status_data['simulation']
                stats = sim_data.get('statistics', {})
                
                # 수익 현황 업데이트
                self.profit_labels['initial_balance'].config(text=f"{sim_data.get('initial_balance', 0):,.0f}원")
                self.profit_labels['current_balance'].config(text=f"{sim_data.get('current_balance', 0):,.0f}원")
                
                profit_rate = sim_data.get('total_profit_rate', 0)
                profit_rate_text = f"{profit_rate:+.2f}%"
                if profit_rate > 0:
                    profit_rate_text += " 🟢"
                elif profit_rate < 0:
                    profit_rate_text += " 🔴"
                self.profit_labels['total_profit_rate'].config(text=profit_rate_text)
                
                self.profit_labels['total_profit'].config(text=f"{sim_data.get('total_profit', 0):,.0f}원")
                self.profit_labels['win_rate'].config(text=f"{stats.get('win_rate', 0):.1f}% ({stats.get('winning_trades', 0)}승/{stats.get('losing_trades', 0)}패)")
                self.profit_labels['max_drawdown'].config(text=f"{stats.get('max_drawdown', 0):.2f}%")
            
            # 포지션 업데이트
            for item in self.position_tree.get_children():
                self.position_tree.delete(item)
            
            if 'simulation' in status_data and 'positions' in status_data['simulation']:
                positions = status_data['simulation']['positions']
                for symbol, pos_data in positions.items():
                    entry_price = pos_data.get('entry_price', 0)
                    current_price = pos_data.get('current_price', 0)
                    profit = pos_data.get('profit', 0)
                    profit_rate = pos_data.get('profit_rate', 0)
                    
                    self.position_tree.insert('', 'end', values=(
                        symbol,
                        f"{entry_price:,.0f}",
                        f"{current_price:,.0f}",
                        f"{profit:,.0f}",
                        f"{profit_rate:.2f}%"
                    ))
            
            # 거래 내역 업데이트
            for item in self.trade_tree.get_children():
                self.trade_tree.delete(item)
            
            # 거래 내역은 별도로 로드 필요 (구현 생략)
            
        except Exception as e:
            logger.error(f"데이터 업데이트 오류: {e}")
    
    def refresh(self):
        """새로고침"""
        if self.on_refresh:
            status_data = self.on_refresh()
            if status_data:
                self.update_data(status_data)
    
    def _show_details(self):
        """상세 보기 (구현 생략)"""
        pass
    
    def _start_auto_refresh(self):
        """자동 새로고침 시작"""
        def refresh_loop():
            while self.auto_refresh:
                time.sleep(self.refresh_interval)
                if self.auto_refresh:
                    self.root.after(0, self.refresh)
        
        thread = threading.Thread(target=refresh_loop, daemon=True)
        thread.start()
    
    def show(self):
        """창 표시"""
        self.root.mainloop()
    
    def destroy(self):
        """창 닫기"""
        self.auto_refresh = False
        self.root.destroy()

