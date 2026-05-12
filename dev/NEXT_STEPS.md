# NEXT_STEPS — integrazione modulo social_outreach

Documento operativo per riprendere il lavoro dopo la fase di scraping in corso.
Tutto quello che e' in `dev/` è **fuori da `app/` di proposito**: file nuovi
in `app/` causano uvicorn reload e killano i job in corso (lezione 2026-05-12).

## Quando integrare

Quando il workflow di scraping NON ha job running. Verifica con:

```powershell
python -c "import sqlite3; con=sqlite3.connect('data/agentscraper.db'); print(con.execute('SELECT id, status FROM jobs WHERE status=\"running\"').fetchall())"
```

Se l'output è `[]`, è safe procedere.

## Step di integrazione (ordine)

### 1. Spostare i file da `dev/social_outreach/` a `app/agent/social/`

```powershell
mkdir app/agent/social
move dev/social_outreach/*.py app/agent/social/
```

### 2. Aggiustare gli import relativi nei file spostati

- `dev/social_outreach/session_manager.py`:
  - `from ...config import DATA_DIR` ← già corretto per la nuova location

Tutti gli altri file usano import relativi `from .X import Y` (es. `from .humanize import ...`) che restano validi quando spostati.

### 3. Creare `app/agent/runner_outreach_social.py`

Entry-point come gli altri runner. Schema:

```python
async def run_agent(task: dict[str, Any], job_id: int) -> str:
    from .social.engine import OutreachEngine
    from .social.platform_base import SocialAccount
    from .. import db

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)

    # 1. Carica accounts dal DB (decifra password)
    accounts = _load_active_accounts_for_task(task)
    if not accounts:
        db.update_job(job_id, status="error", error="Nessun account social attivo")
        return ""

    # 2. Carica target dal DB (contacts qualified con social[] del platform)
    platform_name = task.get("social_platform", "instagram")
    targets = _load_targets_for_platform(task, platform_name, limit=task.get("max_dms_per_run", 40))

    # 3. Genera messaggi personalizzati per ogni target via LLM
    targets_with_msgs = await _generate_messages(targets, task)

    # 4. Engine.run_session()
    engine = OutreachEngine(accounts, headed=True, use_patchright=True)
    results = await engine.run_session(
        platform_name=platform_name,
        targets=targets_with_msgs,
        warmup_min=5.0,
        max_dms_per_session=task.get("max_dms_per_session", 5),
    )

    # 5. Log + update DB
    _persist_results_to_db(results, job_id)
    # 6. Update contacts.status -> 'contacted' per quelli ok
    ...
```

### 3b. Aggiungere campi al task

In `app/db.py:init_db()` aggiungere ALTER TABLE idempotenti (come gli altri):

```python
tcols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
if "social_platform" not in tcols:
    con.execute("ALTER TABLE tasks ADD COLUMN social_platform TEXT")
if "outreach_intent" not in tcols:
    con.execute("ALTER TABLE tasks ADD COLUMN outreach_intent TEXT")
if "message_template_variants" not in tcols:
    con.execute("ALTER TABLE tasks ADD COLUMN message_template_variants TEXT")
if "max_dms_per_run" not in tcols:
    con.execute("ALTER TABLE tasks ADD COLUMN max_dms_per_run INTEGER DEFAULT 30")
if "max_dms_per_session" not in tcols:
    con.execute("ALTER TABLE tasks ADD COLUMN max_dms_per_session INTEGER DEFAULT 5")
if "headed" not in tcols:
    con.execute("ALTER TABLE tasks ADD COLUMN headed INTEGER NOT NULL DEFAULT 1")
```

### 3c. Aggiungere campi nel Form di task

Incollare il contenuto di `dev/staging_task_form_social.html` nell'apposito
punto di `app/templates/task_form.html` (dopo gli altri `<details class="collapsible-section dyn-section">`).

Aggiornare anche le route `/tasks` (in `app/routes/tasks.py`) per ricevere i nuovi
campi via `Form(...)` e passarli a `db.create_task` / `db.update_task`.

### 4. Migration DB (in `app/db.py:init_db()`)

Aggiungere blocco idempotente:

