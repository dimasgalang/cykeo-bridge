<#
.SYNOPSIS
    Wizard + installer Cykeo RFID Bridge Agent untuk Windows (mode cykeo).

.DESCRIPTION
    Script ini UNTUK CLIENT WINDOWS yang menjalankan PC scanner Cykeo CK-D5.

    Yang di-install:
      1. cykeo_bridge.exe      - bridge agent Python (queue, config, HTTP client)
      2. cykeo-helper.exe      - helper .NET yang memanggil GReaderApi.dll
      3. GReaderApi.dll        - SDK Cykeo (dari ekstraksi MSI production)
      4. Newtonsoft.Json.dll   - dependency helper
      5. Startup task          - jalan otomatis saat user login

    Bridge hanya melakukan koneksi KELUAR ke server RFID.
    Tidak ada port listener, tidak ada perubahan setting reader.

    Prasyarat: file sudah ada di folder script (hasil build/packaging).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_cykeo.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_cykeo.ps1 -NonInteractive `
        -ServerUrl "http://10.0.0.5:8080" -ReaderCode "CYKEO-01" `
        -ApiKey "..." -ComPort "COM3"
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\CykeoRfidBridge",
    [string]$ServerUrl,
    [string]$ReaderCode,
    [string]$ApiKey,
    [string]$ComPort,
    [int]$Baudrate = 115200,
    [string]$Mode = 'cykeo',
    [switch]$SkipStartup,
    [switch]$SkipDefender,
    [switch]$SkipBridge,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

$bridgeExeName = 'cykeo_bridge.exe'
$helperExeName = 'cykeo-helper.exe'
$taskName = 'CykeoRfidBridge'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step($msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "[X] $msg" -ForegroundColor Red }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host ""
Write-Host "=============================================="
Write-Host " Cykeo RFID Bridge - wizard installer"
Write-Host "=============================================="

# --- 1. Cek file sumber -----------------------------------------------------
Write-Step "Memeriksa file sumber di $scriptRoot ..."

$need = @($helperExeName, 'GReaderApi.dll', 'Newtonsoft.Json.dll')
if (-not $SkipBridge) { $need = @($bridgeExeName) + $need }
$missing = @()
foreach ($f in $need) {
    $p = Join-Path $scriptRoot $f
    if (Test-Path $p) {
        $len = (Get-Item $p).Length
        Write-Host "    $f ($len byte)"
    }
    else {
        $missing += $f
    }
}

if ($missing.Count -gt 0) {
    Write-Err2 "File tidak lengkap: $($missing -join ', ')"
    Write-Host ""
    if ($missing -contains 'GReaderApi.dll') {
        Write-Host "GReaderApi.dll TIDAK disertakan dalam paket ini." -ForegroundColor Yellow
        Write-Host "DLL itu milik vendor Cykeo (berlisensi proprietary) dan tidak boleh" -ForegroundColor Yellow
        Write-Host "didistribusikan ulang bersama paket ini. Ambil dari:" -ForegroundColor Yellow
        Write-Host "  - folder hasil install aplikasi Cykeo di PC scanner, atau" -ForegroundColor Yellow
        Write-Host "  - ekstraksi dari file setup .exe/.msi resmi Cykeo." -ForegroundColor Yellow
        Write-Host "Salin ke folder paket ini, lalu jalankan ulang wizard." -ForegroundColor Cyan
        Write-Host ""
    }
    if ($missing -contains $bridgeExeName) {
        Write-Host "$bridgeExeName TIDAK disertakan dalam paket ini." -ForegroundColor Yellow
        Write-Host "Bridge adalah aplikasi Python; file .exe harus di-build di mesin Windows" -ForegroundColor Yellow
        Write-Host "(PyInstaller tidak bisa cross-compile dari Linux/macOS)." -ForegroundColor Yellow
        Write-Host "Alternatif tanpa .exe: install Python 3.11 lalu jalankan:" -ForegroundColor Cyan
        Write-Host "  python -m cykeo_bridge.cli run" -ForegroundColor DarkGray
        Write-Host ""
    }
    Write-Host "Perintah cepat untuk diagnose:" -ForegroundColor Cyan
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\install_cykeo.ps1 -SkipBridge" -ForegroundColor DarkGray
    Write-Host "  (mode ini hanya memasang + menguji helper .NET, tanpa bridge)" -ForegroundColor DarkGray
    exit 1
}

