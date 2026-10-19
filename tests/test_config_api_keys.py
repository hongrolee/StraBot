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


def test_bithumb_mode_transfers_not_supported_yet(tmp_path):
    wd = _wd(tmp_path)
    wd.config['app_mode'] = 'bithumb'
    c = wd.app.test_client()
    body = c.get('/api/transfer-history?days=7', environ_base=HERE).get_json()
    assert body['success'] and body['rows'] == [] and 'BITHUMB_UNSUPPORTED' in body['notes']
