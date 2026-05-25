# Argos — Guida super-admin

Questa guida e' rivolta a chi ha il ruolo **`super_admin`**: gestisce la
piattaforma a livello di tenant, utenti e configurazioni cross-organization.

Per le funzionalita' standard (task, workflow, asset, outreach...) il
super-admin segue la stessa [USER_GUIDE.md](USER_GUIDE.md) — questa guida copre
solo l'**aggiuntivo** rispetto a un utente normale.

---

## Indice

1. [I 3 ruoli del sistema](#i-3-ruoli)
2. [Dashboard `/admin`](#dashboard-admin)
3. [Gestione tenant `/admin/tenants`](#gestione-tenant)
4. [Gestione utenti `/admin/users`](#gestione-utenti)
5. [Onboarding: architect + operator](#onboarding-architect-operator)
6. [Flag "Memoria sito condivisa"](#flag-memoria-sito-condivisa)
7. [Filtro "View as tenant"](#filtro-view-as-tenant)
8. [Site Memory cross-tenant](#site-memory-cross-tenant)
9. [Vault chiavi LLM](#vault-chiavi-llm)
10. [Diagnostica DB e configurazione cloud](#diagnostica-db)
11. [Deploy schema + dati su Neon (script)](#deploy-neon)
12. [Best practices operative](#best-practices)

---

<a id="i-3-ruoli"></a>
## I 3 ruoli del sistema

Argos distingue 3 ruoli con UI e capacita' nettamente diverse:

| Ruolo | DB role | URL post-login | Cosa puo' fare |
|---|---|---|---|
| **Super-admin** | `super_admin` | `/admin` | Tutto. Crea tenant, crea utenti, accesso cross-tenant, vault LLM globale, diagnostica DB. |
| **Architect** | `tenant_architect` | `/` | UI completa: costruisce task/workflow, gestisce asset, configura LLM, pubblica agenti per gli operator. Vede solo il proprio tenant. |
| **Operator** | `tenant_user` | `/home` | UI semplificata: dashboard con agenti pubblicati, toolbox (live/agenda/storia), chat assistente, leads read-only + opt-out. Non crea ne' configura nulla. |

Capabilities side-by-side:

| Capability | Operator | Architect | Super admin |
|---|---|---|---|
| Login | ✓ → `/home` | ✓ → `/` | ✓ → `/admin` (default) |
| Vede dashboard operator (`/home`) | ✓ | ✗ (redirect a `/`) | ✗ (redirect a `/`) |
| Crea task / workflow | ✗ | ✓ | ✓ |
| Pubblica task come agente | ✗ | ✓ | ✓ |
| Lancia agente pubblicato (`/home`) | ✓ | indiretto | indiretto |
| Pianifica agente (cron) | ✓ (da `/home` Strumenti) | ✓ (da task detail) | ✓ |
| Vede leads (`/leads`) | ✓ read-only + opt-out | ✓ tramite `/assets` | ✓ + view-as-tenant |
| Vede results / report di un job | ✓ modal viewer | ✓ + edit | ✓ |
| Crea / modifica asset, contatti, social account | ✗ | ✓ | ✓ |
| Configura LLM keys, email, telegram, whatsapp | ✗ | ✓ | ✓ |
| Memoria sito | ✗ | ✓ del proprio tenant + pool community se abilitato | ✓ tutta |
| Vault chiavi LLM | ✗ | Le proprie | Tutte (per supporto tenant) |
| Pagine `/admin/*` | ✗ (403) | ✗ (403) | ✓ |
| Gating: cosa succede se digita URL fuori dal proprio scope | redirect a `/home` | redirect a `/` | accesso totale |

Il super-admin **non ha un tenant_id**: in `tenants_table` non figura. La
ContextVar `tenant_id` per le sue request e' `None` → le query non applicano
filtri, di default vede tutto.

Il super-admin **non ha un tenant_id**: in `tenants_table` non figura. La
ContextVar `tenant_id` per le sue request e' `None` → le query non applicano
filtri, di default vede tutto.

---

## Dashboard `/admin`

URL: [`/admin`](/admin)

Pannello con i contatori globali della piattaforma:
- numero tenant configurati,
- numero utenti totali,
- numero super-admin (per coerenza: dovrebbero essere ≥1).

Link alle sezioni di gestione (tenant + utenti).

---

## Gestione tenant `/admin/tenants`

URL: [`/admin/tenants`](/admin/tenants)

### Cos'e' un tenant

Un **tenant** rappresenta un'azienda / cliente. E' l'unita' di isolamento dei
dati: utenti / task / asset / outreach / messaggi appartengono a un tenant e
**non sono visibili agli altri tenant** (salvo il pool community della
[Site Memory](#site-memory-cross-tenant) opt-in).

Ogni tenant ha:
- **id** (autoincrementale).
- **name** — nome parlante mostrato nella UI (es. "Edg Marketing").
- **slug** — identificatore url-safe auto-derivato (es. "edg-marketing").
- **is_active** — flag attivo/disattivo. Disattivare blocca i login degli
  utenti del tenant, ma i dati restano in DB.
- **site_memory_shared** — flag accesso pool community (vedi sezione dedicata).
- **users_count** — quanti utenti attivi ha (computato).

### Operazioni

| Azione | Effetto |
|---|---|
| **Crea nuovo tenant** | Form in alto. Nome obbligatorio. Slug auto-derivato se vuoto. |
| **Attiva / Disattiva** | Disattivare blocca i login degli utenti del tenant ma preserva i dati. |
| **Toggle memoria sito** | On = il tenant accede al pool community. Off = vede solo le sue righe. |
| **Elimina** | **Cascata**: utenti collegati eliminati. Asset/task del tenant restano in DB (orfani da pulire manualmente — TODO). |

### Sequence "onboarding nuovo cliente"

1. `/admin/tenants` → crea tenant con nome parlante.
2. `/admin/users` → crea un utente `tenant_user` con `tenant_id` = id appena creato.
3. (Opzionale) `/admin/tenants` → attiva "Memoria sito condivisa" se vuoi che il
   cliente acceda al sapere collettivo.
4. (Opzionale) `/accounts/llm-keys` → crea una chiave LLM dedicata per quel
   tenant, oppure lascia che il tenant_user ne configuri una propria.
5. Comunica le credenziali al cliente.

---

## Gestione utenti `/admin/users`

URL: [`/admin/users`](/admin/users)

### Cos'e' un utente

Un utente Argos ha:
- **email** (case-insensitive, lowercased — fa anche da username login).
- **password_hash** (bcrypt, mai loggata in chiaro).
- **role**: `super_admin` / `tenant_architect` / `tenant_user`.
- **tenant_id**: obbligatorio se role=tenant_architect o tenant_user;
  `NULL` se role=super_admin.
- **first_name + last_name** (anagrafica, opzionali).
- **is_active**: disattivare = blocco login senza eliminare.

Il CHECK constraint del DB applica:

```sql
CHECK (role IN ('super_admin', 'tenant_architect', 'tenant_user'))
CHECK (
  (role = 'super_admin' AND tenant_id IS NULL)
  OR (role IN ('tenant_architect','tenant_user') AND tenant_id IS NOT NULL)
)
```

### Operazioni

| Azione | Note |
|---|---|
| **Crea utente** | Form: email + password (min 6 char) + role + tenant_id (se non super_admin). Il dropdown role nel form `/admin/users` ha tutti e 3 i ruoli. |
| **Edit anagrafica** | Cambia first_name / last_name. |
| **Reset password** | Imposti tu la nuova password. Comunica all'utente via canale sicuro. |
| **Toggle attivo/disattivato** | Self-protection: non puoi disattivare te stesso. |
| **Elimina** | Self-protection: non puoi eliminare te stesso. |

### Quando creare un super-admin in piu'

Conviene mantenere almeno 2 super-admin (in caso uno perda l'accesso). Se ne
esiste uno solo e si perde l'accesso, il recovery passa dal DB (manuale).

### Note sicurezza

- Le password sono hashate con bcrypt (costo computazionale alto, lentezza
  apposita per resistere a brute force). Login lento di ~200ms = normale.
- Sessioni firmate con `ARGOS_SECRET` in env. Cambia `ARGOS_SECRET` =
  **invalida tutte le sessioni**, costringendo tutti a re-login.
- Non riusare password tra tenant. Il super-admin deve avere una password
  separata da quella usata su altri sistemi.

### Migrazione utenti legacy (pre-3-ruoli)

Prima del rilascio della UI Operator esistevano solo `super_admin` e
`tenant_user`. Tutti gli utenti `tenant_user` di allora avevano poteri da
architect. Per evitare di "declassarli" automaticamente, e' previsto un flag
env **`ARGOS_PROMOTE_LEGACY_USERS`**:

| Flag in `.env` | Effetto al primo boot |
|---|---|
| `ARGOS_PROMOTE_LEGACY_USERS=true` | Tutti i `tenant_user` esistenti vengono promossi a `tenant_architect` (UPDATE una tantum). |
| Non settato (default) | Gli utenti restano `tenant_user` → vedranno la nuova UI semplificata; perdono accesso a `/`, `/tasks`, ecc. |

Se vuoi forzare l'inquadramento dei nuovi utenti come operator, lascia il
flag spento. Se la tua installazione era "tutti architect", settalo a `true`
prima del primo riavvio post-deploy.

---

<a id="onboarding-architect-operator"></a>
## Onboarding: architect + operator nello stesso tenant

Tipico setup di un cliente con team misto:

1. **Crea tenant** da `/admin/tenants` (es. "Acme Marketing", slug `acme`).
2. **Crea 1 utente architect** per il tenant:
   - role = `tenant_architect`
   - tenant_id = appena creato
   - email: `tech@acme.it` (il referente tecnico)
3. **Crea 1+ utenti operator** per lo stesso tenant:
   - role = `tenant_user`
   - tenant_id = stesso
   - email: `vendite@acme.it`, `marketing@acme.it`, ...
4. (Opzionale) **Crea una chiave LLM per il tenant** in `/accounts/llm-keys`
   con `owner_user_id` = l'architect, cosi' non deve farlo lui dopo.
5. (Opzionale) **Attiva il flag `site_memory_shared`** sul tenant se vuoi
   che l'architect veda la memoria community.

Comunica le credenziali ai 2+ utenti:
- Architect → riceve link `/`, vede UI completa, costruisce e **pubblica**
  gli agenti.
- Operator → riceve link `/home`, vede la dashboard semplificata. Vede gli
  agenti SOLO dopo che l'architect ne ha pubblicato almeno uno.

### Quanti operator per tenant?

Quanti vuoi. Tutti gli operator dello stesso tenant condividono:
- Stessa lista di agenti pubblicati
- Stessa inbox `/messages`
- Stessa cronologia esecuzioni
- Stessa chat orchestrator (history persistente tenant-scoped)

L'operator non ha "i suoi job": quando lancia un agente, il job appartiene
al tenant + viene linkato all'architect proprietario del task (per
risoluzione credenziali).

---

## Flag "Memoria sito condivisa"

URL gestione: [`/admin/tenants`](/admin/tenants) → colonna "Memoria sito"

### Cosa controlla

Il flag `site_memory_shared` di un tenant determina se **quel tenant accede al
pool community** della [Site Memory](#site-memory-cross-tenant).

| Flag | Cosa vede il tenant nella pagina `/site_memory` |
|---|---|
| 🔒 **isolata** (off, default) | Solo le righe con `tenant_id` uguale al suo + righe globali (`tenant_id = NULL`, es. policy di sistema). |
| 🌐 **condivisa** (on) | Le proprie + tutte le righe con `visibility = 'shared'` di altri tenant + globali. |

### Granularita'

Il flag controlla la **lettura** del pool. Non rende automaticamente
condivise le righe del tenant. Per condividere una riga specifica:
- l'utente del tenant clicca il toggle 🌐 nella pagina `/site_memory`,
- oppure interviene l'**auto-promote community** del sistema (vedi sotto).

### Quando attivare "condivisa"

- **Premium feature**: solo per i clienti che pagano un piano con accesso al
  pool collettivo.
- **Tenant interni**: brand multipli della tua organizzazione che vuoi far
  collaborare implicitamente.
- **Beta tester**: clienti early-access che accettano di contribuire al pool.

Lascia OFF (default) per clienti isolati che non vogliono essere
"contaminati" dalle scelte di altri.

---

## Filtro "View as tenant"

Le list-page principali del super-admin (`/`, `/workflows`, `/assets`,
`/qualified`, `/inbox`, `/social/accounts`) hanno un **dropdown "Vista
super-admin"** in alto.

Funziona cosi':
- **Default = "tutti i tenant"**: vedi i dati di tutti i tenant insieme.
- **Selezioni un tenant** dal dropdown: vedi solo i dati di quel tenant
  (come se fossi loggato come uno dei suoi utenti).

URL: aggiunge il parametro `?as_tenant_id=N`. E' bookmarkabile.

Quando usarlo:
- **Supporto cliente**: per riprodurre cio' che vede l'utente del tenant.
- **Debug**: per isolare i dati di un tenant specifico durante test.
- **Audit**: per verificare l'attivita' di un singolo cliente.

Il filtro NON cambia il comportamento di scrittura: se crei un task mentre sei
in "vista tenant X", il task viene creato senza tenant_id (super-admin
default) — non automatically nel tenant X. Per creare risorse in un tenant
specifico, devi loggare come utente di quel tenant, o usare l'API esplicita.

---

## Site Memory cross-tenant

URL: [`/site_memory`](/site_memory)

Vedi la pagina + la guida espandibile in-app per la spiegazione completa. Qui
ricapitolo solo i punti che riguardano il super-admin:

### Cosa vede il super-admin

- **Tutte** le righe di pattern / playbook / intelligence / policy, di
  qualunque tenant.
- Una colonna **"Tenant"** che mostra il nome parlante del proprietario di
  ogni riga (es. "Edg Marketing", "DTC Lab"), oppure "globale" se la riga non
  ha tenant_id (policy di sistema).
- Le righe **`visibility=shared`** sono evidenziate con un badge 🌐.
- Le policy auto-create dalla community hanno `source=community` e un badge
  dedicato.

### Auto-promote community

Quando **3 tenant indipendenti** registrano intelligence negativa sullo stesso
dominio (almeno 1 `fail_count` e 0 `success_count`), il sistema:
1. **Promuove tutte le righe `private`** di quel dominio a `shared`.
2. **Crea una `scraping_policy`** con `source='community'`, `action='warn'`,
   `visibility='shared'`.

I tenant con flag `site_memory_shared=ON` vedono subito il warning quando
provano lo stesso sito. I tenant `OFF` continuano isolati.

Il threshold (3) e' configurato in [app/db.py](../app/db.py)
`auto_promote_to_community_pool(threshold=3)`. Per cambiarlo, edita il codice
+ test E2E + deploy.

### Policy globali (tenant_id NULL)

Puoi creare policy che valgono per **tutti i tenant** inserendole con
`tenant_id=NULL`. Attualmente non c'e' UI dedicata: vanno create via SQL o
via tool dell'orchestrator con privilegi appositi. Da considerare per regole
hard come "mondocamgirl.com → force_skip" che valgono universalmente.

---

## Vault chiavi LLM

URL: [`/accounts/llm-keys`](/accounts/llm-keys)

### Architettura

Le chiavi LLM (OpenAI, Anthropic, Google, Mistral, OpenRouter, DeepSeek...)
sono salvate cifrate (Fernet) in DB. Sono **tenant-scoped**: ogni chiave
appartiene a un tenant + a un owner_user.

Per i task:
- L'utente sceglie quale chiave usare dal dropdown nel form task.
- Se la chiave manca / scaduta / quarantined, il task fallisce con errore
  esplicito.

### Cosa puo' fare il super-admin

- Vedere tutte le chiavi di tutti i tenant ([`/accounts/llm-keys`](/accounts/llm-keys)).
- Creare chiavi a nome di un tenant (assegnando `owner_user_id` a un utente di
  quel tenant). Utile per **onboarding**: configuri tu la chiave master del
  cliente.
- Toggle status active/quarantine quando una chiave viene compromessa o supera
  i token cap.

### Quando creare una chiave "globale" per tutta la piattaforma

Sconsigliato: ogni tenant dovrebbe avere le sue chiavi per:
- billing pulito (chi consuma cosa),
- isolation (revoca chiave su tenant compromesso senza impattare altri),
- rispetto dei limiti rate-limit per chiave.

Eccezione: un tenant "Argos system" interno per i task di sistema (es.
auto-extract di test, master_summary generato dal framework).

---

## Diagnostica DB

URL: [`/dbconfig`](/dbconfig)

Pagina di **configurazione DB runtime**. Login dedicato (`DBadmin`/password
in [routes/dbconfig.py](app/routes/dbconfig.py)), separato dal login utente.

Mostra:
- **DSN attivo** (mascherato) — l'origine: `.env` o `/dbconfig override
  (data/db_config.enc)`.
- **Override attivo**: badge `Sì`/`No`.
- **Switch rapido (preset)**: dropdown con i DB pre-configurati (es. "Locale
  dev", "Neon Production"). Selezione + Applica → il file cifrato
  `data/db_config.enc` viene aggiornato. **Richiede riavvio app** per
  attivare.
- **Connection string custom**: textarea per DSN arbitrarie (caso fuori
  preset). Stesso meccanismo, richiede riavvio.
- **Rimuovi override**: cancella `data/db_config.enc`. Al prossimo riavvio
  l'app usa `DATABASE_URL` di `.env`.

I preset disponibili nel dropdown sono configurati via env var:
- `local` — hardcoded `postgresql://postgres:postgres@localhost:5432/agentscraper_dev` (non e' segreto).
- `neon` — letto da `DBCONFIG_PRESET_NEON_DSN` in `.env`. Se non settato, il preset Neon non appare.
- `staging` — opzionale da `DBCONFIG_PRESET_STAGING_DSN`.

Le DSN dei preset cloud sono **risolte server-side** quando l'utente applica
un preset: il browser invia solo la chiave (es. `neon`), mai la connection
string in chiaro.

---

<a id="deploy-neon"></a>
## Deploy schema + dati su Neon (script)

Lo sviluppo locale lavora su `agentscraper_dev` (Postgres docker locale).
Per promuovere modifiche su Neon (produzione) ci sono **2 script
complementari**:

### 1. `scripts/db.py promote` — schema migration

Allinea lo schema alembic di Neon con local. Comportamenti:

| Scenario | Comportamento |
|---|---|
| Neon ha `alembic_version` < local | `alembic upgrade head` su Neon (applica le revision pending). Pytest gating obbligatorio salvo `--skip-tests`. |
| Neon ha `alembic_version` == local | "Gia' allineati. Niente da promuovere." |
| Neon ha lo schema ma manca `alembic_version` (es. dopo reset) | `alembic stamp <local_head>` — marca la DB alla revision corrente senza ri-applicare le migration (lo schema c'e' gia'). |

```powershell
# Wrapper interattivo con backup db_config.enc e safety check Neon DSN
pwsh scripts/deploy_to_neon.ps1

# Solo schema (skip backfill contacts→assets)
pwsh scripts/deploy_to_neon.ps1 -SkipBackfill

# Diretto, senza pytest gating (usalo solo se sai cosa stai facendo):
python scripts/db.py promote --skip-tests -y
```

Il wrapper PowerShell `deploy_to_neon.ps1`:
1. Sposta temporaneamente `data/db_config.enc` → `.bak` (cosi' i comandi
   risolvono Neon, non l'override locale).
2. `db.py status` mostra alembic versione locale + Neon.
3. Chiede conferma.
4. `db.py promote` esegue il deploy schema.
5. (Opzionale) `backfill_contacts_to_assets.py --dry-run` + apply.
6. (Finally) ripristina `data/db_config.enc`.

### 2. `scripts/copy_data_to_neon.py` — data copy

Copia le righe del DB locale su Neon **idempotentemente** (`INSERT ... ON
CONFLICT (id) DO NOTHING` per le tabelle con PK seriale). Le righe gia'
presenti su Neon vengono **preservate** (no overwrite).

```bash
# Dry-run: mostra cosa farebbe, conta righe per tabella
python scripts/copy_data_to_neon.py --dry-run

# Apply (con conferma interattiva)
python scripts/copy_data_to_neon.py --apply

# Apply senza conferma
python scripts/copy_data_to_neon.py --apply -y

# Check conflitti di PK fra tabelle critiche (tenants, users)
python scripts/copy_data_to_neon.py --dry-run --check-conflicts
```

L'ordine di copia rispetta le FK: `tenants → users → llm_api_keys → tasks
→ workflows → jobs → assets → asset_tags → contacts → threads → messages
→ ...` Vedi `TABLES_IN_ORDER` in `scripts/copy_data_to_neon.py` per la
lista completa.

**Risincronizza automaticamente le SEQUENCE** dei PK al `MAX(id)+1` dopo
ogni tabella, per evitare collisioni future quando l'app crea nuove righe
su Neon.

**Limiti del modello ON CONFLICT DO NOTHING**:
- Non aggiorna righe esistenti su Neon (es. se hai cambiato `task.cron` o
  `is_published_agent` su local, queste modifiche NON si propagano).
- Per propagare UPDATE serve uno script ad-hoc — vedi codice di sync
  manuale del 2026-05-25 che propaga i campi `agent_*` + `cron` dei task
  pubblicati.

**Risoluzione DSN Neon** (per i 2 script): cerca in ordine
1. `NEON_DATABASE_URL` env esplicita,
2. `DBCONFIG_PRESET_NEON_DSN` env (stesso preset del dropdown `/dbconfig`),
3. `c:/tmp/neon_url.txt` (file legacy),
4. `data/db_config.enc` (rifiutata se contiene `localhost` per sicurezza).

### Flusso tipico di rilascio

Ogni volta che ho una nuova feature pronta:

```powershell
# 1. Verifica stato attuale
python scripts/db.py status

# 2. Se servono schema change: applica su local
python scripts/db.py migrate

# 3. Quando pytest e' verde + manual test OK su local
#    → promuovi schema su Neon (CHIEDE CONFERMA)
pwsh scripts/deploy_to_neon.ps1 -SkipBackfill

# 4. Se servono dati locali su Neon (es. nuovi task pubblicati come agenti)
python scripts/copy_data_to_neon.py --dry-run    # cosa farebbe
python scripts/copy_data_to_neon.py --apply -y   # esegui

# 5. (Eventuale) propagazione manuale UPDATE per campi cambiati su righe
#    gia' su Neon — vedi `task.cron`, `is_published_agent`, ecc.

# 6. Git commit + push del codice corrispondente
git add app/ static/ tests/ scripts/ docs/
git commit -m "..."
git push
```

### Migrazioni alembic

- **branch-per-cambio**: ogni modifica schema vive su una branch git.
- LLM-led tramite `python scripts/db.py new "<desc>"` per generare la
  revision vuota.
- Edita `alembic/versions/XXXX_*.py` (upgrade + downgrade a mano).
- `python scripts/db.py migrate` per applicare al DB locale.
- `pwsh scripts/deploy_to_neon.ps1` per Neon — **SEMPRE con conferma
  esplicita**, mai automatico.

---

## Best practices operative

### Onboarding nuovo cliente

1. Crea tenant + utente iniziale.
2. Pre-configuragli (almeno) una chiave LLM utilizzabile.
3. Pre-configura account email per outreach se serve.
4. Decidi se attivare `site_memory_shared` (di solito **off** all'inizio).
5. Comunica credenziali via canale sicuro.

### Disattivazione cliente

1. `/admin/tenants` → toggle "Disattiva" (blocca login, preserva dati).
2. Conserva dati per N giorni (GDPR / retention policy interna).
3. Poi `Elimina` (cascata utenti). **Asset/task del tenant restano**
   orfani in DB — puliscili manualmente da SQL se serve.

### Rotazione segreti

- **`ARGOS_SECRET`** (cifratura sessioni + credenziali): cambialo solo se
  compromesso. Cambio = tutti gli utenti devono re-login E tutte le chiavi
  cifrate vanno re-encryptate (vedi script in `scripts/`).
- **Chiavi LLM**: rotation periodica consigliata (90 gg). Toggle quarantine
  della vecchia + crea nuova + assegna ai task.
- **Account email/social**: rotation password se l'account e' stato compromesso.

### Backup

- DB primary su Neon: gestisce backup automatici (Point-in-Time Restore).
- DB locale: backup manuale via `pg_dump` se sviluppi su dev DB.
- File generati dai task (`data/results/`): non sono in DB, backupp-ali a
  parte se sono storici importanti.

### Audit / log

- Login: tracciati con timestamp + IP nel session log.
- Operazioni admin: ogni create/update/delete passa per logger Python con
  livello INFO. Configura LOG_LEVEL=INFO in produzione e raccogli i log.
- Per audit cross-tenant approfondito: query SQL direttamente in
  `audit_log_table` (se schema ha la tabella audit).

### Monitoring

- `/dbconfig`: stato pool DB e latenza.
- `/admin`: counters globali (sanity check).
- Task in running prolungato: lista in `/` filtrata per status_tag=running.
  Se un task "running" da >2h e' probabilmente bloccato — kill manuale del job.

### Comportamenti vietati documentati

Tenere d'occhio:
- **mondocamgirl.com / camlive.com**: vietato traffico (problemi email
  segnalati). Crea policy `force_skip` se non gia' presente.

### Quando ci sono dubbi

- I dati di chi appartengono?  → guarda `tenant_id` sulla riga + nome parlante
  nel filtro tenant in `/site_memory` o nelle list-page.
- Perche' un task ha fallito? → apri il task detail, vai su "Cronologia run",
  apri l'ultimo report.md, leggi i log + l'analisi del master_summary.
- Perche' un utente non vede X? → controlla che il tenant_user appartenga al
  tenant giusto, che il tenant sia attivo, e (per /site_memory) che il flag
  `site_memory_shared` sia coerente con cio' che ti aspetti.

---

Domande / proposte → apri una issue interna nel repo, o discuti su Slack
canale `#argos-ops`.
