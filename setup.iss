; Inno Setup 설치 스크립트 — StraBot 설치 프로그램
;   빌드: build.bat  (직접: ISCC /DAppVersion=1.1.0 setup.iss)
;   버전은 build.bat 가 gui\__init__.py 의 __version__ 을 읽어 /DAppVersion 으로 넘겨요.
;   들어가는 파일은 dist\StraBot.exe 하나뿐이에요 (설정·키·기록 파일은 넣지 않아요).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef ExePath
  #define ExePath "dist\StraBot.exe"
#endif

#define AppName "StraBot"
#define AppPublisher "StraBot"
; 임시 주소 (PLACEHOLDER): 운영 도메인이 정해지면 utils\server_client.py 의 DEFAULT_SERVER_URL 과 함께 바꿔요
#define AppURL "https://strabot.mayai.kr"
#define AppExeName "StraBot.exe"
#define OutputDir "INSTALLER_OUTPUT_DIR"

[Setup]
AppId={{STRAHUB-STRABOT-2024}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=StraBot_Setup
SetupIconFile=assets\icon.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 봇 실행 파일
Source: "{#ExePath}"; DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
