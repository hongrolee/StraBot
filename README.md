# StraBot

업비트 · 빗썸 원화 마켓에서 **내가 정한 규칙대로** 사고파는 설치형 자동매매 프로그램이에요.
프로그램과 거래소 API 키는 내 PC에만 있고, 실제 주문 전에 시뮬레이션으로 충분히 시험해 볼 수 있어요.

- 홈페이지 · 설치 파일: https://strabot.mayai.kr
- 규칙을 정하기 전에 보는 무료 정보 사이트: https://app.strahub.com (검증 기록 블로그 포함)
- 개선 요청 · 문제 신고: https://strabot.mayai.kr/feedback
- 무료 · 오픈소스(GPL-3.0). MAYAi 그룹이 만들어요.

> StraBot은 규칙을 **실행**해 주는 도구예요. 어떤 코인을 사라고 권하지 않고, 수익을 약속하지 않으며, 가상자산은 원금을 잃을 수 있어요.
> 실제 주문을 켜기 전에 [위험 고지](https://strabot.mayai.kr/risk)를 꼭 읽어 주세요.

## 무엇을 하나요

- **조건으로 사고팔기**: 이동평균 · RSI · 볼린저 · 거래대금 · 급등 같은 기본 조건과 내가 만든 조건을 '그리고/또는'으로 묶어 매수 · 매도 규칙을 만들어요.
- **안전장치**: 1회 매수 금액(비율 · 상한), 최대 보유 코인 수, 손절 · 익절 · 트레일링, 하루 손실 한도, 같은 코인 주문 간격.
- **시뮬레이션 먼저**: 가상 계좌(봇마다 1,000만 원)로 여러 봇을 동시에 돌려 실력을 비교해요. 실제 주문은 업비트 · 빗썸 모드에서만 나가요.
- **백테스트 · 차트 분석 · 보유코인 분석**: 과거 봉으로 규칙을 돌려 보고, 지표를 겹쳐 보고, 들고 있는 코인에 봇을 적용했다면 어땠을지 비교해요.
- **파는 것은 절대 막지 않아요**: 로그인이 풀리거나 서버가 멈춰도 팔기 · 손절 · 봇 멈추기는 그대로예요.

## 설치

### Windows 설치 파일 (권장)

https://strabot.mayai.kr 에서 `StraBot_Setup.exe` 를 받아 실행해요. 설치가 끝나면 브라우저에 화면(`http://localhost:5001`)이 열리고, 작업 표시줄 트레이 아이콘으로 다시 열거나 끌 수 있어요.
설정 · 기록은 `%LOCALAPPDATA%\StraBot` 폴더에 저장돼요.

### 소스로 실행 (Windows · macOS · 리눅스)

Python 3.12 이상이 필요해요 (개발은 3.13).

```bash
git clone https://github.com/hongrolee/StraBot.git
cd StraBot
python -m venv venv
venv/Scripts/activate        # Windows   (macOS · 리눅스: source venv/bin/activate)
pip install -r requirements.txt
python main.py
```

브라우저에서 `http://localhost:5001` 을 열면 돼요. 쓸 수 있는 환경 변수:

| 변수 | 뜻 |
|---|---|
| `STRABOT_DATA_DIR` | 설정 · 기록 폴더 (기본: Windows `%LOCALAPPDATA%\StraBot`, 소스 실행은 저장소 폴더) |
| `STRABOT_PORT` | 화면 포트 (기본 5001) |
| `STRABOT_LICENSE_ENFORCE=1` | 소스 실행에서도 설치 파일처럼 계정 로그인 잠금을 켬 (기본은 꺼짐) |

macOS · 리눅스는 소스로 실행할 수 있지만 트레이 아이콘 등 일부 기능은 Windows 기준이에요. 리눅스 서버용 한 줄 설치 스크립트와 서버 모드(비밀번호 로그인)는 준비 중이에요.

## 계정과 실제 주문

- 시뮬레이션 · 조건 검색 · 백테스트 · 차트 분석은 **계정 없이** 전부 쓸 수 있어요.
- 업비트 · 빗썸 **실제 주문**(봇 시작 · 새로 사기)은 https://strabot.mayai.kr 의 **무료 계정**으로 로그인해야 해요. 서버는 서명한 권한표만 내려 주고, 프로그램은 공개키로 확인해요(`bot_core/license.py`). 서버로 가는 것은 이메일 · 설치 ID · 프로그램 버전뿐이고, 거래소 키 · 잔고 · 매매 기록은 보내지 않아요.
- 거래소 API 키는 이 PC에만 암호화해 저장돼요(`utils/config_encryption.py`). 업비트 키를 만들 때는 **자산조회 · 주문조회 · 주문하기**만 주고 **출금 권한은 주지 마세요.**
- 계정 서버(strabot.mayai.kr)의 코드는 이 저장소에 없어요. 소스로 실행하면 기본적으로 계정 잠금이 꺼져 있어 로그인 없이도 실제 주문 기능을 쓸 수 있어요.

## 저장소 구조

```
main.py                  프로그램 시작 (트레이 아이콘 · 웹 대시보드)
bot_core/                봇 엔진 · 조건 계산 · 백테스트 · 안전장치 · 가상 계좌 · 권한표 확인
gui/web_dashboard.py     Flask 화면 서버 (API · 시세 · 캔들 · 봇 관리)
gui/exchange_feed.py     빗썸 시세 묶음
static/ templates/       화면 (JS · CSS · HTML)
utils/                   업비트 요청 속도 조절 · 설정 암호화 · 업데이트 확인 · 코인 정보
tests/                   pytest (641개)
tools/                   검증 · 백테스트 연구 스크립트 (StraHub 블로그의 검증 기록에 쓴 것)
AutoTradingBot.spec, setup.iss, build.bat   Windows 설치 파일 빌드
```

## 테스트

```bash
python -m pytest -q tests
```

실제 거래소 요청이나 주문은 없어요. 가짜 거래소 · 가짜 시세로 돌아가요.

## Windows 설치 파일 빌드

Windows에서 `build.bat` 를 실행해요 (PyInstaller 로 `dist\StraBot.exe`, Inno Setup 으로 `StraBot_Setup.exe`). 설정 파일이나 키는 실행 파일에 들어가지 않아요.

## 기여

고치고 싶은 것이 있으면 이슈나 PR을 열어 주세요. 자세한 것은 [CONTRIBUTING.md](CONTRIBUTING.md)에 있어요. 보안 문제는 공개 이슈 대신 [SECURITY.md](SECURITY.md)의 방법으로 알려 주세요.

## 라이선스

이 저장소의 코드는 [GNU GPL-3.0](LICENSE)이에요. 가져다 고쳐서 나눠 줄 때는 소스도 같은 조건으로 공개해야 해요.

**StraBot · MAYAi 이름과 마스코트 · 로고(`assets/`, `static/img/`)는 MAYAi의 표시예요.** 코드 라이선스와 별개로, 고친 버전을 나눠 줄 때는 다른 이름과 그림을 써 주세요. 사람들이 공식 StraBot과 헷갈리지 않게요.

## 면책

StraBot은 투자 자문 · 투자일임 · 유사투자자문 서비스가 아니에요. 모든 주문의 결과는 이용자 본인의 책임이고, 운영자(MAYAi, 대표 이홍로)는 이 프로그램을 써서 생긴 손실에 책임지지 않아요. 자세한 것은 [이용약관](https://strabot.mayai.kr/terms)과 [위험 고지](https://strabot.mayai.kr/risk)에 있어요.

문의: contact@mayai.kr
