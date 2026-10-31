$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
$frontendDir = Join-Path $repoRoot "frontend"

Set-Location $frontendDir
npm run dev
