"""
권한표(license) — "실제 주문은 계정 필요" 잠금

- 시뮬레이션 모드는 계정도 서버도 없이 전부 동작합니다.
- 업비트(실제 주문) 모드는 StraBot 서버에 로그인해서 받은 **서명된 권한표**가 있어야 합니다.
  베타 기간에는 가입한 모든 계정이 live=true 이고, 나중에 서버가 정책만 바꾸면 됩니다 (봇은 고칠 것 없음).
- 권한표는 서버가 Ed25519 개인키로 서명하고, 봇에는 공개키만 들어 있습니다 → 서버에 닿지 않아도 확인할 수 있습니다.
  하루에 한 번쯤 새로 받아 오고, 못 받아도 만료(exp, 발급 후 7일)까지는 그대로 씁니다.
- 막는 것은 **봇 시작과 새로 사기**뿐입니다. 팔기 · 손절 · 봇 멈추기는 권한이 없어도 막지 않습니다
  (권한이 끝나도 사용자가 빠져나올 수 있어야 하므로).
- 서버로 보내는 것: 이메일 · 비밀번호(로그인할 때만, 저장하지 않음) · 설치 ID · 봇 버전.
  업비트 키 · 잔고 · 매매 기록은 보내지 않습니다.

잠금이 켜지는 때 (enforced):
  설치 파일로 실행(sys.frozen) 이거나 환경변수 STRABOT_LICENSE_ENFORCE=1 일 때만.
  소스로 실행하면(개발 · 운영자 본인) 잠금이 꺼져 있어 예전과 똑같이 동작합니다.

형식은 strabot-site/API_SPEC.md 기준:  base64url(payload_json) + "." + base64url(signature)  (패딩 없음)
"""

import base64
import binascii
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from utils import server_client
from utils.server_client import ServerUnreachable

logger = logging.getLogger(__name__)

# 서버 공개키 (Ed25519, raw 32바이트를 base64url 로). 개인키는 서버에만 있습니다.
PUBLIC_KEY_B64 = "32CsJwor5ZmncrLJqBfEzHacjfZBQOvc97xmMZSaCtg="
LICENSE_VERSION = 1

REFRESH_EVERY_SEC = 24 * 3600      # 하루에 한 번 새 권한표를 받아 옴
RETRY_AFTER_FAIL_SEC = 3600        # 못 받았으면 1시간 뒤에 다시 (서버를 두드리지 않게)
AUTO_CHECK_EVERY_SEC = 1800        # 백그라운드 확인 간격

ERROR_CODE = 'LICENSE_REQUIRED'    # 화면이 이 코드를 보고 안내 창을 띄움 (CONFIRM_LIVE 와 같은 방식)
REQUIRED_MESSAGE = ('실제 주문(업비트 · 빗썸)은 StraBot 계정으로 로그인해야 쓸 수 있습니다. '
                    '시뮬레이션은 계정 없이 그대로 쓸 수 있습니다.')

# 요금제별 권한 (권한표 ent, 서버 plans.json) — 2026-10-14
LIVE_EXCHANGES = ('upbit', 'bithumb')
EXCHANGE_NAMES = {'upbit': '업비트', 'bithumb': '빗썸'}
PLAN_NAMES = {'free': '무료', 'beta': '베타', 'lite': '라이트', 'pro': '프로', 'max': '맥스', 'lifetime': '평생', 'paid': '유료'}
FEATURE_NAMES = {'live_chart_orders': '실제 돈 차트 예약 매수'}
NO_ENT = {'modes': [], 'live_bots': 0, 'features': []}
FULL_ENT = {'modes': list(LIVE_EXCHANGES), 'live_bots': None, 'features': list(FEATURE_NAMES)}   # live_bots None = 한도 없음


