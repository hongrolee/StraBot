"""대시보드 로그인 (서버 모드) — 내 PC 는 비밀번호 없이, 바깥 접속은 비밀번호, 틀린 시도 제한 (2026-10-17)
실행: venv/Scripts/python.exe -m pytest tests/test_dashboard_auth.py -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.dashboard_auth import DashboardAuth  # noqa: E402
from gui.web_dashboard import WebDashboard  # noqa: E402

FAR = {'REMOTE_ADDR': '10.0.0.5'}
HERE = {'REMOTE_ADDR': '127.0.0.1'}


def _wd(tmp_path, host='127.0.0.1', require_login=None):
    wd = WebDashboard(str(tmp_path / 'config' / 'bot_config.json'), port=0, user_data_dir=tmp_path, host=host, require_login=require_login)
    wd._save_config_internal = lambda *a, **k: None
    return wd


def test_password_store_and_verify(tmp_path):
    a = DashboardAuth(tmp_path)
    assert not a.is_set() and not a.verify('anything')
    with pytest.raises(ValueError):
        a.set_password('short')
    a.set_password('correct horse')
    assert a.is_set() and a.verify('correct horse') and not a.verify('wrong')
    again = DashboardAuth(tmp_path)                       # 파일에서 다시 읽어도 같음
    assert again.verify('correct horse')
    raw = (tmp_path / 'data' / 'dashboard_auth.json').read_text(encoding='utf-8')
    assert 'correct horse' not in raw                     # 평문 저장 안 함


def test_local_only_mode_blocks_outsiders(tmp_path):
    c = _wd(tmp_path).app.test_client()
    assert c.get('/api/status', environ_base=HERE).status_code == 200      # 내 PC: 비밀번호 없이
    r = c.get('/api/status', environ_base=FAR)
    assert r.status_code == 403 and r.get_json()['success'] is False       # 바깥: 비밀번호가 없으니 막힘
    assert c.get('/', environ_base=FAR).status_code == 403


def test_server_mode_requires_login_even_for_localhost(tmp_path):
    DashboardAuth(tmp_path).set_password('server-pass-1')
    c = _wd(tmp_path, host='0.0.0.0').app.test_client()
    r = c.get('/', environ_base=HERE)
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')   # 프록시 뒤면 다 127.0.0.1 로 보이므로 예외 없음
    r = c.get('/api/status', environ_base=FAR)
    assert r.status_code == 401 and r.get_json()['login_required'] is True
    assert c.get('/static/js/bot_dashboard.js', environ_base=FAR).status_code == 200   # 정적 파일 · 로그인 화면은 열림
    assert c.get('/login', environ_base=FAR).status_code == 200
    # 로그인
    r = c.post('/login', data={'password': 'wrong'}, environ_base=FAR)
    assert r.status_code == 401 and '맞지 않습니다' in r.get_data(as_text=True)
    r = c.post('/login?next=/api/status', data={'password': 'server-pass-1'}, environ_base=FAR)
    assert r.status_code == 302 and r.headers['Location'].endswith('/api/status')
    assert c.get('/api/status', environ_base=FAR).status_code == 200
    assert c.get('/', environ_base=FAR).status_code == 200
    # 로그아웃
    c.get('/logout', environ_base=FAR)
    assert c.get('/api/status', environ_base=FAR).status_code == 401


def test_login_locks_after_five_failures(tmp_path):
    DashboardAuth(tmp_path).set_password('server-pass-1')
    c = _wd(tmp_path, host='0.0.0.0').app.test_client()
    for _ in range(5):
        c.post('/login', data={'password': 'nope'}, environ_base=FAR)
    r = c.post('/login', data={'password': 'server-pass-1'}, environ_base=FAR)   # 맞아도 잠김
    assert r.status_code == 401 and '잠겼습니다' in r.get_data(as_text=True)
    other = {'REMOTE_ADDR': '10.0.0.9'}                   # 다른 주소는 영향 없음
    assert c.post('/login', data={'password': 'server-pass-1'}, environ_base=other).status_code == 302


def test_server_mode_without_password_refuses_everyone(tmp_path):
    c = _wd(tmp_path, host='0.0.0.0').app.test_client()
    assert c.get('/api/status', environ_base=HERE).status_code == 403
    assert c.get('/login', environ_base=FAR).status_code == 403
    assert '--set-password' in c.get('/login', environ_base=FAR).get_data(as_text=True)


def test_server_mode_flag_forces_login_even_when_bound_to_localhost(tmp_path):
    """프록시 뒤(STRABOT_HOST=127.0.0.1)에서도 서버 모드면 로그인 — 요청이 모두 127.0.0.1 로 보이기 때문"""
    DashboardAuth(tmp_path).set_password('server-pass-1')
    c = _wd(tmp_path, host='127.0.0.1', require_login=True).app.test_client()
    assert c.get('/api/status', environ_base=HERE).status_code == 401
    assert c.post('/login', data={'password': 'server-pass-1'}, environ_base=HERE).status_code == 302
    assert c.get('/api/status', environ_base=HERE).status_code == 200


def test_next_redirect_only_to_own_paths(tmp_path):
    DashboardAuth(tmp_path).set_password('server-pass-1')
    c = _wd(tmp_path, host='0.0.0.0').app.test_client()
    r = c.post('/login?next=//evil.example/x', data={'password': 'server-pass-1'}, environ_base=FAR)
    assert r.status_code == 302 and r.headers['Location'].endswith('/') and 'evil' not in r.headers['Location']


def test_socket_connect_needs_login_in_server_mode(tmp_path):
    """실시간 연결(socket.io)도 로그인한 세션만 — 핸드셰이크는 Flask 앞단이라 connect 처리기에서 막음"""
    DashboardAuth(tmp_path).set_password('server-pass-1')
    wd = _wd(tmp_path, host='0.0.0.0')
    c = wd.app.test_client()
    sio = wd.socketio.test_client(wd.app, flask_test_client=c)
    assert not sio.is_connected()
    assert c.post('/login', data={'password': 'server-pass-1'}, environ_base=HERE).status_code == 302
    sio = wd.socketio.test_client(wd.app, flask_test_client=c)
    assert sio.is_connected()
    sio.disconnect()
