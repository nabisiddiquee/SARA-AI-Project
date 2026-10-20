@echo off
rem Start Masjid Finder silently (no window) every time you sign in to Windows.
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\MasjidFinder.vbs"
(
  echo Set sh = CreateObject^("WScript.Shell"^)
  echo sh.Run """%~dp0run_server.bat"" /background", 0, False
) > "%VBS%"
if exist "%VBS%" (
    echo Auto-start is ON. Masjid Finder will run in the background after every Windows sign-in.
    echo Open it any time at http://localhost:8000
    echo To turn it off, run autostart_off.bat
) else (
    echo Could not create "%VBS%".
)
pause
