"""설정 API 가 거래소 · LLM 키를 화면으로 내보내지 않는지 (2026-10-19)
조회는 저장 여부만(KEY_KEEP), 그 값이 다시 오면 저장된 키 유지, '' 이면 지움, 새 값이면 바꿈
실행: venv/Scripts/python.exe -m pytest tests/test_config_api_keys.py -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.web_dashboard import KEY_KEEP, SECRET_FIELDS, WebDashboard  # noqa: E402

HERE = {'REMOTE_ADDR': '127.0.0.1'}


def _wd(tmp_path):
    wd = WebDashboard(str(tmp_path / 'config' / 'bot_config.json'), port=0, user_data_dir=tmp_path)
    wd._save_config_internal = lambda *a, **k: None
    return wd


def test_get_hides_keys_and_shows_saved_flag(tmp_path):
    wd = _wd(tmp_path)
    wd.config.update({'api_key': 'UPBIT-ACCESS-1234', 'api_secret': 'UPBIT-SECRET-5678', 'llm_api_key': 'sk-llm', 'bithumb_api_key': ''})
    c = wd.app.test_client()
    body = c.get('/api/config', environ_base=HERE).get_json()
    assert body['api_key'] == KEY_KEEP and body['api_secret'] == KEY_KEEP and body['llm_api_key'] == KEY_KEEP
    assert body['bithumb_api_key'] == '' and body['bithumb_api_secret'] == ''
    raw = c.get('/api/config', environ_base=HERE).get_data(as_text=True)
    for secret in ('UPBIT-ACCESS-1234', 'UPBIT-SECRET-5678', 'sk-llm'):
        assert secret not in raw                                    # 키 자체는 어디에도 없음
    assert set(SECRET_FIELDS) >= {'api_key', 'api_secret', 'bithumb_api_key', 'bithumb_api_secret', 'llm_api_key'}


def test_post_keeps_clears_or_replaces(tmp_path):
    wd = _wd(tmp_path)
    wd.config.update({'api_key': 'KEEP-ME', 'api_secret': 'KEEP-ME-TOO', 'llm_api_key': 'sk-old'})
    c = wd.app.test_client()
    shown = c.get('/api/config', environ_base=HERE).get_json()
    # 화면이 받은 그대로(가린 값) 되돌려 보내면 저장된 키는 그대로
    assert c.post('/api/config', json=shown, environ_base=HERE).get_json()['success']
    assert wd.config['api_key'] == 'KEEP-ME' and wd.config['api_secret'] == 'KEEP-ME-TOO' and wd.config['llm_api_key'] == 'sk-old'
    # '' 이면 지움 (초기화 단추), 새 값이면 바꿈
    shown.update({'api_key': '', 'api_secret': '', 'llm_api_key': 'sk-new'})
    assert c.post('/api/config', json=shown, environ_base=HERE).get_json()['success']
    assert wd.config['api_key'] == '' and wd.config['api_secret'] == '' and wd.config['llm_api_key'] == 'sk-new'


def test_connection_test_uses_saved_key_when_masked(tmp_path, monkeypatch):
    wd = _wd(tmp_path)
    wd.config.update({'bithumb_api_key': 'BK', 'bithumb_api_secret': 'BS'})
    seen = {}

    def fake_test(api_key, api_secret):
        seen.update(key=api_key, secret=api_secret)
        return {'success': True}
    monkeypatch.setattr(wd, '_test_bithumb', fake_test)
    c = wd.app.test_client()
    r = c.post('/api/test-connection', json={'exchange': 'bithumb', 'api_key': KEY_KEEP, 'api_secret': KEY_KEEP}, environ_base=HERE)
    assert r.get_json()['success'] and seen == {'key': 'BK', 'secret': 'BS'}   # 가린 값은 저장된 키로 바꿔 시험


class FakeBithumb:
    """빗썸 계좌 대신: _send(GET, 경로, 쿼리) → 첫 쪽에만 미리 넣은 줄"""

    def __init__(self, pages=None, scope=True):
        self.pages, self.scope, self.calls = pages or {}, scope, []

    def _send(self, method, path, params):
        from bot_core.exchanges import BithumbRequestError
        self.calls.append((method, path, dict(params)))
        if not self.scope:
            raise BithumbRequestError(401, 'out_of_scope', '권한 없음')
        return self.pages.get(path, []) if params.get('page') == 1 else []


def test_bithumb_transfers_use_bithumb_api(tmp_path, monkeypatch):
    """빗썸 모드 입출금: 코인 입금 · 원화 입금 · 코인 출금 · 원화 출금 네 목록을 빗썸에서 (전에는 업비트 것을 보여 줬음, 2026-10-19)"""
    from datetime import datetime, timedelta, timezone
    wd = _wd(tmp_path)
    wd.config['app_mode'] = 'bithumb'
    now = datetime.now(timezone(timedelta(hours=9)))
    recent = (now - timedelta(days=1)).isoformat(timespec='seconds')
    naive = (now - timedelta(days=2)).replace(tzinfo=None).isoformat(timespec='seconds')      # 시간대 없는 값도 한국 시각으로
    old = (now - timedelta(days=40)).isoformat(timespec='seconds')
    fake = FakeBithumb({
        '/v1/deposits/krw': [{'currency': 'KRW', 'amount': '100000', 'fee': '0', 'state': 'ACCEPTED', 'done_at': recent}],
        '/v1/deposits': [{'currency': 'BTC', 'amount': '0.01', 'fee': '0', 'state': 'DEPOSIT_ACCEPTED', 'done_at': naive},
                         {'currency': 'ETH', 'amount': '1', 'fee': '0', 'state': 'DEPOSIT_ACCEPTED', 'done_at': old}],
        '/v1/withdraws': [{'currency': 'BTC', 'amount': '0.005', 'fee': '0.0005', 'state': 'DONE', 'done_at': recent}],
        '/v1/withdraws/krw': [{'currency': 'KRW', 'amount': '50000', 'fee': '1000', 'state': 'DONE', 'done_at': recent}],
    })
    monkeypatch.setattr(wd, '_account', lambda mode=None: fake)
    body = wd.app.test_client().get('/api/transfer-history?days=7', environ_base=HERE).get_json()
    assert body['success'] and body['notes'] == []
    assert {path for _, path, _ in fake.calls} == {'/v1/deposits', '/v1/deposits/krw', '/v1/withdraws', '/v1/withdraws/krw'}
    rows = body['rows']
    assert len(rows) == 4 and 'ETH' not in {r['currency'] for r in rows}                    # 40일 전 입금은 기간 밖
    krw_in = next(r for r in rows if r['kind'] == '입금' and r['currency'] == 'KRW')
    assert krw_in['amount'] == 100000 and krw_in['price'] == 1.0
    assert next(r for r in rows if r['kind'] == '출금' and r['currency'] == 'KRW')['settle'] == 49000
    assert all(r['time'].endswith('+09:00') for r in rows) and rows == sorted(rows, key=lambda r: r['time'], reverse=True)


def test_bithumb_transfers_without_scope_note(tmp_path, monkeypatch):
    wd = _wd(tmp_path)
    wd.config['app_mode'] = 'bithumb'
    monkeypatch.setattr(wd, '_account', lambda mode=None: FakeBithumb(scope=False))
    body = wd.app.test_client().get('/api/transfer-history?days=7', environ_base=HERE).get_json()
    assert body['success'] and body['rows'] == [] and body['notes'] == ['NO_DEPOSIT_SCOPE']


def test_page_texts_follow_mode_exchange(tmp_path):
    """화면 글자: 수수료 · 계좌 이름이 지금 모드 거래소를 따름 (빗썸 0.25% · 업비트 0.05%)"""
    wd = _wd(tmp_path)
    c = wd.app.test_client()
    wd.config.update({'app_mode': 'bithumb', 'bithumb_fee_pct': 0.25})
    html = c.get('/', environ_base=HERE).get_data(as_text=True)
    assert '(수수료 0.25% 별도)' in html and '내 빗썸 계좌의 실제 돈으로' in html and 'data-ex-day-start="0"' in html
    wd.config['app_mode'] = 'upbit'
    html = c.get('/', environ_base=HERE).get_data(as_text=True)
    assert '(수수료 0.05% 별도)' in html and '내 업비트 계좌의 실제 돈으로' in html and 'data-ex-day-start="9"' in html
