"""
시스템 트레이 아이콘 관리
- Windows/macOS/Linux 지원
- 상태 표시
- 컨텍스트 메뉴
"""

import logging
import threading
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

try:
    import pystray
    from PIL import Image, ImageDraw

    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False
    pystray = None  # 타입 체크를 위해 None 할당
    Image = None
    ImageDraw = None
    logging.warning(
        "pystray 또는 Pillow가 설치되지 않았습니다. 트레이 아이콘 기능을 사용할 수 없습니다."
    )

if TYPE_CHECKING:
    from PIL import Image as PILImage
    import pystray as pystray_type

logger = logging.getLogger(__name__)


class TrayIcon:
    """시스템 트레이 아이콘"""

    def __init__(self, icon_path: Optional[str] = None, tooltip: str = "StraBot"):
        """
        Args:
            icon_path: 아이콘 파일 경로
            tooltip: 툴팁 텍스트
        """
        if not HAS_PYSTRAY:
            raise ImportError(
                "pystray와 Pillow가 필요합니다. pip install pystray Pillow"
            )

        self.icon_path = icon_path
        self.tooltip = tooltip
        self.icon = None
        self.menu_items = []
        self.status = "stopped"  # 'running', 'stopped', 'error'
        self.mode = "simulation"  # 'live', 'simulation'

        # 콜백 함수
        self.on_show_window: Optional[Callable] = None
        self.on_show_status: Optional[Callable] = None
        self.on_show_logs: Optional[Callable] = None
        self.on_start: Optional[Callable] = None
        self.on_stop: Optional[Callable] = None
        self.on_exit: Optional[Callable] = None

        logger.info("트레이 아이콘 초기화")

    def _create_icon_image(self, color: str = "gray"):
        """상태 표시등이 있는 아이콘 이미지 생성"""
        if not HAS_PYSTRAY or Image is None:
            return None

        # 색상 매핑
        colors = {
            "green": (34, 197, 94),  # 실행 중 - 밝은 녹색
            "red": (239, 68, 68),  # 오류 - 빨간색
            "gray": (156, 163, 175),  # 중지 - 회색
            "yellow": (234, 179, 8),  # 경고 - 노란색
        }
        fill_color = colors.get(color, (156, 163, 175))

        # 기본 아이콘(icon.png)이 있으면 로드하여 상태 표시등 오버레이
        base_image = None
        if self.icon_path and Path(self.icon_path).exists():
            try:
                base_image = Image.open(self.icon_path).convert("RGBA")
                base_image = base_image.resize((64, 64), Image.LANCZOS)
            except Exception:
                base_image = None

        if base_image:
            # 기존 아이콘 위에 우측 하단 상태 표시등 오버레이
            overlay = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            # 상태 표시등 (우측 하단, 외곽 테두리 포함)
            draw.ellipse([40, 40, 62, 62], fill=(255, 255, 255, 255))  # 흰색 테두리
            draw.ellipse([43, 43, 59, 59], fill=(*fill_color, 255))  # 상태 색상
            # 녹색일 때 발광 효과
            if color == "green":
                draw.ellipse([45, 45, 57, 57], fill=(74, 222, 128, 200))  # 밝은 중심
            image = Image.alpha_composite(base_image, overlay)
            return image
        else:
            # 아이콘 파일 없으면 원형 아이콘 생성
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse(
                [4, 4, 60, 60],
                fill=(*fill_color, 255),
                outline=(255, 255, 255, 255),
                width=2,
            )
            return image

    def _load_icon(self):
        """상태에 따른 아이콘 로드"""
        if not HAS_PYSTRAY or Image is None:
            return None

        color_map = {"running": "green", "stopped": "gray", "error": "red"}
        color = color_map.get(self.status, "gray")
        return self._create_icon_image(color)

    def _create_menu(self):
        """컨텍스트 메뉴 생성"""
        if not HAS_PYSTRAY or pystray is None:
            return None

        items = []

        # 상태 표시
        status_text = (
            f"상태: {'🟢 실행 중' if self.status == 'running' else '⚪ 중지됨'}"
        )
        if self.status == "error":
            status_text = "상태: 🔴 오류"
        items.append(pystray.MenuItem(status_text, None, enabled=False))

        items.append(
            pystray.MenuItem(
                f"모드: {'🎮 시뮬레이션' if self.mode == 'simulation' else '💰 실전'}",
                None,
                enabled=False,
            )
        )
        items.append(pystray.Menu.SEPARATOR)

        # 제어 메뉴
        if self.status == "running":
            items.append(pystray.MenuItem("⏸ 봇 중지", self._on_stop))
        else:
            items.append(pystray.MenuItem("▶ 봇 시작", self._on_start))

        items.append(pystray.Menu.SEPARATOR)

        # 기능 메뉴 (웹 대시보드)
        items.append(pystray.MenuItem("🌐 웹 대시보드 열기", self._on_show_window))
        items.append(pystray.MenuItem("📊 상태 & 수익 현황", self._on_show_status))
        items.append(pystray.MenuItem("📋 거래 내역 & 로그", self._on_show_logs))

        items.append(pystray.Menu.SEPARATOR)

        # 종료
        items.append(pystray.MenuItem("🚪 종료", self._on_exit))

        return pystray.Menu(*items)

    def _on_start(self, icon, item):
        """봇 시작"""
        if self.on_start:
            self.on_start()

    def _on_stop(self, icon, item):
        """봇 중지"""
        if self.on_stop:
            self.on_stop()

    def _on_show_window(self, icon, item):
        """설정 창 열기"""
        if self.on_show_window:
            self.on_show_window()

    def _on_show_status(self, icon, item):
        """상태 창 열기"""
        if self.on_show_status:
            self.on_show_status()

    def _on_show_logs(self, icon, item):
        """로그 창 열기"""
        if self.on_show_logs:
            self.on_show_logs()

    def _on_exit(self, icon, item):
        """종료"""
        if self.on_exit:
            self.on_exit()
        self.stop()

    def update_status(self, status: str, mode: Optional[str] = None):
        """상태 업데이트"""
        self.status = status
        if mode:
            self.mode = mode

        if self.icon:
            self.icon.icon = self._load_icon()
            self.icon.menu = self._create_menu()
            tooltip = f"{self.tooltip} - {status}"
            if mode:
                tooltip += f" ({mode})"
            self.icon.title = tooltip

    def run(self):
        """트레이 아이콘 실행"""
        if not HAS_PYSTRAY:
            logger.error("pystray를 사용할 수 없습니다")
            return

        try:
            icon_image = self._load_icon()
            menu = self._create_menu()

            self.icon = pystray.Icon("StraBot", icon_image, self.tooltip, menu)
            self.icon.run()

        except Exception as e:
            logger.error(f"트레이 아이콘 실행 오류: {e}")

    def stop(self):
        """트레이 아이콘 중지"""
        if self.icon:
            self.icon.stop()

    def run_in_thread(self):
        """별도 스레드에서 실행"""
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread
