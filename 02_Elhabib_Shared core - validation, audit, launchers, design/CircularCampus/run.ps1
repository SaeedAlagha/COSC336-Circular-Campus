# Circular Campus - Windows PowerShell run (Ctrl+C stops the server):
#   powershell -ExecutionPolicy Bypass -File .\run.ps1            (or add: --port 8010)
Set-Location -LiteralPath $PSScriptRoot
& ".\.venv\Scripts\python.exe" "scripts\bootstrap.py" run @args
