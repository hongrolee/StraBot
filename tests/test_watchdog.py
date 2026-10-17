"""서버 멈춤 막기 · 알아채기 (gui/watchdog.py)"""
import concurrent.futures.thread as cft
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui.watchdog as wd  # noqa: E402


def test_no_problem_when_all_fine(tmp_path):
    eng = types.SimpleNamespace(is_running=True, last_loop_at=time.monotonic())
    w = wd.Watchdog(lambda: {('b1', 'sim'): eng}, tmp_path)
    assert w.check() is None and w.state is None


def test_stale_bot_is_reported_once_with_thread_dump(tmp_path):
    notes = []
    eng = types.SimpleNamespace(is_running=True, last_loop_at=time.monotonic() - wd.BOT_STALE_SEC - 5)
    w = wd.Watchdog(lambda: {('b1', 'upbit'): eng}, tmp_path, notify=notes.append, bot_name=lambda b: '새 봇 3')
    st = w.check()
    assert st and "'새 봇 3'(upbit)" in st['problems'][0] and len(notes) == 1
    dump = Path(st['dump'])
    assert dump.exists() and 'most recent call first' in dump.read_text(encoding='utf-8')
    w.check()
    assert len(notes) == 1                                   # 같은 멈춤은 한 번만 알림
    eng.last_loop_at = time.monotonic()
    assert w.check() is None and w.state is None             # 풀리면 지움
    stopped = types.SimpleNamespace(is_running=False, last_loop_at=0)
    assert wd.Watchdog(lambda: {('b', 'sim'): stopped}, tmp_path).check() is None   # 멈춘 봇은 상관없음


def test_global_executor_lock_held_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, 'LOCK_WAIT_SEC', 0.2)
    lock = cft._global_shutdown_lock
    lock.acquire()                                           # 23:20 사고처럼 누가 쥐고 놓지 않음
    try:
        st = wd.Watchdog(lambda: {}, tmp_path).check()
    finally:
        lock.release()
    assert st and '일꾼 잠금' in st['problems'][0]


def test_request_pool_serves_without_new_threads():
    from werkzeug.serving import make_server
    wd.install_request_pool(workers=4)

    def app(environ, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [threading.current_thread().name.encode()]
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        names = {urllib.request.urlopen(f'http://127.0.0.1:{srv.port}/', timeout=5).read().decode() for _ in range(10)}
    finally:
        srv.shutdown()
    assert names and all(n.startswith('http-') for n in names)   # 미리 만든 일꾼이 처리
