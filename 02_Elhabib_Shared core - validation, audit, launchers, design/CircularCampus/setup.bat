@echo off
REM Circular Campus - Windows setup (double-click, or run "setup.bat" in cmd/PowerShell)
cd /d "%~dp0"
set "PYCMD="
py -3.13 --version >nul 2>nul && set "PYCMD=py -3.13"
if not defined PYCMD py -3.12 --version >nul 2>nul && set "PYCMD=py -3.12"
if not defined PYCMD set "PYCMD=python"
%PYCMD% "scripts\bootstrap.py" setup %*
if errorlevel 1 (
  echo.
  echo Setup FAILED - read the message above. See README - Troubleshooting.
  pause
  exit /b 1
)
echo.
echo Setup finished. Next: double-click run.bat
pause
