$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
$backendDir = Join-Path $repoRoot "backend"
$condaCommand = if ($env:CONDA_EXE) { $env:CONDA_EXE } else { "conda" }

Set-Location $backendDir
& $condaCommand run -n infoflowaggregator python -m uvicorn app.main:app --reload --port 8000

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