```python
# social_accounts
con.execute("""
CREATE TABLE IF NOT EXISTS social_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT UNIQUE NOT NULL,
  platform TEXT NOT NULL,            -- 'instagram' | 'tiktok'
  username TEXT NOT NULL,
  encrypted_password BLOB NOT NULL,
  proxy_label TEXT,
  daily_dm_cap INTEGER DEFAULT 10,
  status TEXT DEFAULT 'active',      -- 'active' | 'quarantine' | 'banned' | 'warming_up'
  warmup_started_at TEXT,
  warmup_days_target INTEGER DEFAULT 30,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform, username)
)
""")
con.execute("CREATE INDEX IF NOT EXISTS idx_social_accounts_platform_status ON social_accounts(platform, status)")

# social_dm_log
con.execute("""
CREATE TABLE IF NOT EXISTS social_dm_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES social_accounts(id) ON DELETE CASCADE,
  job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  target_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  target_platform TEXT NOT NULL,
  target_username TEXT NOT NULL,
  message TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  ok INTEGER NOT NULL,
  reason TEXT,
  health_post TEXT
)
""")
con.execute("CREATE INDEX IF NOT EXISTS idx_social_dm_log_account ON social_dm_log(account_id, sent_at)")
con.execute("CREATE INDEX IF NOT EXISTS idx_social_dm_log_target ON social_dm_log(target_contact_id)")
```

### 5. Tasks/agent_modes (aggiungere `outreach_social` come scelta)

In `app/agent/extraction_templates.py` o dove sono definiti i `AGENT_MODES`, aggiungere
`"outreach_social"`. In `app/jobs.py:_run_job_inner`, aggiungere il dispatcher:

```python
elif task["agent_mode"] == "outreach_social":
    from .agent.runner_outreach_social import run_agent as run_os
    await run_os(task, job_id)
```

### 6. UI: `/social/accounts` per gestione account

Pagina con form per aggiungere account social. **Importante**: il login viene
fatto la prima volta via Playwright headed con session_state salvato. L'UI deve:
- Form per username + password (cifrato via `crypto_creds.encrypt`)
- Bottone "Login & save session" che apre Playwright headed
- L'utente fa login manualmente (gestisce captcha, 2FA, ecc.)
- Tool salva session_state
- Account marcato `warming_up` con `warmup_started_at=now`

### 7. Configurazione `.env`

Aggiungere:

```ini
# Master key per cifratura credenziali social account (min 16 char)
AGENTSCRAPER_SECRET=cambia-questa-chiave-segreta-lunga-30-caratteri

# Proxy residenziali (JSON array)
AGENTSCRAPER_PROXIES='[
  {"label":"ip1","url":"http://user:pass@host:port","country":"IT"},
  {"label":"ip2","url":"http://user:pass@host:port","country":"IT"}
]'
```

### 8. Smoke test (stadio 0)

- Aggiungere 1 account personale (NON dedicato) via UI
- Login manuale, salvataggio session
- Creare task `outreach_social` con `max_dms_per_run=5`, target = 5 contatti tryst con `social[platform=instagram]`
- Eseguire task, verificare:
  - Browser headed si apre, naviga, scrolla, scrive DM
  - Account passa `dms_today` da 0 a 5
  - `social_dm_log` ha 5 righe
  - Health rimane `OK`

## File di riferimento

- `dev/social_outreach/*.py` — codice modulo
- `app/agent/runner_outreach_social.py` (da creare) — entry-point runner
- `GUIDA.md §16` — documentazione utente
- Backlog memoria: `project_future_dev_priorities.md §I`

## Cose già fatte 2026-05-12

- ✅ Modulo `social_outreach` completo (10 file Python in dev/)
- ✅ Dipendenze installate: `patchright`, `playwright-stealth`, `cryptography`, `curl_cffi`
- ✅ `pyproject.toml` aggiornato con commenti esplicativi
- ✅ GUIDA.md §16 scritta
- ✅ Lezione operativa registrata: file nuovi in `app/` durante job running = reload kill
- ✅ `message_generator.py` per DM personalizzati via LLM (Qwen locale, $0)
  + supporto `template_variants` user-provided come ispirazione di stile
- ✅ `runner_outreach_social_template.py` con pseudo-codice strutturato +
  parser di `message_template_variants` (separatore `---`)
- ✅ Test unitari humanize 4/4 (no browser, no network)
- ✅ Mock UI HTML in `dev/staging_task_form_social.html` (sezione da incollare
  in `app/templates/task_form.html` al deploy)
- ✅ Script CLI `dev/test_login_flow.py` per validare il setup stealth su
  bot.sannysoft.com (no account reale serve)

