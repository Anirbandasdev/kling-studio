; Kling Studio installer. Build the exe first (build_exe.bat), then compile this with:
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss
; Output: dist\Kling Studio Setup 3.0.0.exe -- send that one file to your friend.
;
; Per-user install: no admin/UAC prompt, Start Menu shortcut, uninstaller included.

#define AppVersion "3.0.1"

[Setup]
AppId={{677AD1D4-F54A-4D0B-B5BD-F582DC5D9B41}
AppName=Kling Studio
AppVersion={#AppVersion}
AppVerName=Kling Studio {#AppVersion}
DefaultDirName={localappdata}\Programs\Kling Studio
DefaultGroupName=Kling Studio
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=KlingStudio-Setup-{#AppVersion}
SetupIconFile=ui\app.ico
UninstallDisplayIcon={app}\Kling Studio.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
AppPublisher=Kling Studio
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\Kling Studio.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Kling Studio"; Filename: "{app}\Kling Studio.exe"
Name: "{autodesktop}\Kling Studio"; Filename: "{app}\Kling Studio.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a &desktop icon"; Flags: unchecked

[Run]
Filename: "{app}\Kling Studio.exe"; Description: "Launch Kling Studio"; Flags: nowait postinstall skipifsilent
