$ErrorActionPreference = 'Continue'
$csc = 'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe'
$d = Join-Path $env:USERPROFILE 'cyktray'
New-Item -ItemType Directory -Path $d -Force | Out-Null
Set-Location $d

$log = Join-Path $d 'build.txt'
Remove-Item $log -ErrorAction SilentlyContinue
$L = { param($s) Add-Content -Path $log -Value $s }

& $L "CSC=$csc exists=$(Test-Path $csc)"

$src = Join-Path $d 'TrayAgent.cs'
$tmp = $src + '.dl'
try {
    Invoke-WebRequest -Uri 'http://192.168.1.252:8777/cykeo-bridge/tray/TrayAgent.cs' -OutFile $tmp -UseBasicParsing -TimeoutSec 90
    Move-Item -LiteralPath $tmp -Destination $src -Force
    & $L ("SRC_OK bytes=" + (Get-Item $src).Length)
} catch {
    & $L ("SRC_FAIL " + $_.Exception.Message)
    & $L 'DONE=1'
    Invoke-WebRequest -Uri 'http://192.168.1.252:8778/traybuild.txt' -Method POST -InFile $log -ContentType 'text/plain' -UseBasicParsing -TimeoutSec 30 | Out-Null
    return
}

$exe = Join-Path $d 'CykeoTrayAgent.exe'
Remove-Item $exe -ErrorAction SilentlyContinue

$args = @(
    '/nologo', '/target:winexe', '/platform:x86', '/optimize+',
    ('/out:' + $exe),
    '/reference:System.dll',
    '/reference:System.Windows.Forms.dll',
    '/reference:System.Drawing.dll',
    $src
)

$out = & $csc $args 2>&1
$code = $LASTEXITCODE
& $L ("CSC_EXIT=$code")
foreach ($line in $out) { & $L ("  CSC: " + $line) }

if (Test-Path $exe) {
    $f = Get-Item $exe
    & $L ("EXE_OK bytes=" + $f.Length)
    $b = [System.IO.File]::ReadAllBytes($exe)
    $peOff = [BitConverter]::ToInt32($b, 0x3C)
    $mach = [BitConverter]::ToUInt16($b, $peOff + 4)
    & $L ("MACHINE=0x{0:X4}" -f $mach)
    if ($mach -eq 0x014C) { & $L 'BITNESS=PE32 (x86) OK' } else { & $L 'BITNESS=UNEXPECTED' }
    & $L ("SHA256=" + (Get-FileHash $exe -Algorithm SHA256).Hash)
    & $L ("VER=" + $f.VersionInfo.FileVersion)
} else {
    & $L 'EXE_MISSING'
}
& $L 'DONE=1'

try {
    Invoke-WebRequest -Uri 'http://192.168.1.252:8778/traybuild.txt' -Method POST -InFile $log -ContentType 'text/plain' -UseBasicParsing -TimeoutSec 30 | Out-Null
    Write-Host 'UPLOAD OK' -ForegroundColor Green
} catch {
    Write-Host ('UPLOAD FAIL ' + $_.Exception.Message) -ForegroundColor Red
}
