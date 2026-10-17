"""서버 멈춤 막기 · 알아채기

2026-10-14 23:20 새 스레드 하나가 끝내 시작되지 않아, 그 스레드를 띄우던 쪽이 파이썬 전체 잠금
(concurrent.futures 의 _global_shutdown_lock)을 쥔 채 멈춤 → 거래 내역 · 차트 · 봇 시세 확인이 모두 멈춘 일이 있었음.

1) 막기: 스레드를 '필요할 때마다 새로' 만들지 않음
   - 일꾼 묶음은 이름별로 한 번 만들어 계속 씀 (web_dashboard._shared_pool)
   - 웹 요청도 요청마다 새 스레드 대신, 켤 때 만들어 둔 일꾼들이 차례로 처리 (install_request_pool)
2) 알아채기: 감시 스레드가 30초마다 살펴봄 (Watchdog)
   - 전체 잠금이 20초 넘게 잡혀 있는지
   - 실행 중인 봇이 10분 넘게 한 바퀴도 못 돌았는지
   → 처음 알아챈 때 모든 스레드가 어디서 멈췄는지 logs/hang-날짜.txt 로 남기고, 화면에 알림
"""
import faulthandler
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

LOCK_WAIT_SEC = 20          # 전체 잠금을 이만큼 못 잡으면 멈춘 것으로 봄
BOT_STALE_SEC = 600         # 실행 중인 봇이 이만큼 한 바퀴도 못 돌면 멈춘 것으로 봄 (기간 시세를 새로 받으면 1분 넘게 걸리기도 함)
CHECK_EVERY_SEC = 30


# ── 웹 요청: 요청마다 새 스레드 대신 미리 만든 일꾼들이 처리 ──
_request_queue: "queue.Queue" = queue.Queue()
_request_workers: List[threading.Thread] = []


def install_request_pool(workers: int = 48) -> None:
    """werkzeug 의 '요청마다 새 스레드'(ThreadingMixIn)를 '미리 만든 일꾼 N명'으로 바꿈. 여러 번 불러도 한 번만.
    일꾼은 daemon 스레드라 프로그램을 끌 때 기다리지 않음 (Socket.IO 처럼 오래 열린 요청이 있어도)"""
    if _request_workers:
        return
    from werkzeug.serving import ThreadedWSGIServer

    def work():
        while True:
            server, request, client_address = _request_queue.get()
            try:
                server.process_request_thread(request, client_address)   # 처리 · 오류 기록 · 연결 닫기 (socketserver 그대로)
            except Exception as e:                                        # 어떤 오류도 일꾼을 죽이지 않음
                logger.debug(f"요청 처리 오류: {e}")

    for i in range(workers):
        t = threading.Thread(target=work, name=f'http-{i}', daemon=True)
        t.start()
        _request_workers.append(t)

    def process_request(self, request, client_address):
        _request_queue.put((self, request, client_address))

    ThreadedWSGIServer.process_request = process_request
    logger.info(f"웹 요청 일꾼 {workers}명 준비 (요청마다 새 스레드를 만들지 않음)")


# ── 감시 ──
class Watchdog:
    def __init__(self, get_engines: Callable[[], Dict], log_dir: Path,
                 notify: Optional[Callable[[str], None]] = None, bot_name: Optional[Callable[[str], str]] = None):
        self._get_engines = get_engines
        self._log_dir = Path(log_dir)
        self._notify = notify
        self._bot_name = bot_name or (lambda bid: bid)
        self._reported = False
        self.state: Optional[Dict] = None        # 화면용: 멈춘 것이 있으면 {at, problems, dump}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def problems(self, now: Optional[float] = None) -> List[str]:
        out = []
        try:
            import concurrent.futures.thread as cft
            lock = getattr(cft, '_global_shutdown_lock', None)
            if lock is not None:
                if lock.acquire(timeout=LOCK_WAIT_SEC):
                    lock.release()
                else:
                    out.append(f'일꾼 잠금이 {LOCK_WAIT_SEC}초 넘게 풀리지 않습니다 (거래 내역 · 차트 · 봇 시세 확인이 멈출 수 있음)')
        except Exception as e:
            logger.debug(f"잠금 확인 오류: {e}")
        now = time.monotonic() if now is None else now
        try:
            for (bid, mode), e in list((self._get_engines() or {}).items()):
                last = getattr(e, 'last_loop_at', None)
                if getattr(e, 'is_running', False) and last is not None and now - last > BOT_STALE_SEC:
                    out.append(f"'{self._bot_name(bid)}'({mode}) 봇이 {int((now - last) // 60)}분째 한 바퀴도 돌지 못했습니다")
        except Exception as e:
            logger.debug(f"봇 확인 오류: {e}")
        return out

    def check(self, now: Optional[float] = None) -> Optional[Dict]:
        found = self.problems(now)
        if not found:
            if self._reported:
                logger.info("멈춤이 풀렸습니다")
            self._reported, self.state = False, None
            return None
        if not self._reported:                     # 한 번 멈출 때 한 번만 남기고 알림
            self._reported = True
            dump = self._dump()
            self.state = {'at': datetime.now().isoformat(timespec='seconds'), 'problems': found, 'dump': str(dump) if dump else ''}
            logger.error("서버 멈춤을 알아챘습니다: " + ' / '.join(found) + (f" (스레드 기록: {dump})" if dump else ''))
            if self._notify:
                try:
                    self._notify('프로그램 일부가 멈췄습니다 — ' + found[0] + '. 프로그램을 다시 켜 주십시오.')
                except Exception:
                    pass
        else:
            self.state = {**(self.state or {}), 'problems': found}
        return self.state

    def _dump(self) -> Optional[Path]:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            path = self._log_dir / f"hang-{datetime.now():%Y%m%d-%H%M%S}.txt"
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f"{datetime.now().isoformat()} 서버 멈춤 — 모든 스레드가 지금 하고 있는 일\n\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
            return path
        except Exception as e:
            logger.warning(f"스레드 기록 실패: {e}")
            return None

    def start(self):
        if self._thread:
            return

        def loop():
            while not self._stop.wait(CHECK_EVERY_SEC):
                try:
                    self.check()
                except Exception as e:
                    logger.debug(f"감시 오류: {e}")
        self._thread = threading.Thread(target=loop, name='Watchdog', daemon=True)
        self._thread.start()
