$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $Root "backend")
foreach ($p in @("package", "function.zip", "_venv_pack")) {
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
New-Item -ItemType Directory -Path package | Out-Null
python -m venv _venv_pack
& .\_venv_pack\Scripts\pip.exe install -r requirements.txt -t package
Remove-Item -Recurse -Force _venv_pack
Copy-Item handler.py package\
Compress-Archive -Path "package\*" -DestinationPath "function.zip" -Force
Write-Host "Gerado: $(Join-Path (Get-Location) 'function.zip')"
