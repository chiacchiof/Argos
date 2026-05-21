# Argos

Web app locale per definire e lanciare **scraper agentici** di informazioni dal web, alimentati da [Ollama](https://ollama.com) in locale.

Ogni **progetto** descrive cosa cercare e con quali vincoli; un loop **ReAct** decide autonomamente le ricerche e i fetch web fino a produrre un report testuale, salvato come `.txt` (o `.md`) su disco.

L'app è single-user, gira tutta sul tuo computer e non manda dati a servizi esterni a parte le query a DuckDuckGo e i fetch HTTP delle pagine target.

---

## Requisiti

- **Python 3.11+** (testato su 3.12)
- **[Ollama](https://ollama.com)** attivo su `http://localhost:11434` con almeno un modello che supporta tool calling (es. `qwen3.5`, `llama3.1`, `gpt-oss`, `mistral`)
- **Chromium** scaricato via Playwright (un comando una volta sola, vedi sotto)

Playwright e [browser-use](https://github.com/browser-use/browser-use) sono dipendenze del progetto e vengono installate automaticamente da `pip install -e .`

---

## Avvio

```powershell
# Crea e attiva l'ambiente virtuale
python -m venv .venv
.\.venv\Scripts\activate

# Installa dipendenze in editable mode (include FastAPI, browser-use, Playwright, ecc.)
pip install -e .

# Scarica il binario di Chromium nella cache di Playwright (una sola volta per macchina)
playwright install chromium

# Copia il file di configurazione
copy .env.example .env

# Avvia il server (con auto-reload)
agentscraper
# oppure equivalente diretto:
# uvicorn app.main:app --reload --reload-include "*.html" --reload-include "*.css"
```

Apri **`http://127.0.0.1:8000`** nel browser.

Il server si ricarica automaticamente quando modifichi `app/**/*.py`, i template `*.html` o `static/*.css` — non serve riavviarlo a mano durante lo sviluppo.

### Test

```powershell
pytest
```

Sono inclusi test smoke che verificano boot dell'app, CRUD progetti e gestione errori di validazione (non richiedono Ollama).

---

## Configurazione (`.env`)

| Variabile | Default | Significato |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Endpoint Ollama |
| `DEFAULT_MODEL` | `qwen3.5:latest` | Modello preselezionato nel form di creazione |
| `HOST` | `127.0.0.1` | Bind del server |
| `PORT` | `8000` | Porta del server |
| `HTTP_USER_AGENT` | `Argos/0.1 (+local research bot)` | UA usato dai fetch HTTP |
| `HTTP_TIMEOUT` | `20` | Timeout richieste HTTP (secondi) |
| `DEFAULT_MAX_ITERATIONS` | `10` | Cap di sicurezza sul loop ReAct |

---

## Struttura del repo

```
Argos/
├── app/
│   ├── main.py             entry FastAPI + lifespan (init DB, scheduler)
│   ├── config.py           Settings da .env (pydantic-settings)
│   ├── db.py               SQLite (no ORM) + schema migration
│   ├── models.py           Pydantic ProjectIn, Project, Job
│   ├── storage.py          scrittura/lettura file di risultato
│   ├── jobs.py             JobManager (asyncio) + APScheduler per cron
│   ├── templates.py        Jinja2Templates singleton
│   ├── routes/
│   │   ├── projects.py     CRUD progetti, form HTML
│   │   ├── jobs.py         POST /run, GET /jobs/{id}/status
│   │   └── results.py      lista + download dei file salvati
│   ├── agent/
│   │   ├── runner.py       loop ReAct (cuore del sistema)
│   │   ├── ollama.py       client async per /api/chat
│   │   ├── prompts.py      system prompt + spec dei tool
│   │   └── tools/
│   │       ├── search.py        web_search via DuckDuckGo (ddgs)
│   │       ├── fetch_http.py    httpx + readability-lxml
│   │       └── fetch_browser.py Playwright (lazy-import, fallback)
│   └── templates/          Jinja + HTMX
├── static/style.css        CSS minimale
├── tests/test_smoke.py     boot + CRUD via TestClient
├── data/                   runtime (gitignored)
│   ├── agentscraper.db     SQLite con projects/jobs
│   └── results/<pid>/<ts>.txt
├── .env.example
├── .gitignore
└── pyproject.toml
```

---

## Come funziona

### CRUD progetti

Un **progetto** rappresenta una ricerca riutilizzabile. Campi:

| Campo | Significato |
|---|---|
| **Nome** | Identità leggibile del progetto |
| **Descrizione** | Note libere |
| **Obiettivo** | Il prompt che guida l'agente — descrive *cosa cercare* e *come strutturare il report* |
| **Seed query** | Query iniziali (una per riga). Se vuoto, l'agente le deduce dall'obiettivo |
| **Whitelist domini** | Se valorizzata, l'agente fa fetch solo da questi domini |
| **Blacklist domini** | Domini da escludere |
| **Max iterazioni** | Cap di sicurezza sul loop ReAct (default 10) |
| **Modello Ollama** | Selettore tra i modelli installati localmente |
| **Output** | `txt`, `md`, o entrambi |
| **Cron** | Espressione cron a 5 campi per esecuzioni ricorrenti (opzionale) |

### Multi-agent + canali (Email / Telegram) + pipeline DAG

Argos supporta **5 tipi di agent**, combinabili in pipeline:

| `agent_mode` | Cosa fa |
|---|---|
| `react` | Loop ReAct su HTTP+DDG (scraping leggero) |
| `browser_use` | Browser-use con Chromium reale (scraping pesante) |
| `qualifier` | Legge `profiles.jsonl`, filtra/scora i contatti via LLM, materializza in `contacts` |
| `outreach` | Manda email + Telegram ai contatti `qualified`, usando `message_template` |
| `responder` | Genera reply automatica via LLM ai messaggi inbound, con opt-out detection |

**Pipeline DAG**: dal menu `🔗 Workflow` puoi collegare progetti A→B→C; quando A finisce, B viene lanciato automaticamente, e l'artifact (es. `profiles.jsonl`) viene passato a B come input.

**Canali messaggistica** (configurazione in `⚙️ Settings`):
- **Email**: SMTP send + IMAP polling ogni 60s. Configura host/user/password (Gmail richiede App Password). Le password vanno meglio in env: `SMTP_PASSWORD`, `IMAP_PASSWORD`.
- **Telegram**: Bot Token (da @BotFather, env `TELEGRAM_BOT_TOKEN`). Polling getUpdates ogni 30s. **Vincolo**: il bot riceve solo da utenti che gli hanno scritto per primi.

**Inbox** (`📨 Inbox`): vede tutti i thread, dettaglio messaggi, reply manuale, opt-out. I messaggi inbound auto-aggiornano contatti e thread.

**Auto-reply LLM senza review**: il `responder` legge i messaggi inbound non processati, genera reply via LLM (provider configurato sul progetto) e invia. **Opt-out automatico** su keywords: `STOP`, `unsubscribe`, `disiscrivi`, `rimuovimi`, `opt-out`, `non contattarmi`, ecc. → marca contatto `optedout` e non risponde.

⚠️ **Caveat etico/legale**: l'auto-reply LLM senza human review può produrre risposte inappropriate. GDPR/CAN-SPAM richiedono base giuridica per outreach commerciale. È responsabilità tua rispettare opt-out, ToS dei provider e norme anti-spam.

### Provider LLM (modalità `browser_use`)

Browser-use può usare qualsiasi LLM che parli protocollo OpenAI-compatible. Il progetto supporta sei provider preset:

| Provider | Base URL | Env var | Modelli consigliati |
|---|---|---|---|
| `ollama` (default) | `http://localhost:11434/v1` | — | `qwen3-coder:30b`, `gpt-oss:20b` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `gpt-4o-mini`, `gpt-4o` |
| `anthropic` | `https://api.anthropic.com/v1/` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5`, `claude-sonnet-4-6` |
| `grok` | `https://api.x.ai/v1` | `XAI_API_KEY` | `grok-2-latest` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `GEMINI_API_KEY` | `gemini-2.5-flash`, `gemini-2.5-pro` |
| `custom` | URL libero (campo nel progetto) | `CUSTOM_API_KEY` | qualsiasi |

**Setup:** copia `.env.example` in `.env` e decommenta le righe delle API key che vuoi usare. Il selettore "Provider LLM" nel form di progetto mostra inline se la chiave è impostata (✓) o mancante (⚠️). Le chiavi non vengono mai salvate nel DB — solo in `.env` (gitignorato).

**Quando usare cosa:**
- **Per task agentici complessi (browser-use)** i modelli frontier remoti (gpt-4o, sonnet 4.6, gemini 2.5 pro) producono JSON tool-call affidabile e sono 5-10× più veloci dei locali sotto 20B parametri. Costo tipico per run: $0.05-1.00.
- **Per ricerche generiche (modalità `react`)** Ollama locale va benone. Niente costi, niente dipendenze esterne.

### Modalità agente (`agent_mode`)

Ogni progetto può scegliere come l'agente esegue la ricerca:

| Modalità | File | Comportamento | Quando usarla |
|---|---|---|---|
| `react` (default) | [app/agent/runner.py](app/agent/runner.py) | Loop ReAct leggero su tre tool: `web_search` (DuckDuckGo), `fetch_url` (httpx + readability, con fallback Playwright), `finalize`. Solo HTTP, niente browser persistente. | Ricerche generaliste, news, documentazione, siti statici o moderatamente dinamici. Veloce, basso consumo. |
| `browser_use` | [app/agent/runner_browseruse.py](app/agent/runner_browseruse.py) | Pilota un **browser reale** via [browser-use](https://github.com/browser-use/browser-use): naviga, scrolla, clicca, gestisce cookie banner, attende il caricamento di JS. L'agente "vede" la pagina come un utente. | Audit di content optimization, raccolta di URL pubblici da cataloghi/listing dinamici, siti SPA, anti-bot leggero. Più lento, richiede più memoria, ma molto più capace. |

Browser-use parla con Ollama via il suo endpoint OpenAI-compatible (`/v1`), quindi non servono API key esterne.

Per task complessi di analisi contenuti, in `browser_use` conviene usare modelli più capaci (es. `gpt-oss:20b` o `qwen3-coder:30b`) e impostare `Max iterazioni / step` su un valore alto (20–50).

### Loop ReAct (`app/agent/runner.py`)

Quando lanci un job, l'agente:

1. Costruisce il system prompt da `prompts.py` con obiettivo, vincoli sui domini e descrizione dei tool.
2. Chiama Ollama via `/api/chat` passando la spec di 3 tool: `web_search`, `fetch_url`, `finalize`.
3. Itera fino a `max_iterations`:
   - se Ollama risponde con `tool_calls` → li esegue, accoda il risultato come messaggio `tool`, rilancia.
   - quando il modello chiama `finalize(report)` → il report finale viene salvato e il loop termina.
4. `fetch_url` prova prima HTTP (`fetch_http`); se il testo estratto è troppo scarno o richiede JS, ricade su Playwright (lazy-imported).
5. Ogni URL viene filtrato contro whitelist/blacklist prima del fetch.
6. Ogni step viene appeso al log del job (visibile in tempo reale via polling HTMX).

Se l'agente non chiama `finalize` entro `max_iterations`, il runner forza un riassunto finale chiedendo al modello di consolidare quanto raccolto.

### Esecuzione e UI

- **▶ Esegui ora** sul detail di un progetto crea un job `queued` → `running` e lancia un task asincrono in background.
- Il riquadro "Stato esecuzione corrente" si auto-aggiorna ogni 2s via HTMX, mostrando log live.
- La tabella "Cronologia job" si auto-aggiorna ogni 3s finché c'è almeno un job attivo. Click su "log" di una riga riporta il dettaglio nel riquadro live.
- I report finali finiscono in `data/results/<project_id>/<ISO-timestamp>.txt`, scaricabili dalla pagina **Risultati salvati**.

### Schedulazione (cron)

Se valorizzi il campo **Cron** di un progetto, [APScheduler](https://apscheduler.readthedocs.io) avvia automaticamente un job alla cadenza specificata. La sintassi è quella standard a 5 campi (`m h dom mon dow`). Esempi:

- `0 9 * * *` — ogni giorno alle 9:00
- `*/30 * * * *` — ogni 30 minuti
- `0 8 * * 1-5` — alle 8:00 dei giorni feriali

La scheduler è in-process (vive nel processo uvicorn): se chiudi il server gli scheduli sono sospesi, ma vengono ricaricati al boot da `app/jobs.py:start_scheduler()`.

---

## Modelli consigliati

Per il loop ReAct serve **tool calling** nativo. Modelli testati su Ollama:

| Modello | Note |
|---|---|
| `qwen3.5:latest` | Default. Buon compromesso velocità/qualità su 9.7B parametri |
| `llama3.1:8b` | Alternativa solida, leggero |
| `gpt-oss:20b` | Più lento ma ragiona meglio su ricerche complesse |
| `qwen3-coder:30b` | Pesante, utile se l'obiettivo riguarda estrazione strutturata |
| `mistral:latest` | Veloce, va bene per task semplici |

`nomic-embed-text` è installato ma non usato (potenzialmente utile in futuro per RAG sui risultati).

---

## Troubleshooting

- **422 Unprocessable Entity** sul POST progetti → di solito un campo del form è effettivamente vuoto a livello HTTP. Controlla che il browser stia inviando tutti i campi (Form vuoti vanno bene, ma campi mancanti no).
- **Job rimane "running" per sempre** → controlla i log del job per capire dove l'agente si è bloccato. Errori comuni: Ollama non raggiungibile, modello non scaricato, timeout HTTP su un sito target.
- **Pagine restituiscono testo vuoto / "needs_browser"** → installa Playwright (`pip install -e ".[browser]" && playwright install chromium`). Il fallback è attivo automaticamente.
- **DuckDuckGo restituisce poco** → verifica connettività; raramente DDG mette in rate-limit l'IP. Si recupera in qualche minuto.

---

## Multi-tenant (cloud DB + login) — opzionale

L'app supporta una modalità multi-tenant in cui:
- Il DB Postgres è condiviso in cloud (Neon o Azure), così più colleghi su PC diversi lavorano insieme.
- Ogni utente fa login con credenziali personali; i dati sono isolati per **tenant** (azienda).
- Esiste un ruolo **super-admin** che da `/admin` crea aziende e utenti.

### Setup rapido con Neon

1. Crea un progetto su [neon.tech](https://neon.tech) — region `eu-central-1` (Frankfurt), Postgres 16.
2. Copia la connection string `Direct connection` dal dashboard Neon e mettila nel tuo `.env`:

   ```ini
   DATABASE_URL=postgresql://neondb_owner:<PASSWORD>@<host>.eu-central-1.aws.neon.tech/neondb?sslmode=require

   # Genera con: python -c "import secrets; print(secrets.token_urlsafe(32))"
   SESSION_SECRET_KEY=<chiave random 32 byte urlsafe>

   # Credenziali iniziali super-admin (lette UNA volta al primo boot, poi cambia password dalla UI)
   BOOTSTRAP_SUPER_ADMIN_EMAIL=edgAdmin
   BOOTSTRAP_SUPER_ADMIN_PASSWORD=Entra123!
   ```

3. Avvia l'app (`agentscraper`). Al primo boot vengono create le tabelle `tenants` / `users` su Neon e l'utente `edgAdmin`.
4. Apri `http://127.0.0.1:8000`, fai login con `edgAdmin / Entra123!`.
5. Vai su **🛡️ Admin** (pill nell'header) → crea aziende e utenti.
6. **Cambia la password** del super-admin dalla dashboard admin → "Il tuo account".

Se `DATABASE_URL` non è settato, l'app gira in modalità legacy single-user esattamente come prima (nessun login richiesto).

Per il piano completo (architettura, Fase 2 in poi, deployment Azure futuro) vedi `SETUP_CLOUD_DB_TENANT.md`.

### Schema migrations (Alembic)

Per gestire modifiche allo schema DB nel workflow dev → prod (branch dedicato per cambio, applicazione automatica con safety check), usa lo script `scripts/db.py`. Vedi `scripts/README.md` per il workflow completo e gli esempi end-to-end.

```powershell
python scripts/db.py status     # mostra alembic_version locale + Neon
python scripts/db.py new "..."  # crea revision Alembic vuota
python scripts/db.py migrate    # applica head LOCALE + esegue pytest
python scripts/db.py promote    # applica head NEON (con safety checks)
```

### Installazione e aggiornamento client

Per distribuire l'app a colleghi/clienti su PC diversi: zip release da GitHub + script PowerShell. Vedi `scripts/CLIENT_INSTALL.md` per la guida completa.

- **Primo install**: scarica zip release → `.\scripts\install_client.ps1` (9 step interattivi: Python check, venv, pip install, Playwright, .env scaffolding, prompt DSN, test connessione).
- **Update**: estrai zip nuovo sopra l'esistente → `.\scripts\update_client.ps1`.
- **Banner di aggiornamento in-app**: opzionale; attivalo settando `GITHUB_REPO=owner/repo` (+ `GITHUB_TOKEN` se repo privato) in `.env`. Senza queste variabili nessuna chiamata HTTP e nessun banner. Quando attivo, l'app al boot fa check GitHub API (`releases/latest`) cache 6h e mostra banner giallo se è disponibile una versione più recente.

---

## Roadmap / fuori scope attuale

Esplicitamente *non* implementato (rimandato):

- Fase 2-6 del multi-tenant: refactor `app/db.py` SQLite→Postgres + `tenant_id` sulle tabelle business + script di migrazione automatica (vedi `SETUP_CLOUD_DB_TENANT.md`).
- Per-user permissioning intra-tenant (per ora tutti gli utenti di un tenant vedono tutto).
- Cloud storage per file `data/results/`, `data/uploads/`, `data/whatsapp_sessions/` (restano locali).
- Export Excel / DB esterno (predisposto via `output_format`, ma non implementato).
- Bot Telegram.
- Embeddings/RAG con `nomic-embed-text` sui risultati storici.
- Rate limiting avanzato e robots.txt enforcement (solo User-Agent identificabile).
- Job queue con worker separato (Celery/RQ) — overkill per single-user.

---

## Licenza

Progetto personale, nessuna licenza esplicita al momento.
