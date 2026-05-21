# scripts/ — operazioni DB

Toolkit per gestire schema migrations in un workflow dev → prod con branch-per-cambio.

## Convenzione di lavoro

Ogni cambio di schema che NON è additive triviale segue questo flusso:

```
1. crea branch dedicato         feature/<descrizione>
2. genera revision Alembic       python scripts/db.py new "..."
3. edita upgrade()/downgrade()   alembic/versions/XXXX_*.py
4. applica + testa locale         python scripts/db.py migrate
5. (review code + commit)
6. promote a prod (Neon)          pwsh scripts/deploy_to_neon.ps1
7. merge branch su main
```

La revision file `alembic/versions/XXXX_*.py` viaggia col branch git: è parte del commit. Quando un altro PC fa `git pull`, riceve la migration; basta che esegua `python scripts/db.py migrate` per applicarla sul suo locale.

**Per il deploy in prod usa `deploy_to_neon.ps1`** (wrapper PowerShell sicuro su `db.py promote`) invece di chiamare `db.py promote` direttamente. Lo wrapper gestisce automaticamente il bypass dell'override locale `data/db_config.enc` — vedi nota nella sezione "DSN Neon" più sotto.

## Comandi

### `python scripts/db.py status`

Mostra lo stato corrente:

```
DB STATUS
======================================================================
Repo HEAD revision: 0002

LOCALE (postgresql://postgres:****@localhost:5432/agentscraper_dev)
  alembic_version: 0001

NEON   (postgresql://neondb_owner:****@ep-delicate-leaf-alnrwlji.c-3.eu-central-1.aws.neon.tech/neondb)
  alembic_version: 0001

⚠️  Locale NON è a head (0002). Esegui `db.py migrate`.
```

Identifica drift (locale ≠ neon) e revision non applicate.

### `python scripts/db.py new "descrizione"`

Genera un file `alembic/versions/XXXX_descrizione.py` vuoto. Output:

```
✓ Revision creata. Prossimi passi:
  1. Edita il file appena generato in alembic/versions/
     (scrivi upgrade() e downgrade() a mano: NO autogenerate)
  2. python scripts/db.py migrate
```

**Safety check**: se sei su branch `main`/`master`, ti avvisa e chiede conferma (convenzione: non si modifica schema direttamente su main).

### `python scripts/db.py migrate [--skip-tests]`

Applica `alembic upgrade head` sul **DB locale** (`agentscraper_dev` Docker) + esegue `pytest tests/`. Se i test passano, sei pronto per `promote`.

```
[1/3] alembic upgrade head su LOCALE (postgresql://postgres:****@localhost:5432/agentscraper_dev)...
      → ora a 0002
[2/3] pytest...
      → tutti i test passano
[3/3] OK locale aggiornato e testato.

Prossimo step (quando sei pronto a deployare in prod):
  python scripts/db.py promote
```

`--skip-tests` salta pytest (sconsigliato).

### `python scripts/db.py promote [--skip-tests] [--yes]`

Applica `alembic upgrade head` su **Neon (prod)** dopo safety check:

1. Verifica che locale sia a HEAD del repo (altrimenti aborto).
2. Esegue pytest (strict by default).
3. Mostra le revision pending (`v_neon` → `v_local`).
4. Chiede conferma esplicita (`y/N`).
5. Applica `alembic upgrade head` su Neon.
6. Verifica allineamento finale.

```
[1/4] pytest gating...
      → tutti i test passano
[2/4] Stato attuale:
  LOCALE: 0002
  NEON:   0001

  Revisioni pending (da applicare su Neon): 1
    - 0002: add priority column to tasks

[3/4] CONFERMA
  Sto per applicare 1 revision(i) su NEON (postgresql://neondb_owner:****@...neon.tech/neondb).
  Procedere? [y/N] y

[4/4] alembic upgrade head su NEON...
      → ora a 0002

✓ PROMOTE COMPLETATO. Locale e Neon entrambi a 0002.
```

**Flag**:
- `--skip-tests`: salta pytest gating (USA SOLO in emergenza).
- `--yes` / `-y`: salta prompt conferma (CI/automation).

## Come gli script trovano la DSN

### DSN Locale (per `migrate`)

Legge **direttamente** `DATABASE_URL` da `.env` con `python-dotenv`, senza passare per `apply_override()` di `app/config.py`. Questo è critico: se l'utente ha attivo l'override `/dbconfig` (= app sta lavorando contro Neon), `migrate` deve comunque applicare al **locale**, non al Neon attivo.

### DSN Neon (per `promote` e `status`)

Fallback chain:

1. **File cifrato** `data/db_config.enc` (gestito via UI `/dbconfig`). Se l'utente ha settato Neon come override per l'app, lo riusiamo.
2. **Env var** `NEON_DATABASE_URL`. Per setup CI o quando non vuoi usare `/dbconfig`.
3. **File** `c:/tmp/neon_url.txt`. Compat con il modo in cui abbiamo deployato la prima volta.

