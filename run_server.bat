@echo off
setlocal EnableExtensions
pushd "%~dp0"
set "PYTHONPATH=%CD%\src"

if exist ".venv\Scripts\python.exe" (
  call ".venv\Scripts\activate.bat"
)

where my-python-project >nul 2>&1 && (
  my-python-project serve %*
) || (
  if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m my_python_project serve %*
  ) else (
    where py >nul 2>&1 && py -3 -m my_python_project serve %* || python -m my_python_project serve %*
  )
)

popd
