"""
프로그램 업데이트 확인 · 설치 파일 받기

- 최신 버전 정보는 계정 서버의 GET /api/version (strabot-site/API_SPEC.md):
    {version: "1.1.0", url, sha256, size, notes, released}   (설치 파일이 아직 없으면 url: null)
  auth: true 이면 설치 파일은 계정 서버가 회원에게만 내려줌 → 로그인 표(refresh_token)와 설치 ID 를 머리말로 붙여 받음
- 버전은 숫자끼리 비교합니다 (1.10.0 > 1.9.0). 지금 버전은 gui/__init__.py 의 __version__.
- 받은 설치 파일은 sha256 이 서버가 알려 준 값과 같을 때만 실행합니다 (다르면 지우고 멈춤).
"""

import hashlib
import logging
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

from utils import server_client

logger = logging.getLogger(__name__)

MAX_INSTALLER_BYTES = 800 * 1024 * 1024     # 이보다 크면 받지 않음 (지금 설치 파일은 약 270MB)
INSTALLER_NAME = 'StraBot_Setup.exe'


class UpdateError(Exception):
    """화면에 그대로 보여 줄 수 있는 업데이트 오류"""


def parse_version(text) -> Tuple[int, ...]:
    """'1.10.0' → (1, 10, 0). 앞의 v, 뒤의 꼬리표(-beta 등)는 무시. 읽을 수 없으면 (0,)"""
    m = re.match(r'\s*v?(\d+(?:\.\d+)*)', str(text or ''))
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split('.'))


def is_newer(latest, current) -> bool:
    """latest 가 current 보다 높은 버전인지 (자릿수가 달라도: 1.1 == 1.1.0)"""
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def fetch_latest() -> Dict:
    """서버의 최신 버전 정보. 닿지 않으면 server_client.ServerUnreachable"""
    data = server_client.request('GET', '/api/version')
    if not data.get('version'):
        raise UpdateError('서버의 버전 정보를 읽지 못했습니다.')
    return data


def installer_url(info: Dict) -> Optional[str]:
    """설치 파일 주소 (서버 주소 기준 상대 주소도 받음). https 만 — 개발용 127.0.0.1·localhost 는 http 허용"""
    url = info.get('url')
    if not url:
        return None
    full = urljoin(server_client.server_url() + '/', str(url))
    p = urlparse(full)
    if p.scheme == 'https' or (p.scheme == 'http' and p.hostname in ('127.0.0.1', 'localhost')):
        return full
    return None


LOGIN_NEEDED = '업데이트를 받으려면 설정의 계정 화면에서 로그인해 주십시오.'


def auth_headers(url: str, refresh_token: str, install_id: str) -> Dict[str, str]:
    """회원 전용 설치 파일을 받을 때 붙이는 머리말. 계정 서버 주소일 때만 붙임 (다른 곳에 로그인 표를 보내지 않음)"""
    if not refresh_token or not install_id:
        raise UpdateError(LOGIN_NEEDED)
    if urlparse(url).netloc != urlparse(server_client.server_url()).netloc:
        raise UpdateError('설치 파일 주소가 계정 서버가 아니어서 받지 않았습니다.')
    return {'Authorization': f'Bearer {refresh_token}', 'X-Install-Id': install_id}


def download_verified(url: str, sha256: str, dest_dir: Optional[Path] = None, timeout: float = 60,
                      headers: Optional[Dict[str, str]] = None) -> Path:
    """설치 파일을 임시 폴더(%TEMP%\\StraBot_Update)에 받고 sha256 을 확인. 맞으면 파일 경로, 아니면 UpdateError"""
    import requests

    expected = str(sha256 or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise UpdateError('서버가 설치 파일 확인값(sha256)을 주지 않아 받지 않았습니다.')
    dest_dir = Path(dest_dir) if dest_dir else Path(tempfile.gettempdir()) / 'StraBot_Update'
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / INSTALLER_NAME
    digest = hashlib.sha256()
    total = 0
    try:
        with requests.get(url, stream=True, timeout=(server_client.TIMEOUT_SEC, timeout),
                          headers={'User-Agent': server_client.USER_AGENT, **(headers or {})}) as resp:
            if resp.status_code == 401:
                raise UpdateError(LOGIN_NEEDED)
            if resp.status_code != 200:
                raise UpdateError(f'설치 파일을 받지 못했습니다 (HTTP {resp.status_code}).')
            with open(path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_INSTALLER_BYTES:
                        raise UpdateError('설치 파일이 너무 커서 받기를 멈췄습니다.')
                    digest.update(chunk)
                    f.write(chunk)
    except UpdateError:
        path.unlink(missing_ok=True)
        raise
    except Exception as e:
        path.unlink(missing_ok=True)
        raise UpdateError(f'설치 파일을 받다가 끊겼습니다: {type(e).__name__}') from e
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        logger.error(f"업데이트 파일 확인값이 다릅니다: 받은 {digest.hexdigest()} / 서버 {expected}")
        raise UpdateError('받은 설치 파일이 서버가 알려 준 것과 달라서 지웠습니다. 실행하지 않았습니다.')
    logger.info(f"업데이트 파일 확인 완료: {path} ({total:,} bytes)")
    return path
