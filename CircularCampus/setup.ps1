# Circular Campus - Windows PowerShell setup:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
Set-Location -LiteralPath $PSScriptRoot
$pyExe = "python"; $pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
  foreach ($v in @("3.13", "3.12")) {
    & py "-$v" --version *> $null
    if ($LASTEXITCODE -eq 0) { $pyExe = "py"; $pyArgs = @("-$v"); break }
  }
}
& $pyExe @pyArgs "scripts\bootstrap.py" setup @args
if ($LASTEXITCODE -ne 0) { Write-Host "Setup FAILED - read the message above." -ForegroundColor Red; exit 1 }
Write-Host "Setup finished. Next: powershell -ExecutionPolicy Bypass -File .\run.ps1" -ForegroundColor Green
