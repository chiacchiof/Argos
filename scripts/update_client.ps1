# update_client.ps1 — aggiornamento incrementale di Argos.
#
# Assume che hai già estratto/sovrascritto il nuovo zip nella cartella corrente.
# Il file .env e la cartella data\ vengono preservati (non li tocchiamo).
#
# Eseguilo dalla cartella radice del progetto:
#   .\scripts\update_client.ps1
#
# Cosa fa:
#  1. Backup di sicurezza di data\ (in data_backup_<timestamp>\)
#  2. pip install -e . (aggiorna le dipendenze cambiate)
#  3. playwright install chromium (in caso di update Chromium)
#  4. Verifica versione installata
#  5. Ricorda di fermare e rilanciare l'app

$ErrorActionPreference = "Stop"

function Write-Step($n, $total, $msg) {
    Write-Host ""
    Write-Host "[$n/$total] $msg" -ForegroundColor Cyan
}
function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "  [ERROR] $msg" -ForegroundColor Red }

$TOTAL = 5

if (-not (Test-Path ".\pyproject.toml")) {
    Write-Err "Esegui dalla cartella radice del progetto (deve contenere pyproject.toml)."
    exit 1
}
if (-not (Test-Path ".\.venv")) {
    Write-Err ".venv non trovata. Forse questa non e' un'installazione di Argos, o e' la primissima volta."
    Write-Host "  In quel caso usa: .\scripts\install_client.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Argos - Update client" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# -------- Step 1: backup --------
Write-Step 1 $TOTAL "Backup di data\ (precauzionale)"
if (Test-Path ".\data") {
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupDir = ".\data_backup_$ts"
    # Solo file/cartelle piccole (.env, db_config.enc), NON i 1+ GB di results/sessions
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    foreach ($f in @(".env", "data\db_config.enc")) {
        if (Test-Path $f) {
            $dest = Join-Path $backupDir (Split-Path $f -Leaf)
            Copy-Item $f $dest -Force
        }
    }
    Write-Ok "Backup config salvato in $backupDir\"
} else {
    Write-Warn "Cartella data\ non trovata, niente backup."
}

# Attivo venv
. .\.venv\Scripts\Activate.ps1

# -------- Step 2: pip install --------
Write-Step 2 $TOTAL "Aggiornamento dipendenze (pip install -e .)"
Write-Host "  In genere 1-3 minuti (solo i pacchetti cambiati). Output pip in corso:" -ForegroundColor Gray
python -m pip install --upgrade pip --quiet
python -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Err "pip install fallito (exit $LASTEXITCODE). Vedi l'errore qui sopra."
    exit 1
}
Write-Ok "Dipendenze aggiornate."

# -------- Step 3: playwright --------
Write-Step 3 $TOTAL "Verifica/aggiornamento Chromium (Playwright)"
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Warn "playwright install ha riportato un errore (exit $LASTEXITCODE). Puoi rilanciarlo dopo con:"
    Write-Host "    .\.venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Gray
}
Write-Ok "Chromium aggiornato (se necessario)."

# -------- Step 4: version check --------
Write-Step 4 $TOTAL "Versione installata"
$ver = python -c "from app import __version__; print(__version__)" 2>&1
Write-Host "  Argos v$ver" -ForegroundColor Green

# -------- Step 5: restart prompt --------
Write-Step 5 $TOTAL "Update completato"
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  IMPORTANTE: riavvia l'app per applicare le modifiche" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  1. Vai nel terminale dove gira `argos` e premi Ctrl+C"
Write-Host "  2. Rilancia `argos` (oppure l'alias legacy `agentscraper`)"
Write-Host ""
Write-Host "Apri http://127.0.0.1:8000 e verifica che il banner di update sia"
Write-Host "scomparso (significa che ora hai la versione piu' recente)."
Write-Host ""
