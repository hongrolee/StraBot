"""API 키 암호화 — 긴 평문 키도 암호화되고, 마스터 키가 바뀌면 암호문을 키로 쓰지 않는다 (2026-10-17)
실행: venv/Scripts/python.exe -m pytest tests/test_config_encryption.py -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config_encryption import ConfigEncryption, sanitize_config_for_logging  # noqa: E402


def test_long_plaintext_is_not_mistaken_for_ciphertext(tmp_path):
    enc = ConfigEncryption(user_data_dir=tmp_path)
    for n in (40, 95, 96, 108, 109, 152, 200):
        key = 'sk-ant-api03-' + 'A' * n
        assert not enc.is_encrypted(key), n
        out = enc.encrypt_config({'llm_api_key': key})
        assert out['llm_api_key'] != key and enc.is_encrypted(out['llm_api_key'])
        assert enc.decrypt_config(out)['llm_api_key'] == key


def test_lost_master_key_gives_empty_not_ciphertext(tmp_path):
    a = ConfigEncryption(user_data_dir=tmp_path / 'a')
    out = a.encrypt_config({'api_key': 'abcdefghijklmnopqrstuvwxyz0123456789ABCD'})
    b = ConfigEncryption(user_data_dir=tmp_path / 'b')          # 다른 마스터 키 (= 키 파일이 없어져 새로 만든 상황)
    assert b.decrypt_config(out)['api_key'] == ''


def test_sanitize_does_not_create_key_files(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    s = sanitize_config_for_logging({'api_key': 'abcdefghijklmnop', 'other': 1})
    assert s['api_key'] == 'abcd...mnop' and s['other'] == 1
    assert not list(tmp_path.rglob('.master_key'))
