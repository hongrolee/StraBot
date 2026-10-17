"""
계정 권한표 — "실제 주문은 계정 필요" 잠금 (bot_core/license.py)

- 권한표 확인: 정상 / 위조 / 만료 / 다른 설치 / 다른 버전 / 모양이 다름
- 잠금 켜짐(설치판·STRABOT_LICENSE_ENFORCE=1) vs 꺼짐(소스 실행): 꺼져 있으면 예전과 똑같이 동작
- 로그인·갱신·로그아웃과 저장 (비밀번호는 저장하지 않음, 토큰·권한표는 암호화)
- 서버에 닿지 않아도 만료(exp)까지는 그대로, 그 뒤에는 막힘
- BotManager.start: 업비트 모드는 막히고 시뮬레이션은 됨
- 실행 중에 만료: 새로 사기만 멈추고 팔기·손절·진행 중이던 다시 사기는 계속
- 화면 경로: 사는 주문(/api/markets/buy, /api/orders/batch)은 막고 파는 주문(/api/holdings/sell)은 막지 않음
- 업데이트 확인: 버전 비교, 설치 파일 sha256 확인

실제 요청·주문 없음: 서버는 가짜(FakeServer), 네트워크 연결은 아예 막음, 파일은 임시 폴더만.
서명용 키는 테스트마다 새로 만들어 쓰고 모듈의 공개키를 바꿔 끼움 (실제 개인키는 쓰지 않음).
실행: venv/Scripts/python.exe -m pytest tests/test_license.py -q
"""
import base64
import hashlib
import json
import socket
import sys
import time
import types
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot_core.bot_engine as be  # noqa: E402
import bot_core.license as lic  # noqa: E402
from bot_core.bot_engine import BotEngine  # noqa: E402
from bot_core.bot_manager import BotManager  # noqa: E402
from bot_core.virtual_account import VirtualAccount  # noqa: E402
from utils import server_client, update_check  # noqa: E402
from utils.config_encryption import ConfigEncryption  # noqa: E402
from utils.server_client import ServerUnreachable  # noqa: E402
from test_bot_manager import PRICES, RULES, QuietEngine, data, engine, tick  # noqa: E402
from test_condition_trader import FakeExchange, NullLogger  # noqa: E402
from test_spike_rebuy import T0 as SPIKE_T0, prices as flow_prices, spike_snap  # noqa: E402

DAY = 86400
WEEK = 7 * DAY
PASSWORD = 'pw-secret-123'


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def sign(priv, **payload) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return f"{b64(body)}.{b64(priv.sign(body))}"


def payload(iid, now, **over):
    return {'v': 1, 'sub': 12, 'email': 'a@b.c', 'plan': 'beta', 'live': True, 'iat': int(now), 'exp': int(now) + WEEK,
            'iid': iid, **over}


class FakeServer:
    """StraBot 계정 서버 대신 (API_SPEC.md 모양 그대로)"""

    def __init__(self, priv, clock):
        self.priv, self.clock = priv, clock
        self.calls = []
        self.down = False
        self.live = True
        self.notice = '베타 기간에는 실제 주문도 무료입니다.'
        self.tokens = set()
        self.version = {'version': '1.1.0', 'url': None, 'sha256': None, 'size': 0, 'notes': '', 'released': '2026-10-01'}
        self.signer = priv                      # 위조 시험: 다른 키로 바꿈
        self.ent = None                         # 요금제 권한 (None = 예전 권한표처럼 ent 없음)
        self.plan = None
        self.until = None

    def _license(self, iid):
        plan = self.plan or ('beta' if self.live else 'free')
        extra = {'ent': self.ent} if self.ent is not None else {}
        return sign(self.signer, **payload(iid, self.clock[0], plan=plan, live=self.live, **extra))

    def __call__(self, method, path, body=None, timeout=None):
        body = dict(body or {})
        self.calls.append((method, path, body))
        if self.down:
            raise ServerUnreachable('down')
        if path == '/api/auth/login':
            if body.get('password') != PASSWORD:
                return {'ok': False, 'error_code': 'BAD_LOGIN', 'message': '이메일 또는 비밀번호가 맞지 않습니다.'}
            token = f'rt-{len(self.tokens) + 1}'
            self.tokens.add(token)
            return {'ok': True, 'refresh_token': token, 'license': self._license(body['install_id']),
                    'plan': 'beta', 'live': self.live, 'notice': self.notice}
        if path == '/api/license/refresh':
            if body.get('refresh_token') not in self.tokens:
                return {'ok': False, 'error_code': 'BAD_TOKEN', 'message': '다시 로그인해 주십시오.'}
            return {'ok': True, 'license': self._license(body['install_id']), 'plan': 'beta', 'live': self.live,
                    'notice': self.notice, 'until': self.until}
        if path == '/api/auth/logout':
            self.tokens.discard(body.get('refresh_token'))
            return {'ok': True}
        if path == '/api/version':
            return dict(self.version)
        raise AssertionError(f'모르는 경로 {path}')

    def paths(self):
        return [p for _, p, _ in self.calls]


# ── 공통 준비 ──
@pytest.fixture(autouse=True)
def safe(monkeypatch):
    """실제 네트워크 연결을 막고(실수로 업비트·서버에 닿지 않게), 잠금은 기본으로 꺼 둠"""
    def no_network(*a, **k):
        raise AssertionError('테스트에서 실제 네트워크에 연결하려 했습니다')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(be.time, 'sleep', lambda s: None)
    monkeypatch.delenv('STRABOT_LICENSE_ENFORCE', raising=False)
    monkeypatch.delenv('STRABOT_SERVER', raising=False)
    PRICES.clear()
    QuietEngine.fake = None
    yield
    QuietEngine.fake = None


@pytest.fixture
def priv(monkeypatch):
    """테스트용 서명 키 (매번 새로). 모듈의 공개키를 이 키의 공개키로 바꿔 끼움"""
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(lic, 'PUBLIC_KEY_B64', base64.urlsafe_b64encode(raw).decode())
    return key


