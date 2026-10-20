@echo off
rem Stop starting Masjid Finder with Windows (and stop the running server).
setlocal
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MasjidFinder.vbs"
if exist "%VBS%" (del "%VBS%" & echo Auto-start is OFF.) else (echo Auto-start was already off.)
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
echo The server on port 8000 has been stopped.
pause
