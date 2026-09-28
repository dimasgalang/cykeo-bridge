@echo off
REM ===================================================================
REM  Smoke test helper Cykeo CK-D5 - tanpa server.
REM
REM  Versi CMD dari smoke_test_helper.py, untuk PC yang tidak punya
REM  Python atau kalau mau cek cepat dari Command Prompt.
REM
REM  Jalankan:
REM      smoke_test_cykeo.bat          (tanpa koneksi reader)
REM      smoke_test_cykeo.bat COM3      (plus coba konek COM3)
REM
REM  Catatan: helper WAJIB 32-bit (win-x86) karena GReaderApi.dll
REM  production juga 32-bit.
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "HELPER=cykeo-helper.exe"
set "RPORT=%~1"

echo ==========================================
echo  Smoke test helper Cykeo CK-D5
echo ==========================================
echo.

REM --- 0. Cek file wajib -----------------------------------------------------
set "MISSING="
for %%F in (%HELPER% GReaderApi.dll Newtonsoft.Json.dll) do (
    if not exist "%%F" set "MISSING=%%F "
)
if defined MISSING (
    echo [X] File hilang: %MISSING%
    echo     Paket tidak lengkap. Salin ulang seluruh folder.
    exit /b 1
)
echo [OK] File wajib lengkap.
echo.

REM --- 1. Cek .NET Framework 4.8 --------------------------------------------
set "NETREL="
for /f "tokens=3" %%V in ('reg query "HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" /v Release 2^>nul ^| find "Release"') do set "NETREL=%%V"
if defined NETREL (
    echo [OK] .NET Framework 4.x release %NETREL%
    if !NETREL! LSS 528040 (
        echo [!] Release ini belum 4.8. Helper tetap jalan, tapi 4.8 disarankan.
    )
) else (
    echo [!] .NET Framework 4.x tidak terdeteksi.
    echo     Install .NET Framework 4.8 bila helper gagal start.
)
echo.

REM --- 2. Smoke test lewat stdio JSON ---------------------------------------
echo Menguji helper via protokol stdio JSON...
echo.

set "REQFILE=%TEMP%\cykeo_smoke_%RANDOM%.jsonl"
set "OUTFILE=%TEMP%\cykeo_smoke_out_%RANDOM%.txt"

> "%REQFILE%" echo {"id":1,"cmd":"ping"}
>> "%REQFILE%" echo {"id":2,"cmd":"detect_ports"}
if not "!RPORT!"=="" >> "%REQFILE%" echo {"id":3,"cmd":"connect","com":"!RPORT!","baud":115200,"timeout":60}
>> "%REQFILE%" echo {"id":99,"cmd":"shutdown"}

"%HELPER%" < "%REQFILE%" > "%OUTFILE%" 2>&1
set "EXITCODE=%ERRORLEVEL%"

echo Balasan helper:
echo.
type "%OUTFILE%"
echo.

del /q "%REQFILE%" 2>nul

if "%EXITCODE%"=="0" (
    echo [OK] Helper selesai dengan exit code 0.
) else (
    echo [!] Helper exit code %EXITCODE%
)

REM Cek apakah ada handshake
findstr /c:"\"type\":\"hello\"" "%OUTFILE%" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [X] Handshake "hello" tidak ditemukan.
    echo.
    echo     Diagnosis:
    echo     1. .NET Framework 4.8 belum terpasang atau rusak
    echo     2. Bitness tidak cocok - helper dan GReaderApi.dll harus 32-bit
    echo     3. DLL lain dari SDK belum ada di folder ini
    echo     4. Antivirus memblokir eksekusi
    exit /b 1
)

echo.
echo [OK] Handshake berhasil.
echo.

REM --- 3. Apakah reader benar-benar terdeteksi? ---------------------------
REM CATATAN 1: di CMD, backslash BUKAN karakter escape, jadi /c:"\"ports\":[]"
REM            tidak akan pernah cocok.
REM CATATAN 2: pola harus 'ports.*\[\]' - terverifikasi di PC Test.
REM           _ports":[]_ tidak cocok dengan ports.:.\[\] (salah: butuh 1 karakter
REM            tambahan sebelum '['). Dan ports":[{"port":"COM3"}] TIDAK akan cocok
REM            karena tidak berisi '[]'.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "if((Get-Content -LiteralPath '%OUTFILE%' -Raw -ErrorAction SilentlyContinue) -match 'ports.*\[\]'){exit 0}else{exit 1}" >nul 2>&1
if not errorlevel 1 (
    echo [!] Helper hidup, TAPI tidak ada port COM yang terdeteksi.
    echo.
    echo     - reader CK-D5 belum terpasang / belum colok ke PC ini.
    echo     Periksa:
    echo       1. Kabel USB reader sudah tertancap?
    echo       2. Device Manager - Ports ^(COM ^& LPT^) - ada CK-D5?
    echo       3. Kalau COM3, jalankan ulang:  smoke_test_cykeo.bat COM3
    echo.
    echo     Status: helper .NET SIAP. Tes baca tag FISIK belum bisa dijalankan.
    echo.
    echo ==========================================
    echo  Smoke test selesai - MENUNGGU HARDWARE.
    echo ==========================================
    endlocal & exit /b 2
)

echo [!] Port COM terdeteksi. Lanjutkan: sambungkan reader lalu tes reading tag.
echo.
echo Baca balasan di atas:
echo    "ok":true          = perintah berhasil
echo    "ok":false / error = helper hidup tapi reader gagal
echo    ports             = daftar port COM yang terdeteksi
echo.
echo ==========================================
echo  Smoke test selesai.
echo ==========================================
endlocal
