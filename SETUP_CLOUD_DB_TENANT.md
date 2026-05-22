# Piano — Migrazione Argos a multi-tenant (DB cloud + app locale)

## Stato implementazione

> **Aggiornato al 2026-05-16.**

| Fase | Descrizione | Stato |
|---|---|---|
| 1 | Scaffold auth + tenants + admin frontend | ✅ COMPLETA |
| 1b | Pagina `/dbconfig` per switch DSN runtime (dev↔prod) | ✅ COMPLETA |
| 2 | Refactor `app/db.py` SQLite → Postgres | 🔴 TODO |
| 3 | `tenant_id` su tabelle business + filtri query | 🔴 TODO |
| 4 | Refactor route/runner per propagare `tenant_id` | 🔴 TODO |
| 5 | Script migrazione SQLite locale → cloud (tenant Default) | 🟡 SCAFFOLDING (`scripts/migrate_to_cloud.py`) |
| 6 | Test isolamento + hardening | 🟡 PARZIALE (54 test, copre Fase 1 + 1b) |

**Cosa funziona ora**:
- Se `DATABASE_URL` è vuoto → app in modalità legacy single-user come prima.
- Se `DATABASE_URL` è settato → auth obbligatoria, super-admin via env (`edgAdmin / Entra123!`), `/admin` per CRUD tenants/users.
- Tabelle business (tasks, jobs, assets, workflows, ecc.) ancora su SQLite locale.

**File implementati in Fase 1**:
- `app/db_cloud.py` — pool psycopg + schema `tenants`/`users` + CRUD.
- `app/auth.py` — bcrypt diretto (no passlib), dipendenze FastAPI `get_current_user` / `require_super_admin`.
- `app/routes/auth.py` — `/login`, `/logout` con protezione open-redirect.
- `app/routes/admin.py` — `/admin/*` (dashboard, tenants, users) dietro `require_super_admin`.
- `app/templates/login.html` + `app/templates/admin/*.html`.
- `app/main.py` — middleware HTTP auth + `SessionMiddleware` (ordine cruciale: auth innermost, session outermost).
- Test: `tests/test_auth_admin.py` (28 test passano, ha catturato il bug ordine middleware).

**Prossimi step (Fase 2)**:
1. Decidere se mantenere fallback SQLite o rimuoverlo subito.
2. Rinominare `app/db.py` → `app/db_sqlite.py` e creare `app/db_pg.py` come gemello Postgres, o riscrivere `app/db.py` polimorfico.
3. Conversione meccanica `?` → `%s` (~600 call site), `cur.lastrowid` → `RETURNING id`.
4. Schema PG completo per tutte le tabelle business (vedi sezione "Differenze SQLite→PG da gestire" più sotto).
5. Test end-to-end con DB Neon reale (richiede la password Neon nel `.env`).

## Context

Oggi Argos è un'app single-user locale: SQLite single-file (`data/agentscraper.db`), zero auth, FastAPI + Jinja2 + HTMX, scheduler APScheduler in-process, Ollama su `localhost:11434`. L'utente vuole distribuirla ai colleghi: ogni collega installa l'app sul proprio PC, ma tutti scrivono su un **DB Postgres condiviso in cloud** (Supabase/Neon). Servono **tenant** isolati a livello dato, un **super-admin** che crea tenant e utenti, e gli utenti di un tenant vedono/modificano tutti gli asset/task/workflow del proprio tenant (per-user filtering è future work). I file (report, profili browser, sessioni WhatsApp) restano locali al PC che li genera — cloud storage è step incrementale futuro.

Decisioni dell'utente raccolte in fase di planning:
- **Backend**: locale (FastAPI gira sul PC del collega). **DB**: Postgres in cloud.
- **Job execution**: solo sul client che li lancia. Cron parte solo se il PC del creatore è acceso.
- **File locali al client**. Cloud storage = futuro.
- **Migrazione dati esistenti**: tutto sotto tenant "Default" + super-admin.
- **Signup**: solo super-admin crea utenti (no self-signup).
- **Social accounts**: record tenant-shared (chiunque del tenant li vede), ma la sessione Playwright è per-PC: ogni PC che vuole operarci deve rifare il login.
- **`site_patterns`/`site_playbooks`**: globali con override per-tenant.

## Deployment cloud DB

Strategia a due step: **Neon** come provider primario per pilot/sviluppo (già creato), **Azure Database for PostgreSQL Flexible Server** come target a regime (produzione). Il codice è agnostico al provider: cambia solo la `DATABASE_URL` in `.env` di ciascun PC.

### Neon (pilot — già creato)

**Project attivo**:
- Region: `eu-central-1` (Frankfurt, AWS) → buona latenza dall'Italia + GDPR EU.
- Database: `neondb` (default).
- User: `neondb_owner`.
- Endpoint host: `ep-delicate-leaf-alnrwlji.c-3.eu-central-1.aws.neon.tech`.
- Connection string (direct, da copiare con password vera dal dashboard Neon):
  ```
  postgresql://neondb_owner:<PASSWORD>@ep-delicate-leaf-alnrwlji.c-3.eu-central-1.aws.neon.tech/neondb?sslmode=require
  ```

