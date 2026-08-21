@echo off
setlocal
cd /d "%~dp0"

python run.py
if errorlevel 9009 (
  echo.
  echo Python was not found on PATH.
  pause
  exit /b 1
)
endlocal
