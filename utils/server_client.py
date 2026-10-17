"""
StraBot 계정 서버와 주고받기 (로그인 · 권한표 갱신 · 버전 확인)

- 보내는 것은 이메일 · 비밀번호(로그인할 때만) · 설치 ID · 봇 버전뿐입니다.
  업비트 키 · 잔고 · 매매 기록은 보내지 않습니다.
- 시뮬레이션 모드는 서버 없이 전부 동작합니다. 서버에 닿지 않아도 봇이 멈추면 안 되므로
  모든 요청은 짧은 제한 시간을 두고, 실패는 ServerUnreachable 하나로 알립니다.
- 형식은 E:\\MAYAI\\strabot-site\\API_SPEC.md 기준.
"""

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ⚠️ 임시 주소 (PLACEHOLDER): 운영 도메인이 아직 정해지지 않았습니다. 정해지면 이 한 줄만 바꾸면 됩니다.
#    개발할 때는 환경변수 STRABOT_SERVER=http://127.0.0.1:8700 로 덮어씁니다.
DEFAULT_SERVER_URL = "https://strabot.mayai.kr"

TIMEOUT_SEC = 6            # 서버 요청 제한 시간 (연결·응답) — 짧게: 서버가 느려도 화면이 멈추지 않게
USER_AGENT = "StraBot"


class ServerUnreachable(Exception):
    """서버에 닿지 않음 (인터넷 끊김 · 서버 점검 · 이상한 응답). 봇은 저장된 권한표로 계속 동작"""


def server_url() -> str:
    """서버 주소: 환경변수 STRABOT_SERVER 가 있으면 그 주소, 없으면 기본 주소"""
    return (os.environ.get('STRABOT_SERVER') or DEFAULT_SERVER_URL).strip().rstrip('/')


def page_url(path: str) -> str:
    """사람이 보는 페이지 주소 (예: /signup)"""
    return f"{server_url()}/{path.lstrip('/')}"


def request(method: str, path: str, body: Optional[Dict] = None, timeout: float = TIMEOUT_SEC) -> Dict:
    """서버 API 한 번 호출 → 응답 JSON(dict).

    - 성공: {'ok': True, ...} (GET /api/version 은 ok 없이 내용만)
    - 서버가 거절(4xx): {'ok': False, 'error_code': ..., 'message': 한국어 안내}
    - 닿지 않음 · 5xx · JSON 이 아닌 응답: ServerUnreachable
    테스트에서는 이 함수를 바꿔 끼웁니다 (실제 요청 없음).
    """
    import requests

    url = f"{server_url()}/{path.lstrip('/')}"
    try:
        resp = requests.request(method, url, json=body if body is not None else None,
                                headers={'User-Agent': USER_AGENT}, timeout=timeout)
    except Exception as e:
        raise ServerUnreachable(f"서버에 연결하지 못했습니다: {type(e).__name__}") from e
    if resp.status_code >= 500:
        raise ServerUnreachable(f"서버가 응답하지 못했습니다 (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except Exception as e:
        raise ServerUnreachable(f"서버 응답을 읽지 못했습니다 (HTTP {resp.status_code})") from e
    if not isinstance(data, dict):
        raise ServerUnreachable("서버 응답 형식이 다릅니다")
    if resp.status_code >= 400:
        data.setdefault('ok', False)
    return data
