$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & (Join-Path $PSScriptRoot "initialize-local-database.ps1")
    uv sync --python 3.13
    $packageManager = (Get-Content -LiteralPath (Join-Path $projectRoot "web\package.json") -Raw | ConvertFrom-Json).packageManager
    corepack $packageManager -C web install --frozen-lockfile
    corepack $packageManager -C web build
    Write-Host ""
    Write-Host "Setup completed. Run start.cmd next." -ForegroundColor Green
}
finally {
    Pop-Location
}