@pytest.fixture
def clock(monkeypatch):
    """권한표 쪽 시계만 바꿈 (만료를 시험하려고)"""
    now = [float(int(time.time()))]
    monkeypatch.setattr(lic, 'time', types.SimpleNamespace(time=lambda: now[0]))
    return now


@pytest.fixture
def server(priv, clock, monkeypatch):
    s = FakeServer(priv, clock)
    monkeypatch.setattr(server_client, 'request', s)
    return s


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv('STRABOT_LICENSE_ENFORCE', '1')


def manager(tmp_path) -> lic.LicenseManager:
    return lic.LicenseManager(tmp_path / 'data', ConfigEncryption(user_data_dir=tmp_path))


def bot_manager(tmp_path, lm, mode='upbit'):
    cfg = {'bots': [], 'app_mode': mode}
    acct = VirtualAccount(tmp_path / 'sim_account.json', lambda m: PRICES.get(m), initial_krw=1_000_000)
    m = BotManager(cfg, lambda c: None, lambda: {}, None, data_root=tmp_path / 'bots', engine_cls=QuietEngine,
                   get_account=lambda: acct, live_block=lm.block_message, live_limit=lm.live_bot_limit)
    return m, cfg


def bought(e):
    return [o[1] for o in e.exchange.orders if o[0] == 'buy']


# ── 권한표 확인 ──
def test_verify_accepts_good_license(priv, clock):
    p = lic.verify_license(sign(priv, **payload('iid-1', clock[0])), 'iid-1')
    assert p['live'] is True and p['plan'] == 'beta' and p['email'] == 'a@b.c'


def test_verify_rejects_forged_and_tampered(priv, clock):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(lic.LicenseError) as e:
        lic.verify_license(sign(other, **payload('iid-1', clock[0])), 'iid-1')       # 다른 키로 서명 = 위조
    assert e.value.reason == 'signature'
    good = sign(priv, **payload('iid-1', clock[0], live=False))
    body = b64(json.dumps(payload('iid-1', clock[0], live=True)).encode())          # 내용만 바꿔치기
    with pytest.raises(lic.LicenseError) as e:
        lic.verify_license(f"{body}.{good.split('.')[1]}", 'iid-1')
    assert e.value.reason == 'signature'


def test_verify_rejects_expired_wrong_install_wrong_version_and_garbage(priv, clock):
    text = sign(priv, **payload('iid-1', clock[0]))
    assert lic.verify_license(text, 'iid-1', now=clock[0] + WEEK - 1)
    for kwargs, install, now, reason in (
            ({}, 'iid-1', clock[0] + WEEK, 'expired'),                 # exp 가 지남
            ({}, 'iid-2', clock[0], 'install'),                        # 다른 설치의 권한표
            ({'v': 2}, 'iid-1', clock[0], 'version'),
            ({'exp': 'soon'}, 'iid-1', clock[0], 'format')):
        with pytest.raises(lic.LicenseError) as e:
            lic.verify_license(sign(priv, **payload('iid-1', clock[0], **kwargs)), install, now=now)
        assert e.value.reason == reason
    for junk in ('', 'abc', 'a.b.c', '....', None, '!!!.???'):
        with pytest.raises(lic.LicenseError):
            lic.verify_license(junk, 'iid-1')


def test_embedded_public_key_is_the_spec_key_and_rejects_other_signers():
    assert lic.PUBLIC_KEY_B64 == '32CsJwor5ZmncrLJqBfEzHacjfZBQOvc97xmMZSaCtg='
    assert len(lic._b64url_decode(lic.PUBLIC_KEY_B64)) == 32
    with pytest.raises(lic.LicenseError) as e:                         # 실제 공개키로는 아무 키로 만든 권한표가 통하지 않음
        lic.verify_license(sign(Ed25519PrivateKey.generate(), **payload('iid-1', time.time())), 'iid-1')
    assert e.value.reason == 'signature'


# ── 잠금 켜짐 / 꺼짐 ──
def test_enforced_only_when_frozen_or_env(monkeypatch):
    assert not lic.enforced()
    monkeypatch.setenv('STRABOT_LICENSE_ENFORCE', '1')
    assert lic.enforced()
    monkeypatch.setenv('STRABOT_LICENSE_ENFORCE', '0')
    assert not lic.enforced()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)            # 설치 파일로 실행
    assert lic.enforced()


def test_not_enforced_allows_live_without_account(tmp_path, monkeypatch):
    lm = manager(tmp_path)
    assert lm.can_trade_live() and lm.block_message() is None          # 소스 실행: 예전과 똑같이
    st = lm.status()
    assert st['enforced'] is False and st['logged_in'] is False and st['can_trade_live'] is True
    monkeypatch.setenv('STRABOT_LICENSE_ENFORCE', '1')
    assert not lm.can_trade_live() and lm.block_message() == lic.REQUIRED_MESSAGE
    st = lm.status()
    assert st['enforced'] and not st['can_trade_live'] and st['state'] == 'off' and '로그인하면' in st['message']


def test_install_id_is_created_once(tmp_path):
    a = manager(tmp_path)
    iid = a.install_id()
    assert manager(tmp_path).install_id() == iid and len(iid) == 36
    assert manager(tmp_path / 'other').install_id() != iid


# ── 로그인 · 저장 · 갱신 · 로그아웃 ──
def test_login_stores_license_but_never_the_password(tmp_path, server, enforce):
    lm = manager(tmp_path)
    ok, msg = lm.login(' a@b.c ', PASSWORD)
    assert ok and lm.can_trade_live() and lm.block_message() is None
    sent = server.calls[0][2]
    assert set(sent) == {'email', 'password', 'install_id', 'version'}      # 보내는 것은 이것뿐
    assert sent['install_id'] == lm.install_id() and sent['email'] == 'a@b.c'
    raw = (tmp_path / 'data' / 'license.json').read_text(encoding='utf-8')
    saved = json.loads(raw)
    assert PASSWORD not in raw and 'password' not in saved
    assert saved['email'] == 'a@b.c' and saved['refresh_token'] != 'rt-1' and '.' not in saved['license']   # 암호화돼 있음
    again = manager(tmp_path)                                              # 프로그램을 껐다 켬
    assert again.can_trade_live() and again.logged_in()
    st = again.status()
    assert st['state'] == 'ok' and st['email'] == 'a@b.c' and st['plan'] == 'beta' and st['live'] is True
    assert '실제 주문 사용 가능' in st['message'] and '까지 확인됨' in st['message'] and st['notice'] == server.notice
    assert st['signup_url'].endswith('/signup') and st['account_url'].endswith('/account')