## Bug noti da fixare al deploy

### Bug "SyncWrappingContextManager" (apparso run #23 2026-05-12)

Quando `auto-discovery FORZATA` chiama `discover_via_browser` su trovagnocca:
```
auto-discovery FAIL: AttributeError: 'SyncWrappingContextManager' object has no attribute 'chromium'
```

**Causa probabile**: dopo installazione di `patchright`, l'import statement
`from playwright.sync_api import sync_playwright` viene intercettato/patcato
in modo errato dato che patchright e playwright condividono namespace simili
ma API leggermente diverse.

**Workaround temporaneo**: il flusso classico (fetch_page + LLM mapping)
funziona comunque senza discover_via_browser → trovagnocca ha estratto profili
nonostante il fail.

**Fix definitivo (da fare al deploy)**:
1. Verificare che `app/agent/url_discovery_browser.py` importi `playwright`
   esplicitamente con namespace pulito, evitando shadow di patchright
2. Eventualmente importare lazy: `from playwright.sync_api import sync_playwright`
   dentro la funzione invece che a livello module
3. Se patchright continua a fare conflict: disattivare l'import di patchright
   dalla path `app/agent/url_discovery_browser.py` (patchright usato SOLO
   in social/engine.py)

Stima fix: 15 minuti.

### Bug N1 — `_ingest_to_assets` fuori dal `try/finally` di site_explorer (incidente 2026-05-12)

In `app/agent/runner_site_explorer.py:_run_agent_inner`, il blocco `# 5. Ingest in DB`
e' fuori dal `try/except/finally` del loop. Quando l'utente preme Stop dalla UI,
viene chiamato `hard_stop_job` → `task.cancel()` → solleva `asyncio.CancelledError`
nel runner. Il `finally` salva la pending_queue ma il `raise` propaga immediatamente
saltando `_ingest_to_assets`. Risultato: **profili nel jsonl ma NON nel DB**.

**Fix**: spostare l'ingest dentro un finally piu' ampio:

```python
try:
    profiles_f = (run_dir / "profiles.jsonl").open("w", ...)
    try:
        while not stopped and ...:
            ...  # loop ReAct
    except asyncio.CancelledError:
        stopped = True
        raise
    finally:
        profiles_f.close()
        if direct_target_queue or exploration_queue:
            try:
                _save_pending_queue(...)
            except Exception:
                pass
finally:
    # CRITICAL: ingest in DB DEVE girare anche con cancellation
    # cosi' i profili gia' nel jsonl finiscono in DB.
    if extraction_template:
        try:
            n_assets_in_db = _ingest_to_assets(
                profiles_path, task["id"], job_id, jlog,
                extraction_template=extraction_template,
            )
        except Exception as e:
            jlog(f"⚠️ ingest fail in finally: {e}")
            n_assets_in_db = 0
```

Stima fix: 20 minuti.

### Bug N2 — `_wait_if_paused` mancante in site_explorer

Il pulsante Pause della UI manda `control_signal='pause'`. In
`app/agent/runner_browseruse.py` c'e' `_wait_if_paused()` che sospende il
loop finche' il signal cambia a 'resume' o 'stop'. **NON esiste in
runner_site_explorer.py** → premere Pause sul site_explorer non ha effetto.

**Fix**: estrarre `_wait_if_paused` come helper in `app/agent/common_runner_utils.py`
(o duplicare la funzione in site_explorer). Chiamarla all'inizio di ogni step
del while loop.

Stima fix: 15 minuti.

### Bug N3 — `_after_job_done` triggera solo su status=done

In `app/jobs.py:_run_job` riga 304:
```python
if cur and (cur.get("status") or "") == "done":
    _after_job_done(job_id, task_id, workflow_run_id=...)
```

Se l'utente preme Stop → status='cancelled' → downstream qualifier NON
parte. Risultato: serve trigger manuale del qualifier ogni volta.

**Fix**: cambiare condizione a `if cur and cur.get("status") in ("done", "cancelled"):`
e nel `_after_job_done` aggiungere un parametro `partial_completion: bool` per
loggare che e' partial. Il qualifier non si accorge della differenza
(opera sugli asset 'new' comunque).

Stima fix: 10 minuti.

### Trio N1+N2+N3 = ~45 min di lavoro

Quando integri il modulo social_outreach (uvicorn reload anyway), fai
anche questi tre fix nello stesso colpo.
