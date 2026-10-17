"""
pyupbit 호환성 패치 (pyupbit import 전에 apply()를 한 번 호출)

1) 업비트 거절 사유 노출
   pyupbit의 error_handler는 모르는 에러 이름(예: no_authorization_ip, expired_access_key)이면
   예외를 내지 않고 응답을 그대로 넘기고, 이어서 Remaining-Req 헤더가 없어
   "요청 수 제한 파싱에 실패했습니다"라는 엉뚱한 에러로 바뀐다.
   → 실패 응답이면 업비트가 보낸 에러 이름/메시지로 바로 예외를 낸다.

2) Remaining-Req 헤더에 min 값이 없는 형식(예: "group=default; sec=29")도 파싱한다.
"""

import re
from typing import Any, Dict

import requests

_PATTERN = re.compile(r"group=([a-z\-]+);(?:\s*min=([0-9]+);)?\s*sec=([0-9]+)")

# 업비트 에러 이름 → 한글 안내
_UPBIT_ERRORS = {
    'no_authorization_ip': '허용되지 않은 IP입니다. 업비트 Open API 관리에서 이 PC의 공인 IP를 등록하십시오.',
    'invalid_access_key': 'Access Key가 올바르지 않습니다. (복사 누락·공백 확인, 또는 삭제된 키)',
    'jwt_verification': 'Secret Key가 올바르지 않습니다. (Access Key와 짝이 맞는지 확인)',
    'expired_access_key': '만료된 API 키입니다. 업비트에서 새로 발급받으십시오.',
    'out_of_scope': '이 API 키에 필요한 권한이 없습니다. (자산조회/주문 권한 확인)',
    'nonce_used': '요청 번호(nonce)가 중복되었습니다. 잠시 후 다시 시도하십시오.',
    'too_many_requests': '요청이 너무 많습니다. 잠시 후 다시 시도하십시오.',
}


class UpbitRequestError(Exception):
    """업비트가 요청을 거절했을 때 (실제 사유 포함)"""

    def __init__(self, status: int, name: str, message: str):
        self.status, self.name, self.upbit_message = status, name, message
        guide = _UPBIT_ERRORS.get(name, message or '알 수 없는 오류')
        super().__init__(f"업비트 거절 [{status} {name or '-'}]: {guide}")


def _raise_for_upbit(resp: requests.Response) -> requests.Response:
    if resp.ok:
        return resp
    name, message = '', ''
    try:
        error = resp.json().get('error', {}) or {}
        name, message = error.get('name', ''), error.get('message', '')
    except Exception:
        message = resp.text[:200]
        if resp.status_code == 429:
            name = 'too_many_requests'
    raise UpbitRequestError(resp.status_code, name, message)


def _call_get(url: str, **kwargs: Any) -> requests.Response:
    return _raise_for_upbit(requests.get(url, **kwargs))


def _call_post(url: str, **kwargs: Any) -> requests.Response:
    return _raise_for_upbit(requests.post(url, **kwargs))


def _call_delete(url: str, **kwargs: Any) -> requests.Response:
    return _raise_for_upbit(requests.delete(url, **kwargs))


def _parse(remaining_req: str) -> Dict[str, Any]:
    from pyupbit.errors import RemainingReqParsingError

    matched = _PATTERN.search(remaining_req or "")
    if matched is None:
        raise RemainingReqParsingError
    return {
        "group": matched.group(1),
        "min": int(matched.group(2)) if matched.group(2) else None,
        "sec": int(matched.group(3)),
    }


def apply():
    import pyupbit.request_api as request_api
    request_api._parse = _parse
    request_api._call_get = _call_get
    request_api._call_post = _call_post
    request_api._call_delete = _call_delete
