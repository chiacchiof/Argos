# AgentScraper — installazione e aggiornamento client (Windows)

Guida per chi riceve l'app da Ferdinando (sviluppatore) e deve installarla / aggiornarla sul proprio PC.

## Prerequisiti (una sola volta)

1. **Python 3.11+** — scarica da <https://www.python.org/downloads/>. In fase di installazione spunta "Add python.exe to PATH".
2. **PowerShell** — già presente in Windows 10/11.
3. **(Opzionale) Git** — solo se vuoi clonare il repo invece di scaricare zip.

Niente Docker, niente Postgres locale: il DB è centralizzato in cloud (Neon), accedi solo via internet.

## Primo install

1. Scarica l'ultima release `agentscraper-vX.Y.Z.zip` dalla pagina GitHub Releases (link nel banner dell'app o ti viene inviato).
2. Estrai il contenuto in una cartella stabile, es. `C:\Apps\AgentScraper\`.
3. Apri **PowerShell** in quella cartella (Shift + tasto destro nello sfondo della cartella → "Apri finestra PowerShell qui").
4. **Sblocca gli script** (i file estratti da uno zip scaricato sono marcati da Windows come "provenienti da internet" e PowerShell li rifiuta finché non li sblocchi):
   ```powershell
   Get-ChildItem -Path .\scripts\ -Recurse | Unblock-File
   ```
   > **Nota**: se è anche la prima volta in assoluto che esegui script `.ps1` su questo PC, esegui PRIMA questo comando una tantum (poi non serve più):
   > ```powershell
   > Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
   > ```
5. Esegui l'installer:
   ```powershell
   .\scripts\install_client.ps1
   ```

   > **In alternativa**, se non vuoi modificare permessi: lancialo bypassando la policy solo per quella invocazione:
   > ```powershell
   > powershell -ExecutionPolicy Bypass -File .\scripts\install_client.ps1
   > ```
6. Lo script ti chiederà la **DATABASE_URL** (la connection string Postgres / Neon). Te la fornisce Ferdinando. Esempio:
   ```
   postgresql://neondb_owner:la-tua-password@ep-xxx.aws.neon.tech/neondb?sslmode=require
   ```
7. Al termine vedrai "Setup completato!". Per avviare l'app, **fai doppio click su `start.bat`** nella cartella radice del progetto. Si aprirà una finestra che attiva la venv e lancia il server.

   In alternativa, da PowerShell:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   agentscraper
   ```
   (la venv va attivata ogni volta che apri un nuovo terminale — `start.bat` lo fa al posto tuo).

8. Apri il browser su <http://127.0.0.1:8000> → vedi la pagina di login.

> **Suggerimento**: crea un collegamento di `start.bat` sul desktop (tasto destro → "Crea collegamento" → trascina sul desktop) così avvii AgentScraper come una normale app Windows.

### Credenziali

Le credenziali di accesso te le fornisce Ferdinando. Saranno qualcosa tipo:
- Email: `nome.cognome@etnadg.com`
- Password: (te la dice lui)

Cambia la password al primo login dalla sezione admin (solo super-admin).

## Aggiornamento (quando l'app ti dice "nuova versione disponibile")

Vedrai nell'header un banner giallo:
> 🔔 **Nuova versione X.Y.Z disponibile** (sei sulla X.Y.W). [Vedi note di rilascio] · [Come aggiornare]

1. Clicca "Come aggiornare" → arrivi a `/update` con istruzioni puntuali.
2. Scarica il nuovo zip dalla release.
3. **Importante**: prima di estrarre, fai un **backup** della cartella `data\` (contiene config locali).
4. Estrai il nuovo zip sopra la cartella attuale, **sovrascrivendo** i file. Il `.env` e `data\` NON vengono toccati (sono ignorati dallo zip).
5. Apri PowerShell nella cartella, **sblocca i nuovi file** (servono perché vengono dallo zip) e lancia:
   ```powershell
   Get-ChildItem -Path .\scripts\ -Recurse | Unblock-File
   .\scripts\update_client.ps1
   ```
6. Quando lo script finisce, vai nel terminale dove gira `agentscraper`, premi **Ctrl+C** per fermarlo, poi rilancia:
   ```powershell
   agentscraper
   ```
7. Apri il browser, ricarica → il banner di update non c'è più: hai la nuova versione.

## Troubleshooting

### "PSSecurityException / UnauthorizedAccess: il file non è firmato digitalmente"
Lo script `.ps1` è marcato come "internet" perché viene da uno zip scaricato. Sblocca i file con:
```powershell
Get-ChildItem -Path .\scripts\ -Recurse | Unblock-File
```
Oppure lancia con bypass per quella sola invocazione:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_client.ps1
```

### "L'app non parte: ImportError"
Hai dimenticato di rieseguire `update_client.ps1` dopo aver scaricato lo zip nuovo. Le dipendenze Python sono cambiate.

### "Login fallisce / errore 503"
Il DB cloud non risponde. Possibili cause:
- Neon è in scaling/cold start (riprova fra 10 secondi).
- La tua connessione internet è giù.
- La `DATABASE_URL` in `.env` è errata.

Per testare la DSN in isolamento:
```powershell
.\.venv\Scripts\activate
python -c "import psycopg, os; from dotenv import load_dotenv; load_dotenv(); print(psycopg.connect(os.environ['DATABASE_URL']).execute('SELECT 1').fetchone())"
```

### "Non vedo il banner di update"
Il check è disabilitato di default. Per attivarlo aggiungi a `.env`:
```
GITHUB_REPO=owner/agentscraper
# opzionale per repo privato:
GITHUB_TOKEN=ghp_xxxxxxx
```
e riavvia.

### "Come faccio a vedere che versione ho installato?"
Apri <http://127.0.0.1:8000/update> — la prima riga ti dice la versione locale.
Oppure in PowerShell:
```powershell
.\.venv\Scripts\python.exe -c "from app import __version__; print(__version__)"
```

### "Voglio reinstallare da zero"
Cancella `.venv\` e rilancia `install_client.ps1`. La cartella `data\` e `.env` puoi mantenerle (così non perdi config) oppure cancellarle se vuoi proprio "factory reset".

## Per il developer (Ferdinando) — come rilasciare una nuova versione

1. Bump version in `app/__init__.py` e `pyproject.toml` (deve essere coerente, es. `1.0.1`).
2. Se ci sono migrazioni schema, applica `python scripts/db.py promote` su Neon prima del rilascio.
3. Commit + tag: `git tag v1.0.1 && git push --tags`.
4. Vai su GitHub → Releases → "Draft a new release":
   - Tag: `v1.0.1`
   - Title: `AgentScraper v1.0.1`
   - Body: changelog markdown (verrà mostrato nella pagina `/update` dei client)
   - Allega lo zip della repo (escludendo `.venv\`, `data\`, `__pycache__`).
5. Click "Publish release".
6. I client con `GITHUB_REPO` configurato vedranno il banner entro 6h (cache TTL).

Per generare lo zip "clean":
```powershell
git archive --format=zip --output=agentscraper-v1.0.1.zip HEAD
```
o usa una GitHub Action (vedi `.github/workflows/release.yml` se configurato).
