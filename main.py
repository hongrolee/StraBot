"""
트레이딩 봇 메인 진입점
- 시스템 트레이 아이콘 시작
- GUI 창 관리
- 봇 엔진 통합
"""

import logging
import logging.handlers
import os
import sys
import json
from pathlib import Path
from typing import Optional

# 경로 설정
if getattr(sys, 'frozen', False):
    # PyInstaller로 패키징된 경우
    BUNDLE_DIR = Path(sys._MEIPASS)      # 내부 리소스 (템플릿, 정적파일)
    EXE_DIR = Path(sys.executable).parent  # 실행 파일 디렉토리 (읽기 전용)
    # 사용자 데이터 디렉토리 (로그, 설정, 전략은 여기에 저장)
    import os
    BASE_DIR = Path(os.getenv('LOCALAPPDATA', os.path.expanduser('~'))) / 'StraBot'
else:
    # 소스 코드로 실행되는 경우
    BUNDLE_DIR = Path(__file__).parent
    BASE_DIR = Path(__file__).parent
    EXE_DIR = BASE_DIR

# 개발용: 데이터 폴더(설정·키·기록)를 따로 지정 — tools/dev_preview_new.py 가 '새로 설치한 화면'을 볼 때 씀
if os.environ.get('STRABOT_DATA_DIR'):
    BASE_DIR = Path(os.environ['STRABOT_DATA_DIR'])
    BASE_DIR.mkdir(parents=True, exist_ok=True)

# 서버 모드 (리눅스 VPS 등, 2026-10-17): 트레이 아이콘 · 브라우저 자동 열기 없이, 바깥(0.0.0.0)에 열고 대시보드 비밀번호로 잠금
#   켜기: STRABOT_SERVER=1 또는 `python main.py --server`. 데이터 폴더 기본값은 ~/.strabot (STRABOT_DATA_DIR 로 바꿀 수 있음)
SERVER_MODE = os.environ.get('STRABOT_SERVER') == '1' or '--server' in sys.argv
if SERVER_MODE and not os.environ.get('STRABOT_DATA_DIR') and not getattr(sys, 'frozen', False):
    BASE_DIR = Path.home() / '.strabot'
    BASE_DIR.mkdir(parents=True, exist_ok=True)

# 로그 디렉토리 생성 (사용자 데이터 디렉토리에)
log_dir = BASE_DIR / 'logs'
log_dir.mkdir(parents=True, exist_ok=True)