def test_login_failures_store_nothing(tmp_path, server, enforce):
    lm = manager(tmp_path)
    ok, msg = lm.login('a@b.c', 'wrong')
    assert not ok and '맞지 않습니다' in msg and not lm.logged_in() and not lm.can_trade_live()
    assert not lm.login('', PASSWORD)[0] and not lm.login('a@b.c', '')[0]
    server.down = True
    ok, msg = lm.login('a@b.c', PASSWORD)
    assert not ok and '서버에 닿지 않습니다' in msg and lm.offline
    server.down = False
    server.signer = Ed25519PrivateKey.generate()                          # 서버인 척하는 곳이 준 위조 권한표
    ok, msg = lm.login('a@b.c', PASSWORD)
    assert not ok and not lm.can_trade_live() and not (tmp_path / 'data' / 'license.json').exists()


def test_license_for_another_install_is_useless_here(tmp_path, server, enforce):
    a = manager(tmp_path / 'pc-a')
    assert a.login('a@b.c', PASSWORD)[0]
    b = manager(tmp_path / 'pc-b')
    b._load().update(a._load())                                           # 권한표·토큰을 다른 PC로 복사
    assert a.can_trade_live() and not b.can_trade_live()
    assert b.status()['state'] == 'invalid'


def test_refresh_daily_and_retry_after_failure(tmp_path, server, clock, enforce):
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    first_exp = lm.status()['exp']
    assert not lm.maybe_refresh()                                         # 방금 받음 → 다시 묻지 않음
    clock[0] += DAY - 60
    assert not lm.maybe_refresh() and server.paths().count('/api/license/refresh') == 0
    clock[0] += 120                                                       # 하루 지남
    assert lm.maybe_refresh() and lm.status()['exp'] > first_exp
    assert set(server.calls[-1][2]) == {'refresh_token', 'install_id', 'version'}
    server.down = True
    clock[0] += DAY + 1
    assert not lm.maybe_refresh() and lm.offline
    n = len(server.calls)
    clock[0] += 600
    assert not lm.maybe_refresh() and len(server.calls) == n              # 실패 뒤 1시간 안에는 다시 두드리지 않음
    clock[0] += 3600
    lm.maybe_refresh()
    assert len(server.calls) == n + 1
    ok, msg = lm.refresh()                                                # [다시 확인]은 언제든
    assert not ok and '그대로 쓸 수 있습니다' in msg


def test_offline_keeps_working_until_exp_then_blocks(tmp_path, server, clock, enforce):
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    server.down = True
    clock[0] += 6 * DAY
    lm.maybe_refresh()
    st = lm.status()
    assert lm.can_trade_live() and st['offline'] and st['state'] == 'ok' and '서버에 닿지 않습니다' in st['message']
    clock[0] += DAY + 1                                                   # 7일이 지남
    assert not lm.can_trade_live()
    st = lm.status()
    assert st['state'] == 'expired' and st['logged_in'] and '확인 기간이 끝났습니다' in st['message']
    assert '확인 기간이 끝났습니다' in lm.block_message()
    server.down = False                                                   # 다시 닿으면 바로 풀림
    assert lm.refresh()[0] and lm.can_trade_live() and not lm.status()['offline']


def test_bad_token_keeps_license_until_exp_and_asks_to_login_again(tmp_path, server, clock, enforce):
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    server.tokens.clear()                                                 # 다른 곳에서 다시 로그인·비밀번호 변경 등
    ok, msg = lm.refresh()
    assert not ok and '다시 로그인' in msg and not lm.logged_in()
    st = manager(tmp_path).status()                                       # 껐다 켜도 같은 상태
    assert lm.can_trade_live() and st['state'] == 'ok' and st['needs_login'] and st['email'] == 'a@b.c'
    assert '까지 확인됨' in st['message'] and '다시 로그인이 필요합니다' in st['message']
    n = len(server.calls)
    clock[0] += 2 * DAY
    assert not lm.maybe_refresh() and len(server.calls) == n              # 토큰이 없으니 서버를 두드리지 않음
    clock[0] += WEEK                                                      # 만료되면 막힘
    assert not lm.can_trade_live() and '다시 로그인이 필요합니다' in lm.block_message()
    st = lm.status()
    assert st['state'] == 'off' and st['needs_login'] and st['message'] == '다시 로그인이 필요합니다'
    assert lm.login('a@b.c', PASSWORD)[0]                                 # 다시 로그인하면 풀림
    st = lm.status()
    assert lm.can_trade_live() and st['logged_in'] and not st['needs_login'] and '다시 로그인' not in st['message']


def test_unknown_error_codes_show_server_message_and_keep_license(tmp_path, server, monkeypatch, enforce):
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    monkeypatch.setattr(server_client, 'request',
                        lambda *a, **k: {'ok': False, 'error_code': 'RATE_LIMITED', 'message': '잠시 뒤 다시 시도해 주십시오.'})
    assert lm.refresh() == (False, '잠시 뒤 다시 시도해 주십시오.')
    assert lm.logged_in() and lm.can_trade_live() and not lm.status()['needs_login']
    assert lm.login('a@b.c', PASSWORD) == (False, '잠시 뒤 다시 시도해 주십시오.')
    monkeypatch.setattr(server_client, 'request', lambda *a, **k: {'ok': False, 'error_code': 'SOMETHING_NEW'})
    ok, msg = lm.refresh()
    assert not ok and msg and lm.can_trade_live()


