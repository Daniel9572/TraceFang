$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$webIndex = Join-Path $projectRoot "web\dist\index.html"
$localEnvFile = Join-Path $projectRoot ".env.local"

if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $webIndex)) {
    throw "The project is not installed. Run setup.cmd first."
}

& (Join-Path $PSScriptRoot "initialize-local-database.ps1")
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $localEnvFile)) {
        docker compose --env-file $localEnvFile up -d postgres nats
    }
    elseif ($LASTEXITCODE -ne 0) {
        Write-Warning "Docker Desktop is not running. PostgreSQL and NATS will remain unavailable until it starts."
    }
}
else {
    Write-Warning "Docker is not installed. Configure TRACEFANG_DATABASE_URL for an external PostgreSQL instance."
}

$url = "http://127.0.0.1:8000"
$openBrowser = "Start-Sleep -Seconds 2; Start-Process '$url'"
Start-Process powershell.exe -ArgumentList "-NoProfile", "-Command", $openBrowser -WindowStyle Hidden
Push-Location $projectRoot
try {
    $env:PYTHONPATH = Join-Path $projectRoot "src"
    & $python -m uvicorn tracefang.api:app --host 127.0.0.1 --port 8000
}
finally {
    Pop-Location
}
