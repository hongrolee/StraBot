"""
로그 뷰어 창
- 실시간 로그 표시
- 로그 필터링
- 로그 검색
- 로그 파일 저장
"""

import logging
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from typing import Optional, List
from datetime import datetime
import re

logger = logging.getLogger(__name__)


class LogViewer:
    """로그 뷰어"""
    
    def __init__(self, log_handler: Optional[logging.Handler] = None):
        """
        Args:
            log_handler: 로그 핸들러 (선택)
        """
        self.root = tk.Toplevel()
        self.root.title("로그 보기")
        self.root.geometry("800x500")
        
        self.log_handler = log_handler
        self.log_buffer: List[str] = []
        self.filter_level = "ALL"
        self.search_text = ""
        
        self._create_widgets()
        
        logger.info("로그 뷰어 초기화")
    
    def _create_widgets(self):
        """위젯 생성"""
        # 필터 및 검색 프레임
        filter_frame = ttk.Frame(self.root)
        filter_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(filter_frame, text="필터:").pack(side=tk.LEFT, padx=5)
        level_combo = ttk.Combobox(filter_frame, values=["ALL", "DEBUG", "INFO", "WARNING", "ERROR"], state="readonly", width=10)
        level_combo.set("ALL")
        level_combo.bind("<<ComboboxSelected>>", lambda e: self._on_filter_change(level_combo.get()))
        level_combo.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(filter_frame, text="검색:").pack(side=tk.LEFT, padx=5)
        search_entry = ttk.Entry(filter_frame, width=20)
        search_entry.bind("<KeyRelease>", lambda e: self._on_search_change(search_entry.get()))
        search_entry.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(filter_frame, text="💾 저장", command=self._save_logs).pack(side=tk.RIGHT, padx=5)
        
        # 로그 텍스트 영역
        self.log_text = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # 로그 핸들러 설정
        if self.log_handler:
            self.log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            self.log_handler.setLevel(logging.DEBUG)
    
    def _on_filter_change(self, level: str):
        """필터 변경"""
        self.filter_level = level
        self._refresh_display()
    
    def _on_search_change(self, text: str):
        """검색어 변경"""
        self.search_text = text
        self._refresh_display()
    
    def _refresh_display(self):
        """로그 표시 새로고침"""
        self.log_text.delete(1.0, tk.END)
        
        for log_line in self.log_buffer:
            # 레벨 필터링
            if self.filter_level != "ALL":
                if f" - {self.filter_level} - " not in log_line:
                    continue
            
            # 검색 필터링
            if self.search_text:
                if self.search_text.lower() not in log_line.lower():
                    continue
            
            self.log_text.insert(tk.END, log_line + "\n")
        
        # 스크롤을 맨 아래로
        self.log_text.see(tk.END)
    
    def add_log(self, message: str, level: str = "INFO"):
        """로그 추가"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {level} - {message}"
        
        self.log_buffer.append(log_line)
        
        # 버퍼 크기 제한 (최근 1000줄만 유지)
        if len(self.log_buffer) > 1000:
            self.log_buffer = self.log_buffer[-1000:]
        
        # 필터 및 검색 조건 확인
        if self.filter_level != "ALL" and f" - {self.filter_level} - " not in log_line:
            return
        
        if self.search_text and self.search_text.lower() not in log_line.lower():
            return
        
        self.log_text.insert(tk.END, log_line + "\n")
        self.log_text.see(tk.END)
    
    def _save_logs(self):
        """로그 저장"""
        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("텍스트 파일", "*.txt"), ("모든 파일", "*.*")]
        )
        
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    for log_line in self.log_buffer:
                        f.write(log_line + "\n")
                tk.messagebox.showinfo("저장 완료", f"로그가 저장되었습니다:\n{filename}")
            except Exception as e:
                tk.messagebox.showerror("저장 오류", f"로그 저장 실패:\n{str(e)}")
    
    def show(self):
        """창 표시"""
        self.root.mainloop()
    
    def destroy(self):
        """창 닫기"""
        self.root.destroy()