def test_logout_clears_everything_even_offline(tmp_path, server, enforce):
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    assert lm.logout()[0] and server.calls[-1][1] == '/api/auth/logout' and not server.tokens
    assert not (tmp_path / 'data' / 'license.json').exists() and not lm.can_trade_live()
    assert manager(tmp_path).status()['email'] == ''
    lm.login('a@b.c', PASSWORD)
    server.down = True
    assert lm.logout()[0] and not lm.logged_in() and not (tmp_path / 'data' / 'license.json').exists()


def test_policy_flip_needs_no_bot_change(tmp_path, server, clock, enforce):
    """유료 전환: 서버가 live=false 인 권한표를 주면 봇은 그대로 막음"""
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    assert lm.can_trade_live()
    server.live = False
    assert lm.refresh()[0]
    st = lm.status()
    assert not lm.can_trade_live() and st['state'] == 'no_live' and st['plan'] == 'free' and st['logged_in']
    assert '실제 주문' in lm.block_message()


def test_corrupt_files_never_crash(tmp_path, enforce):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'license.json').write_text('{not json', encoding='utf-8')
    (tmp_path / 'data' / 'install_id').write_text('not-a-uuid', encoding='utf-8')
    lm = manager(tmp_path)
    assert not lm.can_trade_live() and lm.status()['state'] == 'off' and len(lm.install_id()) == 36
    assert not lm.maybe_refresh() and lm.refresh() == (False, '로그인이 필요합니다.')


# ── 봇 시작 (BotManager.start 한 길목) ──
def test_start_blocked_in_upbit_mode_allowed_in_sim(tmp_path, server, enforce):
    lm = manager(tmp_path)
    m, cfg = bot_manager(tmp_path, lm)
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES})
    m.send_to(a['id'], 'sim')                                             # (업비트에서 만든 봇) 시뮬레이션에도 있게
    ok, msg = m.start(a['id'], 'upbit')
    assert not ok and msg == lic.REQUIRED_MESSAGE and m.live_blocked('upbit') == msg
    assert m.start_all(mode='upbit') == [(a['id'], False, msg)] and m.running_total() == 0
    cfg['live_risk_consent_at'] = '2026-10-01T00:00:00'
    m.get(a['id'])['was_running'] = {'upbit': True}
    assert m.resume() == [(a['id'], False, msg)]                          # 프로그램을 켤 때 이어 켜기도 같은 길목
    assert m.get(a['id'])['was_running']['upbit']                         # 표시는 남김 → 로그인 뒤 다시 켜면 이어 켬
    assert m.live_blocked('sim') is None
    ok, _ = m.start(a['id'], 'sim')                                       # 시뮬레이션은 계정 없이
    assert ok and m.running_modes(a['id']) == ['sim']
    lm.login('a@b.c', PASSWORD)
    ok, _ = m.start(a['id'], 'upbit')
    assert ok and m.running_modes(a['id']) == ['upbit', 'sim']
    assert m.stop(a['id'], 'upbit')[0] and m.stop(a['id'], 'sim')[0]      # 멈추기는 언제나


def test_start_not_gated_when_not_enforced(tmp_path):
    lm = manager(tmp_path)
    m, cfg = bot_manager(tmp_path, lm)
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES})
    assert m.start(a['id'], 'upbit')[0]                                   # 소스 실행: 계정 없이 예전처럼
    assert engine(m, a['id'], 'upbit')._new_buys_allowed()


def test_manager_without_license_hook_is_unchanged(tmp_path, enforce):
    cfg = {'bots': [], 'app_mode': 'upbit'}
    m = BotManager(cfg, lambda c: None, lambda: {}, None, data_root=tmp_path / 'bots', engine_cls=QuietEngine)
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES})
    assert m.live_blocked() is None and m.start(a['id'])[0]


def test_default_mode_is_simulation():
    assert BotManager({}, lambda c: None, lambda: {}, None, data_root=Path('unused')).mode() == 'sim'
    assert BotManager({'app_mode': 'upbit'}, lambda c: None, lambda: {}, None, data_root=Path('unused')).mode() == 'upbit'
    assert BotManager({'app_mode': '???'}, lambda c: None, lambda: {}, None, data_root=Path('unused')).mode() == 'sim'


# ── 실행 중에 만료 ──
def test_expiry_mid_run_blocks_new_buys_but_not_sells(tmp_path, server, clock, enforce):
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    m, cfg = bot_manager(tmp_path, lm)
    QuietEngine.fake = FakeExchange(krw=10_000_000)
    _, _, a = m.create({'name': '실전', 'budget': 10 ** 12, 'trade_rules': RULES,
                        'risk_config': {'position_size_percent': 1, 'max_positions': 10, 'stop_loss_percent': 5,
                                        'take_profit_percent': 50}})
    assert m.start(a['id'])[0]
    e = engine(m, a['id'])
    tick(e, data({'A': 0, 'B': 0, 'C': 0}))                               # 기준
    tick(e, data({'A': 6, 'B': 0, 'C': 0}))
    assert bought(e) == ['KRW-A'] and not e.get_status()['buy_blocked']

    server.down = True
    clock[0] += WEEK + 1                                                  # 서버에 닿지 않은 채 만료
    tick(e, data({'A': 6, 'B': 6, 'C': 0}))                               # B 가 새로 들어와도 사지 않음
    tick(e, data({'A': 6, 'B': 6, 'C': 6}))
    assert bought(e) == ['KRW-A'] and e.get_status()['buy_blocked']
    notes = [d['message'] for d in e.decisions if '새 코인 사기를 멈췄습니다' in d['message']]
    assert len(notes) == 1 and '팔기·손절은 계속' in notes[0]             # 안내는 한 번만
    assert m.is_running(a['id'])                                          # 봇은 멈추지 않음

    tick(e, data({'A': -10, 'B': 6, 'C': 6}, price={'A': 90.0, 'B': 100.0, 'C': 100.0}))   # A 가 -10% → 손절은 그대로
    assert e.exchange.orders[-1][:2] == ('sell', 'KRW-A') and 'KRW-A' not in e.ledger
    assert m.stop(a['id'])[0] and not m.is_running(a['id'])               # 멈추기도 그대로
    ok, msg = m.start(a['id'])
    assert not ok and '확인 기간이 끝났습니다' in msg                         # 다시 시작은 막힘

    server.down = False
    assert lm.refresh()[0]                                                # 다시 확인하면 이어서
    e.is_running = True
    tick(e, data({'B': 6, 'C': 6, 'D': 6}))                               # 막힌 동안 들어온 B·C 는 몰아서 사지 않고 새로 들어온 D 만
    assert bought(e) == ['KRW-A', 'KRW-D'] and not e.get_status()['buy_blocked']
    assert sum('이어서 합니다' in d['message'] for d in e.decisions) == 1


