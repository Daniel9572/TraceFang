param([ValidateSet('start', 'stop', 'status', 'update', 'install', 'restart', 'uninstall')]
      [string]$Action = 'start')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup.cmd to install this project first.' }
if ($Action -in @('install', 'update')) {
    & (Join-Path $PSScriptRoot 'initialize-local-database.ps1')
}
Push-Location $projectRoot
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    & $python -m tracefang.service $Action @args
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
