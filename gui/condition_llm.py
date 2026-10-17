"""
자연어 → 조건 검색 규칙(JSON) 변환

사용자가 한국어로 쓴 조건 설명을, 사용자가 등록한 LLM API(Claude 또는 OpenAI)로
봇이 이해하는 '조건 블록' 조합(JSON)으로 바꾼다. LLM은 코드를 만들지 않고 정해진 블록만 고르며,
결과는 서버에서 다시 검증한다 (지표 이름·숫자 범위·규칙 개수).

보내는 것: 사용자가 쓴 조건 문장뿐 (업비트 키·잔고·보유 목록은 보내지 않음).
"""
from typing import Dict, List, Optional, Tuple

import requests

MAX_RULES = 6
MAX_TEXT = 500

DEFAULT_MODELS = {
    'claude': 'claude-sonnet-5',
    'openai': 'gpt-4o-mini',
    'gemini': 'auto',          # 쓸 수 있는 최신 Gemini flash 모델을 자동으로 고름 (구글이 옛 모델을 자주 닫음)
}

# 지원 지표: 인자 {이름: (최소, 최대, 기본값)}, 값 범위, 설명(LLM 안내용)
METRICS: Dict[str, Dict] = {
    'change_rate': {'args': {}, 'range': (-100, 1000), 'desc': '오늘 전일 종가 대비 등락률(%)'},
    'rsi': {'args': {'period': (2, 20, 14)}, 'range': (0, 100), 'desc': '일봉 RSI (0~100)'},
    'sma_gap': {'args': {'days': (2, 190, 20)}, 'range': (-100, 1000), 'desc': '현재가가 n일 이동평균보다 몇 % 위(+)/아래(-)인지 (n은 2~190일, 50일선 · 100일선 · 120일선도 됨)'},
    'ma_cross': {'args': {'short': (2, 15, 5), 'long': (5, 30, 20), 'within': (1, 10, 3)}, 'dir': True, 'bool': True,
                 'desc': '최근 within일 안에 short일 평균선이 long일 평균선을 위(up)/아래(down)로 교차했는지 (op "==", value 1)'},
    'bb_pos': {'args': {'days': (5, 30, 20), 'k': (0.5, 4, 2)}, 'range': (-100, 1000),
               'desc': '볼린저 밴드 밖으로 벗어난 정도(%): 위 선보다 위면 +, 아래 선보다 아래면 -, 밴드 안이면 0'},
    'from_high': {'args': {'months': (1, 24, 12)}, 'range': (-100, 0), 'desc': '최근 n개월 최고가 대비 현재가(%) (0 또는 음수)'},
    'from_low': {'args': {'span': None}, 'range': (0, 100000),
                 'desc': '기간 최저가보다 현재가가 몇 % 위인지. span: d1(24시간) w1(7일) m1(30일) h6(6개월) y1(1년)'},
    'value_mult': {'args': {'days': (3, 30, 20)}, 'range': (0, 1000), 'desc': '최근 24시간 거래대금이 지난 n일 하루 평균의 몇 배인지'},
    'streak': {'args': {}, 'dir': True, 'range': (0, 30), 'desc': '오늘까지 연속 상승(up)/하락(down) 일수'},
    'move_mult': {'args': {}, 'range': (-100, 100), 'desc': '오늘 등락률이 평소 하루 변동폭(최근 한 달 중앙값)의 몇 배인지'},
    'range_pct': {'args': {'days': (3, 30, 14)}, 'range': (0, 1000), 'desc': '최근 n일 최고가와 최저가 차이(%) — 작을수록 횡보'},
    'rebound': {'args': {}, 'range': (0, 1000), 'desc': '오늘 저점 대비 현재가 반등(%)'},
    'held': {'args': {}, 'bool': True, 'desc': '내가 보유한 코인인지 (op "==", value 1=보유, 0=미보유)'},
    'trade_value': {'args': {}, 'range': (0, 1e14), 'desc': '최근 24시간 거래대금(원)'},
}
SPANS = ['d1', 'w1', 'm1', 'h6', 'y1']
OPS = ['<=', '>=', '<', '>', '==', 'between']

SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string', 'description': '조건 이름 (한국어, 20자 이내, 추천·예측 표현 금지)'},
        'match': {'type': 'string', 'enum': ['all', 'any']},
        'rules': {
            'type': 'array', 'maxItems': MAX_RULES,
            'items': {
                'type': 'object',
                'properties': {
                    'metric': {'type': 'string', 'enum': list(METRICS)},
                    'op': {'type': 'string', 'enum': OPS},
                    'value': {'type': 'number'},
                    'value2': {'type': 'number'},
                    'period': {'type': 'integer'}, 'days': {'type': 'integer'}, 'months': {'type': 'integer'},
                    'short': {'type': 'integer'}, 'long': {'type': 'integer'}, 'within': {'type': 'integer'},
                    'k': {'type': 'number'},
                    'span': {'type': 'string', 'enum': SPANS},
                    'dir': {'type': 'string', 'enum': ['up', 'down']},
                },
                'required': ['metric', 'op', 'value'],
            },
        },
        'sort': {
            'type': 'object',
            'properties': {'rule': {'type': 'integer', 'description': '정렬 기준 규칙 번호(0부터)'},
                           'dir': {'type': 'string', 'enum': ['asc', 'desc']}},
        },
        'unsupported': {'type': 'array', 'items': {'type': 'string'},
                        'description': '지원 지표로 표현할 수 없어 뺀 부분 (한국어)'},
    },
    'required': ['title', 'match', 'rules', 'unsupported'],
}


def system_prompt() -> str:
    def arg_text(k, r):
        return k if not r else f"{k}({r[0]}~{r[1]}, 없으면 {r[2]})"
    lines = [f"- {name}: {m['desc']}" + (f" / 인자: {', '.join(arg_text(k, r) for k, r in m['args'].items())}" if m['args'] else '')
             + (" / dir: up|down 필요" if m.get('dir') else '')
             for name, m in METRICS.items()]
    return (
        "너는 암호화폐 조건 검색 도구의 '조건 번역기'다. 사용자가 한국어로 쓴 조건을 아래 지표 블록의 조합(JSON)으로만 바꾼다.\n"
        "규칙:\n"
        "1. 아래 목록에 있는 지표만 쓴다. 목록으로 표현할 수 없는 부분(뉴스, 호가, 다른 거래소, 미래 예측 등)은 규칙에 넣지 말고 unsupported에 한국어로 적는다.\n"
        "2. 문장에 나온 기간 숫자는 반드시 그 인자에 넣는다 (예: '50일선 근처' → sma_gap, days 50 / 'RSI 9일' → period 9). "
        "숫자가 없을 때만 흔히 쓰는 기본값을 쓴다 (예: RSI 14일, 과매도 30, 과매수 70).\n"
        f"3. 규칙은 최대 {MAX_RULES}개. 모두 만족이면 match=all, 하나라도면 any.\n"
        "4. title은 사실만 담은 짧은 이름. '추천', '유망', '오를', '수익 예상' 같은 권유·예측 표현은 쓰지 않는다.\n"
        "5. 퍼센트는 숫자만 (예: 5% → 5). between이면 value(작은 값)와 value2(큰 값).\n"
        "지표 목록:\n" + '\n'.join(lines)
    )


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f and abs(f) != float('inf') else None
    except (TypeError, ValueError):
        return None


