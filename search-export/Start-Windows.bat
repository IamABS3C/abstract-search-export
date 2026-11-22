@echo off
rem Abstract Search and Export: double-click to open the browser app.
rem Keep this window open while you use the app. Close it to stop.
setlocal
cd /d "%~dp0"
if exist "app\abstract_search.py" cd app
if not exist "abstract_search.py" (
  echo Start-Windows.bat cannot find the rest of the app next to it.
  echo This happens when it is run from inside the zip. Close this window, right-click the zip,
  echo choose Extract All, and then double-click Start-Windows.bat in the extracted folder.
  pause
  exit /b 1
)
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Abstract Search and Export needs Python 3.8 or newer, and it was not found.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup.
  echo Then double-click Start-Windows.bat again.
  pause
  exit /b 1
)
if "%~1"=="" (
  %PY% abstract_search.py --web
) else (
  %PY% abstract_search.py %*
)
if errorlevel 1 pause
endlocal
