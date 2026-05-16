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
6. promote a prod (Neon)          python scripts/db.py promote
7. merge branch su main
```

La revision file `alembic/versions/XXXX_*.py` viaggia col branch git: è parte del commit. Quando un altro PC fa `git pull`, riceve la migration; basta che esegua `python scripts/db.py migrate` per applicarla sul suo locale.

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

# 6. quando soddisfatto, promote in prod
python scripts/db.py promote
# → conferma y → "PROMOTE COMPLETATO. Locale e Neon entrambi a 0002."

# 7. merge su main
git checkout main && git merge feature/task-priority && git push
```

## Altri script in questa cartella

- **`migrate_legacy_to_edg.py`** — one-shot: legge l'SQLite legacy `data/agentscraper.db` e lo migra su Postgres sotto tenant EDG. Già eseguito durante la Fase 2 del piano (40331 righe migrate, 657 duplicati skippati). Lo lasciamo come riferimento, non va più rieseguito.
- **`migrate_to_cloud.py`** — scaffolding originale del piano di Fase 5 (più generico). Soppiantato da `migrate_legacy_to_edg.py`; tieni come template per future migrazioni one-shot ad-hoc.
- **`watchdog_workflow.py`** — non correlato al DB, monitora workflow runs (pre-esistente).

## Note Postgres / Alembic

- **Migrations manuali**, no autogenerate: scriviamo `op.add_column`, `op.create_table`, `op.execute("...")` a mano. Motivo: l'app usa `psycopg` raw, non SQLAlchemy ORM, quindi non c'è metadata da cui Alembic possa derivare un diff. Più sicuro comunque (autogenerate produce risultati imperfetti in casi non triviali).
- **Driver**: `alembic/env.py` riscrive automaticamente `postgresql://` → `postgresql+psycopg://` per usare psycopg3 anziché psycopg2 (non installato).
- **NullPool**: Alembic apre una connessione per operazione, non riusa il pool. Su Neon (PgBouncer transaction mode) evita conflitti con prepared statements.
- **Baseline `0001`**: lo schema esistente post-Fase 2 è gestito da `app.db.init_db()` + `app.db_cloud.init_db()` al boot, NON da Alembic. La baseline `0001_baseline_*.py` è una revision vuota che marca "punto zero". Tutte le revisioni nuove saranno `0002`, `0003`, ...
