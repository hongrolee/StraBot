"""대시보드 로그인 — 바깥(다른 PC · 서버)에서 접속할 때만 비밀번호 (2026-10-17, 리눅스 서버 모드용)

- 보통(내 PC, 127.0.0.1 에만 열림): 지금처럼 비밀번호 없이. 다른 주소에서 온 요청은 403.
- 서버 모드(0.0.0.0 등 바깥에 열림, require_all=True): 모든 요청이 로그인(세션)해야 함 — 같은 서버 안(127.0.0.1)에서 와도.
  (리버스 프록시 뒤에서는 모든 요청이 127.0.0.1 로 보이므로, 바깥에 연 서버는 예외 없이 비밀번호를 받음)
- 비밀번호는 PBKDF2-SHA256(20만 번) 으로 data/dashboard_auth.json 에 저장. 틀린 시도는 주소마다 10분에 5번까지.
- 정하기: `python main.py --set-password` (환경 변수 STRABOT_DASHBOARD_PASSWORD 가 있으면 묻지 않고 그 값)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import jsonify, redirect, render_template, request, session

LOCAL_ADDRS = ('127.0.0.1', '::1', 'localhost')
MAX_FAILS, FAIL_WINDOW_SEC = 5, 600
PBKDF2_ITER = 200_000
MIN_LEN = 8
OPEN_PATHS = ('/login', '/logout', '/favicon.ico')


def _pbkdf2(password: str, salt: bytes, iters: int = PBKDF2_ITER) -> bytes:
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iters)


class DashboardAuth:
    def __init__(self, data_dir, require_all: bool = False):
        self.path = Path(data_dir) / 'data' / 'dashboard_auth.json'
        self.require_all = require_all          # True = 서버 모드: 127.0.0.1 에서 와도 로그인
        self._lock = threading.Lock()
        self._fails: dict = {}                   # 주소 → 틀린 시각들
        self._rec = self._load()

    # ── 비밀번호 저장 · 확인 ──
    def _load(self):
        try:
            rec = json.loads(self.path.read_text(encoding='utf-8'))
            return rec if isinstance(rec, dict) and rec.get('hash') and rec.get('salt') else None
        except Exception:
            return None

    def is_set(self) -> bool:
        return self._rec is not None

    def set_password(self, password: str) -> None:
        password = (password or '').strip()
        if len(password) < MIN_LEN:
            raise ValueError(f'비밀번호는 {MIN_LEN}자 이상이어야 합니다')
        salt = secrets.token_bytes(16)
        rec = {'alg': 'pbkdf2-sha256', 'iter': PBKDF2_ITER, 'salt': base64.b64encode(salt).decode(),
               'hash': base64.b64encode(_pbkdf2(password, salt)).decode(), 'set_at': int(time.time())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(rec), encoding='utf-8')
        os.replace(tmp, self.path)
        with self._lock:
            self._rec = rec
            self._fails.clear()

    def verify(self, password: str) -> bool:
        rec = self._rec
        if not rec:
            return False
        try:
            salt, want = base64.b64decode(rec['salt']), base64.b64decode(rec['hash'])
            return hmac.compare_digest(_pbkdf2(password or '', salt, int(rec.get('iter') or PBKDF2_ITER)), want)
        except Exception:
            return False

    # ── 틀린 시도 제한 ──
    def locked(self, addr: str) -> bool:
        with self._lock:
            now = time.time()
            recent = [t for t in self._fails.get(addr, []) if now - t < FAIL_WINDOW_SEC]
            self._fails[addr] = recent
            return len(recent) >= MAX_FAILS

    def note_fail(self, addr: str) -> None:
        with self._lock:
            self._fails.setdefault(addr, []).append(time.time())

    def note_ok(self, addr: str) -> None:
        with self._lock:
            self._fails.pop(addr, None)

    # ── 요청 판단 ──
    @staticmethod
    def is_local(addr) -> bool:
        return (addr or '') in LOCAL_ADDRS

    def needs_login(self, addr) -> bool:
        """이 주소에서 온 요청이 로그인해야 하는지 (require_all 이면 모두, 아니면 바깥 주소만)"""
        return self.require_all or not self.is_local(addr)

    def allowed(self, req) -> bool:
        """지금 요청이 화면 · API · 소켓을 써도 되는지"""
        if not self.needs_login(req.remote_addr):
            return True
        return self.is_set() and bool(session.get('dash_ok'))

    # ── Flask 에 붙이기 ──
    def install(self, app) -> None:
        app.config.setdefault('SESSION_COOKIE_HTTPONLY', True)
        app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
        app.config.setdefault('PERMANENT_SESSION_LIFETIME', timedelta(days=30))
        if os.environ.get('STRABOT_HTTPS') == '1':        # https 뒤에서만 (아니면 쿠키가 안 붙음)
            app.config['SESSION_COOKIE_SECURE'] = True
        auth = self

        def deny(message: str, code: int):
            if request.path.startswith('/api/') or request.path.startswith('/socket.io'):
                return jsonify({'success': False, 'error': message, 'login_required': code == 401}), code
            if code == 401:
                nxt = request.path if request.method == 'GET' else '/'
                return redirect(f'/login?next={nxt}' if nxt != '/' else '/login')
            return render_template('dashboard_login.html', error=message, disabled=True), code

        @app.before_request
        def _dashboard_guard():
            p = request.path
            if p.startswith('/static/') or p in OPEN_PATHS:
                return None
            if not auth.needs_login(request.remote_addr):
                return None
            if not auth.is_set():
                return deny('바깥 접속용 비밀번호가 아직 없습니다. 서버에서 `python main.py --set-password` 로 정한 뒤 다시 켜 주십시오.', 403)
            if session.get('dash_ok'):
                return None
            return deny('로그인이 필요합니다.', 401)

        @app.route('/login', methods=['GET', 'POST'])
        def dashboard_login():
            if not auth.needs_login(request.remote_addr) or session.get('dash_ok'):
                return redirect('/')
            if not auth.is_set():
                return render_template('dashboard_login.html',
                                       error='바깥 접속용 비밀번호가 아직 없습니다. 서버에서 `python main.py --set-password` 로 정해 주십시오.',
                                       disabled=True), 403
            addr = request.remote_addr or '?'
            error = None
            if request.method == 'POST':
                if auth.locked(addr):
                    error = '틀린 시도가 많아 10분 동안 잠겼습니다. 잠시 뒤 다시 해 주십시오.'
                elif auth.verify(request.form.get('password', '')):
                    auth.note_ok(addr)
                    session.clear()
                    session['dash_ok'] = True
                    session.permanent = True
                    nxt = request.args.get('next') or '/'
                    if not nxt.startswith('/') or nxt.startswith('//'):
                        nxt = '/'
                    return redirect(nxt)
                else:
                    auth.note_fail(addr)
                    error = '비밀번호가 맞지 않습니다.'
            return render_template('dashboard_login.html', error=error, disabled=False), (401 if error else 200)

        @app.route('/logout', methods=['GET', 'POST'])
        def dashboard_logout():
            session.clear()
            return redirect('/login')
