$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & (Join-Path $PSScriptRoot "initialize-local-database.ps1")
    uv sync --python 3.13
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    & (Join-Path $projectRoot '.venv\Scripts\python.exe') -m tracefang.service install --no-browser
    if ($LASTEXITCODE -ne 0) { throw 'Managed runtime installation failed.' }
    Write-Host ""
    Write-Host "Setup completed. The backend is managed by Windows. Run start.cmd to open the app." -ForegroundColor Green
}
finally {
    Pop-Location
}
