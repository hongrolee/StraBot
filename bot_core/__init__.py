"""
트레이딩 봇 코어 모듈
"""

try:
    from gui import __version__          # 버전은 gui/__init__.py 한 곳에만 적습니다
except Exception:                        # gui 없이 bot_core 만 쓸 때
    __version__ = "0.0.0"
