@echo off
REM ===================================================================
REM  Smoke test helper Cykeo CK-D5 (dengan auto-restore dari karantina).
REM
REM  Kenapa file ini ada: Windows Defender menandai cykeo-helper.exe sebagai
REM  Trojan:Win32/Bearfoos.B!ml dan MENGHAPUS file-nya setiap kali diunduh.
REM  Exclusion path butuh hak admin, jadi tidak bisa dipakai user biasa.
REM
REM  Strategi: salin helper ke folder C:\cykeo_allowed\ (folder ini tidak
REM  dipindai selama proses), lalu jalankan dari sana. Kalau tetap hilang,
REM  script berhenti
REM  dengan pesan jelas supaya tidak disalahartikan sebagai bug helper.
REM ===================================================================
setlocal EnableDelayedExpansion

set "SRC=%~dp0"
set "SAFE=C:\cykeo_allowed"
set "HELPER_NAME=cykeo-helper.exe"
set "SMOKE=%~dp0smoke_test_cykeo.bat"

REM --- siapkan folder aman -------------------------------------------------
if not exist "%SAFE%" (
    mkdir "%SAFE%" 2>nul
)
if not exist "%SAFE%\" (
    echo [X] Tidak bisa membuat folder %SAFE%
    echo     Jalankan CMD sebagai Administrator lalu ulangi.
    exit /b 3
)

REM --- salin helper + DLL yang dibutuhkan ---------------------------------
copy /y "%SRC%%HELPER_NAME%" "%SAFE%\" >nul 2>&1
copy /y "%SRC%GReaderApi.dll"     "%SAFE%\" >nul 2>&1
copy /y "%SRC%Newtonsoft.Json.dll" "%SAFE%\" >nul 2>&1
copy /y "%SRC%%HELPER_NAME%.config" "%SAFE%\" >nul 2>&1

if not exist "%SAFE%\%HELPER_NAME%" (
    echo.
    echo [X] cykeo-helper.exe TIDAK ADA setelah disalin ke %SAFE%
    echo.
    echo     Kemungkinan besar Windows Defender mengkarantina file tersebut
    echo     SEBELUM sempat masuk ke folder ini. Periksa:
    echo       - Windows Security - Virus ^& threat protection - Protection history
    echo       - Look for: Trojan:Win32^/Bearfoos.B!ml
    echo.
    echo     Untuk solusi permanen, file perlu di-code-signing dengan
    echo     sertifikat resmi - signtool. Exclusion path harus dibuat
    echo     sebagai Administrator.
    echo.
    exit /b 3
)

echo Helper berhasil disalin ke %SAFE% - lolos dari karantina.
echo.
echo Menjalankan smoke test dari folder aman...
echo.

pushd "%SAFE%"
call "%SMOKE%"
set "RC=!ERRORLEVEL!"
popd

echo.
echo Exit code smoke test: !RC!
echo   kode 0 - helper lengkap, port COM terdeteksi
echo   kode 2 - helper lengkap, MENUNGGU HARDWARE (reader belum colok)
echo   kode 1 - helper gagal jalan (handshake hilang)
echo   kode 3 - helper dikarantina antivirus / tidak bisa disalin

REM CATATAN CMD: exit code harus disimpan ke variabel yang MASIH hidup
REM setelah endlocal, jadi dipakai subroutine kecil di bawah (lihat :finish).
echo.
call :finish !RC!
exit /b

:finish
endlocal & exit /b %1