Se nessuna fonte trova la DSN → errore esplicito.

⚠️ **Caveat in setup dev**: il fallback `db_config.enc` ha PRIORITÀ MASSIMA. Sul PC dev tipico l'override punta al **locale** (label "Locale dev" per lavorare offline). In quel caso `db.py promote` finisce per applicare la migration sul locale invece che su Neon — silenziosamente, perché lo script vede una DSN valida e parte. Usa sempre `deploy_to_neon.ps1` che nasconde temporaneamente `db_config.enc` (Move-Item in `.bak`, ripristino in `finally`) prima di chiamare `db.py promote`, così il fallback cade su `c:/tmp/neon_url.txt` che è la DSN Neon vera.

## Esempio end-to-end completo

Scenario: aggiungere colonna `priority INTEGER DEFAULT 0` a `tasks`.

```powershell
# 1. branch dedicato
git checkout -b feature/task-priority

# 2. genera revision
python scripts/db.py new "add priority column to tasks"
# → crea alembic/versions/0002_add_priority_column_to_tasks.py

# 3. edita la revision
#    apri alembic/versions/0002_*.py e scrivi:
#      def upgrade():
#          op.add_column("tasks", sa.Column("priority", sa.Integer(),
#                                            nullable=False, server_default="0"))
#      def downgrade():
#          op.drop_column("tasks", "priority")

# 4. applica + testa locale
python scripts/db.py migrate
# → "OK locale aggiornato e testato"

# 5. (sviluppa la feature che usa la nuova colonna, commit, test, push branch)
git add alembic/versions/0002_*.py app/models.py app/routes/tasks.py
git commit -m "schema: task priority + UI ordering"

# 6. quando soddisfatto, promote in prod (wrapper sicuro)
pwsh scripts/deploy_to_neon.ps1
# → nasconde db_config.enc → "scripts/db.py status" → conferma → "scripts/db.py promote"
# → opz. dry-run backfill + conferma + apply → ripristina db_config.enc
# Senza backfill: pwsh scripts/deploy_to_neon.ps1 -SkipBackfill

# 7. merge su main
git checkout main && git merge feature/task-priority && git push
```

> Volendo si può chiamare `python scripts/db.py promote` direttamente, ma SOLO se sei sicuro che la DSN risolta sia davvero quella di Neon (vedi caveat nella sezione "DSN Neon").

## Altri script in questa cartella

### Deployment (PowerShell wrapper su `db.py`)

- **`deploy_to_neon.ps1`** — wrapper "sicuro" per applicare schema + dati su Neon prod. Per ogni esecuzione:
  1. nasconde `data/db_config.enc` in `.bak` (se presente) — così `db.py promote` cade sulla DSN Neon vera
  2. setta `$env:NEON_DATABASE_URL` dal file `c:/tmp/neon_url.txt`
  3. mostra `db.py status` (locale vs Neon)
  4. chiede conferma esplicita → `db.py promote`
  5. (opzionale) dry-run + conferma + apply `backfill_contacts_to_assets.py`
  6. `finally` ripristina `db_config.enc` e unsetta `NEON_DATABASE_URL` — anche su crash o Ctrl+C
  
  Flag: `-SkipBackfill` per fare solo lo schema promote (release schema-only).
  È il punto d'ingresso consigliato per ogni deploy di prod.

- **`restore_dev_from_neon.ps1`** — pipeline `pg_dump` da Neon + `pg_restore` su locale `agentscraper_dev`. Usa il container `agentscraper-postgres-dev` come "client" pg_dump/pg_restore (così non serve installare Postgres tools su Windows). Quando serve:
  - Hai appena creato/ricreato il container Postgres dev e il locale è schema-only
  - Vuoi una copia fresca dei dati prod per testare offline
  - Hai sporcato il locale con test distruttivi e vuoi ripartire da una baseline pulita
  
  Flag: `-KeepDump` per non cancellare `/tmp/neon_backup.dump` nel container (utile per restore ripetuti). Pre-check: rileva mismatch versione pg_dump↔server Neon e propone l'upgrade del container.
  
  ⚠️ Caveat noto: `pg_restore --clean` non fa CASCADE → se sul locale ci sono tabelle nuove (con FK in entrata verso `users`/`tenants`) che ancora non esistono su Neon, il restore fallisce a metà lasciando dati incoerenti. Soluzione: `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` a mano nel container, poi rerun.

### Install / update lato cliente

- **`install_client.ps1`** — installer PowerShell per primo deploy su PC nuovo (Python check, venv, pip, Playwright, `.env` scaffolding, prompt DSN, generazione `ARGOS_SECRET` e `SESSION_SECRET_KEY`). Vedi `CLIENT_INSTALL.md`.
- **`update_client.ps1`** — updater PowerShell per applicare un nuovo zip release dopo estrazione.
- **`CLIENT_INSTALL.md`** — guida cliente completa Windows (primo install, banner update, troubleshooting, sezione developer release).

