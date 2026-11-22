@echo off
REM Circular Campus - Windows run. Open http://127.0.0.1:8000/ ; press Ctrl+C to stop.
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\bootstrap.py" run %*
if %ERRORLEVEL% NEQ 0 pause
