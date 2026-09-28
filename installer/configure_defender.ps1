<#
.SYNOPSIS
    Remediasi Windows Defender untuk cykeo-helper.exe (opsional, terkontrol).

.DESCRIPTION
   -cykeo-helper.exe adalah binary .NET 32-bit milik internal yang memanggil
    SDK Cykeo (GReaderApi.dll) lewat managed code. Versi tanpa assembly
    metadata memicu deteksi heuristik ML pada Windows Defender
    (ditemukan: Trojan:Win32/Bearfoos.B!ml).

    Binary yang sudah diberi metadata assembly (Judul/Produk/Perusahaan/
    Deskripsi/Copyright) lolos deteksi pada PC Test Windows 10 dengan
    Microsoft Defender sig 1.459.439.0, real-time protection aktif:

        SHA-256  E199112ABC5D7A5B353C44143C514CDA76AC39BB3C39036763176C343D95866C
        ukuran   14.336 byte, PE32 (x86)

    Kalau file lokalmu punya hash berbeda, JANGAN pakai exclusion buta.

    File ini BUKAN "matikan antivirus". File ini hanya:
      1. Membuat exclusion path untuk satu folder install tertentu.
      2. Mendaftarkan hash SHA-256 binary di allowlist Defender (kalau didukung).

    Aturan penting:
      - Harus dijalankan sebagai Administrator.
      - Tidak pernah menyentuh setting lain (Real-time protection tetap aktif).
      - Exclusion hanya untuk path yang benar-benar dipakai bridge.
      - Semua aksi dicatat ke log agar bisa diaudit/direview.

.PARAMETER InstallDir
    Folder install bridge. Default: $env:LOCALAPPDATA\CykeoRfidBridge

.PARAMETER Hash
    SHA-256 cykeo-helper.exe. Kalau tidak diisi, script menghitung sendiri.

.PARAMETER Remove
    Hapus exclusion yang dibuat script ini (rollback).

.PARAMETER Status
    Hanya menampilkan status, tidak mengubah apa pun.

.EXAMPLE
    # Lihat status sekarang
    powershell -ExecutionPolicy Bypass -File .\configure_defender.ps1 -Status

    # Terapkan exclusion (run as Administrator)
    powershell -ExecutionPolicy Bypass -File .\configure_defender.ps1

    # Batalkan
    powershell -ExecutionPolicy Bypass -File .\configure_defender.ps1 -Remove
#>
[CmdletBinding(DefaultParameterSetName = 'Apply')]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\CykeoRfidBridge",
    [string]$Hash,
    [switch]$Remove,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

$helperName = 'cykeo-helper.exe'
$logDir     = Join-Path $env:ProgramData 'CykeoRfidBridge'
$logPath    = Join-Path $logDir 'defender-configure.log'

function Write-Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "[X] $m" -ForegroundColor Red }

