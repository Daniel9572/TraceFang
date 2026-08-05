$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$webIndex = Join-Path $projectRoot "web\dist\index.html"

if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $webIndex)) {
    throw "The project is not installed. Run setup.cmd first."
}

if ([string]::IsNullOrWhiteSpace($env:JIN10_MCP_BEARER_TOKEN)) {
    Write-Warning "JIN10_MCP_BEARER_TOKEN is not set. MCP candles are unavailable; the local desktop quote source can still run."
}

$url = "http://127.0.0.1:8000"
$openBrowser = "Start-Sleep -Seconds 2; Start-Process '$url'"
Start-Process powershell.exe -ArgumentList "-NoProfile", "-Command", $openBrowser -WindowStyle Hidden
Push-Location $projectRoot
try {
    & $python -m uvicorn market_analysis.api:app --host 127.0.0.1 --port 8000
}
finally {
    Pop-Location
}