**Direct vs Pooled endpoint**: per il nostro caso (FastAPI long-running con `psycopg_pool.ConnectionPool` lato app, max 5-10 conn per PC × max ~10 PC = ~100 conn totali, sotto il limite Neon) **usiamo l'endpoint direct** (quello mostrato sopra, senza suffisso `-pooler`). Vantaggi:
- Prepared statements pieni (più performante per query ripetute).
- `LISTEN/NOTIFY` funzionante (per eventuali notifiche real-time future).
- `prepare_threshold` può restare al default.

Se in futuro il numero di connessioni supera ~100 (es. 20 PC × 10 conn) → passare al pooled endpoint (host con `-pooler`) e settare `prepare_threshold=None` nel pool psycopg3.

**`.env` di ogni PC** (file `c:\Progetti\Argos\.env`):
```ini
DATABASE_URL=postgresql://neondb_owner:<PASSWORD>@ep-delicate-leaf-alnrwlji.c-3.eu-central-1.aws.neon.tech/neondb?sslmode=require
SESSION_SECRET_KEY=<32 byte random urlsafe, generato una sola volta e condiviso fra tutti i PC>
BOOTSTRAP_SUPER_ADMIN_EMAIL=chifer81@gmail.com
BOOTSTRAP_SUPER_ADMIN_PASSWORD=<password forte, usata solo al primo boot per creare il super_admin>
```

> **Importante**: `SESSION_SECRET_KEY` deve essere **identico** su tutti i PC del tenant — altrimenti un cookie firmato su PC A non verrebbe accettato su PC B (improbabile nello use case, ma teoricamente possibile). In pratica i cookie vivono solo sul browser di ciascun utente quindi non è critico, ma standardizzare evita sorprese. Genera una volta con `python -c "import secrets; print(secrets.token_urlsafe(32))"` e distribuisci.

**Comando rapido per generare i secrets**:
```powershell
python -c "import secrets; print('SESSION_SECRET_KEY=' + secrets.token_urlsafe(32))"
```

**Limiti free tier Neon** (al 2026): 0.5 GB storage, 191.9h compute/mese, scale-to-zero dopo 5 min idle (cold start ~500ms alla prima query). Sufficiente per il pilot.

**Branching Neon** (opzionale, utile per dev/test): dal dashboard Neon puoi creare un branch del DB (snapshot istantaneo) per testare migrazioni senza toccare il main. Connection string del branch è diversa, la usi solo in dev.

### Azure Database for PostgreSQL Flexible Server (produzione, futuro)

**Quando passare**: quando il pilot è stabile, il volume cresce oltre il free tier Neon, o servono SLA enterprise/networking privato.

**Setup (~20 min)**:
1. Portal Azure → **Create a resource** → **Azure Database for PostgreSQL** → **Flexible server** (NON Single Server, deprecato).
2. Resource group dedicato: `rg-agentscraper-prod`.
3. Region: **West Europe** (Amsterdam) o **North Europe** (Dublino).
4. Postgres version: **16**.
5. Compute tier: **Burstable B1ms** (1 vCore, 2 GB RAM). Storage: **32 GB SSD** auto-grow ON. HA disabilitato all'inizio.
6. Authentication: PostgreSQL only. Admin user + password salvati in Azure Key Vault.
7. Networking — opzione A semplice (pilot esteso): **Public access** + firewall rules con IP dei colleghi (oppure Tailscale/ZeroTier per IP virtuali stabili). Opzione B sicura (vera produzione): **Private endpoint** + VNet + VPN Azure.
8. Backup: retention 7-35gg, geo-redundant se serve DR.
9. SSL Enforced (default).
10. Server parameters → `azure.extensions` → abilitare `citext`, `pg_trgm`, `uuid-ossp`.

**Connection string Azure** (esempio):
```
postgresql://agentscraper_user:<PASSWORD>@agentscraper-prod.postgres.database.azure.com:5432/agentscraper?sslmode=require
```

**Costo stimato** (West Europe, listino 2026): B1ms compute ~$12-15/mese + 32 GB SSD ~$3 + backup ~$2 ≈ **~$20/mese**.

### Migrazione Neon → Azure (a regime)

Quando si passerà da pilot a produzione, downtime stimato 10-30 min su dataset attuali:
1. Annuncio downtime ai colleghi.
2. Schema + dati con `pg_dump`:
   ```powershell
   pg_dump --no-owner --no-acl "$NEON_URL" > backup.sql
   psql "$AZURE_URL" -f backup.sql
   ```
3. Reset sequenze (stesso pattern dello script di Fase 5):
   ```sql
   SELECT setval(pg_get_serial_sequence('<tbl>','id'), (SELECT MAX(id) FROM <tbl>));
   ```
4. Sanity check counts su ogni tabella.
5. Update `.env` su ogni PC con la nuova `DATABASE_URL`.
6. Restart app su ciascun PC.

Il codice non cambia, è solo una migrazione data-only.

### Confronto sintetico

| Aspetto | Neon (pilot — attivo) | Azure Flexible Server (regime — futuro) |
|---|---|---|
| Costo iniziale | $0 (free tier) | ~$20/mese |
| Region attiva | Frankfurt (eu-central-1) | West Europe (futuro) |
| Scale-to-zero | Sì (5 min idle, cold start ~500ms) | No, sempre attivo |
| SLA | Best-effort | 99.9% (HA: 99.99%) |
| Networking privato | No | VNet + Private endpoint |
| Backup | 7gg snapshot | 7-35gg configurabile |

### Compatibilità codice

