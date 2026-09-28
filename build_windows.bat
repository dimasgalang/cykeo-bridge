@echo off
REM ===========================================================================
REM Build Cykeo RFID Bridge Agent untuk Windows (jalankan di mesin WINDOWS).
REM
REM Output:  dist\cykeo_bridge.exe
REM
REM Prasyarat:
REM   - Python 3.11 (python.org, centang "Add to PATH")
REM   - Inno Setup 6 (untuk .exe installer, opsional)
REM
REM Usage:   build_windows.bat
REM ===========================================================================
setlocal

echo ==============================================
echo  Cykeo RFID Bridge - build untuk Windows
echo ==============================================
echo.

REM --- 1. Cek Python -------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python tidak ditemukan di PATH.
    echo         Install Python 3.11 lalu jalankan ulang script ini.
    exit /b 1
)

echo [1/4] Membersihkan build sebelumnya...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/4] Memasang dependensi build...
python -m pip install --upgrade pip >nul
python -m pip install pyinstaller
if errorlevel 1 (
    echo [ERROR] Gagal memasang pyinstaller.
    exit /b 1
)

echo [3/4] Build executable satu-file...
REM --paths . supaya paket cykeo_bridge ikut terbawa.
python -m PyInstaller --noconfirm --clean --onefile ^
    --name cykeo_bridge ^
    --paths . ^
    cykeo_bridge\__main__.py
if errorlevel 1 (
    echo [ERROR] Build PyInstaller gagal.
    exit /b 1
)

echo [4/4] Verifikasi...
if not exist dist\cykeo_bridge.exe (
    echo [ERROR] dist\cykeo_bridge.exe tidak terbentuk.
    exit /b 1
)
echo.
echo [OK] Build selesai: dist\cykeo_bridge.exe
echo.
echo Smoke test (simulator, tanpa hardware):
echo    dist\cykeo_bridge.exe check
echo.
echo Untuk membuat installer .exe, jalankan Inno Setup:
echo    "%%ProgramFiles(x86)%%\Inno Setup 6\ISCC.exe" installer\bridge_agent.iss
echo.

endlocal
