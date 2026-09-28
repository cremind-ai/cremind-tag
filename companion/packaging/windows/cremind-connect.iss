; Cremind Connect for Windows: a per-user installer (no administrator rights). Built by
; packaging/build_connect.py --installer, which passes the defines below (docs/connect-packaging.md).
;
; It unpacks the PyInstaller bundle into
;     %LOCALAPPDATA%\Programs\Cremind Connect\versions\<version>\
; and runs `cremind-connect.exe install --register-only` from there: that switches the
; `current` junction to this version, stops an older service, registers the logon task
; and the cremind-connect: link handler, starts the service and checks it answers
; (rolling back to the previous version if it does not). Uninstall runs
; `cremind-connect.exe uninstall --keep-data`: workers, keys and logs stay in
; %LOCALAPPDATA%\Cremind\Connect.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "0.0.0.0"
#endif
#ifndef SourceDir
  #error Pass /DSourceDir=<the PyInstaller bundle directory, dist\connect\cremind-connect>
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifndef Arch
  #define Arch "x64"
#endif

[Setup]
; Never change the AppId: upgrades and the uninstaller find earlier installs through it.
AppId={{6E0C3D52-94A1-4B7B-9F4E-3C1B7A2D8E51}
AppName=Cremind Connect
AppVersion={#AppVersion}
AppVerName=Cremind Connect {#AppVersion}
AppPublisher=Cremind
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductName=Cremind Connect
DefaultDirName={localappdata}\Programs\Cremind Connect
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Cremind-Connect-{#AppVersion}-windows-{#Arch}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The running service lives in another versions\ directory: nothing to close.
CloseApplications=no
RestartApplications=no
UninstallDisplayName=Cremind Connect
UninstallDisplayIcon={app}\current\cremind-connect.exe
SetupLogging=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{app}\versions\{#AppVersion}\cremind-connect.exe"; Parameters: "install --register-only"; \
  StatusMsg: "Starting Cremind Connect..."; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\current\cremind-connect.exe"; Parameters: "uninstall --keep-data"; \
  RunOnceId: "CremindConnectUninstall"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}\versions"
Type: dirifempty; Name: "{app}"