Entrambi sono Postgres 16 standard → zero differenze nel codice applicativo. Le uniche cose da gestire:
- Connection string nel `.env` (cambia host).
- Se in futuro passi a Neon **pooled endpoint** o ad Azure dietro PgBouncer → `prepare_threshold=None` viene **auto-applicato** da `app.db._is_pgbouncer_dsn()` se hostname contiene `-pooler`/`pgbouncer` o porta `6543`. Override manuale: `DATABASE_DISABLE_PREPARED=1`.
- SSL `sslmode=require` su entrambi (già nella connection string).
- I parametri TCP keepalive (`keepalives=1`, `keepalives_idle=30`, ecc.) sono auto-appesi alla DSN da `app.db._compose_conninfo()` per evitare drop su NAT/firewall.
- Tuning pool: `DATABASE_POOL_MIN_SIZE` (default 4) tiene connessioni warm → niente handshake TLS nel critical path. Vedi `.env.example`.

---

## Autenticazione utenti (no JWT — cookie session)

### Approccio
Login form HTML standard + cookie di sessione firmato. **No API token / JWT**: non servono nel nostro use case (UI server-rendered con HTMX, no client mobile esterno, no API third-party che consuma).

### Flow di login
1. Utente apre `http://127.0.0.1:8000/login` sul suo PC (FastAPI locale).
2. Form HTML con email + password → `POST /login`.
3. Backend cerca `SELECT id, password_hash, tenant_id, role FROM users WHERE email = %s AND is_active = TRUE`.
4. `passlib.bcrypt.verify(password, password_hash)` → se fallisce, redirect a `/login?error=invalid`.
5. Se ok, `request.session["user_id"] = user.id` (Starlette `SessionMiddleware` cripta+firma il cookie con `SESSION_SECRET_KEY`).
6. Redirect alla home `/` con il cookie settato.

### Flow di richiesta autenticata (qualunque pagina dopo login)
1. Browser invia automaticamente il cookie `session=...` (HttpOnly, SameSite=Lax).
2. La dependency FastAPI `get_current_user(request)`:
   - Legge `user_id = request.session.get("user_id")`.
   - Se assente → redirect 302 a `/login?next=<url>` (per GET HTML) oppure 401 JSON (se header `HX-Request: true`).
   - Se presente → `SELECT * FROM users WHERE id = %s AND is_active = TRUE` e ritorna `User` (con `tenant_id`, `role`).
3. La route riceve `current_user: User = Depends(get_current_user)` e propaga `tenant_id=current_user.tenant_id` a tutte le `db.*`.

### Sicurezza del cookie
- **HttpOnly**: il cookie non è leggibile da JS → immune a XSS leak.
- **SameSite=Lax**: protezione CSRF base.
- **Secure**: in produzione (HTTPS) sì; in dev su `http://127.0.0.1` no (altrimenti il browser non lo invia).
- **Firma + cifratura**: `SessionMiddleware` cifra il contenuto con `itsdangerous` + `SESSION_SECRET_KEY`. Se qualcuno copia il cookie da un altro PC ma non ha la chiave, non può forgiarne uno valido.
- **Scadenza**: 7 giorni di default (configurabile). Refresh automatico ad ogni richiesta.

### Tabella `users`
```sql
CREATE TABLE users (
  id BIGSERIAL PRIMARY KEY,
  tenant_id BIGINT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email CITEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,        -- bcrypt, ~60 char
  role TEXT NOT NULL CHECK (role IN ('super_admin','tenant_user')),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK ((role = 'super_admin' AND tenant_id IS NULL)
      OR (role = 'tenant_user' AND tenant_id IS NOT NULL))
);
```

