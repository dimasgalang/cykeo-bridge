; Inno Setup script — installer untuk Cykeo RFID Bridge Agent (Windows client).
;
; CARA BUILD (di mesin Windows, bukan di server Linux):
;   1. Build payload lebih dulu:
;        python -m pip install pyinstaller
;        pyinstaller --onefile --name cykeo_bridge ^
;                    --paths . cykeo_bridge\__main__.py
;        (atau pakai build_windows.bat yang disertakan)
;   2. Build installer:
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\bridge_agent.iss
;   3. Hasil: Output\CykeoBridgeSetup.exe
;
; CATATAN PENTING:
;  - Installer TIDAK mengubah konfigurasi Zebra FX7500.
;  - Bridge TIDAK membuka port listener; hanya koneksi KELUAR (POST) ke server.
;  - API key diisi lewat wizard SETELAH install (tidak di-hardcode).
;  - Agent dijalankan sebagai user yang sedang login (bukan SYSTEM) supaya bisa
;    mengakses COM port milik user tersebut.

#define AppName "Cykeo RFID Bridge"
#define AppVersion "1.0.0"
#define AppPublisher "Chutex IT"
#define AppExeName "cykeo_bridge.exe"

[Setup]
AppId={{7C4E2A11-9B3D-4F6A-8C21-5D7E9A0B4C13}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\CykeoRfidBridge
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=CykeoBridgeSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
ChangesEnvironment=yes
CloseApplications=yes
CloseApplicationsFilter=*.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Run the bridge automatically at Windows logon"; GroupDescription: "Additional tasks:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional tasks:"; Flags: unchecked

[Files]
; Payload wajib
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; Payload opsional (hanya ada kalau step 1 di bawah dijalankan)
Source: "..\dist\embedded\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; Dokumentasi pendukung
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\build_windows.bat"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\install_agent.ps1"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
; Data runtime ditulis ke user profile agar tidak butuh admin saat reinstall
Name: "{userappdata}\CykeoBridge"; Permissions: users-modify
Name: "{userappdata}\CykeoBridge\logs"; Permissions: users-modify

[Icons]
; Command yang dipakai installer sudah sesuai CLI asli:
;   cykeo_bridge.exe wizard  -> buka wizard konfigurasi
;   cykeo_bridge.exe run     -> jalankan agent
;   cykeo_bridge.exe check   -> validasi config + status device
Name: "{group}\Configure Bridge (Wizard)"; Filename: "{app}\{#AppExeName}"; Parameters: "wizard"; WorkingDir: "{app}"
Name: "{group}\Run Bridge"; Filename: "{app}\{#AppExeName}"; Parameters: "run"; WorkingDir: "{app}"
Name: "{group}\Check Configuration"; Filename: "{app}\{#AppExeName}"; Parameters: "check"; WorkingDir: "{app}"
Name: "{autodesktop}\Configure Bridge (Wizard)"; Filename: "{app}\{#AppExeName}"; Parameters: "wizard"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "wizard"; Description: "Open the configuration wizard now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Hentikan agent bila sedang jalan (abaikan error kalau memang tidak jalan).
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExeName} /F"; Flags: runhidden; RunOnceId: "StopAgent"
; Copot startup entry yang dibuat wizard.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""CykeoRfidBridge"""; Flags: runhidden runhidden; RunOnceId: "DeleteStartupTask"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
; Data runtime (log, queue, config) sengaja TIDAK dihapus supaya tidak
; hilang saat reinstall; hapus manual dari {userappdata}\CykeoBridge.
