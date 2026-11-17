; Inno Setup script for Clipper-Setup.exe
; Build the app first (PyInstaller Clipper.spec), then:
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss
; Output: dist\Clipper-Setup.exe

#define AppName "Clipper"
#define AppVersion "1.2.0"
#define AppPublisher "Sonic-Bound"
#define AppURL "https://github.com/Sonic-Bound/clipper"

[Setup]
AppId={{6F1C2B7E-4D3A-4E8B-9C21-7A5D0F3E9B14}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
; per-user install: no admin prompt, and updates just install over the old version
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=dist
OutputBaseFilename=Clipper-Setup
SetupIconFile=clipper\web\clipper.ico
UninstallDisplayIcon={app}\Clipper.exe
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Clipper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; drop files left over from an older version before copying the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Clipper.exe"; AppUserModelID: "Clipper.Desktop"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Clipper.exe"; Tasks: desktopicon; AppUserModelID: "Clipper.Desktop"

[Run]
Filename: "{app}\Clipper.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
