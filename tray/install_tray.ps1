<#
.SYNOPSIS
    Build CykeoTrayAgent.exe dari source lokal memakai csc.exe bawaan Windows.

.DESCRIPTION
    Tray agent dikompilasi di PC target, bukan didownload, supaya paket tidak
    perlu memuat binary siap pakai. Setiap Windows yang punya .NET Framework 4
    sudah punya csc.exe di:
        C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe

    Output wajib PE32 (x86) karena SDK Cykeo hanya 32-bit.

.PARAMETER InstallDir
    Folder tujuan install. Default: %LOCALAPPDATA%\CykeoRfidBridge

.PARAMETER SourceDir
    Folder yang memuat TrayAgent.cs. Default: folder script ini.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_tray.ps1
#>
[CmdletBinding()]
param(
    [string] $InstallDir = (Join-Path $env:LOCALAPPDATA 'CykeoRfidBridge'),
    [string] $SourceDir  = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "[tray] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[tray] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[tray] $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------- cari csc.exe
$csc = $null
foreach ($candidate in @(
        'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe',
        'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')) {
    if (Test-Path $candidate) { $csc = $candidate; break }
}
if (-not $csc) {
    # Cari lewat .NET Framework lain yang terpasang
    $found = Get-ChildItem 'C:\Windows\Microsoft.NET\Framework' `
                -Filter csc.exe -Recurse -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { $csc = $found.FullName }
}
if (-not $csc) {
    throw 'csc.exe tidak ditemukan. Pasang .NET Framework 4.x lalu ulangi.'
}
Write-Step "compiler: $csc"

# ---------------------------------------------------------------- cek source
$src = Join-Path $SourceDir 'TrayAgent.cs'
if (-not (Test-Path $src)) { throw "TrayAgent.cs tidak ditemukan di $SourceDir" }
Write-Step ("source: " + (Get-Item $src).Length + " byte")

# ---------------------------------------------------------------- build
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$out = Join-Path $InstallDir 'CykeoTrayAgent.exe'
$errFile = Join-Path $env:TEMP 'cykeo-tray-build.err'
Remove-Item $errFile -ErrorAction SilentlyContinue

Write-Step 'compile...'
& $csc /nologo /target:winexe /platform:x86 /optimize+ `
       "/out:$out" $src 2> $errFile
$code = $LASTEXITCODE

if ($code -ne 0) {
    Write-Host '[tray] compile GAGAL:' -ForegroundColor Red
    if (Test-Path $errFile) { Get-Content $errFile | ForEach-Object { Write-Host "       $_" } }
    exit $code
}
if (-not (Test-Path $out)) { throw 'compile melaporkan sukses tapi exe tidak ada' }

Write-Ok ("exe: " + (Get-Item $out).Length + " byte")

# ---------------------------------------------------------------- verifikasi
$hash = (Get-FileHash $out -Algorithm SHA256).Hash
Write-Step "sha256: $hash"

# PE32 = 0x014C. PE32+ (x64) = 0x8664 dan tidak kompatibel dengan SDK 32-bit.
$bytes = [IO.File]::ReadAllBytes($out)
$peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
$machine = [BitConverter]::ToUInt16($bytes, $peOffset + 4)
if ($machine -ne 0x014C) {
    Remove-Item $out -Force
    throw ("exe bukan PE32 x86 (machine=0x{0:X4}). SDK Cykeo butuh 32-bit." -f $machine)
}
Write-Ok 'arsitektur: PE32 (x86) OK'

# Ikon aplikasi agar agent mudah dikenali di system tray.
$icon = Join-Path $SourceDir 'cykeo.ico'
if (Test-Path $icon) {
    Write-Step ' pasang icon...'
    $out2 = Join-Path $InstallDir 'CykeoTrayAgent.icon.exe'
    & $csc /nologo /target:winexe /platform:x86 /optimize+ `
           "/win32icon:$icon" "/out:$out2" $src 2> $errFile
    if ($LASTEXITCODE -eq 0 -and (Test-Path $out2)) {
        Move-Item $out2 $out -Force
        Write-Ok 'icon terpasang'
    } else {
        Write-Warn2 'icon tidak bisa dipasang, exe tetap valid'
        Remove-Item $out2 -Force -ErrorAction SilentlyContinue
    }
}

Write-Ok 'selesai'
Write-Step "jalankan: $out"
exit 0
