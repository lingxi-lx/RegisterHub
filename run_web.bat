@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RegisterHub Web Workbench (:18886)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.10+ not found in PATH. Please install Python first: https://www.python.org/
    pause
    exit /b 1
)

if not exist "config.json" (
    if exist "config.example.json" (
        copy /Y "config.example.json" "config.json" >nul
        echo [INFO] Initialized config.json from config.example.json
    )
)

python -c "import playwright, requests" >nul 2>nul
if errorlevel 1 (
    echo [WARN] Missing optional browser dependencies. Web Workbench will still start,
    echo        run `pip install -r requirements.txt && playwright install chromium` before running browser adapters.
)

echo [*] Starting RegisterHub Web Workbench at http://127.0.0.1:18886 ...
python main.py --web --port 18886 --open
pause
