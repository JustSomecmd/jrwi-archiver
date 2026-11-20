@echo off
rem Double-click to run the JRWI archiver. The first run sets itself up (takes a minute).
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python isn't installed yet. Get it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not set up Python. Install it from https://www.python.org/downloads/ and try again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet --upgrade -r requirements.txt
".venv\Scripts\python.exe" jrwi_archive.py %*
echo.
pause
