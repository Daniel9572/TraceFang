$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & (Join-Path $PSScriptRoot "initialize-local-database.ps1")
    uv sync --python 3.13
    corepack pnpm -C web install --frozen-lockfile
    corepack pnpm -C web build
    Write-Host ""
    Write-Host "Setup completed. Run start.cmd next." -ForegroundColor Green
}
finally {
    Pop-Location
}