def validate_condition(spec: Dict) -> Tuple[Optional[Dict], Optional[str]]:
    """LLM 결과를 검증·정리. (정리된 조건, None) 또는 (None, 한국어 오류)"""
    if not isinstance(spec, dict):
        return None, '조건 형식이 올바르지 않습니다.'
    rules_in = spec.get('rules')
    if not isinstance(rules_in, list) or not rules_in:
        why = ', '.join(str(x) for x in (spec.get('unsupported') or [])[:3])
        return None, '지원하는 지표로 바꿀 수 있는 조건이 없습니다.' + (f' (지원 안 됨: {why})' if why else '')
    if len(rules_in) > MAX_RULES:
        return None, f'규칙은 최대 {MAX_RULES}개까지입니다.'

    rules = []
    for i, r in enumerate(rules_in):
        if not isinstance(r, dict) or r.get('metric') not in METRICS:
            return None, f'{i + 1}번째 규칙의 지표를 알 수 없습니다.'
        name = r['metric']
        m = METRICS[name]
        op = r.get('op')
        if op not in OPS:
            return None, f'{i + 1}번째 규칙의 비교 방법을 알 수 없습니다.'
        out = {'metric': name, 'op': op}
        if m.get('bool'):
            out['op'] = '=='
            out['value'] = 0 if _num(r.get('value')) == 0 else 1
        else:
            value = _num(r.get('value'))
            if value is None:
                return None, f'{i + 1}번째 규칙의 숫자가 없습니다.'
            lo, hi = m['range']
            if op == '==':
                out['op'] = '>='   # 연속값에 '같다'는 거의 안 맞으므로 '이상'으로
            if op == 'between':
                value2 = _num(r.get('value2'))
                if value2 is None:
                    return None, f'{i + 1}번째 규칙의 범위(두 번째 숫자)가 없습니다.'
                value, value2 = sorted((value, value2))
                out['value2'] = min(max(value2, lo), hi)
            out['value'] = min(max(value, lo), hi)
        for arg, spec_range in m['args'].items():
            if arg == 'span':
                out['span'] = r.get('span') if r.get('span') in SPANS else 'w1'
                continue
            a_lo, a_hi, a_def = spec_range
            v = _num(r.get(arg))
            v = a_def if v is None else min(max(v, a_lo), a_hi)
            out[arg] = v if arg == 'k' else int(round(v))
        if name == 'ma_cross' and out['short'] >= out['long']:
            out['short'], out['long'] = min(out['short'], out['long'] - 1), out['long']
            if out['short'] < 2:
                return None, f'{i + 1}번째 규칙의 이동평균 기간이 올바르지 않습니다.'
        if m.get('dir'):
            out['dir'] = 'down' if r.get('dir') == 'down' else 'up'
        rules.append(out)

    title = str(spec.get('title') or '내 조건').strip()[:30] or '내 조건'
    sort_in = spec.get('sort') if isinstance(spec.get('sort'), dict) else {}
    sort_rule = sort_in.get('rule') if isinstance(sort_in.get('rule'), int) and 0 <= sort_in.get('rule') < len(rules) else 0
    sort = {'rule': sort_rule, 'dir': 'asc' if sort_in.get('dir') == 'asc' else 'desc'}
    unsupported = [str(x)[:100] for x in (spec.get('unsupported') or []) if str(x).strip()][:5]
    return {'title': title, 'match': 'any' if spec.get('match') == 'any' else 'all',
            'rules': rules, 'sort': sort, 'unsupported': unsupported}, None


class LLMError(Exception):
    pass


def _call_claude(api_key: str, model: str, text: str) -> Dict:
    resp = _post(
        'https://api.anthropic.com/v1/messages',
        headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
        json={
            'model': model, 'max_tokens': 1024, 'system': system_prompt(),
            'tools': [{'name': 'make_condition', 'description': '조건 검색 규칙 만들기', 'input_schema': SCHEMA}],
            'tool_choice': {'type': 'tool', 'name': 'make_condition'},
            'messages': [{'role': 'user', 'content': text}],
        },
        timeout=60,
    )
    data = _json_or_error(resp)
    for block in data.get('content') or []:
        if block.get('type') == 'tool_use':
            return block.get('input') or {}
    raise LLMError('AI 응답에서 조건을 찾지 못했습니다.')


