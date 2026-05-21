<#
.SYNOPSIS
  Deploy delle Alembic revision pending + backfill su Neon prod.

.DESCRIPTION
  Script one-shot per applicare lo schema del branch corrente su Neon e
  (opzionalmente) eseguire il backfill contacts -> assets. Pensato per
  l'ambiente di sviluppo locale che ha un override `data/db_config.enc`
  puntato a Postgres locale: lo script lo nasconde temporaneamente cosi'
  i comandi `scripts/db.py` parlano con Neon, poi lo ripristina (try/finally).

  Pre-requisiti:
    - c:/tmp/neon_url.txt deve contenere la DSN Neon completa
    - python disponibile nel PATH (idealmente venv attivo)
    - branch git con le revision Alembic gia' committate

  Step:
    0. Backup data/db_config.enc -> .bak (se presente)
    1. db.py status (mostra alembic version locale + remoto)
    2. CHIEDE CONFERMA prima del promote
    3. db.py promote (alembic upgrade head + pytest locale + apply Neon)
    4. backfill --dry-run (counts attesi)
    5. CHIEDE CONFERMA prima del backfill reale
    6. backfill (vero) — solo se l'utente conferma
    7. (finally) ripristina data/db_config.enc

.PARAMETER SkipBackfill
  Esegue solo il promote alembic, salta il backfill (utile per release
  schema-only senza migrazione dati).

.EXAMPLE
  pwsh scripts/deploy_to_neon.ps1
  pwsh scripts/deploy_to_neon.ps1 -SkipBackfill
#>
[CmdletBinding()]
param(
    [switch]$SkipBackfill
)

$ErrorActionPreference = 'Stop'

# Risolvi root repo (questo script vive in scripts/, repo = ../)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

Write-Host ''
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host '  DEPLOY TO NEON PROD' -ForegroundColor Cyan
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host ('  Repo root: {0}' -f $RepoRoot)
Write-Host ''

# --- Pre-check Neon URL ---
$NeonUrlPath = 'c:\tmp\neon_url.txt'
if (-not (Test-Path $NeonUrlPath)) {
    Write-Host "[ERROR] Manca $NeonUrlPath. Mettici la DSN Neon completa e rilancia." -ForegroundColor Red
    exit 1
}
$NeonDsn = (Get-Content -Raw -Path $NeonUrlPath).Trim()
if (-not $NeonDsn) {
    Write-Host "[ERROR] $NeonUrlPath e' vuoto." -ForegroundColor Red
    exit 1
}
# Mostra DSN mascherato (sostituisce :password@ con :****@)
$DsnMasked = $NeonDsn -replace ':[^:@/]+@', ':****@'
Write-Host ('  Neon DSN: {0}' -f $DsnMasked)
Write-Host ''

# --- Step 0: backup db_config.enc ---
$ConfigEnc = 'data\db_config.enc'
$ConfigBak = 'data\db_config.enc.bak'
$RestoreNeeded = $false
if (Test-Path $ConfigEnc) {
    if (Test-Path $ConfigBak) {
        Write-Host "[ERROR] $ConfigBak esiste gia' (deploy precedente interrotto?)." -ForegroundColor Red
        Write-Host "        Verifica manualmente e rimuovi il .bak prima di rilanciare." -ForegroundColor Red
        exit 1
    }
    Write-Host '[0/6] Nascondo override locale data/db_config.enc -> .bak' -ForegroundColor Yellow
    Move-Item -Path $ConfigEnc -Destination $ConfigBak
    $RestoreNeeded = $true
} else {
    Write-Host '[0/6] (niente override locale da nascondere)' -ForegroundColor DarkGray
}

try {
    # Setta env var per tutti i sotto-comandi python
    $env:NEON_DATABASE_URL = $NeonDsn

    # --- Step 1: status ---
    Write-Host ''
    Write-Host '[1/6] Stato attuale (locale vs Neon)' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    python scripts/db.py status
    if ($LASTEXITCODE -ne 0) {
        throw "scripts/db.py status fallito (exit $LASTEXITCODE)"
    }

    # --- Step 2: conferma promote ---
    Write-Host ''
    Write-Host '[2/6] Pronto a fare PROMOTE sulle revision pending verso Neon.' -ForegroundColor Yellow
    Write-Host '      Lo script scripts/db.py promote chiedera'' lui stesso "yes" interattivamente.'
    $ans = Read-Host '      Proseguire? [y/N]'
    if ($ans -notmatch '^(y|yes|s|si)$') {
        Write-Host '      Annullato dall''utente.' -ForegroundColor DarkYellow
        return
    }

    # --- Step 3: promote ---
    Write-Host ''
    Write-Host '[3/6] python scripts/db.py promote' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    python scripts/db.py promote
    if ($LASTEXITCODE -ne 0) {
        throw "scripts/db.py promote fallito (exit $LASTEXITCODE)"
    }

    if ($SkipBackfill) {
        Write-Host ''
        Write-Host '[4-6/6] -SkipBackfill attivo: salto il backfill.' -ForegroundColor DarkYellow
        return
    }

    # --- Step 4: backfill dry-run ---
    Write-Host ''
    Write-Host '[4/6] backfill_contacts_to_assets.py --dry-run' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    python scripts/backfill_contacts_to_assets.py --dry-run
    if ($LASTEXITCODE -ne 0) {
        throw "backfill --dry-run fallito (exit $LASTEXITCODE)"
    }

    # --- Step 5: conferma backfill vero ---
    Write-Host ''
    Write-Host '[5/6] Pronto a eseguire il BACKFILL REALE su Neon.' -ForegroundColor Yellow
    Write-Host '      Modifica dati di produzione: COALESCE-safe ma irreversibile in 1 step.'
    $ans = Read-Host '      Proseguire? [y/N]'
    if ($ans -notmatch '^(y|yes|s|si)$') {
        Write-Host '      Annullato dall''utente (schema gia'' promosso, backfill saltato).' -ForegroundColor DarkYellow
        return
    }

    # --- Step 6: backfill vero ---
    Write-Host ''
    Write-Host '[6/6] backfill_contacts_to_assets.py (apply)' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    python scripts/backfill_contacts_to_assets.py
    if ($LASTEXITCODE -ne 0) {
        throw "backfill apply fallito (exit $LASTEXITCODE)"
    }

    Write-Host ''
    Write-Host '======================================================================' -ForegroundColor Green
    Write-Host '  DEPLOY COMPLETATO' -ForegroundColor Green
    Write-Host '======================================================================' -ForegroundColor Green
}
finally {
    # Ripristino override locale (ANCHE in caso di errore o ctrl-c)
    if ($RestoreNeeded -and (Test-Path $ConfigBak)) {
        Write-Host ''
        Write-Host '[cleanup] Ripristino data/db_config.enc' -ForegroundColor DarkGray
        Move-Item -Path $ConfigBak -Destination $ConfigEnc -Force
    }
    # Unset env var per non lasciarla a giro nella shell
    Remove-Item Env:\NEON_DATABASE_URL -ErrorAction SilentlyContinue
}
