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
OutputBaseFilename=Rothbald_{#MyAppVersion}_windows-x86_64-setup
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
