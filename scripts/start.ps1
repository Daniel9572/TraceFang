# Compatibility entry for callers of the original Windows script.
& (Join-Path $PSScriptRoot 'service.ps1') start @args
exit $LASTEXITCODE
