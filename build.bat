@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  StraBot 설치 파일 만들기
REM    1) PyInstaller : AutoTradingBot.spec  ->  dist\StraBot.exe
REM    2) Inno Setup  : setup.iss            ->  INSTALLER_OUTPUT_DIR\StraBot_Setup.exe  (ISCC 가 있을 때만)
REM  버전은 gui\__init__.py 의 __version__ 한 곳에서 읽어 설치 파일에 넘겨요.
REM
REM  쓰는 법:  build.bat          전체 빌드 (몇 분 걸리고 실행 파일이 약 270MB)
REM            build.bat check    빌드하지 않고 버전과 도구만 확인
REM ============================================================

set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "APP_VERSION="
for /f "usebackq delims=" %%v in (`%PY% -c "import gui; print(gui.__version__)"`) do set "APP_VERSION=%%v"
if "%APP_VERSION%"=="" (
    echo [오류] gui\__init__.py 에서 버전을 읽지 못했어요.
    exit /b 1
)

set "ISCC="
where ISCC >nul 2>nul && set "ISCC=ISCC"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

echo ========================================
echo  StraBot %APP_VERSION% 빌드
echo  Python     : %PY%
if defined ISCC echo  Inno Setup : "%ISCC%"
if not defined ISCC echo  Inno Setup : 없음 - 실행 파일만 만들어요
echo ========================================

%PY% -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [오류] PyInstaller 가 없어요. 먼저 설치해 주세요:  %PY% -m pip install pyinstaller
    exit /b 1
)

if /i "%~1"=="check" (
    echo 확인만 했어요. 빌드하려면 인자 없이 build.bat 를 실행해 주세요.
    exit /b 0
)

%PY% -m PyInstaller --noconfirm --clean AutoTradingBot.spec
if errorlevel 1 (
    echo [오류] PyInstaller 빌드에 실패했어요.
    exit /b 1
)
if not exist "dist\StraBot.exe" (
    echo [오류] dist\StraBot.exe 가 만들어지지 않았어요.
    exit /b 1
)
echo 실행 파일: dist\StraBot.exe

if not defined ISCC (
    echo Inno Setup 6 이 없어 설치 파일은 만들지 않았어요. https://jrsoftware.org/isdl.php
    exit /b 0
)

"%ISCC%" /DAppVersion=%APP_VERSION% setup.iss
if errorlevel 1 (
    echo [오류] 설치 파일 만들기에 실패했어요.
    exit /b 1
)

echo ========================================
echo  설치 파일: INSTALLER_OUTPUT_DIR\StraBot_Setup.exe  (버전 %APP_VERSION%)
echo  아래 SHA256 을 서버의 버전 안내(/api/version 의 sha256)에 넣어 주세요.
echo ========================================
certutil -hashfile "INSTALLER_OUTPUT_DIR\StraBot_Setup.exe" SHA256
exit /b 0
