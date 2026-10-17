"""내 조건(자연어 → 조건 규칙) 검증·LLM 응답 처리 테스트 (실제 AI 호출 없음)"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest

from gui import condition_llm as cl


def test_valid_condition_is_cleaned():
    spec, err = cl.validate_condition({
        'title': 'RSI 과매도 + 거래대금 급증', 'match': 'all',
        'rules': [{'metric': 'rsi', 'op': '<=', 'value': 30},
                  {'metric': 'value_mult', 'op': '>=', 'value': 2, 'days': 20}],
        'unsupported': [],
    })
    assert err is None
    assert spec['rules'][0]['period'] == 14          # 기본값 채움
    assert spec['rules'][1]['days'] == 20
    assert spec['sort'] == {'rule': 0, 'dir': 'desc'}


def test_unknown_metric_rejected():
    spec, err = cl.validate_condition({'title': 'x', 'match': 'all', 'rules': [{'metric': 'news', 'op': '>=', 'value': 1}]})
    assert spec is None and '지표' in err


def test_empty_rules_reports_unsupported():
    spec, err = cl.validate_condition({'title': 'x', 'match': 'all', 'rules': [], 'unsupported': ['뉴스 호재']})
    assert spec is None and '뉴스 호재' in err


def test_values_and_args_clamped():
    spec, err = cl.validate_condition({'title': 'x', 'match': 'any', 'rules': [
        {'metric': 'rsi', 'op': '>=', 'value': 500, 'period': 999},
        {'metric': 'from_high', 'op': 'between', 'value': -10, 'value2': -80, 'months': 99},
        {'metric': 'ma_cross', 'op': '>=', 'value': 7, 'short': 20, 'long': 10, 'dir': 'sideways'},
        {'metric': 'from_low', 'op': '<=', 'value': 5, 'span': 'forever'},
    ]})
    assert err is None
    r = spec['rules']
    assert r[0]['value'] == 100 and r[0]['period'] == 20
    assert (r[1]['value'], r[1]['value2'], r[1]['months']) == (-80, -10, 24)
    assert r[2]['op'] == '==' and r[2]['value'] == 1 and r[2]['short'] < r[2]['long'] and r[2]['dir'] == 'up'
    assert r[3]['span'] == 'w1'
    assert spec['match'] == 'any'


def test_too_many_rules():
    rules = [{'metric': 'rsi', 'op': '<=', 'value': 30}] * (cl.MAX_RULES + 1)
    spec, err = cl.validate_condition({'title': 'x', 'match': 'all', 'rules': rules})
    assert spec is None


class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


def test_claude_tool_use_parsed(monkeypatch):
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return FakeResp(200, {'content': [{'type': 'tool_use', 'input': {
            'title': 'RSI 낮음', 'match': 'all', 'rules': [{'metric': 'rsi', 'op': '<=', 'value': 30}], 'unsupported': []}}]})

    monkeypatch.setattr(cl.requests, 'post', fake_post)
    spec = cl.generate_condition('claude', 'sk-test', '', 'RSI 30 이하')
    assert spec['rules'][0]['metric'] == 'rsi'
    assert sent['body']['model'] == cl.DEFAULT_MODELS['claude']
    assert sent['body']['messages'][0]['content'] == 'RSI 30 이하'     # 보내는 것은 문장뿐
    assert 'upbit' not in str(sent['body']).lower()


def test_openai_json_parsed(monkeypatch):
    import json as _json
    content = _json.dumps({'title': '연속 하락', 'match': 'all',
                           'rules': [{'metric': 'streak', 'op': '>=', 'value': 4, 'dir': 'down'}], 'unsupported': []})
    monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: FakeResp(200, {'choices': [{'message': {'content': content}}]}))
    spec = cl.generate_condition('openai', 'sk-test', 'gpt-x', '4일 연속 하락')
    assert spec['rules'][0]['dir'] == 'down'


def test_auth_error_message(monkeypatch):
    monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: FakeResp(401, {'error': {'message': 'invalid x-api-key'}}))
    with pytest.raises(cl.LLMError, match='키'):
        cl.generate_condition('claude', 'bad', '', 'RSI 30 이하')


def test_missing_key():
    with pytest.raises(cl.LLMError):
        cl.generate_condition('claude', '', '', 'RSI 30 이하')


def test_sanitize_saved_drops_broken():
    good = {'id': 'my_1', 'title': 'a', 'match': 'all', 'rules': [{'metric': 'rsi', 'op': '<=', 'value': 30}], 'role': 'buy'}
    bad = {'id': 'my_2', 'title': 'b', 'match': 'all', 'rules': [{'metric': 'nope', 'op': '<=', 'value': 1}]}
    out = cl.sanitize_saved([good, bad, 'junk'])
    assert [c['id'] for c in out] == ['my_1'] and out[0]['role'] == 'buy'


def test_gemini_json_parsed(monkeypatch):
    import json as _json
    sent = {}
    content = _json.dumps({'title': 'RSI 낮음', 'match': 'all',
                           'rules': [{'metric': 'rsi', 'op': '<=', 'value': 30}], 'unsupported': []})

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, body=json)
        return FakeResp(200, {'candidates': [{'content': {'parts': [{'text': content}]}}]})

    models = {'models': [
        {'name': 'models/gemini-2.5-flash', 'supportedGenerationMethods': ['generateContent']},
        {'name': 'models/gemini-3.8-flash', 'supportedGenerationMethods': ['generateContent']},
        {'name': 'models/gemini-4.0-flash-lite', 'supportedGenerationMethods': ['generateContent']},
        {'name': 'models/gemini-4.1-flash-image', 'supportedGenerationMethods': ['generateContent']},
        {'name': 'models/gemini-3.10-flash', 'supportedGenerationMethods': ['embedContent']},
    ]}
    monkeypatch.setattr(cl.requests, 'get', lambda *a, **k: FakeResp(200, models))
    monkeypatch.setattr(cl.requests, 'post', fake_post)
    cl._gemini_model_cache.clear()
    cl._gemini_list_cache.clear()
    spec = cl.generate_condition('gemini', 'g-key', '', 'RSI 30 이하')
    assert spec['rules'][0]['metric'] == 'rsi'
    # 모델 칸이 비면 쓸 수 있는 최신 'gemini-N-flash' (lite·image·글 못 만드는 모델 제외)
    assert sent['url'].endswith('/models/gemini-3.8-flash:generateContent')
    assert sent['headers']['x-goog-api-key'] == 'g-key'
    assert sent['body']['contents'][0]['parts'][0]['text'] == 'RSI 30 이하'   # 보내는 것은 문장뿐
    schema = sent['body']['generationConfig']['responseSchema']
    assert schema['type'] == 'OBJECT' and schema['properties']['rules']['items']['type'] == 'OBJECT'


def test_gemini_bad_key_message(monkeypatch):
    monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: FakeResp(400, {'error': {
        'message': 'API key not valid', 'details': [{'reason': 'API_KEY_INVALID'}]}}))
    with pytest.raises(cl.LLMError, match='키'):
        cl.generate_condition('gemini', 'bad', '', 'RSI 30 이하')


def test_gemini_explicit_model_and_retired_model_message(monkeypatch):
    sent = {}
    monkeypatch.setattr(cl.requests, 'post', lambda url, **k: (sent.update(url=url), FakeResp(404, {'error': {
        'message': 'This model models/gemini-2.5-flash is no longer available to new users.'}}))[1])
    with pytest.raises(cl.LLMError, match='모델'):
        cl.generate_condition('gemini', 'g-key', 'gemini-2.5-flash', 'RSI 30 이하')
    assert sent['url'].endswith('/models/gemini-2.5-flash:generateContent')   # 직접 넣은 모델은 그대로



# ── 무료 한도 아끼기: 한도 문구 · 가벼운 확인 · 테스트 재시도 없음 · 확인 결과 기억 ──
def gemini_429(quota_id, delay=None):
    details = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                'violations': [{'quotaMetric': 'generativelanguage.googleapis.com/generate_content_free_tier_requests',
                                'quotaId': quota_id}]}]
    if delay:
        details.append({'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': delay})
    return {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'details': details,
                      'message': 'You exceeded your current quota, please check your plan and billing details.'}}


def test_gemini_free_tier_limit_messages(monkeypatch):
    cases = [(gemini_429('GenerateRequestsPerDayPerProjectPerModel-FreeTier'), '하루 한도'),
             (gemini_429('GenerateRequestsPerMinutePerProjectPerModel-FreeTier', '37s'), '38초|37초')]
    for body, want in cases:
        monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: FakeResp(429, body))
        with pytest.raises(cl.LLMError, match=want) as e:
            cl.generate_condition('gemini', 'g-key', 'gemini-3.8-flash', 'RSI 30 이하')
        assert '무료' in str(e.value) and '결제할 필요는 없' in str(e.value)       # 결제 문제로 오해하지 않게
    # OpenAI 결제 한도는 그대로 결제 안내
    monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: FakeResp(429, {'error': {
        'type': 'insufficient_quota', 'message': 'You exceeded your current quota, please check your plan and billing details.'}}))
    with pytest.raises(cl.LLMError, match='결제'):
        cl.generate_condition('openai', 'sk', 'gpt-x', 'RSI 30 이하')


def test_check_key_only_reads_model_info(monkeypatch):
    """자동 연결 확인은 모델 정보 조회(GET)만 — 글 생성(POST) 요청은 보내지 않음"""
    gets = []
    monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: pytest.fail('글 생성 요청을 보냄'))
    monkeypatch.setattr(cl.requests, 'get', lambda url, **k: (gets.append(url), FakeResp(200, {'models': [
        {'name': 'models/gemini-3.8-flash', 'supportedGenerationMethods': ['generateContent']}]}))[1])
    cl._gemini_model_cache.clear()
    cl._gemini_list_cache.clear()
    assert cl.check_key('gemini', 'g-key', '') == 'gemini-3.8-flash'
    assert cl.check_key('gemini', 'g-key', 'gemini-2.5-flash') == 'gemini-2.5-flash'
    assert cl.check_key('openai', 'sk', 'gpt-x') == 'gpt-x'
    assert gets[-2].endswith('/models/gemini-2.5-flash') and gets[-1].endswith('/v1/models/gpt-x')
    monkeypatch.setattr(cl.requests, 'get', lambda *a, **k: FakeResp(404, {'error': {'message': 'not found'}}))
    with pytest.raises(cl.LLMError, match='모델'):
        cl.check_key('claude', 'k', 'claude-x')


def test_connection_test_does_not_retry(monkeypatch):
    posts = []
    monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: (posts.append(1), FakeResp(503, {}))[1])
    with pytest.raises(cl.LLMError, match='바쁩'):
        cl.test_connection('gemini', 'g-key', 'gemini-3.8-flash')
    assert len(posts) == 1


def test_llm_status_remembered_across_restarts(tmp_path, monkeypatch):
    """연결됨 결과는 파일에 기억 → 다시 켜도(새 대시보드) 하루 동안은 확인 요청 없음. [연결 테스트]는 실제 요청"""
    import types
    from gui.web_dashboard import WebDashboard
    calls = []
    monkeypatch.setattr(cl, 'check_key', lambda *a: (calls.append('check'), 'gemini-3.8-flash')[1])
    monkeypatch.setattr(cl, 'test_connection', lambda *a: (calls.append('ping'), 'gemini-3.8-flash')[1])

    def dash():
        return types.SimpleNamespace(config={'llm_provider': 'gemini', 'llm_api_key': 'g-key', 'llm_model': ''},
                                     user_data_dir=tmp_path, LLM_STATUS_TTL=86400, _save_json=WebDashboard._save_json)
    a = dash()
    assert WebDashboard._check_llm(a)['state'] == 'ok'
    assert WebDashboard._check_llm(a)['state'] == 'ok'
    assert calls == ['check']                               # 같은 실행에서는 한 번
    b = dash()                                              # 프로그램을 다시 켬
    assert WebDashboard._check_llm(b)['state'] == 'ok' and calls == ['check']
    WebDashboard._check_llm(b, force=True)
    assert calls == ['check', 'ping']
    b.config['llm_api_key'] = 'other-key'                   # 키가 바뀌면 다시 확인
    WebDashboard._check_llm(b)
    assert calls[-1] == 'check' and len(calls) == 3


# ── 최신 Gemini 모델이 늦거나 붐빌 때: 자동 모델이면 바로 아래 버전으로 한 번만 ──
TWO_FLASH = {'models': [
    {'name': 'models/gemini-3.8-flash', 'supportedGenerationMethods': ['generateContent']},
    {'name': 'models/gemini-3.6-flash', 'supportedGenerationMethods': ['generateContent']},
    {'name': 'models/gemini-3.1-flash', 'supportedGenerationMethods': ['generateContent']},
]}


def _fresh(monkeypatch):
    cl._gemini_model_cache.clear()
    cl._gemini_list_cache.clear()
    cl._gemini_lite.clear()
    monkeypatch.setattr(cl.requests, 'get', lambda *a, **k: FakeResp(200, TWO_FLASH))


def test_connection_falls_back_once_when_newest_times_out(monkeypatch):
    _fresh(monkeypatch)
    urls = []

    def post(url, **k):
        urls.append(url)
        if '3.8' in url:
            raise cl.requests.Timeout('read timed out')
        return FakeResp(200, {'candidates': [{'content': {'parts': [{'text': 'pong'}]}}]})
    monkeypatch.setattr(cl.requests, 'post', post)
    assert cl.test_connection('gemini', 'g-key', '') == 'gemini-3.6-flash'
    assert [u.split('/models/')[1].split(':')[0] for u in urls] == ['gemini-3.8-flash', 'gemini-3.6-flash']
    assert cl.gemini_model('g-key', '') == 'gemini-3.6-flash'          # 한동안 되는 모델을 씀


def test_both_candidates_time_out_gives_plain_message(monkeypatch):
    _fresh(monkeypatch)
    posts = []
    monkeypatch.setattr(cl.requests, 'post', lambda *a, **k: (posts.append(1), (_ for _ in ()).throw(cl.requests.Timeout('x')))[1])
    with pytest.raises(cl.LLMError, match='제때 답하지') as e:
        cl.test_connection('gemini', 'g-key', '')
    assert 'HTTPSConnectionPool' not in str(e.value) and 2 <= len(posts) <= 3     # 최대 세 모델, 한 번씩 (전체 45초 안)


def test_generate_falls_back_when_newest_is_busy(monkeypatch):
    import json as _json
    _fresh(monkeypatch)
    monkeypatch.setattr('time.sleep', lambda s: None)
    content = _json.dumps({'title': 'RSI 낮음', 'match': 'all',
                           'rules': [{'metric': 'rsi', 'op': '<=', 'value': 30}], 'unsupported': []})
    urls = []

    def post(url, **k):
        urls.append(url)
        if '3.8' in url:
            return FakeResp(503, {'error': {'message': 'high demand'}})
        return FakeResp(200, {'candidates': [{'content': {'parts': [{'text': content}]}}]})
    monkeypatch.setattr(cl.requests, 'post', post)
    spec = cl.generate_condition('gemini', 'g-key', '', 'RSI 30 이하')
    assert spec['rules'][0]['metric'] == 'rsi'
    assert urls[-1].endswith('/models/gemini-3.6-flash:generateContent')
    assert not any('3.1' in u for u in urls)                                       # 세 번째 모델까지는 안 감


def test_explicit_model_never_falls_back(monkeypatch):
    _fresh(monkeypatch)
    urls = []
    monkeypatch.setattr(cl.requests, 'post', lambda url, **k: (urls.append(url), (_ for _ in ()).throw(cl.requests.Timeout('x')))[1])
    with pytest.raises(cl.LLMError, match='제때 답하지'):
        cl.test_connection('gemini', 'g-key', 'gemini-3.8-flash')
    assert len(urls) == 1


def test_sma_gap_allows_long_days():
    """'50일선 근처' 같은 문장이 20일로 줄지 않게 — 이동평균 괴리는 190일까지"""
    assert cl.METRICS['sma_gap']['args']['days'][1] >= 190


def test_gemini_gives_up_within_total_time(monkeypatch):
    """구글이 붐벼 계속 늦으면 몇 분씩 돌지 않고 전체 시간 안에 '제때 답하지' 안내"""
    import time as _t
    _fresh(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(_t, 'time', lambda: clock[0])
    monkeypatch.setattr(_t, 'sleep', lambda s: clock.__setitem__(0, clock[0] + s))

    def post(url, timeout=None, **k):
        clock[0] += timeout                   # 매번 시간 초과까지 기다린 것으로
        raise cl.requests.Timeout('x')
    monkeypatch.setattr(cl.requests, 'post', post)
    with pytest.raises(cl.LLMError, match='제때 답하지'):
        cl.generate_condition('gemini', 'g-key', '', 'RSI 30 이하')
    assert clock[0] - 1000.0 <= cl.TOTAL_SECONDS + 1


def test_days_from_text_when_ai_leaves_it_out():
    """AI가 days를 빼먹어도 '50일선'이면 50 (기본 20으로 바뀌지 않게)"""
    raw = {'title': '50일선 근처', 'match': 'all', 'rules': [{'metric': 'sma_gap', 'op': 'between', 'value': -2, 'value2': 2}]}
    cl._fill_days_from_text(raw, '일봉으로 이동평균선 50선 근처에 와 있는 코인')
    spec, err = cl.validate_condition(raw)
    assert err is None and spec['rules'][0]['days'] == 50
    raw = {'rules': [{'metric': 'sma_gap', 'op': '>=', 'value': 0, 'days': 20}]}
    cl._fill_days_from_text(raw, '120일 이동평균 위에 있는 코인')
    assert raw['rules'][0]['days'] == 120
    raw = {'rules': [{'metric': 'sma_gap', 'op': '>=', 'value': 0, 'days': 60}]}
    cl._fill_days_from_text(raw, '120일 이동평균 위에 있는 코인')
    assert raw['rules'][0]['days'] == 60          # AI가 일부러 넣은 값은 그대로


def test_prompt_lists_arg_ranges():
    assert 'days(2~190' in cl.system_prompt()


def test_auto_candidates_end_with_lite(monkeypatch):
    """자동 모델: 최신 flash → 붐비면 바로 가벼운 flash-lite → 그 아래 flash"""
    _fresh(monkeypatch)
    models = {'models': [{'name': f'models/{n}', 'supportedGenerationMethods': ['generateContent']}
                         for n in ('gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite')]}
    monkeypatch.setattr(cl.requests, 'get', lambda *a, **k: FakeResp(200, models))
    assert cl._gemini_candidates('g-key', '') == ['gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-3.7-flash']


def test_gemini_choices_for_picker(monkeypatch):
    """설정 화면 모델 고르기: 글 만드는 flash · lite · pro 만, 최신 세대만, 최신 순"""
    names = ['gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-pro-preview', 'gemini-2.5-flash', 'gemini-3.8-flash-tts', 'gemini-3-pro-image']
    models = {'models': [{'name': f'models/{n}', 'supportedGenerationMethods': ['generateContent']} for n in names]}
    monkeypatch.setattr(cl.requests, 'get', lambda *a, **k: FakeResp(200, models))
    got = [m['id'] for m in cl.gemini_choices('g-key')]
    assert got == ['gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-pro-preview']