def test_rebuy_in_progress_continues_without_license(tmp_path):
    """봇이 스스로 판 몫을 되찾는 '다시 사기'와 일부 팔기는 권한이 없어도 끝까지 함"""
    ON = {'enabled': True}
    config = {'mode': 'live', 'risk_config': {}, 'trade_rules': {'buy': {'id': 'my_up'}, 'spike_rebuy': ON}}
    e = BotEngine(config, data_dir=tmp_path, can_buy=lambda: False)
    e.trade_logger = NullLogger()
    e.exchange = FakeExchange(krw=0, holdings={'A': [1000.0, 100.0]})
    e.ledger = {'KRW-A': {'qty': 1000.0, 'entry_price': 100.0, 'opened_at': '', 'condition': 't'}}
    e.exchange.prices = {'KRW-A': 106.0}
    e.spike_tick(spike_snap(), now=SPIKE_T0)
    e.exchange.prices = {'KRW-A': 102.0}
    e.spike_tick(flow_prices(102.0), now=SPIKE_T0 + 10)
    assert e.exchange.orders[-1][0] == 'sell' and e.ledger['KRW-A']['qty'] == pytest.approx(700)
    e.exchange.prices = {'KRW-A': 90.0}
    e.spike_tick(flow_prices(90.0), now=SPIKE_T0 + 60)
    assert e.exchange.orders[-1][0] == 'buy' and e.ledger['KRW-A']['qty'] > 1000
    assert not e._new_buys_allowed()                                      # 새 코인 사기는 막힌 상태


def test_can_buy_error_blocks_buys_not_sells(tmp_path):
    def boom():
        raise RuntimeError('x')
    e = BotEngine({'mode': 'live', 'trade_rules': {'buy': {'id': 'my_up'}}}, data_dir=tmp_path, can_buy=boom)
    assert not e._new_buys_allowed()
    assert BotEngine({'mode': 'live', 'trade_rules': {}}, data_dir=tmp_path)._new_buys_allowed()      # 기본: 막지 않음


# ── 화면 경로 (Flask) ──
class FakeUpbit:
    def __init__(self):
        self.orders = []

    def get_balances(self):
        return [{'currency': 'KRW', 'balance': '1000000'}, {'currency': 'BTC', 'balance': '2', 'locked': '0', 'avg_buy_price': '100'}]

    def buy_market_order(self, symbol, amount):
        self.orders.append(('buy', symbol, amount))
        return {'uuid': 'fake-buy'}

    def sell_market_order(self, symbol, qty):
        self.orders.append(('sell', symbol, qty))
        return {'uuid': 'fake-sell'}


@pytest.fixture
def dash(tmp_path, monkeypatch, server):
    """임시 폴더의 대시보드 (실제 설정·기록 파일을 건드리지 않음). 업비트는 가짜, 봇 엔진은 스레드 없는 것"""
    import pyupbit
    from gui.web_dashboard import WebDashboard
    cfg_path = tmp_path / 'config' / 'bot_config.json'
    cfg_path.parent.mkdir()
    cfg_path.write_text(json.dumps({'app_mode': 'upbit', 'bots': [
        {'id': 'b1', 'name': '봇', 'budget': 0, 'trade_rules': RULES, 'risk_config': {}, 'was_running': {}}]}), encoding='utf-8')
    d = WebDashboard(str(cfg_path), port=0, user_data_dir=tmp_path)
    d.bots._engine_cls = QuietEngine
    d.upbit = FakeUpbit()
    monkeypatch.setattr(d, '_account', lambda mode=None: d.sim_account if (mode or d.app_mode()) == 'sim' else d.upbit)
    monkeypatch.setattr(pyupbit, 'get_tickers', lambda fiat='KRW', **k: ['KRW-BTC', 'KRW-ETH'])
    monkeypatch.setattr(pyupbit, 'get_current_price', lambda t: {x: 50_000.0 for x in t} if isinstance(t, list) else 50_000.0)
    monkeypatch.setattr(d, '_live_start_summary', lambda bot_id=None: {})
    # 주문 화면은 거래소별 원화 마켓 · 시세 캐시를 씀 (가짜: BTC · ETH, 5만 원)
    monkeypatch.setattr(d, '_get_krw_markets', lambda: [{'market': 'KRW-BTC', 'korean_name': '비트코인'}, {'market': 'KRW-ETH', 'korean_name': '이더리움'}])
    monkeypatch.setattr(d, '_krw_tickers', lambda max_age=2.0: {m: {'trade_price': 50_000.0} for m in ('KRW-BTC', 'KRW-ETH')})
    d.client = d.app.test_client()
    return d


def post(d, path, body=None):
    r = d.client.post(path, json=body or {})
    return r.status_code, r.get_json()


def test_routes_refuse_live_start_and_buys_without_account(dash, enforce):
    for path, body in (('/api/control', {'action': 'start', 'confirm_live': True}),
                       ('/api/bots/b1/start', {'confirm_live': True}),
                       ('/api/markets/buy', {'currency': 'BTC', 'amount': 10_000}),
                       ('/api/orders/batch', {'orders': [{'currency': 'BTC', 'side': 'buy', 'amount': 10_000},
                                                         {'currency': 'ETH', 'side': 'buy', 'amount': 10_000}]})):
        code, out = post(dash, path, body)
        assert code == 403 and out['error_code'] == 'LICENSE_REQUIRED' and out['success'] is False, path
        assert out['error'] == lic.REQUIRED_MESSAGE and out['license']['state'] == 'off'
    assert dash.upbit.orders == [] and dash.bots.running_total() == 0
    assert 'live_risk_consent_at' not in dash.config                      # 막힌 요청은 동의 기록도 남기지 않음


