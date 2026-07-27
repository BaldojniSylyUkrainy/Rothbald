#ifndef MyAppVersion
  #define MyAppVersion "0.0.0.0"
#endif

#define MyAppName "Rothbald"
#define MyAppPublisher "Baldojni Syly Ukrainy"
#define MyAppExeName "Rothbald.exe"

[Setup]
AppId={{6A61CBF1-5F82-4935-9A65-D9E9A8540135}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Rothbald
DefaultGroupName=Rothbald
DisableProgramGroupPage=yes
OutputDir=..\release-assets
OutputBaseFilename=Rothbald-{#MyAppVersion}-Windows-Setup
SetupIconFile=..\assets\app-icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes

[Languages]
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Створити ярлик на робочому столі"; GroupDescription: "Додаткові ярлики:"; Flags: unchecked

[Files]
Source: "..\dist\Rothbald\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Rothbald"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Rothbald"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустити Rothbald"; Flags: nowait postinstall skipifsilent

[Code]
type
  TMemoryStatusEx = record
    dwLength: Cardinal;
    dwMemoryLoad: Cardinal;
    ullTotalPhys: Int64;
    ullAvailPhys: Int64;
    ullTotalPageFile: Int64;
    ullAvailPageFile: Int64;
    ullTotalVirtual: Int64;
    ullAvailVirtual: Int64;
    ullAvailExtendedVirtual: Int64;
  end;

function GlobalMemoryStatusEx(var Buffer: TMemoryStatusEx): Boolean;
  external 'GlobalMemoryStatusEx@kernel32.dll stdcall';

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Memory: TMemoryStatusEx;
  FreeBytes, TotalBytes: Int64;
  DataPath: String;
begin
  Result := '';
  Memory.dwLength := SizeOf(Memory);
  if GlobalMemoryStatusEx(Memory) and (Memory.ullTotalPhys < Int64(8589934592)) then
  begin
    Result := 'Rothbald потребує щонайменше 8 ГБ оперативної пам''яті для локальних моделей. Встановлення зупинено.';
    Exit;
  end;

  DataPath := ExpandConstant('{localappdata}');
  if GetSpaceOnDisk64(DataPath, FreeBytes, TotalBytes) and (FreeBytes < Int64(6442450944)) then
  begin
    Result := 'Rothbald потребує щонайменше 6 ГБ вільного місця для застосунку, моделей і робочого кешу. Звільни місце та повтори встановлення.';
    Exit;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Cores: Integer;
begin
  Result := True;
  if CurPageID <> wpReady then
    Exit;
  Cores := StrToIntDef(GetEnv('NUMBER_OF_PROCESSORS'), 0);
  if (Cores > 0) and (Cores < 4) then
    Result := MsgBox(
      'Знайдено менше 4 логічних ядер. Rothbald встановиться, але QtWebEngine і локальне розпізнавання можуть працювати дуже повільно. Продовжити?',
      mbConfirmation, MB_YESNO) = IDYES;
end;
