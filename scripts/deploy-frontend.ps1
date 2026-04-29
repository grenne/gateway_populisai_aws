$ErrorActionPreference = "Stop"
if (-not $env:PORTAL_BUCKET) {
  Write-Error "Defina `$env:PORTAL_BUCKET (ex.: populis-portal-static)"
}
$Root = Split-Path -Parent $PSScriptRoot
aws s3 sync (Join-Path $Root "frontend") "s3://$($env:PORTAL_BUCKET)/" --delete
Write-Host "Sync concluído para s3://$($env:PORTAL_BUCKET)/"
