@echo off
REM ===================================================================
REM  Build paket installer Cykeo RFID Bridge (Windows client).
REM
REM  Keluaran:  dist\install_cykeo\  -> salin folder ini ke PC scanner
REM
REM  Prasyarat:
REM    - .NET Framework 4.8 (bawaan Windows 10/11)
REM    - Python 3.11 dengan PyInstaller
REM    - Folder helper sudah di-build (cykeo-helper.exe + GReaderApi.dll)
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================
echo  Build paket installer Cykeo Bridge
echo ==========================================

set "DIST=dist\install_cykeo"
set "HELPER=helper\bin\Release\net48\win-x86"
set "SDK=sdk"

if not exist "%HELPER%\cykeo-helper.exe" (
    echo [X] Helper belum di-build: %HELPER%\cykeo-helper.exe tidak ada
    echo     Build helper lebih dulu di mesin dev, lalu salin hasilnya ke folder helper\
    exit /b 1
)

echo [1/5] Membersihkan folder distribusi...
if exist "%DIST%" rmdir /s /q "%DIST%"
mkdir "%DIST%"

echo [2/5] Menyalin helper .NET...
copy /y "%HELPER%\cykeo-helper.exe"        "%DIST%\" > nul
if exist "%HELPER%\cykeo-helper.exe.config" copy /y "%HELPER%\cykeo-helper.exe.config" "%DIST%\" > nul
if exist "%HELPER%\GReaderApi.dll"     copy /y "%HELPER%\GReaderApi.dll"     "%DIST%\" > nul
if exist "%HELPER%\Newtonsoft.Json.dll" copy /y "%HELPER%\Newtonsoft.Json.dll" "%DIST%\" > nul
if exist "%SDK%\GReaderApi.dll"        copy /y "%SDK%\GReaderApi.dll"         "%DIST%\" > nul
if exist "%SDK%\Newtonsoft.Json.dll"   copy /y "%SDK%\Newtonsoft.Json.dll"    "%DIST%\" > nul

echo [3/5] Build bridge agent (PyInstaller)...
if exist "build\cykeo_bridge.spec" (
    pyinstaller --noconfirm --clean build\cykeo_bridge.spec
) else (
    pyinstaller --noconfirm --clean ^
        --name cykeo_bridge ^
        --onedir ^
        --console ^
        --paths cykeo_bridge ^
        cykeo_bridge\main.py
)
if errorlevel 1 (
    echo [X] PyInstaller gagal
    exit /b 1
)

xcopy /e /i /y "dist\cykeo_bridge" "%DIST%\bridge" > nul

echo [4/5] Menyalin skrip installer + smoke test...
copy /y "installer\install_cykeo.ps1"    "%DIST%\" > nul
copy /y "installer\smoke_test_helper.py" "%DIST%\" > nul
copy /y "installer\smoke_test_cykeo.bat" "%DIST%\" > nul

echo [5/5] Menyalin README...
copy /y "installer\README-INSTALL.txt" "%DIST%\" > nul

echo.
echo ==========================================
echo  Selesai. Folder distribusi:
echo    %DIST%
echo ==========================================
endlocal
