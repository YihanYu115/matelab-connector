@echo off
setlocal
cd /d "%~dp0"
if exist "dist\MatElabConnector.exe" (
  start "" "dist\MatElabConnector.exe"
  exit /b 0
)
if not exist ".venv\Scripts\matelab-bridge.exe" (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python 3.11 or newer is required.
    pause
    exit /b 1
  )
  echo Installing MatElab Connector for the first run...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -e .
  if errorlevel 1 (
    echo Installation failed. Check the network and try again.
    pause
    exit /b 1
  )
)
if not exist ".venv\Scripts\matelab-connector.exe" (
  ".venv\Scripts\python.exe" -m pip install -e .
  if errorlevel 1 (
    echo Connector update failed. Check the network and try again.
    pause
    exit /b 1
  )
)
start "" ".venv\Scripts\matelab-connector.exe"
if errorlevel 1 pause
