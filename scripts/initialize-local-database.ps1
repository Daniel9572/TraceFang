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
    if (-not (Test-ConfiguredValue -Path $localEnvFile -Name 'TRACEFANG_NATS_URL') -and
        -not (Test-ConfiguredValue -Path $baseEnvFile -Name 'TRACEFANG_NATS_URL')) {
        $natsPort = '14222'
        $portLine = Get-Content -LiteralPath $localEnvFile | Where-Object {
            $_ -match '^\s*TRACEFANG_NATS_PORT\s*=\s*\d+\s*$'
        } | Select-Object -First 1
        if ($portLine) { $natsPort = ($portLine -split '=', 2)[1].Trim() }
        Add-Content -LiteralPath $localEnvFile -Value "`nTRACEFANG_NATS_URL=nats://127.0.0.1:$natsPort" -Encoding utf8
    }
    return
}

if (Test-ConfiguredValue -Path $baseEnvFile -Name "TRACEFANG_DATABASE_URL") {
    throw 'External database configuration detected. Create .env.local with the intended database and NATS settings before managed installation.'
}

$databaseName = "tracefang"
$databaseUser = "tracefang"
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
    "TRACEFANG_POSTGRES_PORT=$databasePort"
    "TRACEFANG_DATABASE_URL=$databaseUrl"
    "TRACEFANG_NATS_URL=nats://127.0.0.1:14222"
) | Set-Content -LiteralPath $localEnvFile -Encoding utf8

Write-Host "Created private local PostgreSQL settings in .env.local." -ForegroundColor Green