def _call_openai(api_key: str, model: str, text: str) -> Dict:
    import json
    resp = _post(
        'https://api.openai.com/v1/chat/completions',
        headers={'Authorization': f'Bearer {api_key}', 'content-type': 'application/json'},
        json={
            'model': model,
            'messages': [{'role': 'system', 'content': system_prompt()}, {'role': 'user', 'content': text}],
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'condition', 'schema': SCHEMA}},
        },
        timeout=60,
    )
    data = _json_or_error(resp)
    try:
        return json.loads(data['choices'][0]['message']['content'])
    except Exception:
        raise LLMError('AI 응답을 조건으로 읽지 못했습니다.')


GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'


def _gemini_schema(schema):
    """JSON 스키마 → Gemini responseSchema (타입 대문자, Gemini가 모르는 키는 뺌)"""
    if isinstance(schema, list):
        return [_gemini_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == 'type':
            out['type'] = str(v).upper()
        elif k in ('properties',):
            out[k] = {name: _gemini_schema(sub) for name, sub in v.items()}
        elif k in ('items',):
            out[k] = _gemini_schema(v)
        elif k in ('enum', 'required', 'description', 'maxItems'):
            out[k] = v
    return out


TOTAL_SECONDS = 45          # 한 번 요청에 기다리는 전체 시간 (넘으면 '붐빔' 안내 — 화면이 몇 분씩 돌지 않게)
import threading
_tls = threading.local()           # 요청(스레드)마다 따로


def _left() -> float:
    import time
    d = getattr(_tls, 'deadline', 0.0)
    return d - time.time() if d else 1e9


def _post(url, **kwargs):
    """AI 서버에 보내기. 서버가 잠깐 붐비면(500·502·503) 2초·4초 기다렸다 두 번 더 시도 — 전체 시간(45초) 안에서만"""
    import time
    timeout = kwargs.pop('timeout', 60)
    if _left() < 3:
        raise requests.Timeout('시간 다 씀')
    resp = requests.post(url, timeout=min(timeout, max(3, _left())), **kwargs)
    for wait in (2, 4):
        if resp.status_code not in (500, 502, 503) or _left() < wait + 8:
            break
        time.sleep(wait)
        resp = requests.post(url, timeout=min(timeout, max(3, _left())), **kwargs)
    return resp


GEMINI_MODELS_URL = 'https://generativelanguage.googleapis.com/v1beta/models'
_gemini_model_cache: Dict[str, str] = {}


def _version_key(name: str):
    import re
    m = re.match(r'gemini-(\d+(?:\.\d+)*)-flash$', name)
    return tuple(int(x) for x in m.group(1).split('.')) if m else None


_gemini_list_cache: Dict[str, List[str]] = {}
_gemini_lite: Dict[str, str] = {}


def gemini_models(api_key: str) -> List[str]:
    """이 키로 쓸 수 있는 'gemini-N-flash' 모델들, 최신 순

    (preview·lite·image 같은 특수 모델은 빼고, 글을 만드는(generateContent) 모델만. 한 번 읽으면 기억)
    """
    if api_key in _gemini_list_cache:
        return _gemini_list_cache[api_key]
    resp = requests.get(GEMINI_MODELS_URL, headers={'x-goog-api-key': api_key}, params={'pageSize': 1000}, timeout=30)
    data = _json_or_error(resp)
    found, all_names = [], []
    for m in data.get('models') or []:
        name = str(m.get('name', '')).replace('models/', '')
        if 'generateContent' not in (m.get('supportedGenerationMethods') or []):
            continue
        all_names.append(name)
        ver = _version_key(name)
        if ver:
            found.append((ver, name))
    if not found:
        raise LLMError('이 키로 쓸 수 있는 Gemini 모델을 찾지 못했습니다. 설정의 모델 칸에 모델 이름을 직접 넣어 주십시오.')
    names = [n for _, n in sorted(found, reverse=True)]
    lites = sorted((v, n) for v, n in ((_version_key(n.replace('-lite', '')), n) for n in all_names if n.endswith('-flash-lite')) if v)
    if lites:
        _gemini_lite[api_key] = lites[-1][1]
    _gemini_list_cache[api_key] = names
    return names


def gemini_choices(api_key: str) -> List[Dict]:
    """설정 화면 모델 고르기용: 글을 만드는 Gemini flash · flash-lite · pro (음성 · 그림 · 미리보기 특수 모델 빼고), 최신 순"""
    import re
    resp = requests.get(GEMINI_MODELS_URL, headers={'x-goog-api-key': api_key}, params={'pageSize': 1000}, timeout=30)
    data = _json_or_error(resp)
    out = []
    for m in data.get('models') or []:
        name = str(m.get('name', '')).replace('models/', '')
        if 'generateContent' not in (m.get('supportedGenerationMethods') or []):
            continue
        g = re.match(r'gemini-(\d+(?:\.\d+)*)-(flash-lite|flash|pro)(-preview)?$', name)
        if not g:
            continue
        kind = g.group(2)
        note = {'flash-lite': '가볍고 빠름 · 붐빌 때 좋음', 'flash': '보통', 'pro': '가장 똑똑함 · 무료 한도가 없을 수 있음'}[kind]
        if g.group(3):
            note += ' · 시험판'
        out.append({'id': name, 'kind': kind, 'note': note, 'ver': tuple(int(x) for x in g.group(1).split('.')), 'preview': bool(g.group(3))})
    newest = max((x['ver'][0] for x in out), default=0)
    out = [x for x in out if x['ver'][0] >= newest]          # 옛 세대(예: 2.5)는 목록에 남아 있어도 새 사용자는 못 씀 (2026-10-14 확인: 404)
    out.sort(key=lambda x: (x['ver'], not x['preview']), reverse=True)
    return [{k: v for k, v in x.items() if k != 'ver'} for x in out]


def gemini_model(api_key: str, model: str) -> str:
    """모델 칸이 비었거나 'auto'면: 이 키로 쓸 수 있는 가장 최신 'gemini-N-flash'. 아니면 적힌 모델"""
    model = (model or '').strip()
    if model and model != 'auto':
        return model.replace('models/', '')
    if api_key in _gemini_model_cache:
        return _gemini_model_cache[api_key]
    best = gemini_models(api_key)[0]
    _gemini_model_cache[api_key] = best
    return best


def _gemini_candidates(api_key: str, model: str) -> List[str]:
    """자동이면 최신 모델과 그 바로 아래 버전 (최신이 붐비거나 늦을 때 한 번 바꿔 보낼 곳). 직접 적었으면 그 모델 하나"""
    model = (model or '').strip()
    if model and model != 'auto':
        return [model.replace('models/', '')]
    first = gemini_model(api_key, model)
    rest = [n for n in gemini_models(api_key) if n != first]
    lite = _gemini_lite.get(api_key)                    # 최신이 붐비면 바로 가벼운 flash-lite (2026-10-14: 큰 모델 넷이 모두 25초 넘게 멈출 때도 lite는 2초)
    return [first] + ([lite] if lite and lite != first else []) + rest[:1]


TIMEOUT_MESSAGE = ('AI 서버(Google)가 제때 답하지 않았습니다. 사용자가 몰리는 시간일 수 있습니다. '
                   '1~2분 뒤 다시 시도하거나, 설정의 모델에서 이름에 lite가 붙은 가벼운 모델을 골라 보십시오.')


def _gemini_send(api_key: str, model: str, send):
    """send(모델)로 보내기. 자동 모델이 늦거나(시간 초과) 붐비면(500·502·503) 다음 후보로 한 번 바꿔 보냄
    (같은 모델로 다시 보내지는 않음 — 모델이 다르면 무료 한도도 따로). 돌려주는 값: (쓴 모델, 응답)"""
    import time
    candidates = _gemini_candidates(api_key, model)
    _tls.deadline = time.time() + TOTAL_SECONDS          # 모든 후보를 합쳐 45초 안에
    try:
        for i, name in enumerate(candidates):
            last = i == len(candidates) - 1 or _left() < 8          # 남은 시간이 적으면 이번이 마지막
            try:
                resp = send(name)
            except requests.Timeout:
                if last:
                    raise LLMError(TIMEOUT_MESSAGE)
                continue
            if resp.status_code in (500, 502, 503) and not last:
                continue
            if i > 0:
                _gemini_model_cache[api_key] = name        # 최신 모델이 안 될 때는 한동안 이 모델로
            return name, resp
        raise LLMError(TIMEOUT_MESSAGE)
    finally:
        _tls.deadline = 0.0


def _call_gemini(api_key: str, model: str, text: str) -> Dict:
    import json
    _used, resp = _gemini_send(api_key, model, lambda name: _post(
        GEMINI_URL.format(model=name),
        headers={'x-goog-api-key': api_key, 'content-type': 'application/json'},
        json={
            'systemInstruction': {'parts': [{'text': system_prompt()}]},
            'contents': [{'role': 'user', 'parts': [{'text': text}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': _gemini_schema(SCHEMA)},
        },
        timeout=20,
    ))
    data = _json_or_error(resp)
    try:
        return json.loads(data['candidates'][0]['content']['parts'][0]['text'])
    except Exception:
        raise LLMError('AI 응답을 조건으로 읽지 못했습니다.')


def _json_or_error(resp) -> Dict:
    try:
        data = resp.json()
    except Exception:
        data = {}
    bad_key = resp.status_code in (401, 403) or (
        resp.status_code == 400 and 'API_KEY_INVALID' in str(data))      # Gemini는 잘못된 키를 400으로 알려줌
    if bad_key:
        raise LLMError('AI API 키가 올바르지 않습니다. 설정에서 키를 확인해 주십시오.')
    err = data.get('error') if isinstance(data, dict) else None
    detail = str(err.get('message') if isinstance(err, dict) else '')[:200]
    if resp.status_code == 429:
        raise LLMError(rate_limit_message(err, detail))
    if resp.status_code in (500, 502, 503):
        raise LLMError('AI 서버에 지금 사용자가 몰려 잠시 바쁩니다. 1~2분 뒤 다시 시도해 주십시오.')
    if resp.status_code == 404 and ('model' in detail.lower()):
        raise LLMError(f'이 AI 모델은 더 이상 쓸 수 없습니다. 설정의 모델 칸을 비우면(자동) 쓸 수 있는 최신 모델을 고릅니다. ({detail})')
    if resp.status_code >= 400:
        err = data.get('error') if isinstance(data, dict) else None
        msg = err.get('message') if isinstance(err, dict) else None
        raise LLMError(f'AI 요청 실패 ({resp.status_code}){": " + str(msg)[:200] if msg else ""}')
    return data


RESET_KST = '한국 시간 오후 4시(겨울엔 5시)쯤'      # 구글 하루 한도가 다시 채워지는 때 = 미국 서부 자정


def rate_limit_message(err, detail: str = '') -> str:
    """429(요청 한도) 오류를 알아듣기 쉬운 말로. Gemini는 무료 한도·분당/하루를 구분해 알려 줌
    (Gemini 무료 한도 오류에도 'check your plan and billing' 문장이 들어 있어 결제 문제로 오해하기 쉬움)"""
    import json
    import math
    import re
    blob = json.dumps(err, ensure_ascii=False) if isinstance(err, dict) else str(detail)
    low = blob.lower()
    if 'spending cap' in low or 'spend cap' in low:
        return ('AI 계정의 월 지출 한도를 넘었습니다. Google AI Studio(https://ai.studio/spend)에서 지출 한도를 올리거나, '
                '다른 프로젝트의 키를 넣어 주십시오.')
    if isinstance(err, dict) and err.get('status') == 'RESOURCE_EXHAUSTED':          # Gemini
        who = '무료 요청 수' if ('free_tier' in low or 'freetier' in low) else '요청 수'
        if 'perday' in low or 'per_day' in low:
            return f'AI {who}의 하루 한도를 다 썼습니다. 결제할 필요는 없고, {RESET_KST} 다시 채워집니다.'
        if 'perminute' in low or 'per_minute' in low:
            m = re.search(r'retryDelay"?\s*:\s*"(\d+(?:\.\d+)?)s"', blob)
            wait = f'{math.ceil(float(m.group(1)))}초' if m else '1분'
            return f'AI {who}의 분당 한도에 걸렸습니다. 결제할 필요는 없고, {wait}쯤 뒤 다시 해 보십시오.'
        return (f'AI {who}를 다 썼습니다. 결제할 필요는 없습니다. 분당 한도면 1분쯤 뒤, 하루 한도면 {RESET_KST} 다시 채워집니다.')
    if 'insufficient_quota' in low or 'billing' in low or 'credit' in low or 'insufficient' in low or 'quota' in low:
        return f'AI 계정의 사용량·결제 한도에 걸렸습니다. 제공사 사이트에서 결제·한도를 확인해 주십시오. ({detail})'
    return f'AI 요청 한도에 걸렸습니다. 잠시 뒤 다시 시도해 주십시오.{f" ({detail})" if detail else ""}'


def generate_condition(provider: str, api_key: str, model: str, text: str) -> Dict:
    """자연어 → 검증된 조건. 실패하면 LLMError (한국어 메시지)"""
    text = (text or '').strip()[:MAX_TEXT]
    if not text:
        raise LLMError('만들 조건을 문장으로 적어 주십시오.')
    if not api_key:
        raise LLMError('설정 탭의 AI 조건 도우미에 API 키를 먼저 넣어 주십시오.')
    provider = provider if provider in DEFAULT_MODELS else 'claude'
    model = (model or '').strip() or DEFAULT_MODELS[provider]
    try:
        call = {'openai': _call_openai, 'gemini': _call_gemini}.get(provider, _call_claude)
        raw = call(api_key, model, text)
    except requests.Timeout:
        raise LLMError(TIMEOUT_MESSAGE)
    except requests.RequestException as e:
        raise LLMError(f'AI 서버에 연결하지 못했습니다: {e}')
    _fill_days_from_text(raw, text)
    spec, error = validate_condition(raw)
    if error:
        raise LLMError(error)
    spec['source_text'] = text
    return spec


def _fill_days_from_text(raw, text: str) -> None:
    """AI가 기간을 빼먹으면 기본값(20일)이 되어 '50일선'이 20일선으로 바뀜 → 문장의 'N일선 · N일 이동평균 · N일 평균'으로 채움
    (규칙에 이동평균 괴리가 하나뿐이고 문장에 그런 숫자가 하나뿐일 때만)"""
    import re
    if not isinstance(raw, dict) or not isinstance(raw.get('rules'), list):
        return
    nums = {int(x) for x in re.findall(r'(\d{1,3})\s*(?:일\s*)?(?:선|이평|이동\s*평균|이동평균|평균)', text or '')}
    gaps = [r for r in raw['rules'] if isinstance(r, dict) and r.get('metric') == 'sma_gap']
    if len(nums) == 1 and len(gaps) == 1 and _num(gaps[0].get('days')) in (None, METRICS['sma_gap']['args']['days'][2]):
        n = nums.pop()
        if n != METRICS['sma_gap']['args']['days'][2]:
            gaps[0]['days'] = n


def check_key(provider: str, api_key: str, model: str) -> str:
    """가벼운 확인: 모델 정보만 조회 (글을 만들지 않아 무료 요청 한도를 쓰지 않음). 성공하면 쓸 모델 이름
    프로그램을 켤 때의 자동 연결 확인용. 실제로 글이 만들어지는지는 test_connection([AI 연결 테스트])"""
    if not api_key:
        raise LLMError('API 키를 입력해 주십시오.')
    provider = provider if provider in DEFAULT_MODELS else 'claude'
    model = (model or '').strip() or DEFAULT_MODELS[provider]
    try:
        if provider == 'gemini':
            if model == 'auto':
                return gemini_model(api_key, model)                     # 모델 목록 조회 (키 확인 겸)
            model = model.replace('models/', '')
            resp = requests.get(f'{GEMINI_MODELS_URL}/{model}', headers={'x-goog-api-key': api_key}, timeout=30)
        elif provider == 'openai':
            resp = requests.get(f'https://api.openai.com/v1/models/{model}', headers={'Authorization': f'Bearer {api_key}'}, timeout=30)
        else:
            resp = requests.get(f'https://api.anthropic.com/v1/models/{model}',
                                headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01'}, timeout=30)
    except requests.Timeout:
        raise LLMError(TIMEOUT_MESSAGE)
    except requests.RequestException as e:
        raise LLMError(f'AI 서버에 연결하지 못했습니다: {e}')
    if resp.status_code == 404:
        raise LLMError(f'이 AI 모델은 쓸 수 없습니다. 설정의 모델 칸을 확인해 주십시오. ({model})')
    _json_or_error(resp)
    return model


def test_connection(provider: str, api_key: str, model: str) -> str:
    """키·모델 확인용 아주 짧은 요청 한 번 ([AI 연결 테스트]). 성공하면 사용한 모델 이름
    한도를 아끼려고 서버가 바빠도(503) 다시 보내지 않음"""
    if not api_key:
        raise LLMError('API 키를 입력해 주십시오.')
    provider = provider if provider in DEFAULT_MODELS else 'claude'
    model = (model or '').strip() or DEFAULT_MODELS[provider]
    try:
        if provider == 'gemini':
            model, resp = _gemini_send(api_key, model, lambda name: requests.post(
                GEMINI_URL.format(model=name), headers={'x-goog-api-key': api_key},
                json={'contents': [{'role': 'user', 'parts': [{'text': 'ping'}]}],
                      'generationConfig': {'maxOutputTokens': 5}}, timeout=60))
        elif provider == 'openai':
            resp = requests.post('https://api.openai.com/v1/chat/completions',
                                 headers={'Authorization': f'Bearer {api_key}'},
                                 json={'model': model, 'max_tokens': 5, 'messages': [{'role': 'user', 'content': 'ping'}]},
                                 timeout=30)
        else:
            resp = requests.post('https://api.anthropic.com/v1/messages',
                                 headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
                                 json={'model': model, 'max_tokens': 5, 'messages': [{'role': 'user', 'content': 'ping'}]},
                                 timeout=30)
    except requests.Timeout:
        raise LLMError(TIMEOUT_MESSAGE)
    except requests.RequestException as e:
        raise LLMError(f'AI 서버에 연결하지 못했습니다: {e}')
    _json_or_error(resp)
    return model


def sanitize_saved(conditions: List) -> List[Dict]:
    """저장된 내 조건 목록을 다시 검증 (파일을 직접 고쳤을 때 대비)"""
    out = []
    for c in conditions if isinstance(conditions, list) else []:
        spec, error = validate_condition(c)
        if error:
            continue
        spec['id'] = str(c.get('id') or '')[:40] or None
        spec['role'] = c.get('role') if c.get('role') in ('buy', 'sell', 'both', '') else ''
        spec['source_text'] = str(c.get('source_text') or '')[:MAX_TEXT]
        if spec['id']:
            out.append(spec)
    return out
