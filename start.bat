@echo off
REM ================================================================
REM  Vehicle Monitoring Server — start script
REM  - Aktifkan venv lokal
REM  - Cek dependency dasar (flask)
REM  - Jalankan app.py
REM ================================================================

setlocal
cd /d "%~dp0"

echo.
echo === Vehicle Monitoring Server ===
echo.

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Folder venv tidak ditemukan di "%CD%\venv"
    echo Buat dulu: python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"

REM Cek Flask sudah ke-install belum (sekali per run)
python -c "import flask" 2>nul
if errorlevel 1 (
    echo [INFO] Dependency belum lengkap. Install dari requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Gagal install dependency.
        pause
        exit /b 1
    )
)

echo [OK] venv aktif. Menjalankan server...
echo URL: http://127.0.0.1:5000   (LAN: http://0.0.0.0:5000)
echo Tekan CTRL+C untuk berhenti.
echo.

python app.py

endlocal
pause