# --- 2. Buat folder install -------------------------------------------------
Write-Step "Menyiapkan folder install $InstallDir ..."
if (Test-Path $InstallDir) {
    $running = Get-Process -Name 'cykeo_bridge', 'cykeo-helper' -ErrorAction SilentlyContinue
    if ($running) {
        Write-Step "Menghentikan proses bridge yang sedang berjalan..."
        $running | ForEach-Object { try { $_.Kill(); $_.WaitForExit(5000) } catch {} }
    }
}
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

foreach ($f in $need) {
    Copy-Item (Join-Path $scriptRoot $f) -Destination $InstallDir -Force
}
Write-Ok "File disalin ke $InstallDir"

# --- 2b. Deteksi karantina antivirus -----------------------------------------
# Windows Defender bisa mengkarantina cykeo-helper.exe SEDANG/JUSTRU setelah
# disalin (heuristik machine learning untuk binary .NET unsigned).
# Kalau file hilang, jangan diam-diam lanjut - hentikan dan jelaskan.
$helperInstalled = Join-Path $InstallDir $helperExeName
if (-not (Test-Path $helperInstalled)) {
    Write-Host ""
    Write-Err2 "cykeo-helper.exe TIDAK ADA di $InstallDir setelah disalin."
    Write-Host ""
    Write-Host "  Penyebab paling mungkin: Windows Defender mengkarantina file." -ForegroundColor Yellow
    Write-Host ""

    $threatName = $null
    try {
        $threats = @(Get-MpThreat -ErrorAction Stop | Where-Object {
            ($_.Resources -join ' ') -like '*cykeo*'
        })
        if ($threats.Count -gt 0) {
            $t = $threats[0]
            Write-Host "  Ancaman terdeteksi:" -ForegroundColor Red
            Write-Host "    Nama  : $($t.ThreatName)"
            Write-Host "    ID    : $($t.ThreatID)"
            Write-Host "    Waktu : $($t.InitialDetectionTime)"
            $threatName = $t.ThreatName
        }
    } catch { }

    if ($threatName) {
        Write-Host ""
        Write-Host "  Catatan: helper ini binary .NET 32-bit milik internal yang memanggil" -ForegroundColor DarkGray
        Write-Host "  SDK Cykeo (GReaderApi.dll) via managed code. Tidak ada P/Invoke, tidak ada" -ForegroundColor DarkGray
        Write-Host "  network listener, tidak menyentuh registry atau proses lain. Deteksi" -ForegroundColor DarkGray
        Write-Host " heuristik seperti di atas adalah false positive yang umum terjadi pada" -ForegroundColor DarkGray
        Write-Host "  binary .NET kecil yang baru dibuat dan belum punya reputasi." -ForegroundColor DarkGray
    }

    Write-Host ""
    Write-Host "  Cara menanganinya (pilih salah satu):" -ForegroundColor Cyan
    Write-Host ""
    if ($SkipDefender) {
        Write-Host "    -SkipDefender aktif, remediation otomatis dilewati." -ForegroundColor DarkGray
    }
    elseif (Test-Admin) {
        Write-Host "    1. Jalankan remediation otomatis (memakai exclusion path/lupa proses):" -ForegroundColor Green
        Write-Host "         powershell -ExecutionPolicy Bypass -File `"$(Join-Path $scriptRoot 'configure_defender.ps1')`" -InstallDir `"$InstallDir`"" -ForegroundColor Green
        Write-Host ""
    }
    else {
        Write-Host "    1. Buka PowerShell SEBAGAI ADMINISTRATOR lalu jalankan:" -ForegroundColor Yellow
        Write-Host "         powershell -ExecutionPolicy Bypass -File `"$(Join-Path $scriptRoot 'configure_defender.ps1')`" -InstallDir `"$InstallDir`"" -ForegroundColor Yellow
        Write-Host ""
    }
    Write-Host "    2. Lalu jalankan ulang script installer ini." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    3. Alternatif yang lebih permanen: code-sign helper dengan" -ForegroundColor DarkGray
    Write-Host "       sertifikat kode-signing organisasi (signtool), lalu submit" -ForegroundColor DarkGray
    Write-Host "       ke vendor antivirus sebagai false positive." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Lihat status Defender tanpa mengubah apa pun:" -ForegroundColor Cyan
    Write-Host "       powershell -ExecutionPolicy Bypass -File `"$(Join-Path $scriptRoot 'configure_defender.ps1')`" -Status" -ForegroundColor Cyan
    Write-Host ""
    exit 2
}
Write-Ok "cykeo-helper.exe lolos (tidak dikarantina)."

if (-not $SkipDefender -and (Test-Admin)) {
    $defenderScript = Join-Path $scriptRoot 'configure_defender.ps1'
    if (Test-Path $defenderScript) {
        Write-Step "Mendaftarkan exclusion Defender untuk folder install (real-time protection tetap aktif)..."
        try {
            & powershell -ExecutionPolicy Bypass -File $defenderScript -InstallDir $InstallDir | Out-Null
            Write-Ok "Exclusion Defender terdaftar."
        } catch {
            Write-Warn2 "Gagal mendaftarkan exclusion: $($_.Exception.Message)"
        }
    }
}

# --- 3. Validasi helper bisa jalan ------------------------------------------
Write-Step "Validasi helper .NET (handshake stdio JSON)..."
$helperExe = Join-Path $InstallDir $helperExeName
$helperOk = $false
$helperMsg = $null

# Helper bersifat stdio: dia push {"type":"hello"} lalu menunggu perintah.
# Kita kirim {"id":1,"cmd":"ping"} lalu baca satu baris balasan.
try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $helperExe
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $null = $proc.StandardOutput.ReadLine()      # baris hello
    $proc.StandardInput.WriteLine('{"id":1,"cmd":"ping"}')
    $proc.StandardInput.Flush()
    $reply = $proc.StandardOutput.ReadLine()
    $proc.StandardInput.WriteLine('{"id":2,"cmd":"shutdown"}')
    $proc.StandardInput.Flush()
    if (-not $proc.WaitForExit(5000)) { try { $proc.Kill() } catch { } }
    $proc.Dispose()

    if ($reply -and $reply -match '"ok"\s*:\s*true') {
        $helperOk = $true
        $helperMsg = $reply
        Write-Ok "Helper merespons handshake+ping: $reply"
    } else {
        $helperMsg = $reply
        Write-Err2 "Helper tidak merespons ping dengan benar."
        Write-Host "    Balasan: $reply" -ForegroundColor DarkGray
    }
} catch {
    $helperMsg = $_.Exception.Message
    Write-Err2 "Gagal menjalankan helper: $($_.Exception.Message)"
}

if ($helperOk) {
    $hash = (Get-FileHash -Path $helperExe -Algorithm SHA256).Hash
    Write-Host "    SHA-256: $hash"
}

# --- 4. Deteksi COM port ----------------------------------------------------
Write-Step "Mendeteksi COM port..."
$ports = @()
try {
    $ports = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object
}
catch {
    Write-Warn2 "Gagal enumerasi COM port: $($_.Exception.Message)"
}

$readerFound = $false
if ($ports.Count -gt 0) {
    Write-Host "    ditemukan: $($ports -join ', ')" -ForegroundColor DarkGray
    if ($ComPort) {
        if ($ports -contains $ComPort.ToUpper()) {
            Write-Ok "COM port yang diminta ($ComPort) terdaftar."
            $readerFound = $true
        } else {
            Write-Warn2 "COM port $ComPort TIDAK ada di daftar port yang terdeteksi."
        }
    }
}
else {
    Write-Warn2 "Tidak ada COM port sama sekali di PC ini."
}

if (-not $readerFound) {
    Write-Host ""
    Write-Host "  PENTING: reader CK-D5 belum terlihat sebagai COM port." -ForegroundColor Yellow
    Write-Host "  Bridge tetap ter-install, tapi belum bisa membaca tag." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Yang perlu dicek:" -ForegroundColor Cyan
    Write-Host "    1. Colok kabel USB reader CK-D5 ke PC ini." -ForegroundColor DarkGray
    Write-Host "    2. Buka Device Manager - Ports (COM ^& LPT)." -ForegroundColor DarkGray
    Write-Host "       Cari entri 'USB Serial Port (COMx)' atau 'Cykeo'." -ForegroundColor DarkGray
    Write-Host "    3. Jika reader tidak muncul, install driver USB yang disertakan" -ForegroundColor DarkGray
    Write-Host "       vendor Cykeo." -ForegroundColor DarkGray
    Write-Host "    4. Setelah port muncul, jalankan wizard dan isi COM port yang benar." -ForegroundColor DarkGray
    Write-Host ""
}

# --- 5. Startup task --------------------------------------------------------
if ($SkipStartup -or $SkipBridge) {
    if ($SkipBridge) { $skipWhy = ' (-SkipBridge: bridge executable tidak ada di paket)' }
    else { $skipWhy = ' (-SkipStartup)' }
    Write-Step "Startup task: dilewati$skipWhy."
}
else {
    Write-Step "Mendaftarkan startup task '$taskName' (ONLOGON, per-user)..."
    & schtasks.exe /Delete /F /TN $taskName 2>$null | Out-Null
    $tr = '"{0}" run' -f (Join-Path $InstallDir $bridgeExeName)
    & schtasks.exe /Create /F /SC ONLOGON /TN $taskName /TR $tr | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Gagal membuat startup task (exit=$LASTEXITCODE)."
    }
    Write-Ok "Startup task terdaftar."
}

# --- 6. Wizard konfigurasi --------------------------------------------------
$bridgeExe = Join-Path $InstallDir $bridgeExeName
$needWizard = $true

if ($SkipBridge) {
    Write-Step "Wizard konfigurasi: dilewati (-SkipBridge)."
    Write-Host "  Bridge tidak ter-install, jadi config wizard tidak bisa dijalankan." -ForegroundColor DarkGray
    Write-Host "  Konfigurasi server/API key dilakukan manual di:" -ForegroundColor DarkGray
    Write-Host "    $env:APPDATA\CykeoBridge\config.json" -ForegroundColor DarkGray
    Write-Host ""
    $needWizard = $false
}
elseif ($NonInteractive) {
    Write-Step "Mode non-interaktif: menulis config.json dari parameter..."
    $cfgDir = Join-Path $env:APPDATA 'CykeoBridge'
    New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
    $cfg = [ordered]@{
        server_url   = $ServerUrl
        reader_code  = $ReaderCode
        api_key      = $ApiKey
        mode         = $Mode
        com_port     = $ComPort
        baudrate     = $Baudrate
        init_param   = ''
        antenna      = 1
        enabled      = $true
    }
    $cfgPath = Join-Path $cfgDir 'config.json'
    $cfg | ConvertTo-Json -Depth 5 | Set-Content -Path $cfgPath -Encoding UTF8
    Write-Ok "Config ditulis ke $cfgPath"
    $needWizard = $false
}

if ($needWizard) {
    Write-Step "Membuka wizard konfigurasi..."
    Write-Host ""
    Write-Host "  Wizard akan meminta:" -ForegroundColor DarkGray
    Write-Host "    - URL server RFID" -ForegroundColor DarkGray
    Write-Host "    - reader code" -ForegroundColor DarkGray
    Write-Host "    - API key" -ForegroundColor DarkGray
    Write-Host "    - mode reader (zebra | cykeo)" -ForegroundColor DarkGray
    Write-Host "    - COM port dan baudrate (default COM3 / 115200)" -ForegroundColor DarkGray
    Write-Host "    - helper .NET (deteksi otomatis)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Folder install : $InstallDir"
    Write-Host "  Lokasi log     : $env:APPDATA\CykeoBridge\logs"
    Write-Host "  Lokasi config  : $env:APPDATA\CykeoBridge\config.json"
    Write-Host ""
    & $bridgeExe wizard
}

# --- 7. Ringkasan ------------------------------------------------------------
Write-Host ""
Write-Host "=============================================="
Write-Host " Ringkasan instalasi"
Write-Host "=============================================="
Write-Host ""
Write-Host "  Folder install : $InstallDir"
Write-Host "  Helper        : $helperExe"
if ($SkipBridge) {
    Write-Host "  Bridge        : TIDAK di-install (-SkipBridge)"
    Write-Host "  Startup task  : tidak dibuat (bridge tidak ada)"
    Write-Host "  Config        : belum dibuat (jalankan bridge wizard nanti)"
} else {
    Write-Host "  Bridge        : $bridgeExe"
    Write-Host "  Startup task  : $(if ($SkipStartup) { 'dilewati' } else { $taskName })"
    Write-Host "  Config        : $env:APPDATA\CykeoBridge\config.json"
}
Write-Host "  Log           : $env:APPDATA\CykeoBridge\logs"
Write-Host ""

Write-Host "  Status:" -ForegroundColor Cyan
$helperStatus = if ($helperOk) { "LULUS  - helper handshake + ping berhasil" } else { "GAGAL  - helper tidak merespons dengan benar" }
$portStatus   = if ($readerFound) { "LULUS  - COM port $ComPort terdaftar" } else { "TUNGGU - reader CK-D5 belum terdeteksi sebagai COM port" }
Write-Host "    Helper 32-bit : $helperStatus" -ForegroundColor $(if ($helperOk) { 'Green' } else { 'Red' })
Write-Host "    Hardware      : $portStatus" -ForegroundColor $(if ($readerFound) { 'Green' } else { 'Yellow' })
Write-Host ""

if ($helperOk -and $readerFound) {
    Write-Host "  Bridge siap dipakai untuk membaca tag." -ForegroundColor Green
} elseif ($helperOk) {
    Write-Host "  Helper ter-install dan jalan, TAPI belum bisa baca tag" -ForegroundColor Yellow
    Write-Host "  karena reader CK-D5 belum terdeteksi." -ForegroundColor Yellow
} else {
    Write-Host "  Instalasi BELUM lengkap. Perbaiki masalah helper di atas," -ForegroundColor Red
    Write-Host "  lalu jalankan ulang script ini." -ForegroundColor Red
}
Write-Host ""
if ($SkipBridge) {
    Write-Host "  Langkah berikutnya:" -ForegroundColor Cyan
    Write-Host "    1. Salin GReaderApi.dll dari install Cykeo ke folder paket ini." -ForegroundColor DarkGray
    Write-Host "    2. Build cykeo_bridge.exe di mesin Windows (PyInstaller)." -ForegroundColor DarkGray
    Write-Host "    3. Jalankan ulang: install_cykeo.ps1  (tanpa -SkipBridge)" -ForegroundColor DarkGray
    Write-Host "    4. Uji reader dengan smoke_test_cykeo.bat" -ForegroundColor DarkGray
} else {
    Write-Host "  Langkah berikutnya:" -ForegroundColor Cyan
    Write-Host "    1. Pastikan config benar:  $bridgeExe check" -ForegroundColor DarkGray
    Write-Host "    2. Uji koneksi reader:    $bridgeExe cykeo-check" -ForegroundColor DarkGray
    Write-Host "    3. Mulai baca tag:        $bridgeExe run" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "  Kalau ada masalah dengan antivirus:" -ForegroundColor DarkGray
Write-Host "    powershell -ExecutionPolicy Bypass -File `"$(Join-Path $scriptRoot 'configure_defender.ps1')`" -Status" -ForegroundColor DarkGray
Write-Host ""

if (-not $helperOk) { exit 1 }
exit 0
