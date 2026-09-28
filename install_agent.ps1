<#
.SYNOPSIS
    Install Cykeo RFID Bridge Agent untuk Windows, termasuk startup task.

.DESCRIPTION
    Script ini UNTUK CLIENT WINDOWS yang menjalankan PC scanner.
    Bridge hanya melakukan koneksi KELUAR ke server RFID; tidak membuka
    port listener, dan tidak mengubah konfigurasi Zebra FX7500.

    Prasyarat: file cykeo_bridge.exe sudah ada (hasil build_windows.bat).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_agent.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\CykeoRfidBridge",
    [switch]$SkipStartup,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$exe = Join-Path $InstallDir 'cykeo_bridge.exe'
$taskName = 'CykeoRfidBridge'

Write-Host "=============================================="
Write-Host " Cykeo RFID Bridge - installer agent"
Write-Host "=============================================="

if (-not (Test-Path $exe)) {
    throw "cykeo_bridge.exe tidak ditemukan di $InstallDir. Jalankan build_windows.bat lebih dulu."
}

# --- 1. Validasi executable bisa jalan ---------------------------------------
Write-Host "[1/3] Validasi executable..."
& $exe config-path | Write-Host
if ($LASTEXITCODE -ne 0) {
    throw "Executable gagal dijalankan (exit=$LASTEXITCODE)."
}

# --- 2. Registrasi startup (per-user, ONLOGON) ------------------------------
if ($SkipStartup) {
    Write-Host "[2/3] Startup task: dilewati (-SkipStartup)."
}
else {
    Write-Host "[2/3] Mendaftarkan startup task '$taskName' (ONLOGON, per-user)..."
    # Hapus dulu kalau ada, supaya tidak duplikat.
    & schtasks.exe /Delete /F /TN $taskName 2>$null | Out-Null
    $tr = '"{0}" run' -f $exe
    & schtasks.exe /Create /F /SC ONLOGON /TN $taskName /TR $tr | Write-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Gagal membuat startup task (exit=$LASTEXITCODE)."
    }
}

# --- 3. Buka wizard konfigurasi ---------------------------------------------
Write-Host "[3/3] Membuka wizard konfigurasi..."
Write-Host "  Wizard akan meminta: IP server RFID, port, reader code, API key,"
Write-Host "  mode reader (zebra/cykeo), COM port, baudrate, dan path helper."
Write-Host ""
Write-Host "  Lokasi log   : $env:APPDATA\CykeoBridge\logs"
Write-Host "  Lokasi queue : $env:APPDATA\CykeoBridge"
Write-Host ""

& $exe wizard

Write-Host ""
Write-Host "[OK] Instalasi selesai. Bridge akan aktif otomatis saat user login."
Write-Host "     Periksa dengan: $exe check"
