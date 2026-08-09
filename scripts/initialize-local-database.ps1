$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$baseEnvFile = Join-Path $projectRoot ".env"
$localEnvFile = Join-Path $projectRoot ".env.local"

function Test-ConfiguredValue {
    param(
        [string]$Path,
        [string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    return [bool](Get-Content -LiteralPath $Path | Where-Object {
        $_ -match "^\s*$([regex]::Escape($Name))\s*=\s*\S+"
    } | Select-Object -First 1)
}

if (Test-Path -LiteralPath $localEnvFile) {
    return
}

if (Test-ConfiguredValue -Path $baseEnvFile -Name "MARKET_ANALYSIS_DATABASE_URL") {
    Write-Host "External PostgreSQL configuration found in .env; local database credentials were not generated."
    return
}

$databaseName = "market_analysis"
$databaseUser = "market_analysis"
$databasePort = "15432"
$databasePassword = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$databaseUrl = "postgresql://${databaseUser}:${databasePassword}@127.0.0.1:${databasePort}/${databaseName}"

@(
    "# Generated for this machine. Git ignores this file; do not share or commit it."
    "POSTGRES_DB=$databaseName"
    "POSTGRES_USER=$databaseUser"
    "POSTGRES_PASSWORD=$databasePassword"
    "MARKET_ANALYSIS_POSTGRES_PORT=$databasePort"
    "MARKET_ANALYSIS_DATABASE_URL=$databaseUrl"
) | Set-Content -LiteralPath $localEnvFile -Encoding utf8

Write-Host "Created private local PostgreSQL settings in .env.local." -ForegroundColor Green
