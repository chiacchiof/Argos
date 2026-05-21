# install_client.ps1 — primo install di Argos su un PC nuovo (Windows).
#
# Eseguilo dalla cartella radice del progetto (dove c'è pyproject.toml):
#   .\scripts\install_client.ps1
#
# Cosa fa:
#  1. Verifica Python 3.11+
#  2. Crea virtual env in .venv\
#  3. pip install -e . (installa l'app + dipendenze)
#  4. playwright install chromium (browser per gli agenti)
#  5. Copia .env.example -> .env se non esiste
#  6. Genera SESSION_SECRET_KEY e AGENTSCRAPER_SECRET random
#  7. Chiede interattivamente DATABASE_URL (connection Postgres Neon)
#  8. Verifica che la connessione funzioni
#  9. Mostra come avviare l'app

$ErrorActionPreference = "Stop"

function Write-Step($n, $total, $msg) {
    Write-Host ""
    Write-Host "[$n/$total] $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "  [ERROR] $msg" -ForegroundColor Red }

$TOTAL = 9
$root = (Get-Item -Path ".\").FullName

if (-not (Test-Path ".\pyproject.toml")) {
    Write-Err "Esegui questo script dalla cartella radice del progetto Argos (quella che contiene pyproject.toml)."
    exit 1
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Argos - Installazione client" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Cartella: $root"

# -------- Step 1: Python --------
Write-Step 1 $TOTAL "Verifica Python 3.11+"
try {
    $pyversion = (python --version 2>&1)
    if ($pyversion -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
            Write-Err "Python $major.$minor trovato, serve 3.11 o superiore."
            Write-Host "  Scarica da https://www.python.org/downloads/" -ForegroundColor Yellow
            exit 1
        }
        Write-Ok "Python $major.$minor"
    } else {
        Write-Err "Python non trovato. Installa Python 3.11+ da https://www.python.org/downloads/"
        exit 1
    }
} catch {
    Write-Err "Python non trovato nel PATH. Installa Python 3.11+ e riavvia il terminale."
    exit 1
}

# -------- Step 2: venv --------
Write-Step 2 $TOTAL "Creazione virtual environment in .venv\"
if (Test-Path ".\.venv") {
    Write-Warn ".venv esiste già, lo riuso."
} else {
    python -m venv .venv
    Write-Ok ".venv creato."
}

$activate = ".\.venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Err "Impossibile trovare $activate. Setup venv fallito."
    exit 1
}
. $activate

# -------- Step 3: pip install --------
Write-Step 3 $TOTAL "Installazione dipendenze Python (pip install -e .)"
Write-Host "  Scarica ~30-50 pacchetti (200-400 MB). Su fibra 3-6 min, su connessione" -ForegroundColor Gray
Write-Host "  lenta anche 15-20 min. NON chiudere la finestra anche se sembra ferma —" -ForegroundColor Gray
Write-Host "  pip mostrera' 'Collecting ...' e progress bar per ogni pacchetto." -ForegroundColor Gray
python -m pip install --upgrade pip --quiet
python -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Err "pip install fallito (exit $LASTEXITCODE). Vedi l'errore qui sopra."
    exit 1
}
Write-Ok "Dipendenze installate."

# -------- Step 4: playwright --------
Write-Step 4 $TOTAL "Download browser Chromium (Playwright, ~150MB)"
Write-Host "  Anche questo step richiede qualche minuto. Output di playwright in corso:" -ForegroundColor Gray
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Warn "playwright install ha riportato un errore (exit $LASTEXITCODE). Puoi rilanciarlo dopo con:"
    Write-Host "    .\.venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Gray
}
Write-Ok "Chromium installato nella cache di Playwright."

# -------- Step 5: .env --------
Write-Step 5 $TOTAL "Setup file .env"
if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Ok ".env creato da .env.example."
} else {
    Write-Warn ".env esiste già, non lo sovrascrivo."
}

# -------- Step 6: secrets --------
Write-Step 6 $TOTAL "Generazione chiavi di sicurezza (SESSION_SECRET_KEY, ARGOS_SECRET)"
$envContent = Get-Content ".\.env" -Raw

