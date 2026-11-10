@echo off
setlocal
title Edge Trader for Schwab Setup
cd /d "%~dp0"

echo Edge Trader for Schwab setup
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
    goto :fail
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Python 3.11 or newer is needed. This machine has:
    python --version
    goto :fail
)

echo [1/3] Creating the Python environment in .venv
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/3] Installing the packages (alpaca-py, schwabdev, FastAPI, uvicorn, pywebview)
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] Creating .env for your selected broker
if exist ".env" (
    echo       .env already exists, left as it is
) else (
    copy ".env.example" ".env" >nul
    echo       Created. Open .env and paste your selected broker's credentials.
)

echo.
echo Setup finished. Next:
echo   1. Put Alpaca Paper credentials in .env for the default paper account overview
echo   2. Or select Schwab, add its keys to .env, and run login.bat
echo   3. Run launch.bat, then use http://127.0.0.1:8770
echo Every install starts empty: Edge Trader only shows what the selected broker reports.
echo.
pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the problem above and run setup.bat again; finished steps are skipped or repeated safely.
pause
exit /b 1