def test_sell_routes_are_never_blocked(dash, enforce):
    code, out = post(dash, '/api/holdings/sell', {'currency': 'BTC', 'ratio': 1})
    assert code == 200 and out['success'] and dash.upbit.orders == [('sell', 'KRW-BTC', 2.0)]
    code, out = post(dash, '/api/orders/batch', {'orders': [{'currency': 'BTC', 'side': 'sell', 'ratio': 1}]})
    assert code == 200 and out['ok'] == 1 and dash.upbit.orders[-1] == ('sell', 'KRW-BTC', 2.0)
    # 섞인 주문: 파는 것은 보내고 사는 것만 거절
    code, out = post(dash, '/api/orders/batch', {'orders': [{'currency': 'ETH', 'side': 'buy', 'amount': 10_000},
                                                            {'currency': 'BTC', 'side': 'sell', 'ratio': 1}]})
    by = {r['currency']: r for r in out['results']}
    assert code == 200 and by['BTC']['success'] and not by['ETH']['success'] and by['ETH']['error_code'] == 'LICENSE_REQUIRED'
    assert [o[0] for o in dash.upbit.orders] == ['sell', 'sell', 'sell']
    assert post(dash, '/api/control', {'action': 'stop'})[1]['success']   # 멈추기도 막지 않음
    assert post(dash, '/api/bots/b1/stop')[1]['success']


def test_simulation_mode_needs_no_account(dash, enforce):
    dash.config['app_mode'] = 'sim'
    dash._price_cache['KRW-BTC'] = (time.time(), 50_000.0)
    PRICES['KRW-BTC'] = 50_000.0
    code, out = post(dash, '/api/markets/buy', {'currency': 'BTC', 'amount': 10_000})
    assert code == 200 and out['success'], out                            # 가상 계좌로 삼
    code, out = post(dash, '/api/bots/b1/start')
    assert code == 200 and out['success'] and dash.bots.running_modes('b1') == ['sim']
    assert dash.upbit.orders == [] and '/api/auth/login' not in dash_server_paths(dash)


def dash_server_paths(d):
    return server_client.request.paths()


def test_routes_unchanged_when_not_enforced(dash):
    code, out = post(dash, '/api/bots/b1/start')                           # 소스 실행: 예전처럼 위험 고지 확인부터
    assert code == 400 and out['error_code'] == 'CONFIRM_LIVE'
    code, out = post(dash, '/api/markets/buy', {'currency': 'BTC', 'amount': 10_000})
    assert code == 200 and out['success'] and dash.upbit.orders == [('buy', 'KRW-BTC', 10_000.0)]
    code, out = post(dash, '/api/bots/b1/start', {'confirm_live': True})
    assert code == 200 and out['success'] and dash.bots.running_modes('b1') == ['upbit']
    st = dash.client.get('/api/license/status').get_json()
    assert st['enforced'] is False and st['can_trade_live'] is True and st['version']


def test_login_route_unlocks_live_and_logout_locks_again(dash, enforce):
    code, out = post(dash, '/api/license/login', {'email': 'a@b.c', 'password': 'wrong'})
    assert code == 400 and not out['success'] and out['state'] == 'off' and '맞지 않습니다' in out['error']
    code, out = post(dash, '/api/license/login', {'email': 'a@b.c', 'password': PASSWORD})
    assert code == 200 and out['success'] and out['state'] == 'ok' and out['can_trade_live'] and out['email'] == 'a@b.c'
    assert PASSWORD not in json.dumps(out)
    code, out = post(dash, '/api/bots/b1/start')
    assert out['error_code'] == 'CONFIRM_LIVE'                            # 잠금을 지나 다음 단계(위험 고지)로
    code, out = post(dash, '/api/bots/b1/start', {'confirm_live': True})
    assert code == 200 and out['success']
    code, out = post(dash, '/api/markets/buy', {'currency': 'BTC', 'amount': 10_000})
    assert code == 200 and out['success']
    code, out = post(dash, '/api/license/refresh')
    assert code == 200 and out['success']
    code, out = post(dash, '/api/license/logout')
    assert out['success'] and out['state'] == 'off' and not out['can_trade_live']
    assert dash.bots.is_running('b1', 'upbit')                            # 실행 중인 봇은 멈추지 않음 (새로 사기만 멈춤)
    assert post(dash, '/api/markets/buy', {'currency': 'BTC', 'amount': 10_000})[1]['error_code'] == 'LICENSE_REQUIRED'
    # 권한이 없는 채로 재시작하면 봇이 꺼진 채 남으므로, 멈추기 전에 거절하고 그대로 둠
    dash.is_running = True
    code, out = post(dash, '/api/control', {'action': 'restart'})
    assert code == 403 and out['error_code'] == 'LICENSE_REQUIRED' and dash.bots.is_running('b1', 'upbit')


def test_page_shows_strabot_and_first_run_is_simulation(tmp_path, dash, monkeypatch):
    html = dash.client.get('/').get_data(as_text=True)
    assert 'StraBot' in html and 'AutoTradingBot' not in html and '>PRO<' not in html
    assert 'id="set-account"' in html and 'id="license-modal"' in html and 'id="btn-check-update"' in html
    assert '잠금이 꺼져 있습니다' in html                                    # 소스 실행 표시
    assert 'data-app-mode="upbit"' in html                                # 이미 고른 모드는 그대로
    monkeypatch.setenv('STRABOT_LICENSE_ENFORCE', '1')
    assert '잠금이 꺼져 있습니다' not in dash.client.get('/').get_data(as_text=True)
    del dash.config['app_mode']                                           # 처음 실행 (설정에 모드가 없음)
    assert dash.app_mode() == 'sim' and dash.bots.mode() == 'sim'
    assert '<body class="app-sim"' in dash.client.get('/').get_data(as_text=True)