def ent_of(payload: Optional[Dict]) -> Dict:
    """권한표 → 권한 {modes, live_bots, features}. live 가 아니면 아무것도 없음.
    ent 가 없는 예전 권한표(live=true)는 예전처럼 전부 허용"""
    if not payload or payload.get('live') is not True:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in NO_ENT.items()}
    e = payload.get('ent')
    if not isinstance(e, dict):
        return {k: (list(v) if isinstance(v, list) else v) for k, v in FULL_ENT.items()}
    lb = e.get('live_bots')
    return {'modes': [m for m in (e.get('modes') or []) if m in LIVE_EXCHANGES],
            'live_bots': int(lb) if isinstance(lb, (int, float)) and not isinstance(lb, bool) and lb >= 0 else None,
            'features': [str(f) for f in (e.get('features') or [])]}


class LicenseError(Exception):
    """권한표가 무효. reason: format | signature | version | expired | install"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def enforced() -> bool:
    """잠금을 실제로 적용할지: 설치 파일로 실행 중이거나 STRABOT_LICENSE_ENFORCE=1"""
    return bool(getattr(sys, 'frozen', False)) or os.environ.get('STRABOT_LICENSE_ENFORCE') == '1'


def _b64url_decode(text: str) -> bytes:
    text = text.strip().rstrip('=')
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def verify_license(text: str, install_id: str, now: Optional[float] = None) -> Dict:
    """권한표 문자열을 확인하고 payload(dict)를 돌려줌. 무효면 LicenseError.

    거절: 모양이 다름 · 서명이 틀림(위조) · v 가 다름 · exp 가 지남 · iid 가 이 설치와 다름
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        body_b64, sig_b64 = str(text or '').split('.')
        body, sig = _b64url_decode(body_b64), _b64url_decode(sig_b64)
        if not body or not sig:
            raise ValueError('empty')
    except (ValueError, binascii.Error, AttributeError):
        raise LicenseError('format')
    try:
        Ed25519PublicKey.from_public_bytes(_b64url_decode(PUBLIC_KEY_B64)).verify(sig, body)   # 서명 대상 = payload 바이트 그대로
    except (InvalidSignature, ValueError):
        raise LicenseError('signature')
    try:
        payload = json.loads(body.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('not a dict')
    except (ValueError, UnicodeDecodeError):
        raise LicenseError('format')
    if payload.get('v') != LICENSE_VERSION:
        raise LicenseError('version')
    try:
        exp = float(payload.get('exp'))
    except (TypeError, ValueError):
        raise LicenseError('format')
    if exp <= (time.time() if now is None else now):
        raise LicenseError('expired')
    if not install_id or payload.get('iid') != install_id:
        raise LicenseError('install')
    return payload


def _day_text(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d.month}월 {d.day}일"


class LicenseManager:
    """권한표 저장 · 확인 · 갱신. 저장 위치는 사용자 데이터 폴더의 data/ (license.json, install_id)

    license.json: {email, license(암호화), refresh_token(암호화), notice, last_refresh, relogin}
      - relogin: 서버가 토큰을 무효로 했을 때(BAD_TOKEN) 켜짐 — 권한표는 exp 까지 그대로 쓰되 화면에 '다시 로그인이 필요합니다'
      - 비밀번호는 저장하지 않습니다. 암호화는 utils/config_encryption.py 의 마스터 키(설정의 업비트 키와 같은 방식).
    """

    def __init__(self, data_dir: Path, encryption=None):
        """
        Args:
            data_dir: 저장 폴더 (<사용자 데이터 폴더>/data)
            encryption: ConfigEncryption (encrypt/decrypt). 없으면 그대로 저장 (테스트용)
        """
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / 'license.json'
        self.install_id_path = self.data_dir / 'install_id'
        self._enc = encryption
        self._lock = threading.RLock()
        self._state: Optional[Dict] = None
        self._install_id: Optional[str] = None
        self.offline = False                # 마지막 서버 요청이 닿지 않았는지 (화면 안내용)
        self._last_try = 0.0                # 마지막으로 서버에 물어본 시각 (성공·실패 모두)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── 설치 ID ──
    def install_id(self) -> str:
        """이 설치의 ID: 처음 한 번 만든 무작위 uuid (권한표가 다른 PC로 복사되는 것을 막는 용도)"""
        with self._lock:
            if self._install_id:
                return self._install_id
            try:
                value = self.install_id_path.read_text(encoding='utf-8').strip()
                uuid.UUID(value)
            except Exception:
                value = str(uuid.uuid4())
                try:
                    self.data_dir.mkdir(parents=True, exist_ok=True)
                    self.install_id_path.write_text(value, encoding='utf-8')
                except Exception as e:
                    logger.warning(f"설치 ID 저장 실패 (이번 실행 동안만 씀): {e}")
            self._install_id = value
            return value

    # ── 저장 ──
    def _load(self) -> Dict:
        with self._lock:
            if self._state is None:
                try:
                    raw = json.loads(self.path.read_text(encoding='utf-8'))
                    if not isinstance(raw, dict):
                        raise ValueError('not a dict')
                except Exception:
                    raw = {}
                st = {'email': str(raw.get('email') or ''), 'notice': str(raw.get('notice') or ''),
                      'last_refresh': float(raw.get('last_refresh') or 0), 'relogin': bool(raw.get('relogin')),
                      'until': raw.get('until') if isinstance(raw.get('until'), (int, float)) else None}   # 요금제 끝나는 날 (2026-10-17)
                for k in ('license', 'refresh_token'):
                    v = str(raw.get(k) or '')
                    try:
                        st[k] = self._enc.decrypt(v) if (v and self._enc) else v
                    except Exception:
                        st[k] = ''
                self._state = st
            return self._state

    def _save(self):
        with self._lock:
            st = self._load()
            out = {'email': st['email'], 'notice': st['notice'], 'last_refresh': st['last_refresh'],
                   'relogin': bool(st.get('relogin')), 'until': st.get('until')}
            for k in ('license', 'refresh_token'):
                out[k] = self._enc.encrypt(st[k]) if (st[k] and self._enc) else st[k]
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp, self.path)
            except Exception as e:
                logger.warning(f"권한표 저장 실패: {e}")

    def _clear(self):
        with self._lock:
            self._state = {'email': '', 'notice': '', 'last_refresh': 0.0, 'license': '', 'refresh_token': '',
                           'relogin': False}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"권한표 지우기 실패: {e}")
                self._save()

    # ── 확인 ──
    def _check(self, now: Optional[float] = None) -> Tuple[Optional[Dict], str]:
        """(payload 또는 None, 이유). 이유: '' | none(권한표 없음) | format | signature | version | expired | install"""
        text = self._load().get('license')
        if not text:
            return None, 'none'
        try:
            return verify_license(text, self.install_id(), now), ''
        except LicenseError as e:
            return None, e.reason
        except Exception as e:                     # 어떤 오류도 봇을 멈추게 하지 않음 — 확인 못 하면 '권한 없음'
            logger.warning(f"권한표 확인 오류: {e}")
            return None, 'format'

    def logged_in(self) -> bool:
        return bool(self._load().get('refresh_token'))

    def refresh_token(self) -> str:
        """저장된 로그인 표 (회원 전용 업데이트 받기용). 로그인 안 했으면 ''"""
        return str(self._load().get('refresh_token') or '')

    def ent(self, now: Optional[float] = None) -> Dict:
        """지금 쓸 수 있는 실제 주문 권한. 잠금이 꺼져 있으면(소스 실행) 전부"""
        if not enforced():
            return {k: (list(v) if isinstance(v, list) else v) for k, v in FULL_ENT.items()}
        payload, _ = self._check(now)
        return ent_of(payload)

    def plan_name(self, now: Optional[float] = None) -> str:
        payload, _ = self._check(now)
        return PLAN_NAMES.get(str((payload or {}).get('plan') or ''), str((payload or {}).get('plan') or ''))

    def can_trade_live(self, now: Optional[float] = None, mode: Optional[str] = None) -> bool:
        """실제 계좌 모드(업비트 · 빗썸)에서 봇 시작 · 새로 사기를 해도 되는지. mode 를 주면 그 거래소가 요금제에 있는지까지.
        잠금이 꺼져 있으면(소스 실행) 언제나 True."""
        if not enforced():
            return True
        modes = self.ent(now)['modes']
        return bool(modes) if mode is None else mode in modes

    def live_bot_limit(self, now: Optional[float] = None) -> Optional[int]:
        """실제 모드(업비트 + 빗썸 합쳐)에서 동시에 돌릴 수 있는 봇 수. None = 한도 없음"""
        return None if not enforced() else self.ent(now)['live_bots']

    def feature_block(self, feature: str, now: Optional[float] = None) -> Optional[str]:
        """요금제에 없는 기능이면 안내, 아니면 None"""
        if not enforced() or feature in self.ent(now)['features']:
            return None
        name = FEATURE_NAMES.get(feature, feature)
        return (f"{name}는 지금 계정 권한({self.plan_name(now) or '무료'})에 없습니다. StraBot은 무료로 바뀌어 로그인한 계정은 모두 쓸 수 있으니, "
                '설정 > 계정에서 [다시 확인]을 눌러 새 권한을 받아 주십시오. 팔기는 그대로 할 수 있습니다.')

    def block_message(self, mode: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
        """막혀 있으면 사용자에게 보여 줄 이유, 아니면 None (mode: 그 거래소에서 사도 되는지)"""
        if self.can_trade_live(now, mode):
            return None
        payload, reason = self._check(now)
        if payload is not None:                     # 권한표는 멀쩡한데 요금제에 실제 주문(또는 이 거래소)이 없음
            plan = self.plan_name(now) or '무료'
            modes = ent_of(payload)['modes']
            if modes and mode:
                ok = ' · '.join(EXCHANGE_NAMES.get(m, m) for m in modes)
                return (f"지금 계정 권한({plan})으로는 {EXCHANGE_NAMES.get(mode, mode)}에서 새로 사거나 봇을 시작할 수 없습니다 "
                        f"({ok}만 됩니다). StraBot은 무료로 바뀌어 두 거래소를 모두 쓸 수 있으니 설정 > 계정에서 [다시 확인]을 눌러 주십시오. 팔기는 그대로 할 수 있습니다.")
            return (f"지금 계정 권한({plan})에는 실제 주문이 없습니다. StraBot은 무료로 바뀌어 로그인한 계정은 모두 실제 주문을 쓸 수 있으니, "
                    '설정 > 계정에서 [다시 확인]을 눌러 새 권한을 받아 주십시오. 시뮬레이션과 팔기는 그대로 쓸 수 있습니다.')
        if reason == 'expired' and self.logged_in():
            return ('계정 확인 기간이 끝났습니다. 인터넷에 연결한 뒤 설정의 계정 화면에서 [다시 확인]을 눌러 주십시오. '
                    '그동안에도 팔기는 그대로 할 수 있습니다.')
        if self._load().get('relogin'):
            return ('다시 로그인이 필요합니다. 설정의 계정 화면에서 로그인해 주십시오. '
                    '시뮬레이션과 팔기는 그대로 쓸 수 있습니다.')
        return REQUIRED_MESSAGE

    # ── 서버와 주고받기 ──
    @staticmethod
    def _version() -> str:
        try:
            from gui import __version__
            return __version__
        except Exception:
            return '0.0.0'

    def _accept(self, data: Dict, now: Optional[float]) -> bool:
        """서버가 준 권한표를 확인해 저장. 확인에 실패하면 예전 것을 그대로 둠"""
        text = str(data.get('license') or '')
        try:
            verify_license(text, self.install_id(), now)
        except LicenseError as e:
            logger.warning(f"서버가 준 권한표를 확인하지 못했습니다: {e.reason}")
            return False
        with self._lock:
            st = self._load()
            st['license'] = text
            if data.get('refresh_token'):
                st['refresh_token'] = str(data['refresh_token'])
                st['relogin'] = False
            st['notice'] = str(data.get('notice') or '')[:300]
            st['until'] = data.get('until') if isinstance(data.get('until'), (int, float)) else None   # 요금제 끝나는 날 (평생 · 무료는 없음)
            st['last_refresh'] = time.time() if now is None else now
            self._save()
        return True

    def login(self, email: str, password: str, now: Optional[float] = None) -> Tuple[bool, str]:
        """(성공 여부, 안내). 비밀번호는 서버로 한 번 보내기만 하고 저장하지 않습니다"""
        email = str(email or '').strip()
        if '@' not in email or not password:
            return False, '이메일과 비밀번호를 넣어 주십시오.'
        self._last_try = time.time() if now is None else now
        try:
            data = server_client.request('POST', '/api/auth/login', {
                'email': email, 'password': password, 'install_id': self.install_id(), 'version': self._version()})
        except ServerUnreachable as e:
            self.offline = True
            return False, f'서버에 닿지 않습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주십시오. ({e})'
        except Exception as e:
            logger.warning(f"로그인 오류: {type(e).__name__}")
            return False, '로그인 중 문제가 생겼습니다. 잠시 뒤 다시 시도해 주십시오.'
        self.offline = False
        if not data.get('ok'):                            # 어떤 오류 코드든 서버가 준 안내를 그대로 보여 줌
            return False, str(data.get('message') or '로그인하지 못했습니다. 이메일과 비밀번호를 확인해 주십시오.')
        if not data.get('refresh_token'):
            return False, '서버 응답이 올바르지 않습니다. 잠시 뒤 다시 시도해 주십시오.'
        with self._lock:
            self._load()['email'] = email
            if not self._accept(data, now):
                return False, '서버가 보낸 권한표를 확인하지 못했습니다. 프로그램을 최신 버전으로 바꾼 뒤 다시 시도해 주십시오.'
        logger.info("계정 로그인 완료")
        return True, '로그인했습니다.'

    def refresh(self, now: Optional[float] = None) -> Tuple[bool, str]:
        """저장된 refresh_token 으로 새 권한표를 받아 옴. 닿지 않으면 예전 권한표를 exp 까지 그대로 씀"""
        token = self._load().get('refresh_token')
        if not token:
            return False, '로그인이 필요합니다.'
        self._last_try = time.time() if now is None else now
        try:
            data = server_client.request('POST', '/api/license/refresh', {
                'refresh_token': token, 'install_id': self.install_id(), 'version': self._version()})
        except ServerUnreachable:
            self.offline = True
            return False, '서버에 닿지 않습니다. 저장된 확인 기간까지는 그대로 쓸 수 있습니다.'
        except Exception as e:
            logger.warning(f"권한표 갱신 오류: {type(e).__name__}")
            return False, '확인 중 문제가 생겼습니다. 잠시 뒤 다시 시도해 주십시오.'
        self.offline = False
        if not data.get('ok'):
            if data.get('error_code') == 'BAD_TOKEN':
                # 토큰이 무효 (다른 곳에서 다시 로그인 · 비밀번호 변경 · 오래 안 씀) → 다시 로그인해야 새로 받을 수 있음.
                # 이미 받은 권한표는 exp 까지 그대로 쓰고, 화면에 '다시 로그인이 필요합니다' 를 띄움
                with self._lock:
                    st = self._load()
                    st['refresh_token'] = ''
                    st['relogin'] = True
                    self._save()
                return False, str(data.get('message') or '다시 로그인이 필요합니다.')
            # 그 밖의 오류 코드는 서버가 준 안내를 그대로 (권한표·토큰은 건드리지 않음)
            return False, str(data.get('message') or '확인하지 못했습니다. 잠시 뒤 다시 시도해 주십시오.')
        if not self._accept(data, now):
            return False, '서버가 보낸 권한표를 확인하지 못했습니다.'
        return True, '계정을 다시 확인했습니다.'

    def maybe_refresh(self, now: Optional[float] = None) -> bool:
        """하루가 지났으면 새로 받아 옴 (실패하면 1시간 뒤에 다시). 새로 받았으면 True. 예외를 내지 않음"""
        try:
            t = time.time() if now is None else now
            if not self.logged_in():
                return False
            if t - self._load().get('last_refresh', 0) < REFRESH_EVERY_SEC:
                return False
            if t - self._last_try < RETRY_AFTER_FAIL_SEC:
                return False
            return self.refresh(now)[0]
        except Exception as e:
            logger.warning(f"권한표 자동 갱신 오류: {e}")
            return False

    def logout(self) -> Tuple[bool, str]:
        """서버에 토큰을 무효로 해 달라고 하고(닿지 않아도 됨) 이 PC에 저장한 것을 지움"""
        token = self._load().get('refresh_token')
        if token:
            try:
                server_client.request('POST', '/api/auth/logout', {'refresh_token': token})
            except Exception:
                pass                                     # 서버에 닿지 않아도 이 PC에서는 로그아웃
        self._clear()
        self.offline = False
        return True, '로그아웃했습니다.'

    # ── 화면용 상태 ──
    def status(self, now: Optional[float] = None) -> Dict:
        st = self._load()
        payload, reason = self._check(now)
        logged_in = self.logged_in()
        needs_login = bool(st.get('relogin')) and not logged_in      # 서버가 토큰을 무효로 함 → 다시 로그인해야 함
        live = bool(payload and payload.get('live') is True)
        exp = int(float(payload['exp'])) if payload else None
        if not logged_in and not payload:
            state = 'off'
            message = ('다시 로그인이 필요합니다' if needs_login else '로그인하면 업비트(실제 주문) 모드를 쓸 수 있습니다')
        elif payload and live:
            state = 'ok'
            message = f"실제 주문 사용 가능 · {_day_text(exp)}까지 확인됨"
            if needs_login:
                message += ' · 다시 로그인이 필요합니다'
            elif self.offline:
                message += ' (지금은 서버에 닿지 않습니다. 그때까지는 그대로 쓸 수 있습니다)'
        elif payload:
            state, message = 'no_live', '지금 계정 권한에는 실제 주문이 없습니다. StraBot은 무료로 바뀌었으니 [다시 확인]을 눌러 새 권한을 받아 주십시오'
        elif reason == 'expired':
            state = 'expired'
            message = ('확인 기간이 끝났습니다. 서버에 닿지 않습니다 — 인터넷에 연결한 뒤 [다시 확인]을 눌러 주십시오' if self.offline
                       else '확인 기간이 끝났습니다. [다시 확인]을 눌러 주십시오')
        else:
            state, message = 'invalid', '저장된 권한표를 확인하지 못했습니다. 다시 로그인해 주십시오'
        return {
            'enforced': enforced(),
            'logged_in': logged_in,
            'needs_login': needs_login,
            'email': st.get('email', '') if (logged_in or payload or needs_login) else '',
            'plan': (payload or {}).get('plan', ''),
            'plan_name': self.plan_name(now) if payload else '',
            'ent': self.ent(now),                                   # {modes, live_bots(None=한도 없음), features}
            'plan_until': st.get('until') if payload else None,
            'live': live,
            'exp': exp,
            'notice': st.get('notice', '') if (logged_in or payload) else '',
            'offline': self.offline,
            'can_trade_live': self.can_trade_live(now),
            'state': state,
            'message': message,
            'last_refresh': int(st.get('last_refresh') or 0) or None,
            'signup_url': server_client.page_url('/signup'),
            'account_url': server_client.page_url('/account'),      # 비밀번호 변경 · 계정 삭제
            'pricing_url': server_client.page_url('/pricing'),
            'feedback_url': server_client.page_url('/feedback'),     # 개선 요청 게시판
        }

    # ── 백그라운드 갱신 (프로그램이 켜져 있는 동안) ──
    def start_auto_refresh(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            self._stop.wait(5)                           # 켜자마자 다른 준비와 겹치지 않게 잠깐 뒤
            while not self._stop.is_set():
                self.maybe_refresh()
                self._stop.wait(AUTO_CHECK_EVERY_SEC)
        self._thread = threading.Thread(target=loop, name='LicenseRefresh', daemon=True)
        self._thread.start()

    def stop_auto_refresh(self):
        self._stop.set()
