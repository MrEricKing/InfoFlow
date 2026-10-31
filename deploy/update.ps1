param(
  [string]$RollbackTag = "",
  [switch]$SkipBackup,
  [string]$HealthUrl = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DeployRoot = Resolve-Path $PSScriptRoot
$EnvPath = Join-Path $DeployRoot ".env"
$ComposePath = Join-Path $DeployRoot "docker-compose.yml"
$BackupDir = Join-Path $DeployRoot "backups"

function Read-EnvFile {
  param([string]$Path)

  $values = @{}
  if (-not (Test-Path $Path)) {
    return $values
  }

  foreach ($line in Get-Content -LiteralPath $Path) {
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) {
      continue
    }

    $separator = $trimmed.IndexOf("=")
    if ($separator -lt 1) {
      continue
    }

    $key = $trimmed.Substring(0, $separator).Trim()
    $value = $trimmed.Substring($separator + 1).Trim().Trim('"').Trim("'")
    $values[$key] = $value
  }

  return $values
}

function Invoke-Compose {
  param([string[]]$Arguments)

  & docker @("compose", "--env-file", $EnvPath, "-f", $ComposePath) @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
  }
}

function Test-ComposeServiceRunning {
  param([string]$ServiceName)

  $serviceId = & docker @("compose", "--env-file", $EnvPath, "-f", $ComposePath, "ps", "-q", $ServiceName)
  if ($LASTEXITCODE -ne 0) {
    return $false
  }

  return -not [string]::IsNullOrWhiteSpace($serviceId)
}

function Backup-Postgres {
  param([hashtable]$EnvValues)

  if (-not (Test-ComposeServiceRunning -ServiceName "postgres")) {
    Write-Host "PostgreSQL container is not running; skipping pre-update backup." -ForegroundColor Yellow
    return
  }

  New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
  $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $backupFile = Join-Path $BackupDir "infoflowaggregator_postgres_$timestamp.sql"
  $postgresUser = if ($EnvValues.ContainsKey("POSTGRES_USER")) { $EnvValues["POSTGRES_USER"] } else { "postgres" }
  $postgresDb = if ($EnvValues.ContainsKey("POSTGRES_DB")) { $EnvValues["POSTGRES_DB"] } else { "infoflowaggregator" }

  Write-Host "Writing PostgreSQL backup: $backupFile" -ForegroundColor Cyan
  & docker @("compose", "--env-file", $EnvPath, "-f", $ComposePath, "exec", "-T", "postgres", "pg_dump", "-U", $postgresUser, "-d", $postgresDb) |
    Out-File -FilePath $backupFile -Encoding utf8

  if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL backup failed with exit code $LASTEXITCODE"
  }
}

function Wait-ForHealth {
  param([string]$Url)

  Write-Host "Waiting for health check: $Url" -ForegroundColor Cyan
  $deadline = (Get-Date).AddSeconds(90)
  $lastError = $null

  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
      if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
        Write-Host "Health check passed." -ForegroundColor Green
        return
      }
    }
    catch {
      $lastError = $_.Exception.Message
    }

    Start-Sleep -Seconds 3
  }

  throw "Health check did not pass within 90 seconds. Last error: $lastError"
}

if (-not (Test-Path $EnvPath)) {
  throw "Missing .env beside update.ps1. Copy deploy/.env.example to .env and edit it first."
}
if (-not (Test-Path $ComposePath)) {
  throw "Missing docker-compose.yml beside update.ps1."
}

$envValues = Read-EnvFile -Path $EnvPath
$configuredRollbackTag = if ($envValues.ContainsKey("PREVIOUS_IMAGE_TAG")) { $envValues["PREVIOUS_IMAGE_TAG"] } else { "" }
$effectiveRollbackTag = if (-not [string]::IsNullOrWhiteSpace($RollbackTag)) { $RollbackTag } else { $configuredRollbackTag }
$appPort = if ($envValues.ContainsKey("APP_PORT")) { $envValues["APP_PORT"] } else { "5173" }
$effectiveHealthUrl = if (-not [string]::IsNullOrWhiteSpace($HealthUrl)) { $HealthUrl } else { "http://127.0.0.1:$appPort/api/health" }

Push-Location $DeployRoot

try {
  if (-not $SkipBackup) {
    Backup-Postgres -EnvValues $envValues
  }

  Write-Host "Pulling images..." -ForegroundColor Cyan
  Invoke-Compose -Arguments @("pull")

  Write-Host "Starting containers..." -ForegroundColor Cyan
  Invoke-Compose -Arguments @("up", "-d")

  Wait-ForHealth -Url $effectiveHealthUrl

  Write-Host "Current container status:" -ForegroundColor Cyan
  Invoke-Compose -Arguments @("ps")
  Write-Host "Update finished." -ForegroundColor Green
}
catch {
  Write-Host "Update failed: $($_.Exception.Message)" -ForegroundColor Red

  if (-not [string]::IsNullOrWhiteSpace($effectiveRollbackTag)) {
    Write-Host "Attempting rollback with IMAGE_TAG=$effectiveRollbackTag ..." -ForegroundColor Yellow
    $previousImageTag = $env:IMAGE_TAG
    try {
      $env:IMAGE_TAG = $effectiveRollbackTag
      Invoke-Compose -Arguments @("up", "-d", "app")
      Wait-ForHealth -Url $effectiveHealthUrl
      Write-Host "Rollback container start completed. If a database migration already ran, restore a dump from ./backups when schema rollback is required." -ForegroundColor Yellow
    }
    finally {
      $env:IMAGE_TAG = $previousImageTag
    }
  }
  else {
    Write-Host "No rollback tag configured. Set PREVIOUS_IMAGE_TAG in .env or run .\update.ps1 -RollbackTag <tag>." -ForegroundColor Yellow
  }

  throw
}
finally {
  Pop-Location
}