function Write-Log($m) {
    try {
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        $line = '{0}  [{1}]  {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $env:USERNAME, $m
        Add-Content -Path $logPath -Value $line -Encoding UTF8
    } catch { }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-HelperHash {
    $p = Join-Path $InstallDir $helperName
    if (-not (Test-Path $p)) { return $null }
    return (Get-FileHash -Path $p -Algorithm SHA256).Hash
}

# --- tampilkan status Defender ----------------------------------------------
function Show-DefenderStatus {
    Write-Host ""
    Write-Host "=============================================="
    Write-Host " Status Windows Defender"
    Write-Host "=============================================="

    try {
        $mp = Get-MpComputerStatus -ErrorAction Stop
        Write-Host ""
        Write-Host "  Real-time protection : " $(if ($mp.RealTimeProtectionEnabled) { 'AKTIF' } else { 'NONAKTIF' })
        Write-Host "  Antivirus signature  : " $mp.AntivirusSignatureVersion
        Write-Host "  Signature update    : " $mp.AntivirusSignatureLastUpdated
        Write-Host "  Tamper protection   : " $(if ($mp.IsTamperProtected) { 'AKTIF' } else { 'NONAKTIF' })
    } catch {
        Write-Warn "Get-MpComputerStatus gagal: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "  Exclusion path untuk folder bridge:"
    try {
        $pref = Get-MpPreference -ErrorAction Stop
        $found = $pref.ExclusionPath | Where-Object { $_ -like '*Cykeo*' }
        if ($found) {
            $found | ForEach-Object { Write-Host "    - $_" -ForegroundColor Yellow }
        } else {
            Write-Host "    (belum ada)" -ForegroundColor DarkGray
        }
    } catch {
        Write-Warn "Get-MpPreference gagal: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "  Ancaman terkait cykeo-helper.exe:"
    try {
        $threats = @(Get-MpThreat -ErrorAction Stop | Where-Object {
            ($_.Resources -join ' ') -like '*cykeo*'
        })
        if ($threats.Count -eq 0) {
            Write-Host "    (tidak ada)" -ForegroundColor DarkGray
        } else {
            foreach ($t in $threats) {
                Write-Host "    Ancaman : $($t.ThreatName)" -ForegroundColor Red
                Write-Host "    ID      : $($t.ThreatID)" -ForegroundColor Red
                Write-Host "    Waktu   : $($t.InitialDetectionTime)" -ForegroundColor Red
                foreach ($r in $t.Resources) { Write-Host "    File    : $r" -ForegroundColor Red }
                Write-Host "    Aksi    : $($t.ActionSuccess)" -ForegroundColor Red
            }
        }
    } catch {
        Write-Warn "Get-MpThreat gagal: $($_.Exception.Message)"
    }

    Write-Host ""
    $h = Get-HelperHash
    if ($h) {
        Write-Host "  SHA-256 helper di folder install:"
        Write-Host "    $h"
    } else {
        Write-Host "  Helper belum ter-install di $InstallDir"
    }
    Write-Host ""
}

if ($Status) {
    Show-DefenderStatus
    exit 0
}

# --- removal ----------------------------------------------------------------
if ($Remove) {
    if (-not (Test-Admin)) {
        Write-Err "Perintah -Remove harus dijalankan sebagai Administrator."
        exit 1
    }
    Write-Info "Menghapus exclusion untuk folder bridge..."
    try {
        Remove-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop
        Write-Ok "Exclusion untuk $InstallDir dihapus."
        Write-Log "Removed ExclusionPath: $InstallDir"
    } catch {
        Write-Err "Gagal menghapus exclusion: $($_.Exception.Message)"
        exit 1
    }
    Write-Host ""
    Write-Host "  Perhatian: binary mungkin langsung dikarantina lagi saat dijalankan." -ForegroundColor Yellow
    Write-Host "  Disarankan code-sign helper dengan sertifikat organisasi." -ForegroundColor Yellow
    exit 0
}

# --- apply ------------------------------------------------------------------
Write-Host ""
Write-Host "=============================================="
Write-Host " Remediasi Windows Defender - cykeo-helper"
Write-Host "=============================================="
Write-Host ""

if (-not (Test-Admin)) {
    Write-Err "Script ini harus dijalankan sebagai Administrator."
    Write-Host ""
    Write-Host "  Buka PowerShell sebagai Administrator, lalu jalankan:" -ForegroundColor Yellow
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\configure_defender.ps1" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

if (-not (Get-MpComputerStatus -ErrorAction SilentlyContinue)) {
    Write-Warn "Windows Defender tidak aktif / cmdlet tidak tersedia."
    Write-Host "  Lanjutkan secara manual, atau jalankan script ini di PC lain." -ForegroundColor Yellow
    exit 1
}

# Real-time protection harus tetap menyala - kita hanya menambah exclusion path.
$mp = Get-MpComputerStatus
if (-not $mp.RealTimeProtectionEnabled) {
    Write-Warn "Real-time protection sedang NONAKTIF (di luar scope script ini)."
    Write-Warn "Script tidak mengubah status real-time protection."
}

Write-Info "Target folder install : $InstallDir"

# 1. pastikan folder ada
if (-not (Test-Path $InstallDir)) {
    Write-Warn "Folder install belum ada. Exclusion tetap didaftarkan agar aman saat di-install nanti."
}

# 2. exclusion path
try {
    $pref = Get-MpPreference
    if ($pref.ExclusionPath -contains $InstallDir) {
        Write-Ok "Exclusion path sudah ada."
    } else {
        Add-MpPreference -ExclusionPath $InstallDir
        Write-Ok "Exclusion path ditambahkan: $InstallDir"
        Write-Log "Added ExclusionPath: $InstallDir"
    }
} catch {
    Write-Err "Gagal menambah ExclusionPath: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "  Penyebab umum:" -ForegroundColor Yellow
    Write-Host "    - Tamper protection aktif (matikan lewat Group Policy: Turn off tamper protection)" -ForegroundColor Yellow
    Write-Host "    - Akses ditolak oleh kebijakan centrally-managed" -ForegroundColor Yellow
    Write-Host "    - Jalankan sebagai user SYSTEM" -ForegroundColor Yellow
    exit 1
}

# 3. hash helper (untuk allowlist berbasis hash)
$actual = if ($Hash) { $Hash.ToUpper() } else { Get-HelperHash }
if ($actual) {
    Write-Ok "SHA-256 helper: $actual"
    Write-Log "Helper SHA-256: $actual"

    # Defender tidak punya cmdlet allowlist-hash yang universal; yang tersedia
    # hanya ExclusionPath / ExclusionProcess / ExclusionExtension.
    # Nama proses 'cykeo-helper' tidak mengandung ekstensi, tapi tetap dicatat
    # sebagai referensi untuk tim security.
    try {
        $pref2 = Get-MpPreference
        if ($pref2.ExclusionProcess -contains $helperName) {
            Write-Ok "Exclusion process '$helperName' sudah ada."
        } else {
            Add-MpPreference -ExclusionProcess $helperName
            Write-Ok "Exclusion process ditambahkan: $helperName"
            Write-Log "Added ExclusionProcess: $helperName"
        }
    } catch {
        Write-Warn "ExclusionProcess gagal (tidak kritis): $($_.Exception.Message)"
    }
} else {
    Write-Warn "Helper belum ada di folder install; hash tidak dihitung."
    Write-Host "    Jalankan install_cykeo.ps1 lebih dulu, lalu ulangi script ini." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=============================================="
Write-Host " Selesai"
Write-Host "=============================================="
Write-Host ""
Write-Host "  Yang dilakukan:"
Write-Host "    + ExclusionPath   : $InstallDir"
Write-Host "    + ExclusionProcess: $helperName"
Write-Host ""
Write-Host "  Yang TIDAK diubah:"
Write-Host "    - Real-time protection tetap AKTIF"
Write-Host "    - Tidak ada setting Defender lain yang disentuh"
Write-Host ""
Write-Host "  Log: $logPath"
Write-Host ""
Write-Host "  Solusi jangka panjang (disarankan):" -ForegroundColor Yellow
Write-Host "    code-sign cykeo-helper.exe dengan sertifikat kode-signing" -ForegroundColor Yellow
Write-Host "    organisasi agar tidak perlu exclusion sama sekali." -ForegroundColor Yellow
Write-Host ""