### Creazione utenti
- **Super_admin**: creato al primo boot via `BOOTSTRAP_SUPER_ADMIN_EMAIL/PASSWORD` da `.env`. Se già esiste, skip.
- **Tenant_user**: creato dal super_admin via `/admin/users` (form HTML che genera password random visibile una sola volta, oppure password scelta dall'admin).
- **Reset password**: out of scope v1. Per ora super_admin può azzerare/reimpostare la password di un utente tramite la stessa UI admin.

### Credenziali super-admin iniziali (bootstrap)

Per il pilot interno useremo queste credenziali fisse, da cambiare al primo accesso reale:

| Campo | Valore |
|---|---|
| Identificativo login | `edgAdmin` |
| Password iniziale | `Entra123!` |

Nel `.env` di chi farà il primo deploy:
```ini
BOOTSTRAP_SUPER_ADMIN_EMAIL=edgAdmin
BOOTSTRAP_SUPER_ADMIN_PASSWORD=Entra123!
```

Note tecniche:
- Il campo `users.email` è `CITEXT` (case-insensitive). Non c'è alcun vincolo a livello DB che imponga il formato email — accetta qualunque stringa univoca. Quindi `edgAdmin` funziona come identificativo di login a tutti gli effetti.
- Il form di login mostra "Email o username" e usa `<input type="text">` (non `type="email"`), così il browser non blocca la submit di una stringa senza `@`.
- Dopo il primo login il super-admin DEVE cambiare password dalla sezione admin (in roadmap UI; per ora si fa via `/admin/users/<id>/reset-password`).

## Sezione admin frontend

Pagina raggiungibile da una pill "Admin" nell'header, visibile **solo** se `current_user.is_super_admin`. Tutte le route sono dietro la dependency `require_super_admin`.

### Rotte
- `GET /admin` — dashboard con counters (n. tenants, n. utenti totali, n. super-admin) e link rapidi.
- `GET /admin/tenants` — tabella tenant: id, nome, slug, n. utenti, attivo (toggle), data creazione, azioni (modifica/elimina).
- `POST /admin/tenants` — crea tenant (`name`, `slug` auto-derivato dal nome se non fornito).
- `POST /admin/tenants/<id>/toggle` — attiva/disattiva tenant.
- `POST /admin/tenants/<id>/delete` — elimina (con conferma; cascade su users del tenant via FK `ON DELETE CASCADE`).
- `GET /admin/users` — tabella utenti: id, login, ruolo, tenant, attivo, data, azioni.
- `POST /admin/users` — crea utente (`email/login`, `password`, `tenant_id` obbligatorio se ruolo=tenant_user, ruolo=tenant_user|super_admin).
- `POST /admin/users/<id>/toggle` — attiva/disattiva utente.
- `POST /admin/users/<id>/reset-password` — imposta nuova password (super-admin può resettare la propria e quella di chiunque).
- `POST /admin/users/<id>/delete` — elimina utente (non si può eliminare se stesso).

### Vincoli UI
- Quando si crea un utente con ruolo `tenant_user`, la select del tenant è obbligatoria.
- Quando si crea un utente con ruolo `super_admin`, il campo tenant è disabilitato e ignorato (DB CHECK enforza `tenant_id IS NULL`).
- Conferma JS minimale sulle delete (`onclick="return confirm(...)"`).
- Niente edit avanzato in v1: per cambiare il tenant di un utente esistente si elimina e ricrea.

### File implementativi
- [app/routes/admin.py](app/routes/admin.py) — router prefix `/admin`, tutte le route protette da `Depends(require_super_admin)`.
- [app/templates/admin/](app/templates/admin/) — `dashboard.html`, `tenants.html`, `users.html` (estendono `base.html`).
- Pill "Admin" aggiunta in [app/templates/base.html](app/templates/base.html) accanto a quella utente, visibile solo per super-admin.

### Out-of-scope sezione admin v1
- Edit nominativo/email di utente esistente (si fa elimina+ricrea).
- Audit log delle azioni admin.
- Inviti via email (creazione manuale con password fornita dall'admin).
- 2FA per super-admin.
- Gestione fine-grained intra-tenant (chi può creare task, chi può fare outreach, ecc.) — Fase futura.

## Schema migrations (Alembic)

> **Workflow operativo completo**: vedi [`scripts/README.md`](scripts/README.md). Comandi disponibili: `status`, `new`, `migrate`, `promote`.

Per gestire modifiche di schema dopo la Fase 2 (rename, drop, change type, data migrations) usiamo Alembic. Setup completo + baseline revision già committati:

- `alembic.ini` — config principale (URL placeholder; il vero DSN è letto da env.py).
- `alembic/env.py` — risoluzione DSN:
  1. Se `os.environ["DATABASE_URL"]` è settata esplicitamente in shell → usa quella (override esplicito per applicare su DB diverso dal default dell'app).
  2. Altrimenti importa `app.config` (= load_dotenv + apply_override `/dbconfig`) come fa l'app.
- `alembic/versions/0001_baseline_*.py` — baseline marker, NON crea tabelle (lo schema esistente è gestito da `app.db.init_db()` + `app.db_cloud.init_db()` al boot).
- Entrambi i DB attivi (locale dev + Neon prod) sono stati `alembic stamp head` a `0001`.

### Workflow tipico per una nuova modifica di schema

**Esempio: aggiungere colonna `priority INTEGER DEFAULT 0` a `tasks`.**

```powershell
# 1. Genera revision (vuota)
python -m alembic revision -m "add priority column to tasks"
# → crea alembic/versions/XXXX_add_priority_column_to_tasks.py

# 2. Edita upgrade()/downgrade() a mano:
def upgrade():
    op.add_column("tasks", sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
def downgrade():
    op.drop_column("tasks", "priority")

# 3. Apply su LOCALE (DB attivo per l'app, default da .env)
python -m alembic upgrade head
pytest       # verifica niente regressioni

# 4. Quando soddisfatto, apply su Neon (DSN esplicita in env shell)
$env:DATABASE_URL="postgresql://neondb_owner:<PASS>@<host>.neon.tech/neondb?sslmode=require"
python -m alembic upgrade head
$env:DATABASE_URL=""   # cleanup

# 5. Commit della revision + push
git add alembic/versions/XXXX_*.py
git commit -m "schema: priority column on tasks"
```

### Comandi utili

| Comando | Cosa fa |
|---|---|
| `alembic current` | Mostra la revisione corrente del DB attivo |
| `alembic history` | Lista tutte le revisioni del repo |
| `alembic upgrade head` | Applica tutte le revisioni mancanti |
| `alembic upgrade +1` | Applica solo la prossima |
| `alembic downgrade -1` | Rollback di una revisione |
| `alembic upgrade head --sql` | Genera SQL senza eseguire (review pre-prod) |
| `alembic stamp head` | Marca DB come "up-to-date" senza eseguire (per DB già allineati a mano) |

### Caso A: modifica ADDITIVE semplice (ADD COLUMN, CREATE INDEX, ecc.)

Alternativa più rapida ad Alembic per modifiche idempotenti: aggiungere `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` a `_apply_multitenant_columns()` in `app/db.py`. Si applica automaticamente al boot dell'app, su qualunque DB target. Niente revisione da committare.

**Quando usare init_db idempotente vs Alembic**:
- `init_db`: ADD COLUMN nullable, CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS. Cose che non possono rompere niente.
- Alembic: RENAME, DROP, CHANGE TYPE, NOT NULL constraint nuovo su tabella popolata, data backfill. Cose che richiedono pianificazione + rollback.

### Caso B: data migration (riempire/normalizzare dati)

Alembic supporta script Python custom nelle revision. Esempio:

```python
def upgrade():
    op.add_column("contacts", sa.Column("phone_country", sa.String(2)))
    # Backfill: estrai country code da `whatsapp` per i record esistenti
    op.execute("""
        UPDATE contacts SET phone_country = SUBSTRING(whatsapp FROM '^\\+(\\d{2})')
        WHERE whatsapp ~ '^\\+\\d{2}'
    """)
    op.alter_column("contacts", "phone_country", nullable=False, server_default="IT")
```

### Note importanti

- **`target_metadata = None`** in `env.py`: usiamo migrations manuali (op.add_column, op.create_table). Non possiamo usare `--autogenerate` perché l'app non ha SQLAlchemy ORM models (usiamo psycopg raw). Pazienza: scrivere a mano le revision è più sicuro comunque.
- **NullPool**: alembic apre una connessione per operazione, non riusa il pool dell'app. Su Neon (PgBouncer) evita conflitti.
- **Driver psycopg3**: alembic/env.py riscrive `postgresql://` → `postgresql+psycopg://` per usare il driver psycopg3 (già installato).
- **Auto-upgrade al boot**: NON attivato. Se vuoi, in `app/main.py` lifespan dopo `init_db` puoi aggiungere `command.upgrade(alembic_cfg, "head")`. Pro: deploy automatici. Contro: se una migration fallisce, l'app non parte.

---

## Pagina /dbconfig — switch DSN runtime (dev ↔ prod)

Per permettere a chi sviluppa di puntare l'app a un Postgres locale (dev) o al Postgres cloud (prod) **senza editare `.env`**, esiste una pagina isolata `/dbconfig` con credenziali dedicate.

### Credenziali
- **Utente**: `DBadmin`
- **Password**: `Entra123!`

Sono **offuscate** (hash bcrypt hardcoded in [app/routes/dbconfig.py](app/routes/dbconfig.py)) — la stringa in chiaro non appare nel sorgente né in nessun file dell'app. Per ruotarle serve modificare il codice e rideployare. Volutamente diverse dalle credenziali super-admin `edgAdmin/Entra123!` per separare i due ruoli:
- **edgAdmin** = chi amministra tenant/utenti dell'app (UI in `/admin`)
- **DBadmin** = chi configura *a quale database* punta l'app (UI in `/dbconfig`)

### Flusso
1. Vai a `http://127.0.0.1:8000/dbconfig` (URL non linkato dalla nav principale).
2. Login con `DBadmin / Entra123!`.
3. Inserisci una connection string Postgres (validazione: deve iniziare con `postgresql://` o `postgres://`) + un'etichetta opzionale (es. "Locale dev", "Neon prod").
4. Click **Salva DSN**.
5. **Riavvia l'app** (chiudi `agentscraper.exe`, rilancia `agentscraper`). La nuova DSN entra in gioco al boot.
6. Per tornare alla `DATABASE_URL` di `.env`, clicca **Rimuovi override**.

### Persistenza
- La DSN viene salvata cifrata in `data/db_config.enc` (Fernet con chiave derivata da `AGENTSCRAPER_SECRET`).
- All'avvio, `app/config.py` chiama `_runtime_db_override.apply_override()` che — se il file esiste e decifra — sovrascrive `os.environ["DATABASE_URL"]` PRIMA che `Settings()` sia istanziata.
- Se il file viene cancellato o `AGENTSCRAPER_SECRET` manca, l'app fallback a `.env::DATABASE_URL` (o, se anche quella manca, modalità legacy SQLite).

### Sicurezza
- La cifratura protegge la DSN da occhi indiscreti su backup/log/file-sharing, **non** da un utente con accesso fisico al PC che può cancellare il file.
- La pagina è in `_PUBLIC_PATH_PREFIXES` del middleware auth (non richiede login utente), ma ha il proprio gate `DBadmin` interno.
- Le route `/dbconfig/save` e `/dbconfig/clear` rifiutano richieste non autenticate come `DBadmin` (redirect a `/dbconfig` senza fare nulla).

### Setup Postgres locale per dev (con Docker)

Per testare modifiche di schema prima di portarle in prod, conviene avere un Postgres locale identico a Neon (Postgres 16).

**Docker compose minimale** (creare `docker-compose.dev.yml` nella root del repo):
```yaml
services:
  postgres-dev:
    image: postgres:16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: agentscraper_dev
    ports:
      - "5432:5432"
    volumes:
      - ./data/postgres-dev:/var/lib/postgresql/data
```

Avvio: `docker compose -f docker-compose.dev.yml up -d`.

Connection string per `/dbconfig` (preset già pronto nella UI):
```
postgresql://postgres:postgres@localhost:5432/agentscraper_dev
```

**Alternativa senza Docker**: installer ufficiale Postgres per Windows da [postgresql.org/download/windows](https://www.postgresql.org/download/windows/). Crea un DB `agentscraper_dev`, usa la stessa connection string sopra (cambia `postgres:postgres` con le tue credenziali admin).

### Out-of-scope /dbconfig v1
- Rotazione delle credenziali `DBadmin` via UI (richiede modifica codice + redeploy).
- Test "ping" della DSN prima di salvarla (utile: avviare un `psycopg.connect` di prova).
- Multi-profile: salvare più DSN nominate e switchare tra esse con un click.
- Hot-reload del pool a runtime senza restart (vedi Fase 2: serve coordinare con APScheduler e job in corso).

### Quando servirebbe JWT (out of scope ora)
Solo se in futuro:
- Aggiungiamo un'app mobile/desktop che chiama API REST stateless.
- Esportiamo un'API pubblica per integrazioni third-party.
- Vogliamo SSO con Azure Entra ID o Google → in quel caso si introduce OAuth2 + JWT come complemento, non sostituto del cookie.

Per ora: **solo cookie session**.

---

## Stack & decisioni tecniche

- **Driver DB**: `psycopg[binary,pool] >=3.2`, **sync** (il codice `app/db.py` è già tutto sync, asyncpg costringerebbe a riscrivere ~600 call site).
- **Pooling**: `psycopg_pool.ConnectionPool` singleton lazy. Con Supabase/Neon (PgBouncer transaction mode) settare `prepare_threshold=None`.
- **No ORM**: refactor di `app/db.py` con placeholder `%s` invece di `?` e `RETURNING id` invece di `cur.lastrowid`. JSON colonne → `JSONB` con `psycopg.types.json.Jsonb`.
- **Migrazioni**: `init_db()` riscritta in DDL Postgres-compatibile per bootstrap idempotente + **Alembic** come autorità per evoluzioni future. Baseline Alembic = snapshot post-`init_db()`.
- **Auth**: `SessionMiddleware` di Starlette con cookie firmato (`SESSION_SECRET_KEY`), `HttpOnly`, `SameSite=Lax`, `Secure` solo in prod. Password hash con `passlib[bcrypt]`.
- **Tenant context**: parametro esplicito `tenant_id` sulle funzioni `db.*` (no `ContextVar`: si rompe attraverso i thread di `_run_in_proactor_thread` in [app/jobs.py](app/jobs.py)).
- **Scheduler per-utente**: file `data/local_owner.json` registra `{user_id, tenant_id}` al primo login riuscito; APScheduler carica solo i task di quell'utente. Cron parte solo se quel PC è acceso.
- **Fallback SQLite**: mantenuto durante la migrazione, rimosso dopo la Fase 6.

## Schema multi-tenant

### Nuove tabelle
- `tenants(id BIGSERIAL PK, name TEXT UNIQUE, slug TEXT UNIQUE, is_active BOOL, created_at TIMESTAMPTZ)`
- `users(id BIGSERIAL PK, tenant_id BIGINT NULL FK→tenants, email CITEXT UNIQUE, password_hash TEXT, role TEXT CHECK IN ('super_admin','tenant_user'), is_active BOOL, created_at TIMESTAMPTZ, CHECK super_admin↔tenant_id NULL)`

### Colonne aggiunte alle tabelle esistenti
`tenant_id BIGINT NOT NULL REFERENCES tenants(id)` su: `tasks`, `jobs`, `workflows`, `workflow_runs`, `workflow_edges`, `assets`, `asset_tags`, `contacts`, `threads`, `messages`, `orchestrator_messages`, `social_accounts`, `social_dm_log`, `recon_runs`, `recon_checkpoints`, `recon_visited`, `whatsapp_api_config`, `channel_config`.

`created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL` su: `tasks`, `workflows`, `assets`, `contacts`, `social_accounts`, `whatsapp_api_config`, `jobs`. Non usato per filtering nella v1, è metadato per future per-user permissioning.

### Knowledge base (globale + override per-tenant)
- `site_patterns` e `site_playbooks`: **restano globali** (no `tenant_id`).
- Nuove: `site_patterns_overrides(tenant_id, domain, …)` e `site_playbooks_overrides(tenant_id, domain, …)` con stessa struttura.
- Lookup helper in `app/db.py`: `get_site_pattern(domain, tenant_id)` → prima cerca override per tenant, fallback su globale.

### Sessioni social per-PC
- `social_accounts` resta tenant-shared.
- Nuova tabella `social_account_local_sessions(account_id, pc_fingerprint, session_dir, last_login_at, PRIMARY KEY (account_id, pc_fingerprint))` per tracciare quali PC del tenant hanno già la sessione cookie locale. `pc_fingerprint` = hash di hostname+mac o UUID generato al primo boot, salvato in `data/local_owner.json`. UI mostra "loggato sul PC `<fingerprint>`" e propone "fai login qui" se quel PC manca.
- Le directory dei profili browser (`data/whatsapp_sessions/<account_id>/`) restano su filesystem locale: presenza = sessione attiva.

### Indici
Composti su `(tenant_id, created_at DESC)` o `(tenant_id, status)` per le tabelle ad alto traffico (`assets`, `social_dm_log`, `jobs`).

## Auth & route admin

- `app/auth.py` (nuovo): `hash_password`, `verify_password`, `get_current_user` (dipendenza FastAPI che legge `request.session["user_id"]`, ricarica user+tenant, redirect 302 a `/login?next=…` o 401 se header `HX-Request`), `require_super_admin`.
- `app/routes/auth.py` (nuovo): `GET/POST /login`, `POST /logout`. Nessun signup pubblico.
- `app/routes/admin.py` (nuovo, gating super_admin): `/admin/tenants` CRUD, `/admin/users` CRUD, `/admin/tenants/{id}/switch` (super_admin imposta `active_tenant_id` in sessione per agire dentro un tenant).
- `app/main.py` aggiunge `SessionMiddleware` + middleware globale che redirige a `/login` se non autenticato (eccetto `/login`, `/logout`, `/static/*`).
- `app/templates/base.html`: user pill in header con email + tenant attivo + link admin (solo super_admin) + logout. Context processor in `app/templates.py` per iniettare `current_user` automaticamente.

## Fasi di implementazione

### Fase 1 — Scaffold auth + tenants (senza tenant filtering)
**File**: `pyproject.toml` (deps), `app/config.py` (`database_url`, `session_secret_key`), `app/db.py` (pool psycopg + SCHEMA Postgres + tabelle `tenants`/`users`), `app/auth.py` (nuovo), `app/routes/auth.py` (nuovo), `app/templates/login.html` (nuovo), `app/main.py` (middleware), `app/templates/base.html` (user pill).

**Fatto quando**: avvio app con `DATABASE_URL` + `BOOTSTRAP_SUPER_ADMIN_EMAIL/PASSWORD` in env → super_admin creato → login web funziona → routes esistenti continuano a girare (ancora senza filtro tenant).

### Fase 2 — Refactor db layer SQLite→Postgres (no tenant ancora)
**File**: tutto `app/db.py`: conversione meccanica `?` → `%s`, `cur.lastrowid` → `RETURNING id`, `sqlite3.Row` → `psycopg.rows.dict_row`. JSON-as-text → `JSONB` (`Jsonb(...)` per scrivere, dict diretto per leggere). Booleani `INTEGER 0/1` → `BOOLEAN`. `BLOB` (Fernet) → `BYTEA`. Riscrivere `init_db()` con `information_schema.columns` invece di `PRAGMA`. Setup Alembic (`alembic/env.py`, baseline revision).

**Rischio chiave**: bug subtili in conversione tipi (es. funzioni che fanno `json.loads(row["raw_json"])` ora ricevono dict). Mitigare con test di integrazione che girano contro PG.

**Fatto quando**: test suite passa contro Postgres, end-to-end di un task reale funziona identicamente.

### Fase 3 — `tenant_id` su tabelle + funzioni `db.*`
**File**: Alembic revision con `ADD COLUMN tenant_id` + backfill `UPDATE … SET tenant_id = (SELECT id FROM tenants WHERE slug='default')`. Aggiunta `created_by_user_id` dove indicato. Indici compositi. Funzioni `app/db.py`: tutte le `list_*/get_*/create_*/update_*/delete_*` ricevono `tenant_id` come parametro **obbligatorio** (non opzionale: forza l'errore subito). `get_*(id, tenant_id)` controlla `WHERE id=%s AND tenant_id=%s` (anti-IDOR). Nuove funzioni `tenants_*`/`users_*`. Helper `get_site_pattern(domain, tenant_id)` con lookup override→globale.

**Fatto quando**: nuovo test `tests/test_db_tenant_isolation.py` (2 tenant, asset/task isolati). `mypy --strict` su `app/db.py` per intercettare call site dimenticati.

### Fase 4 — Refactor route + UI super-admin
**File**: tutti gli ~70 endpoint in `app/routes/*.py` ricevono `Depends(get_current_user)` e propagano `tenant_id` alle chiamate `db.*`. Endpoint di creazione passano anche `created_by_user_id=current_user.id`. `app/routes/admin.py` nuovo. Template `app/templates/admin/*.html` nuovi. `app/main.py` registra route admin + middleware redirect-to-login. `app/jobs.py`: `_refresh_schedules`, `_poll_email_inbound`, `_poll_telegram_inbound`, `_ingest_inbound` filtrano per `local_owner.tenant_id` + `local_owner.user_id`. Audit di tutti i runner in `app/agent/` per propagare `task["tenant_id"]` (~30 file).

**Rischio chiave**: endpoint dimenticato = leak cross-tenant. Mitigare con `tests/test_tenant_isolation.py` che per ogni endpoint listante crea fixture in 2 tenant e verifica isolamento.

**Fatto quando**: smoke test manuale con 2 account di tenant diversi → ognuno vede solo le proprie risorse; super_admin accede a `/admin` e switcha tenant; cron schedulati di Alice partono solo sul PC di Alice.

### Fase 5 — Script di migrazione dati esistenti
**File**: `scripts/migrate_to_cloud.py` (nuovo). Entry-point `agentscraper-migrate` in `pyproject.toml`. Logica:
1. Verifica DB target vuoto (o `--force`).
2. Crea schema via `init_db()`.
3. Crea tenant "Default" + super_admin (email/password da env o prompt).
4. Per ogni tabella in ordine topologico (tenants→users→tasks→workflows→workflow_runs→workflow_edges→jobs→assets→asset_tags→contacts→threads→messages→orchestrator_messages→social_accounts→social_dm_log→recon_runs→recon_checkpoints→recon_visited→whatsapp_api_config→channel_config): `SELECT *` da SQLite, conversione tipi (bool 0/1→TRUE/FALSE, JSON text→`Jsonb`, timestamp ISO→`datetime.fromisoformat`, BLOB→bytes), `INSERT` con `tenant_id=default_id` e `created_by_user_id=super_admin_id` aggiunti.
5. Tabelle globali (`site_patterns`, `site_playbooks`): import senza `tenant_id`.
6. Reset sequenze: `SELECT setval(pg_get_serial_sequence('<tbl>','id'), MAX(id))` per ogni tabella.
7. Sanity check counts source vs target.
8. Supporto `--dry-run` e `--resume`.

I file `data/results/`, `data/uploads/`, `data/whatsapp_sessions/` restano sul filesystem del PC che esegue la migrazione: i path nel DB rimangono validi solo per quel PC, coerente con la scelta "file locali al client".

**Rischio chiave**: BLOB Fernet → BYTEA deve preservare i bytes integri (test esplicito: decrypt funziona post-migration). Timestamp ISO con `Z` finale → forzare UTC.

**Fatto quando**: migrazione su dump reale del `data/agentscraper.db` corrente produce DB cloud con counts identici; login super_admin → vede tutti i task/asset di "Default"; lancio un job dal cloud-DB → completa correttamente.

### Fase 6 — Testing, hardening, rollout
**File**: `tests/test_auth.py`, `tests/test_tenant_isolation.py`, `tests/test_admin_routes.py`, `tests/test_migration_script.py`. Rifinitura `local_owner.json` e scheduler per-utente in `app/jobs.py`. README aggiornato con "Multi-tenant setup". Rimozione del fallback SQLite (decisione finale dopo verifica stabilità).

**Fatto quando**: test verde, 2 PC reali con 2 utenti dello stesso tenant lavorano senza interferenze, cron di un utente parte solo sul suo PC.

## File critici da modificare

- [app/db.py](app/db.py) — refactor pesante (driver, schema, tenant_id su tutte le funzioni)
- [app/main.py](app/main.py) — middleware sessione + redirect login + montaggio route admin
- [app/config.py](app/config.py) — `database_url`, `session_secret_key`
- [app/jobs.py](app/jobs.py) — scheduler per-utente via `local_owner.json`
- [app/templates/base.html](app/templates/base.html) — user pill, link admin, logout
- [app/templates.py](app/templates.py) — context processor `current_user`
- Tutti i file in [app/routes/](app/routes/) — `Depends(get_current_user)` + propagazione `tenant_id`
- Tutti i runner in [app/agent/](app/agent/) — propagazione `tenant_id` alle chiamate `db.*`
- `pyproject.toml` — `psycopg[binary,pool]`, `passlib[bcrypt]`, `itsdangerous`, `alembic`
- Nuovi: `app/auth.py`, `app/routes/auth.py`, `app/routes/admin.py`, `app/templates/login.html`, `app/templates/admin/*.html`, `scripts/migrate_to_cloud.py`, `alembic/`

## Out of scope (esplicito)

- Per-user permissioning intra-tenant.
- Cloud storage per `data/results/`, `data/uploads/`, `data/whatsapp_sessions/`.
- Audit log persistente, password recovery via email, 2FA, rate limiting su login.
- Row-Level Security PostgreSQL (rinforzo futuro).
- Job leases SQL per cron concorrenti (stesso utente loggato su 2 PC contemporaneamente → cron può partire 2 volte; rischio raro accettato per ora).
- Sostituire Ollama localhost (ogni PC ha il suo).
- Sincronizzazione sessioni browser tra PC (un utente che vuole operare con un social account da un nuovo PC fa login Playwright lì).

## Verifica end-to-end

1. **Setup**: creare istanza Supabase/Neon, `.env` con `DATABASE_URL` e `SESSION_SECRET_KEY`. Avviare app → `init_db()` crea schema → bootstrap super_admin via env.
2. **Auth**: `/login` con credenziali super_admin → redirect a homepage, user pill visibile.
3. **Migrazione**: `agentscraper-migrate --target $DATABASE_URL` da PC con `data/agentscraper.db` esistente → counts identici tra source e target.
4. **Tenant CRUD**: `/admin/tenants` → creare tenant "Acme", creare user `alice@acme.com`.
5. **Isolamento**: login come Alice (browser 1) → vede solo task/asset/workflow di Acme. Login come super_admin (browser 2) → switch tenant "Default" → vede dati migrati.
6. **Job locale**: come Alice, creare task → `Start` → job parte sul PC di Alice → report generato in `data/results/` di Alice. Stesso task visibile a un secondo utente di Acme (Bob su un altro PC), ma il file di report è leggibile solo da Alice (path locale).
7. **Cron per-utente**: schedulare task come Alice. Spegnere PC Alice → cron non parte. Riaccendere → cron parte.
8. **Social account**: Alice crea social account WhatsApp e fa login Playwright sul suo PC. Bob (stesso tenant, altro PC) vede l'account ma `data/whatsapp_sessions/<account_id>/` non esiste sul suo PC → UI propone "fai login qui". Bob fa login → sessione locale aggiornata, `social_account_local_sessions` registra il suo `pc_fingerprint`.
9. **Knowledge base globale**: pattern imparato dal tenant Default è visibile (e usato dai runner) dal tenant Acme. Override creato da Acme ha precedenza per Acme ma non influenza Default.
10. **Test suite**: `pytest tests/` verde, in particolare `test_tenant_isolation.py` e `test_migration_script.py`.
