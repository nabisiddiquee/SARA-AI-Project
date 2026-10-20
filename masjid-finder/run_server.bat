@echo off
rem ============================================================
rem  Masjid Finder - one-click start
rem    run_server.bat              start server + open browser
rem    run_server.bat /background  start server only (used by Windows startup)
rem    run_server.bat /rebuild     rebuild the website first, then start
rem ============================================================
setlocal
title Masjid Finder
cd /d "%~dp0"
set "PORT=8000"
set "URL=http://localhost:%PORT%"
set "MODE=%~1"

rem --- Already running? Just open it. ---------------------------------------
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
    echo Masjid Finder is already running at %URL%
    if /i not "%MODE%"=="/background" start "" "%URL%"
    exit /b 0
)

rem --- Python environment (first run only) -----------------------------------
if not exist "backend\.venv\Scripts\python.exe" (
    echo [1/3] Setting up Python environment, please wait...
    python -m venv "backend\.venv" || goto :error
    "backend\.venv\Scripts\python.exe" -m pip install -q -r "backend\requirements.txt" || goto :error
)
if not exist "backend\.env" copy "backend\.env.example" "backend\.env" >nul

rem --- Website build (first run, or with /rebuild) ----------------------------
if /i "%MODE%"=="/rebuild" if exist "frontend\dist" rmdir /s /q "frontend\dist"
if not exist "frontend\dist\index.html" (
    echo [2/3] Building the website...
    where npm >nul 2>nul || (echo Node.js is not installed: get it from https://nodejs.org & goto :error)
    pushd frontend
    if not exist node_modules (call npm install --no-fund --no-audit || (popd & goto :error))
    call npm run build || (popd & goto :error)
    popd
)

rem --- Start ------------------------------------------------------------------
echo [3/3] Starting Masjid Finder at %URL%
echo       Keep this window open. Press Ctrl+C to stop.
if /i not "%MODE%"=="/background" start "" cmd /c "timeout /t 4 /nobreak >nul & start "" "%URL%""
cd backend
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
exit /b %errorlevel%

:error
echo.
echo Something went wrong. Read the message above, fix it, then run this file again.
if /i not "%MODE%"=="/background" pause
exit /b 1