# 로깅 설정
try:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            # 로그 파일은 10MB가 차면 새 파일로, 옛 것은 3개까지만 (끝없이 커지지 않게)
            logging.handlers.RotatingFileHandler(str(log_dir / 'bot.log'), maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
except Exception as e:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

logger = logging.getLogger(__name__)

# (예전에는 실행 파일 끝에 붙여 둔 설정 묶음(config/)을 여기서 풀었습니다. 지금 배포판은 아무 설정·키도 담지 않고,
#  사용자가 처음 켤 때 기본 설정(시뮬레이션 모드)으로 시작하므로 그 기능을 없앴습니다.)

# 모듈 import
try:
    # sys.path에 BUNDLE_DIR 추가하여 내부 모듈 찾기
    sys.path.insert(0, str(BUNDLE_DIR))

    from utils import pyupbit_patch
    pyupbit_patch.apply()

    from gui.web_dashboard import WebDashboard
    # 트레이 아이콘(pystray)은 run() 에서 필요할 때만 불러옴 — 리눅스 서버(화면 없음)에서는 안 씀 (2026-10-17)
except ImportError as e:
    error_msg = f"모듈 import 오류: {e}\nBUNDLE_DIR: {BUNDLE_DIR}\nsys.path: {sys.path}"
    logger.error(error_msg)
    print(f"\n❌ 오류: {error_msg}\n")
    import traceback
    traceback.print_exc()
    import time
    print("\nEnter 키를 눌러 종료하십시오...")
    try:
        input()
    except Exception:
        time.sleep(5)
    sys.exit(1)


class TradingBotApp:
    """트레이딩 봇 애플리케이션"""

    def __init__(self):
        self.config_path = BASE_DIR / "config" / "bot_config.json"
        self.config = self._load_config()
        self.config['base_dir'] = str(BASE_DIR)

        self.tray_icon = None                     # gui.tray_icon.TrayIcon (서버 모드에서는 없음)
        self.web_dashboard: Optional[WebDashboard] = None

        logger.info("트레이딩 봇 애플리케이션 초기화")

    def _load_config(self) -> dict:
        """설정 파일 로드 (암호화된 필드 자동 복호화)"""
        # 암호화 유틸리티 초기화
        from utils.config_encryption import ConfigEncryption
        encryption = ConfigEncryption(user_data_dir=BASE_DIR)

        default_config = {
            'mode': 'simulation',
            # 봇 매매 규칙: 조건 목록에서 고른 매수·매도 조건 (설정 탭 '봇 매매 규칙')
            'trade_rules': {'buy': {}, 'sell': {}, 'exclude': []},
            'api_key': '',
            'api_secret': '',
            'risk_config': {
                'position_size_percent': 10.0,
                'stop_loss_percent': 5.0,
                'take_profit_percent': 10.0,
                'daily_loss_limit_percent': 10.0,
                'max_positions': 3,
                'risk_level': 'basic'
            },
            'simulation_config': {
                'initial_balance': 10000000.0
            }
        }

        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                if config:
                    # 암호화된 필드 복호화
                    config = encryption.decrypt_config(config)
                    # default_config를 베이스로 하고 로드된 값으로 업데이트
                    merged = default_config.copy()
                    merged.update(config)
                    return merged
        except Exception as e:
            logger.warning(f"설정 파일 로드 실패, 기본값 사용: {e}")

        return default_config

    def _bots(self):
        """여러 봇 관리자 (대시보드 서버 안: 시장 데이터·실시간 체결·설정을 같이 씀)"""
        return self._ensure_dashboard(open_browser=False).bots

    def _on_signal(self, signal: dict, price: float):
        """신호 콜백 (지금은 쓰지 않음)"""
        logger.info(f"신호: {signal}, 가격: {price:,.0f}")

    def _on_trade(self, side: str, trade_data):
        """거래 콜백"""
        logger.info(f"거래 완료: {side}, {trade_data.symbol}")

        # 트레이 아이콘 업데이트
        if self.tray_icon:
            # 상태 업데이트 (구현 생략)
            pass

    def _on_activity(self, message: str, level: str = 'info', icon: str = None):
        """활동 콜백 - 웹 대시보드에 실시간 활동 메시지 전송"""
        if self.web_dashboard:
            self.web_dashboard.emit_activity(message, level, icon)

    def _on_error(self, error: Exception):
        """오류 콜백 - 실행 루프 내 일시적 에러는 running 상태 유지"""
        logger.error(f"봇 오류: {error}")

        # 봇이 여전히 실행 중이면 running 상태 유지 (일시적 에러)
        # 봇이 중지된 경우에만 error 상태로 변경
        if self.web_dashboard and self.web_dashboard.bots.running_total():
            logger.warning("일시적 오류 발생, 봇은 계속 실행 중")
        else:
            if self.tray_icon:
                self.tray_icon.update_status('error')

    def _show_main_window(self, section: str = None):
        """웹 대시보드 열기

        Args:
            section: 이동할 섹션 ('status', 'logs', 'settings' 등)
        """
        import webbrowser

        if self.web_dashboard:
            # 이미 실행 중이면 브라우저만 열기
            url = f'http://localhost:{self.web_dashboard.port}'
            if section:
                url += f'#{section}'
            webbrowser.open(url)
            return

        self._ensure_dashboard(open_browser=True)

        # 특정 섹션으로 이동
        if section:
            import time
            time.sleep(1)  # 서버 시작 대기
            url = f'http://localhost:{self.web_dashboard.port}#{section}'
            webbrowser.open(url)

    def _ensure_dashboard(self, open_browser: bool = False, host: str = '127.0.0.1', require_login: Optional[bool] = None) -> WebDashboard:
        """웹 대시보드 서버가 없으면 만들어 시작 (봇은 이 서버의 시장 데이터·설정을 씀)"""
        if self.web_dashboard:
            return self.web_dashboard
        # 포트는 보통 5001. 시험용으로만 STRABOT_PORT 로 바꿀 수 있음 (같은 PC에서 두 개를 띄울 때)
        self.web_dashboard = WebDashboard(str(self.config_path), port=int(os.environ.get('STRABOT_PORT') or 5001),
                                          user_data_dir=BASE_DIR, host=host, require_login=require_login)   # 키·기록·계정 정보도 같은 데이터 폴더
        self.web_dashboard.on_save = self._on_config_save
        self.web_dashboard.on_start = self._on_bot_start
        self.web_dashboard.on_stop = self._on_bot_stop
        self.web_dashboard.on_restart = self._on_bot_restart
        self.web_dashboard.start_server(open_browser=open_browser)
        # 프로그램을 끌 때 켜져 있던 봇은 다시 켬
        try:
            resumed = self.web_dashboard.bots.resume()
            if any(ok for _, ok, _ in resumed):
                self.web_dashboard.is_running = True
                if self.tray_icon:
                    self.tray_icon.update_status('running', 'simulation')
            for _, ok, msg in resumed:
                logger.info(f"봇 다시 켜기: {msg}")
        except Exception as e:
            logger.warning(f"봇 다시 켜기 실패: {e}")
        return self.web_dashboard

    def _show_status_window(self):
        """상태 창 표시 (웹 대시보드의 상태 섹션으로 이동)"""
        self._show_main_window(section='status')

    def _show_log_viewer(self):
        """로그 뷰어 표시 (웹 대시보드의 로그 탭으로 이동)"""
        self._show_main_window(section='logs')

    def _on_config_save(self, config: dict):
        """설정 저장"""
        self.config = config
        # 로그에는 민감 정보 마스킹
        from utils.config_encryption import sanitize_config_for_logging
        sanitized_config = sanitize_config_for_logging(config)
        logger.info(f"설정 저장 완료: {sanitized_config}")

    def _on_bot_start(self):
        """모든 봇 시작 (매수 조건이 있는 봇). 하나라도 켜지면 (True, msg), 아니면 (False, 이유)"""
        try:
            bots = self._bots()
            blocked = bots.live_blocked()            # 업비트 모드: 계정 권한이 있어야 시작 (bot_core/license.py)
            if blocked:
                return False, blocked
            if bots.mode() in ('upbit', 'bithumb') and not bots.config.get('live_risk_consent_at'):
                return False, "업비트 모드는 처음 한 번 화면에서 위험 안내를 확인하고 시작해 주십시오"
            results = bots.start_all()      # 지금 보는 모드의 봇
            if not results:
                running = self._bots().running_count()
                if running:
                    return True, f"봇 {running}개가 이미 실행 중입니다"
                return False, "시작할 봇이 없습니다. 봇의 매매 규칙에서 매수 조건을 먼저 골라 주십시오."
            ok_any = any(ok for _, ok, _ in results)
            msg = ' / '.join(m for _, _, m in results)
            if self.tray_icon:
                self.tray_icon.update_status('running' if ok_any else 'error', 'simulation')
            return (True, msg) if ok_any else (False, msg)
        except Exception as e:
            logger.error(f"봇 시작 중 예외: {e}", exc_info=True)
            if self.tray_icon:
                self.tray_icon.update_status('error')
            return False, str(e)

    def _on_bot_stop(self):
        """모든 봇 중지 (업비트·시뮬레이션 두 모드 모두)"""
        if self.web_dashboard:
            self.web_dashboard.bots.stop_all()
            self.web_dashboard.update_status("⚪ 중지됨")
        if self.tray_icon:
            self.tray_icon.update_status('stopped')
        logger.info("모든 봇 중지 완료")

    def _on_bot_restart(self):
        """실행 중이던 봇만 멈췄다가 다시 시작"""
        bots = self._bots()
        running = [(bid, m) for (bid, m) in list(bots.engines) if bots.is_running(bid, m)]
        bots.stop_all()
        import time
        time.sleep(1)
        results = []
        for m in ('upbit', 'bithumb', 'sim'):
            ids = [bid for bid, mm in running if mm == m]
            if ids:
                results += bots.start_all(only=ids, mode=m)
        ok_any = any(ok for _, ok, _ in results)
        return ok_any, ' / '.join(m for _, _, m in results) or '다시 켤 봇이 없습니다'

    def _get_bot_status(self) -> Optional[dict]:
        """봇 상태 조회 (실행 중 개수)"""
        if self.web_dashboard:
            return {'running': self.web_dashboard.bots.running_total(), 'total': len(self.web_dashboard.bots.bots())}
        return None

    def _run_server_mode(self):
        """서버 모드: 트레이 · 브라우저 없이 화면 서버만. 바깥에 여는 만큼 대시보드 비밀번호가 꼭 있어야 함"""
        from gui.dashboard_auth import DashboardAuth
        host = os.environ.get('STRABOT_HOST') or '0.0.0.0'
        auth = DashboardAuth(BASE_DIR, require_all=True)
        if not auth.is_set():
            env_pw = os.environ.get('STRABOT_DASHBOARD_PASSWORD')
            if env_pw:
                auth.set_password(env_pw)
                logger.info("환경 변수의 대시보드 비밀번호를 저장했습니다")
            else:
                logger.error("서버 모드는 대시보드 비밀번호가 있어야 켜집니다: `python main.py --set-password` 로 먼저 정해 주십시오")
                sys.exit(2)
        self._ensure_dashboard(open_browser=False, host=host, require_login=True)   # 서버 모드는 127.0.0.1 에 묶어도(프록시 뒤) 로그인 필수
        logger.info(f"서버 모드로 켜졌습니다: http://{host}:{self.web_dashboard.port} (데이터 폴더 {BASE_DIR})")
        import time
        while True:
            time.sleep(1)

    def _on_exit(self):
        """종료"""
        logger.info("애플리케이션 종료")

        if self.web_dashboard:
            self.web_dashboard.bots.stop_all(remember=True)   # 다음에 켜면 켜져 있던 봇을 다시 켬
            self.web_dashboard.stop_server()

        # pystray 콜백 스레드에서 sys.exit()은 SystemExit가 잡혀서 무시됨
        # os._exit()으로 프로세스를 확실히 종료
        import os
        os._exit(0)

    def run(self):
        """애플리케이션 실행"""
        try:
            import signal
            try:
                signal.signal(signal.SIGTERM, lambda *_: self._on_exit())    # systemd stop · kill: 봇을 '다시 켤 것'으로 남기고 끔
            except (ValueError, OSError):
                pass
            if SERVER_MODE:
                self._run_server_mode()
                return
            # 트레이 아이콘 생성 시도
            try:
                from gui.tray_icon import TrayIcon
                icon_path = BASE_DIR / "assets" / "icon.png"
                if not icon_path.exists():
                    icon_path = None

                self.tray_icon = TrayIcon(icon_path=icon_path if icon_path else None)

                # 콜백 설정
                self.tray_icon.on_show_window = self._show_main_window
                self.tray_icon.on_show_status = self._show_status_window
                self.tray_icon.on_show_logs = self._show_log_viewer
                self.tray_icon.on_start = self._on_bot_start
                self.tray_icon.on_stop = self._on_bot_stop
                self.tray_icon.on_exit = self._on_exit

                # 트레이 아이콘 실행 (별도 스레드)
                self.tray_icon.run_in_thread()

                logger.info("트레이 아이콘 시작 완료")
                logger.info("트레이 아이콘을 우클릭하여 메뉴에 접근할 수 있습니다.")

            except ImportError as e:
                logger.warning(f"트레이 아이콘을 사용할 수 없습니다: {e}")
                logger.warning("⚠️ 트레이 아이콘을 사용하려면 다음 명령을 실행하십시오:")
                logger.warning("   install_tray_dependencies.bat 또는")
                logger.warning("   pip install pystray Pillow")
                logger.info("웹 대시보드로 실행합니다...")

            # 웹 대시보드 자동 시작
            self._show_main_window()

            # 메인 루프 (트레이 아이콘이 종료될 때까지 대기)
            import time
            while True:
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("사용자 중단")
            self._on_exit()
        except Exception as e:
            logger.error(f"애플리케이션 실행 오류: {e}")
            import traceback
            traceback.print_exc()
            self._on_exit()


def main():
    """메인 함수"""
    if '--set-password' in sys.argv:                      # 서버 모드용 대시보드 비밀번호 정하기 (키 · 봇은 그대로)
        from gui.dashboard_auth import DashboardAuth
        import getpass
        pw = os.environ.get('STRABOT_DASHBOARD_PASSWORD') or getpass.getpass('대시보드 비밀번호 (8자 이상): ')
        try:
            DashboardAuth(BASE_DIR).set_password(pw)
        except ValueError as e:
            print(f"저장하지 못했습니다: {e}")
            sys.exit(2)
        print(f"대시보드 비밀번호를 저장했습니다 ({BASE_DIR / 'data' / 'dashboard_auth.json'})")
        return
    try:
        app = TradingBotApp()
        app.run()
    except Exception as e:
        logger.error(f"프로그램 실행 중 치명적 오류: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        # 오류 메시지를 보여주기 위해 잠시 대기
        import time
        print("\n오류가 발생했습니다. Enter 키를 눌러 종료하십시오...")
        try:
            input()
        except Exception:
            time.sleep(5)
        sys.exit(1)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()     # 설치판(exe)에서 신뢰도 계산을 여러 프로세스로 돌릴 때 필요
    main()