### Migration one-shot (scaffolding storico)

- **`migrate_legacy_to_edg.py`** — one-shot: legge l'SQLite legacy `data/agentscraper.db` e lo migra su Postgres sotto tenant EDG. Già eseguito durante la Fase 2 del piano (40331 righe migrate, 657 duplicati skippati). Lo lasciamo come riferimento, non va più rieseguito.
- **`migrate_to_cloud.py`** — scaffolding originale del piano di Fase 5 (più generico). Soppiantato da `migrate_legacy_to_edg.py`; tieni come template per future migrazioni one-shot ad-hoc. **NON usare per propagare schema changes dev → prod** — quello è compito di `deploy_to_neon.ps1` + Alembic.
- **`backfill_contacts_to_assets.py`** — backfill Fase 2B: per ogni `contacts` row, copia i canali (email/telegram/whatsapp/social) sull'`assets` linkato (`asset_id`) o crea uno "shadow asset" `asset_type='contact_legacy'` se orfano. COALESCE-safe (non sovrascrive valori già presenti). Idempotente. Invocato automaticamente da `deploy_to_neon.ps1` (con dry-run + conferma) — invocazione diretta `--dry-run` / `--limit N` solo per testing.

### Altri

- **`watchdog_workflow.py`** — non correlato al DB, monitora workflow runs (pre-esistente).

## Banner di aggiornamento in-app (release check GitHub)

Per attivare il banner che avvisa i client quando esce una nuova versione, setta in `.env` (lato dev E lato ogni client):

```ini
GITHUB_REPO=owner/repo          # es. chiacchiof/Argos
# Solo se il repo è privato:
GITHUB_TOKEN=ghp_xxxxxxx        # PAT classic con scope `repo` o fine-grained `Contents: Read`
```

Senza queste variabili il banner è disabilitato (zero chiamate HTTP). Quando attivo:
- L'app al boot fetcha `https://api.github.com/repos/<owner>/<repo>/releases/latest` (timeout 5s, non bloccante).
- Risultato cached in `data/version_check.json` per 6h.
- Confronto: `release.tag_name` (rimuove prefisso `v`) vs `app.__version__` da `app/__init__.py`.
- Se diversi → banner giallo in `base.html` con link a release notes + pagina `/update` con istruzioni.

Per il workflow di rilascio (dev): bump `app/__init__.py` + `pyproject.toml`, tag git, GitHub Release con zip allegato. Dettagli in `CLIENT_INSTALL.md` sezione "Per il developer".

## Nota rebrand 2026-05-21 (AgentScraper → Argos)

Dopo il rebrand alcuni nomi "interni" sono stati **preservati come legacy** per non rompere installazioni esistenti:

- DB Postgres locale dev: `agentscraper_dev` (mantenuto — corrisponde al container `agentscraper-postgres-dev` e al volume bind-mount `data/postgres-dev/`. Cambiarlo richiederebbe ricreare container + volume + dump/restore).
- SQLite legacy (modalità single-user pre-multi-tenant): `data/agentscraper.db` (mantenuto — gitignored, già migrato via `migrate_legacy_to_edg.py`).
- Master key env var: `ARGOS_SECRET` primaria con fallback `AGENTSCRAPER_SECRET` (gestito in `app/_runtime_db_override.py` + `app/agent/social/crypto_creds.py`). Rinominare nel `.env` locale preservando il VALORE — cambiare valore renderebbe indecifrabili i credentials cifrati.
- Proxy list: `ARGOS_PROXIES` primaria con fallback `AGENTSCRAPER_PROXIES`.

I percorsi user-facing nuovi (entry point, cookie session, ContextVar, prompt LLM) usano tutti il prefisso `argos`. L'entry point `agentscraper.exe` è ancora installato come alias legacy via `pyproject.toml::[project.scripts]`.

## Note Postgres / Alembic

- **Migrations manuali**, no autogenerate: scriviamo `op.add_column`, `op.create_table`, `op.execute("...")` a mano. Motivo: l'app usa `psycopg` raw, non SQLAlchemy ORM, quindi non c'è metadata da cui Alembic possa derivare un diff. Più sicuro comunque (autogenerate produce risultati imperfetti in casi non triviali).
- **Driver**: `alembic/env.py` riscrive automaticamente `postgresql://` → `postgresql+psycopg://` per usare psycopg3 anziché psycopg2 (non installato).
- **NullPool**: Alembic apre una connessione per operazione, non riusa il pool. Su Neon (PgBouncer transaction mode) evita conflitti con prepared statements.
- **Baseline `0001`**: lo schema esistente post-Fase 2 è gestito da `app.db.init_db()` + `app.db_cloud.init_db()` al boot, NON da Alembic. La baseline `0001_baseline_*.py` è una revision vuota che marca "punto zero". Tutte le revisioni nuove saranno `0002`, `0003`, ...