function Ensure-Secret($name, $envText) {
    if ($envText -notmatch "(?m)^\s*$name\s*=\s*\S") {
        $secret = python -c "import secrets; print(secrets.token_urlsafe(32))"
        Add-Content ".\.env" "`n$name=$secret"
        Write-Ok "$name generata."
        return $true
    } else {
        Write-Warn "$name già impostata in .env, non la rigenero."
        return $false
    }
}
Ensure-Secret "SESSION_SECRET_KEY" $envContent | Out-Null
# Rebrand 2026-05-21: genera ARGOS_SECRET. Se l'utente ha ancora AGENTSCRAPER_SECRET
# da una install pre-rebrand, il codice runtime la legge come fallback (vedi
# app/agent/social/crypto_creds.py) — non serve rigenerarla.
if ($envContent -match "(?m)^\s*AGENTSCRAPER_SECRET\s*=\s*\S") {
    Write-Warn "AGENTSCRAPER_SECRET (legacy pre-rebrand) presente in .env: il codice la legge come fallback. Per pulire, rinominala manualmente in ARGOS_SECRET con lo STESSO valore (cambiare valore renderebbe indecifrabili i dati esistenti)."
} else {
    Ensure-Secret "ARGOS_SECRET" $envContent | Out-Null
}

# -------- Step 7: DATABASE_URL --------
Write-Step 7 $TOTAL "Configurazione connessione DB (Postgres / Neon)"
$envContent = Get-Content ".\.env" -Raw
if ($envContent -match "(?m)^\s*DATABASE_URL\s*=\s*\S") {
    Write-Warn "DATABASE_URL già impostata in .env, non chiedo nulla."
} else {
    Write-Host "  Incolla la connection string Postgres (es. Neon):" -ForegroundColor Yellow
    Write-Host "    postgresql://user:password@host:5432/dbname?sslmode=require"
    Write-Host "  Lascia vuoto per skippare (potrai impostarla dopo da /dbconfig)."
    $dburl = Read-Host "  DATABASE_URL"
    if ($dburl.Trim()) {
        Add-Content ".\.env" "`nDATABASE_URL=$($dburl.Trim())"
        Write-Ok "DATABASE_URL salvata in .env."
    } else {
        Write-Warn "DATABASE_URL non impostata. L'app non partira' finche' non la setti."
    }
}

# -------- Step 8: connection test --------
Write-Step 8 $TOTAL "Test connessione DB"
$testResult = python -c @"
import os
from dotenv import load_dotenv
load_dotenv()
url = os.environ.get('DATABASE_URL', '').strip()
if not url:
    print('SKIP: DATABASE_URL non impostata')
else:
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            v = conn.execute('SELECT version()').fetchone()[0]
            print('OK:', v[:60])
    except Exception as e:
        print('ERR:', str(e)[:200])
"@ 2>&1

Write-Host "  $testResult"
if ($testResult -match "^ERR") {
    Write-Warn "Connessione DB non funziona. Controlla DATABASE_URL in .env."
} elseif ($testResult -match "^OK") {
    Write-Ok "Connessione DB OK."
}

# -------- Step 9: pronto --------
Write-Step 9 $TOTAL "Setup completato!"
Write-Host ""
Write-Host "Per avviare l'app (modo consigliato):" -ForegroundColor Green
Write-Host "  Doppio click su start.bat nella cartella del progetto." -ForegroundColor White
Write-Host ""
Write-Host "  In alternativa da PowerShell (con venv da attivare ogni volta):" -ForegroundColor Gray
Write-Host "    .\.venv\Scripts\Activate.ps1; argos" -ForegroundColor Gray
Write-Host "    (alias legacy 'agentscraper' funziona ancora per installazioni esistenti)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Poi apri http://127.0.0.1:8000 nel browser." -ForegroundColor Green
Write-Host ""
Write-Host "Se è la prima installazione del cluster (= devi creare i tenant/utenti):" -ForegroundColor Cyan
Write-Host "  1. Aggiungi BOOTSTRAP_SUPER_ADMIN_EMAIL e _PASSWORD a .env" -ForegroundColor Gray
Write-Host "  2. Avvia argos, login come super-admin"
Write-Host "  3. Crea tenant + utenti via /admin"
Write-Host "  4. Rimuovi le BOOTSTRAP_* da .env per sicurezza"
Write-Host ""