# ── 업데이트 확인 ──
def test_version_compare():
    assert update_check.is_newer('1.1.1', '1.1.0') and update_check.is_newer('1.10.0', '1.9.9')
    assert update_check.is_newer('v2.0', '1.99.99') and update_check.is_newer('1.2.0-beta', '1.1.0')
    assert not update_check.is_newer('1.1.0', '1.1.0') and not update_check.is_newer('1.1', '1.1.0')
    assert not update_check.is_newer('1.0.9', '1.1.0') and not update_check.is_newer('', '1.1.0')
    assert not update_check.is_newer(None, '1.1.0') and not update_check.is_newer('이상한값', '1.1.0')


def test_check_update_route_uses_single_version_source(dash):
    import bot_core
    import gui
    assert bot_core.__version__ == gui.__version__
    out = dash.client.get('/api/check-update').get_json()
    assert out['success'] and out['current_version'] == gui.__version__ and not out['update_available']
    server_client.request.version.update(version='9.0.0', url='/downloads/StraBot_Setup.exe', sha256='a' * 64, size=1234)
    out = dash.client.get('/api/check-update').get_json()
    assert out['update_available'] and out['latest_version'] == '9.0.0' and out['downloadable'] and out['file_size'] == 1234
    assert out['can_install'] is False                                    # 소스 실행: 자동 설치는 하지 않음
    code, res = post(dash, '/api/download-update')
    assert code == 400 and not res['success']
    server_client.request.down = True
    r = dash.client.get('/api/check-update')
    assert r.status_code == 502 and not r.get_json()['success']


def test_installer_url_rules(monkeypatch):
    assert update_check.installer_url({'url': None}) is None
    assert update_check.installer_url({'url': '/d/x.exe'}) == f'{server_client.DEFAULT_SERVER_URL}/d/x.exe'
    assert update_check.installer_url({'url': 'http://example.com/x.exe'}) is None        # https 가 아니면 받지 않음
    assert update_check.installer_url({'url': 'https://example.com/x.exe'}) == 'https://example.com/x.exe'
    monkeypatch.setenv('STRABOT_SERVER', 'http://127.0.0.1:8700/')
    assert server_client.server_url() == 'http://127.0.0.1:8700'
    assert update_check.installer_url({'url': '/d/x.exe'}) == 'http://127.0.0.1:8700/d/x.exe'


class FakeDownload:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), 7):
            yield self.content[i:i + 7]


def test_download_runs_only_when_sha256_matches(tmp_path, monkeypatch):
    import requests
    content = b'installer-bytes' * 100
    monkeypatch.setattr(requests, 'get', lambda url, **k: FakeDownload(content))
    good = hashlib.sha256(content).hexdigest()
    path = update_check.download_verified('https://x/y.exe', good.upper(), dest_dir=tmp_path)
    assert path.read_bytes() == content and path.parent == tmp_path
    with pytest.raises(update_check.UpdateError) as e:                    # 확인값이 다르면 지우고 멈춤
        update_check.download_verified('https://x/y.exe', 'b' * 64, dest_dir=tmp_path)
    assert '달라서 지웠습니다' in str(e.value) and not path.exists()
    with pytest.raises(update_check.UpdateError):                         # 확인값이 없으면 받지도 않음
        update_check.download_verified('https://x/y.exe', None, dest_dir=tmp_path)
    monkeypatch.setattr(requests, 'get', lambda url, **k: FakeDownload(b'', status=404))
    with pytest.raises(update_check.UpdateError):
        update_check.download_verified('https://x/y.exe', good, dest_dir=tmp_path)
    assert not path.exists()


def test_server_client_maps_failures_to_unreachable(monkeypatch):
    import requests

    class R:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body
    seen = {}

    def fake(method, url, json=None, headers=None, timeout=None):
        seen.update(method=method, url=url, json=json, timeout=timeout)
        return seen['resp']
    monkeypatch.setattr(requests, 'request', fake)
    seen['resp'] = R(200, {'ok': True})
    assert server_client.request('POST', '/api/auth/login', {'a': 1}) == {'ok': True}
    assert seen['url'] == f'{server_client.DEFAULT_SERVER_URL}/api/auth/login' and seen['timeout'] <= 10
    seen['resp'] = R(401, {'error_code': 'BAD_LOGIN', 'message': 'x'})
    assert server_client.request('POST', '/x')['ok'] is False
    for resp in (R(502, {}), R(200, ValueError('html')), R(200, [1, 2])):
        seen['resp'] = resp
        with pytest.raises(ServerUnreachable):
            server_client.request('GET', '/api/version')

    def broken(*a, **k):
        raise requests.ConnectionError('no route')
    monkeypatch.setattr(requests, 'request', broken)
    with pytest.raises(ServerUnreachable):
        server_client.request('GET', '/api/version')


def test_member_only_update_sends_login_token(tmp_path, monkeypatch):
    """회원 전용 설치 파일: 계정 서버 주소일 때만 로그인 표를 붙이고, 401 이면 '로그인해 주십시오'"""
    import requests
    content = b'installer-bytes' * 10
    good = hashlib.sha256(content).hexdigest()
    seen = {}

    def fake_get(url, **k):
        seen.update(k['headers'])
        return FakeDownload(content)
    monkeypatch.setattr(requests, 'get', fake_get)
    url = update_check.installer_url({'url': '/api/update/download'})
    h = update_check.auth_headers(url, 'rt-1', 'iid-1')
    update_check.download_verified(url, good, dest_dir=tmp_path, headers=h)
    assert seen['Authorization'] == 'Bearer rt-1' and seen['X-Install-Id'] == 'iid-1'
    with pytest.raises(update_check.UpdateError) as e:                    # 로그인 안 함
        update_check.auth_headers(url, '', 'iid-1')
    assert '로그인' in str(e.value)
    with pytest.raises(update_check.UpdateError):                         # 다른 곳에는 로그인 표를 보내지 않음
        update_check.auth_headers('https://example.com/x.exe', 'rt-1', 'iid-1')
    monkeypatch.setattr(requests, 'get', lambda url, **k: FakeDownload(b'', status=401))
    with pytest.raises(update_check.UpdateError) as e:
        update_check.download_verified(url, good, dest_dir=tmp_path, headers=h)
    assert '로그인' in str(e.value)



