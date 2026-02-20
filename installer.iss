; Inno Setup script for ScriptCompiler Bridge
; Requires Inno Setup 6+
; Build with: iscc installer.iss

#define MyAppName "ScriptCompiler Bridge"
#ifndef MyAppVersion
  #define MyAppVersion "1.1.2"
#endif
#define MyAppPublisher "ScriptCompiler"
#define MyAppURL "https://github.com/scriptcompiler/scriptcompiler-bridge"
#define MyAppExeName "ScriptCompilerBridge.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=ScriptCompilerBridge-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Allow upgrading in-place without uninstalling first
UsePreviousAppDir=yes
CloseApplications=yes
RestartApplications=no
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "launchstartup"; Description: "Launch on Windows startup"; GroupDescription: "Startup:"; Flags: checkedonce

[Files]
Source: "dist\ScriptCompilerBridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Launch on startup (current user only)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "ScriptCompilerBridge"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Flags: uninsdeletevalue; Tasks: launchstartup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent shellexec

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM ScriptCompilerBridge.exe"; Flags: runhidden

[Code]
// Kill running instance before install/upgrade
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill', '/F /IM ScriptCompilerBridge.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill', '/F /IM tracker.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;
