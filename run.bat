@echo off
setlocal EnableExtensions
pushd "%~dp0"

set "PYTHONPATH=%CD%\src"

if "%~1"=="" (
  set "DEFAULT_CMD=hello --name Friend"
) else (
  set "DEFAULT_CMD=%*"
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m my_python_project %DEFAULT_CMD%
) else (
  where py >nul 2>&1 && (
    py -3 -m my_python_project %DEFAULT_CMD%
  ) || (
    python -m my_python_project %DEFAULT_CMD%
  )
)

set "EXITCODE=%ERRORLEVEL%"
popd
exit /b %EXITCODE%