# ── 요금제별 권한 (ent, 2026-10-14) ──
LITE_UPBIT = {'modes': ['upbit'], 'live_bots': 2, 'features': []}
PRO = {'modes': ['upbit', 'bithumb'], 'live_bots': 10, 'features': ['live_chart_orders']}


def test_ent_of_old_and_new_licenses():
    assert lic.ent_of(None)['modes'] == [] and lic.ent_of({'live': False, 'ent': PRO})['modes'] == []
    old = lic.ent_of({'live': True})                                          # 예전 권한표(ent 없음) = 전부 · 한도 없음
    assert old['modes'] == ['upbit', 'bithumb'] and old['live_bots'] is None and 'live_chart_orders' in old['features']
    assert lic.ent_of({'live': True, 'ent': {'modes': ['upbit', 'binance'], 'live_bots': 2}}) == \
        {'modes': ['upbit'], 'live_bots': 2, 'features': []}                  # 모르는 거래소는 뺌


def test_beta_plan_is_simulation_only(tmp_path, server, enforce):
    server.live, server.plan, server.ent = False, 'beta', {'modes': [], 'live_bots': 0, 'features': []}
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    m, cfg = bot_manager(tmp_path, lm)
    _, _, a = m.create({'name': 'A', 'trade_rules': RULES})
    m.send_to(a['id'], 'sim')
    ok, msg = m.start(a['id'], 'upbit')
    assert not ok and '베타' in msg and '실제 주문이 없습니다' in msg and '다시 확인' in msg
    assert m.start(a['id'], 'sim')[0]                                         # 시뮬레이션은 전부
    st = lm.status()
    assert st['plan_name'] == '베타' and st['ent']['modes'] == [] and st['pricing_url'].endswith('/pricing')


def test_lite_one_exchange_and_bot_limit(tmp_path, server, enforce):
    server.plan, server.ent = 'lite', LITE_UPBIT
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    assert lm.can_trade_live(mode='upbit') and not lm.can_trade_live(mode='bithumb') and lm.live_bot_limit() == 2
    m, cfg = bot_manager(tmp_path, lm)
    bots = [m.create({'name': f'B{i}', 'trade_rules': RULES})[2] for i in range(3)]
    m.send_to(bots[0]['id'], 'bithumb')
    ok, msg = m.start(bots[0]['id'], 'bithumb')
    assert not ok and '빗썸' in msg and '라이트' in msg and '업비트만' in msg
    assert m.start(bots[0]['id'], 'upbit')[0] and m.start(bots[1]['id'], 'upbit')[0]
    ok, msg = m.start(bots[2]['id'], 'upbit')
    assert not ok and '2개까지' in msg and m.limit_blocked(bots[2]['id'], 'upbit') == msg
    assert m.stop(bots[1]['id'], 'upbit')[0]
    ok, msg = m.start(bots[2]['id'], 'upbit')
    assert ok, msg                                                               # 하나 멈추면 다른 봇을 켤 수 있음


def test_downgrade_over_limit_stops_new_buys_of_later_bots(tmp_path, server, clock, enforce):
    server.plan, server.ent = 'pro', PRO
    lm = manager(tmp_path)
    lm.login('a@b.c', PASSWORD)
    m, cfg = bot_manager(tmp_path, lm)
    bots = [m.create({'name': f'B{i}', 'trade_rules': RULES})[2] for i in range(3)]
    for b in bots:
        assert m.start(b['id'], 'upbit')[0]
    assert all(engine(m, b['id'], 'upbit')._new_buys_allowed() for b in bots)
    server.plan, server.ent = 'lite', LITE_UPBIT                              # 프로 → 라이트 (봇 2개)
    clock[0] += DAY + 1
    assert lm.refresh()[0]
    allowed = [engine(m, b['id'], 'upbit')._new_buys_allowed() for b in bots]
    assert allowed == [True, True, False]                                      # 늦게 켠 봇만 새로 사기를 멈춤 (팔기는 그대로)
    assert m.running_modes(bots[2]['id']) == ['upbit']                         # 멈추지는 않음


def test_feature_block_for_live_chart_orders(tmp_path, server, enforce):
    lm = manager(tmp_path)
    assert lm.feature_block('live_chart_orders')                               # 로그인 전
    server.plan, server.ent = 'lite', LITE_UPBIT
    lm.login('a@b.c', PASSWORD)
    msg = lm.feature_block('live_chart_orders')
    assert msg and '다시 확인' in msg and '라이트' in msg
    server.plan, server.ent = 'pro', PRO
    assert lm.refresh()[0] and lm.feature_block('live_chart_orders') is None


def test_feature_and_limit_open_when_not_enforced(tmp_path):
    lm = manager(tmp_path)
    assert lm.feature_block('live_chart_orders') is None and lm.live_bot_limit() is None
    assert lm.can_trade_live(mode='bithumb')


def test_plan_until_survives_restart(tmp_path, server, enforce):
    """요금제 끝나는 날(until)은 껐다 켜도 남는다 (전에는 저장하지 않아 다음 하루 갱신 전까지 화면에서 빠졌음, 2026-10-17)"""
    server.until = 1_900_000_000
    lm = manager(tmp_path)
    assert lm.login('a@b.c', PASSWORD)[0] and lm.refresh()[0]      # (가짜 서버는 새로 받기 응답에만 until 을 넣음)
    assert lm.status()['plan_until'] == 1_900_000_000
    again = manager(tmp_path)
    assert again.status()['plan_until'] == 1_900_000_000
