; Canopeo Drone - Inno Setup script
; Build the app first:  python app\build.py   (creates dist\CanopeoDrone\)
; Then open this file in Inno Setup and click Compile.

#define AppName "Canopeo Drone"
#define AppVersion "0.4.0"
#define AppPublisher "Andres Patrignani and Tyson E. Ochsner"
#define AppURL "https://soilwater.github.io/canopeo-drone/"
#define AppExe "CanopeoDrone.exe"
; Repo root, relative to this .iss file (installer\ is one level down).
#define RepoRoot ".."

[Setup]
; A stable, unique ID for this app. Keep it the same across versions so
; upgrades replace the previous install instead of stacking up.
AppId={{2AC99059-DDC8-423D-80A9-5D2180C67625}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
; Per-user install by default (no admin prompt). Use "admin" + {autopf}
; only if you want an all-users install.
PrivilegesRequiredOverridesAllowed=dialog commandline
LicenseFile={#RepoRoot}\LICENSE.txt
SetupIconFile={#RepoRoot}\icons\canopeo.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir={#RepoRoot}\installer\Output
OutputBaseFilename=CanopeoDrone-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; Ship the ENTIRE onedir build: the exe AND its _internal folder. The
; recurse flags pull in _internal\ and everything under it.
Source: "{#RepoRoot}\dist\CanopeoDrone\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
