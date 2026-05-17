<#
.SYNOPSIS
  Ripristina il DB locale `agentscraper_dev` da un dump fresco di Neon prod.

.DESCRIPTION
  Pipeline read-from-Neon + write-locale, usando il container docker
  `agentscraper-postgres-dev` per pg_dump e pg_restore (cosi' non serve
  installare i tool postgres su Windows).

  Step:
    0. Pre-check: file neon_url.txt esistente, container docker su, no app live.
    1. Conta righe su Neon (sanity check).
    2. CHIEDE CONFERMA prima di scrivere sul locale.
    3. pg_dump da Neon in formato custom (-Fc) -> file dentro il container.
    4. Conta righe sul locale PRIMA (cosi' il log mostra il delta).
    5. pg_restore su `agentscraper_dev` con --clean --if-exists --no-owner.
       Sostituisce TUTTE le tabelle del schema public.
    6. Conta righe sul locale DOPO (verifica).
    7. Pulisce il file dump dentro il container.

  Cosa NON fa:
    - Non tocca Neon (read-only).
    - Non modifica data/db_config.enc.
    - Non sovrascrive c:/tmp/neon_url.txt.

  Tempo atteso: 1-3 minuti su ~6k asset / 5881 contacts.

.PARAMETER KeepDump
  Non cancella il file dump dentro il container al termine (utile per
  debug o per fare un secondo restore senza ri-dumpare).

.EXAMPLE
  pwsh scripts/restore_dev_from_neon.ps1
#>
[CmdletBinding()]
param(
    [switch]$KeepDump
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

Write-Host ''
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host '  RESTORE agentscraper_dev FROM Neon prod' -ForegroundColor Cyan
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host ('  Repo root: {0}' -f $RepoRoot)

# --- Pre-check 1: file Neon URL ---
$NeonUrlPath = 'c:\tmp\neon_url.txt'
if (-not (Test-Path $NeonUrlPath)) {
    Write-Host "[ERROR] Manca $NeonUrlPath. Crea il file con la DSN Neon." -ForegroundColor Red
    exit 1
}
$NeonDsn = (Get-Content -Raw -Path $NeonUrlPath).Trim()
if (-not $NeonDsn) {
    Write-Host "[ERROR] $NeonUrlPath e' vuoto." -ForegroundColor Red
    exit 1
}
$DsnMasked = $NeonDsn -replace ':[^:@/]+@', ':****@'
Write-Host ('  Neon DSN: {0}' -f $DsnMasked)

# --- Pre-check 2: container docker locale up ---
$ContainerName = 'agentscraper-postgres-dev'
$DockerCheck = docker inspect -f '{{.State.Running}}' $ContainerName 2>$null
if ($LASTEXITCODE -ne 0 -or $DockerCheck -ne 'true') {
    Write-Host "[ERROR] Container docker '$ContainerName' non e' in esecuzione." -ForegroundColor Red
    Write-Host "        Avvialo con: docker compose -f docker-compose.dev.yml up -d" -ForegroundColor Yellow
    exit 1
}
Write-Host ('  Container docker: {0} (running)' -f $ContainerName)

# --- Pre-check 3: avviso se l'app sembra in esecuzione ---
$AppRunning = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
if ($AppRunning) {
    Write-Host '[WARN] La porta 8000 sembra occupata (app in esecuzione?).' -ForegroundColor Yellow
    Write-Host '       Il restore DROPPERA le tabelle: connessioni attive verranno killate.' -ForegroundColor Yellow
    $ans = Read-Host '       Procedere comunque? [y/N]'
    if ($ans -notmatch '^(y|yes|s|si)$') {
        Write-Host '       Annullato. Chiudi l''app e rilancia.' -ForegroundColor DarkYellow
        exit 0
    }
}

# Versioni pg_dump / pg_restore disponibili nel container
$PgVersionRaw = docker exec $ContainerName pg_dump --version 2>&1
$PgVersion = $PgVersionRaw -replace 'pg_dump \(PostgreSQL\) ', ''
Write-Host ('  pg_dump version (container): {0}' -f $PgVersion)

# Estrai major version pg_dump (es. "16.14" -> 16, "17.5" -> 17)
$PgMajor = 0
if ($PgVersion -match '^(\d+)') { $PgMajor = [int]$Matches[1] }

# Versione server Neon: query rapida via psycopg
$probeNeonVer = @'
import os, psycopg
with psycopg.connect(os.environ['NEON_DSN_TMP']) as c, c.cursor() as cur:
    cur.execute('SHOW server_version')
    print(cur.fetchone()[0])
'@
$env:NEON_DSN_TMP = $NeonDsn
$NeonVersion = (python -c $probeNeonVer 2>$null).Trim()
$NeonMajor = 0
if ($NeonVersion -match '^(\d+)') { $NeonMajor = [int]$Matches[1] }
Remove-Item Env:\NEON_DSN_TMP -ErrorAction SilentlyContinue
Write-Host ('  Neon server version:         {0}' -f $NeonVersion)

# Check compatibilita': pg_dump major >= Neon server major
if ($PgMajor -gt 0 -and $NeonMajor -gt 0 -and $PgMajor -lt $NeonMajor) {
    Write-Host ''
    Write-Host '[ERROR] Version mismatch: pg_dump v' -NoNewline -ForegroundColor Red
    Write-Host -NoNewline $PgMajor -ForegroundColor Red
    Write-Host ' non puo'' leggere server Neon v' -NoNewline -ForegroundColor Red
    Write-Host $NeonMajor -ForegroundColor Red
    Write-Host ''
    Write-Host '   Il container locale e'' su una versione vecchia di Postgres.' -ForegroundColor Yellow
    Write-Host '   Soluzione: aggiorna il container.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '   AVVISO: aggiornare il major Postgres distrugge i dati locali esistenti' -ForegroundColor Yellow
    Write-Host '   (volume docker non auto-upgradabile). Visto che agentscraper_dev e''' -ForegroundColor Yellow
    Write-Host '   gia'' vuoto, il restore subito dopo ripopolera'' tutto da Neon.' -ForegroundColor Yellow
    Write-Host ''
    $ans = Read-Host '   Eseguire ORA: docker compose down -v + up -d (con la nuova versione)? [y/N]'
    if ($ans -notmatch '^(y|yes|s|si)$') {
        Write-Host '   Annullato. Esegui manualmente:' -ForegroundColor DarkYellow
        Write-Host '     docker compose -f docker-compose.dev.yml down -v' -ForegroundColor DarkYellow
        Write-Host '     docker compose -f docker-compose.dev.yml up -d' -ForegroundColor DarkYellow
        Write-Host '   poi rilancia questo script.' -ForegroundColor DarkYellow
        exit 1
    }

    Write-Host ''
    Write-Host '   -> docker compose down -v ...' -ForegroundColor Cyan
    docker compose -f docker-compose.dev.yml down -v
    if ($LASTEXITCODE -ne 0) { throw "docker compose down -v fallito (exit $LASTEXITCODE)" }

    # IMPORTANTE: docker-compose.dev.yml usa un BIND MOUNT (./data/postgres-dev),
    # NON un named volume. `down -v` non lo cancella. Postgres 17 rifiuta di
    # avviarsi su dati creati da Postgres 16 ('database files are incompatible
    # with server'). Cancello manualmente la directory.
    $PgDataDir = 'data\postgres-dev'
    if (Test-Path $PgDataDir) {
        Write-Host ('   -> wipe bind-mount: {0} ...' -f $PgDataDir) -ForegroundColor Cyan
        try {
            Remove-Item -Path $PgDataDir -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Host ('   [ERROR] Impossibile cancellare {0}: {1}' -f $PgDataDir, $_.Exception.Message) -ForegroundColor Red
            Write-Host '   Probabilmente il container precedente sta ancora bloccando dei file.' -ForegroundColor Yellow
            Write-Host '   Esegui manualmente:' -ForegroundColor Yellow
            Write-Host '     docker compose -f docker-compose.dev.yml down -v' -ForegroundColor DarkYellow
            Write-Host ('     Remove-Item -Path {0} -Recurse -Force' -f $PgDataDir) -ForegroundColor DarkYellow
            Write-Host '     docker compose -f docker-compose.dev.yml up -d' -ForegroundColor DarkYellow
            Write-Host '   poi rilancia questo script.' -ForegroundColor Yellow
            throw "wipe bind-mount fallito"
        }
    }

    Write-Host '   -> docker compose up -d ...' -ForegroundColor Cyan
    docker compose -f docker-compose.dev.yml up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up -d fallito (exit $LASTEXITCODE)" }

    Write-Host '   -> wait healthcheck ...' -ForegroundColor DarkGray
    $tries = 0
    while ($tries -lt 30) {
        Start-Sleep -Seconds 2
        $tries++
        $health = docker inspect -f '{{.State.Health.Status}}' $ContainerName 2>$null
        if ($health -eq 'healthy') {
            Write-Host '   -> container healthy.' -ForegroundColor Green
            break
        }
    }
    if ($health -ne 'healthy') {
        throw "Container non e'' diventato healthy dopo 60s (status=$health)"
    }
    # Re-check versione dopo upgrade
    $PgVersionRaw = docker exec $ContainerName pg_dump --version 2>&1
    $PgVersion = $PgVersionRaw -replace 'pg_dump \(PostgreSQL\) ', ''
    Write-Host ('   -> nuova pg_dump version: {0}' -f $PgVersion)
}

# --- Step 1: SELECT COUNT su Neon (sanity check) ---
Write-Host ''
Write-Host '[1/7] Sanity check Neon (read-only)' -ForegroundColor Cyan
Write-Host '----------------------------------------------------------------------'
$env:NEON_DSN_TMP = $NeonDsn
$probeScript = @'
import os, psycopg
dsn = os.environ['NEON_DSN_TMP']
with psycopg.connect(dsn) as c, c.cursor() as cur:
    cur.execute('SELECT current_database()')
    print('  connected to:', cur.fetchone()[0])
    for tbl in ['tenants', 'users', 'assets', 'contacts', 'asset_tags', 'tasks', 'jobs', 'threads', 'messages']:
        try:
            cur.execute(f'SELECT COUNT(*) FROM {tbl}')
            print(f'    {tbl}: {cur.fetchone()[0]}')
        except Exception as e:
            print(f'    {tbl}: ERROR {e}')
'@
python -c $probeScript
if ($LASTEXITCODE -ne 0) {
    Remove-Item Env:\NEON_DSN_TMP -ErrorAction SilentlyContinue
    throw "Sanity check Neon fallito (exit $LASTEXITCODE)"
}

# --- Step 2: conferma esplicita prima di scrivere locale ---
Write-Host ''
Write-Host '[2/7] Conferma' -ForegroundColor Yellow
Write-Host '----------------------------------------------------------------------'
Write-Host '  Procedendo: TUTTE le tabelle di agentscraper_dev verranno droppate'
Write-Host '  e ricreate dal dump di Neon. I dati attuali del locale (se ce ne sono)'
Write-Host '  saranno persi. Operazione non distruttiva su Neon (solo lettura).'
$ans = Read-Host '  Procedere? [y/N]'
if ($ans -notmatch '^(y|yes|s|si)$') {
    Remove-Item Env:\NEON_DSN_TMP -ErrorAction SilentlyContinue
    Write-Host '  Annullato.' -ForegroundColor DarkYellow
    exit 0
}

try {
    # --- Step 3: pg_dump da Neon dentro il container ---
    Write-Host ''
    Write-Host '[3/7] pg_dump da Neon -> /tmp/neon_backup.dump (nel container)' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    # Passo la DSN al container via env var (sicuro, non finisce in ps).
    docker exec -e PGSSLMODE=require -e NEON_DSN_TMP=$NeonDsn $ContainerName `
        bash -c 'pg_dump "$NEON_DSN_TMP" -Fc --no-owner --no-acl -f /tmp/neon_backup.dump'
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump fallito (exit $LASTEXITCODE)"
    }
    # Dimensione del dump
    $dumpSize = docker exec $ContainerName stat -c '%s' /tmp/neon_backup.dump
    $dumpSizeMb = [math]::Round([int64]$dumpSize / 1024 / 1024, 2)
    Write-Host ('  Dump pronto: {0} MB' -f $dumpSizeMb)

    # --- Step 4: count locale PRIMA (delta) ---
    Write-Host ''
    Write-Host '[4/7] Count locale PRIMA del restore' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    $preCountSql = @'
SELECT 'tenants' AS t, COUNT(*) FROM tenants
UNION ALL SELECT 'users', COUNT(*) FROM users
UNION ALL SELECT 'assets', COUNT(*) FROM assets
UNION ALL SELECT 'contacts', COUNT(*) FROM contacts
UNION ALL SELECT 'asset_tags', COUNT(*) FROM asset_tags
UNION ALL SELECT 'tasks', COUNT(*) FROM tasks
UNION ALL SELECT 'jobs', COUNT(*) FROM jobs
UNION ALL SELECT 'threads', COUNT(*) FROM threads
UNION ALL SELECT 'messages', COUNT(*) FROM messages;
'@
    docker exec -e PGPASSWORD=postgres $ContainerName `
        psql -U postgres -d agentscraper_dev -c $preCountSql 2>$null
    # NB: non blocco su exit code (se schema parziale, psql puo' fallire)

    # --- Step 5: pg_restore con --clean --if-exists ---
    Write-Host ''
    Write-Host '[5/7] pg_restore -> agentscraper_dev (clean + restore)' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    docker exec -e PGPASSWORD=postgres $ContainerName `
        pg_restore -U postgres -d agentscraper_dev `
        --clean --if-exists --no-owner --no-acl `
        --verbose /tmp/neon_backup.dump 2>&1 | Select-Object -Last 30
    # pg_restore puo' uscire con warning (objects gia' assenti, ecc) -> exit 1 ma OK
    Write-Host ('  pg_restore exit code: {0} (warning accettati, errori fatali stoppano la pipe)' -f $LASTEXITCODE)

    # --- Step 6: count locale DOPO ---
    Write-Host ''
    Write-Host '[6/7] Count locale DOPO il restore (verifica)' -ForegroundColor Cyan
    Write-Host '----------------------------------------------------------------------'
    docker exec -e PGPASSWORD=postgres $ContainerName `
        psql -U postgres -d agentscraper_dev -c $preCountSql
    if ($LASTEXITCODE -ne 0) {
        throw "Verifica post-restore fallita (exit $LASTEXITCODE)"
    }

    Write-Host ''
    Write-Host '======================================================================' -ForegroundColor Green
    Write-Host '  RESTORE COMPLETATO' -ForegroundColor Green
    Write-Host '======================================================================' -ForegroundColor Green
    Write-Host '  Confronta i count sopra con quelli letti su Neon allo step [1/7].'
    Write-Host '  Se quadrano: restore OK, puoi riaprire l''app.'
    Write-Host ''
}
finally {
    # --- Step 7: cleanup dump (a meno di -KeepDump) ---
    if (-not $KeepDump) {
        Write-Host '[7/7] Cleanup dump nel container' -ForegroundColor DarkGray
        docker exec $ContainerName rm -f /tmp/neon_backup.dump 2>$null
    } else {
        Write-Host '[7/7] -KeepDump attivo: /tmp/neon_backup.dump rimane nel container.' -ForegroundColor DarkYellow
    }
    # Pulizia env var
    Remove-Item Env:\NEON_DSN_TMP -ErrorAction SilentlyContinue
}
