# -*- mode: python ; coding: utf-8 -*-
# StraBot 실행 파일 빌드 (PyInstaller) → dist\StraBot.exe
#   실행: build.bat  (직접: venv\Scripts\python.exe -m PyInstaller --noconfirm --clean AutoTradingBot.spec)
#   버전은 gui/__init__.py 의 __version__ 한 곳에서 읽어요 (설치 파일 버전은 build.bat 가 setup.iss 에 넘김).
#
# 실행 파일에 넣는 것은 프로그램 코드와 아래 datas(아이콘·화면 파일)뿐이에요.
# 개인 파일(.master_key, .secret_key, auth_token.json, config/bot_config.json, strategies/*.enc, data/, logs/, venv)은
# 절대 들어가면 안 돼요 → 맨 아래에서 한 번 더 확인하고, 하나라도 섞이면 빌드를 멈춰요.

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets'), ('templates', 'templates'), ('static', 'static')],
    hiddenimports=['engineio.async_drivers.threading', 'socketio.async_drivers.threading'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['eventlet', 'gevent'],
    noarchive=False,
    optimize=0,
)

# ── 개인 파일이 섞이지 않았는지 확인 ──
_FORBIDDEN_NAMES = {'.master_key', '.secret_key', 'auth_token.json', 'bot_config.json', 'license.json', 'install_id'}
_FORBIDDEN_TOP = ('config/', 'data/', 'logs/', 'strategies/', 'venv/', 'venv_old_f/', 'tests/', 'tools/')
_leaks = []
for _entry in list(a.datas) + list(a.binaries):
    _dest = str(_entry[0]).replace('\\', '/')
    _name = _dest.rsplit('/', 1)[-1].lower()
    if _name in _FORBIDDEN_NAMES or _name.endswith('.enc') or _dest.lower().startswith(_FORBIDDEN_TOP):
        _leaks.append(_dest)
if _leaks:
    raise SystemExit(f"[BUILD] 배포 파일에 넣으면 안 되는 파일이 섞였어요: {_leaks[:10]}")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='StraBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
)
