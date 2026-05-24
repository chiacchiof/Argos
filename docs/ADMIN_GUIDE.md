# Argos — Guida super-admin

Questa guida e' rivolta a chi ha il ruolo **`super_admin`**: gestisce la
piattaforma a livello di tenant, utenti e configurazioni cross-organization.

Per le funzionalita' standard (task, workflow, asset, outreach...) il
super-admin segue la stessa [USER_GUIDE.md](USER_GUIDE.md) — questa guida copre
solo l'**aggiuntivo** rispetto a un utente normale.

---

## Indice

1. [Il ruolo super-admin](#il-ruolo-super-admin)
2. [Dashboard `/admin`](#dashboard-admin)
3. [Gestione tenant `/admin/tenants`](#gestione-tenant)
4. [Gestione utenti `/admin/users`](#gestione-utenti)
5. [Flag "Memoria sito condivisa"](#flag-memoria-sito-condivisa)
6. [Filtro "View as tenant"](#filtro-view-as-tenant)
7. [Site Memory cross-tenant](#site-memory-cross-tenant)
8. [Vault chiavi LLM](#vault-chiavi-llm)
9. [Diagnostica DB e configurazione cloud](#diagnostica-db)
10. [Best practices operative](#best-practices)

---

## Il ruolo super-admin

Il super-admin ha visibilita' totale: vede i dati di tutti i tenant, puo'
crearne di nuovi, gestire utenti, e ha l'unico accesso alle pagine `/admin/*`.

| Capability | Tenant user | Super admin |
|---|---|---|
| Crea task / workflow / asset | ✓ (nel proprio tenant) | ✓ (in qualunque tenant via [View as tenant](#filtro-view-as-tenant)) |
| Lista globale assets/tasks/workflows | Solo i propri | Tutti, filtrabili per tenant |
| Crea tenant | ✗ | ✓ |
| Crea utenti | ✗ | ✓ |
| Vault chiavi LLM | Le proprie | Tutte (per supportare i tenant) |
| Memoria sito | Solo del proprio tenant + pool community se abilitato | Tutta, tutti i tenant |
| Pagine `/admin/*` | ✗ (403) | ✓ |

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
- **password_hash** (bcrypt o argon, mai loggata in chiaro).
- **role**: `super_admin` o `tenant_user`.
- **tenant_id**: obbligatorio se role=tenant_user; deve essere `None` se
  role=super_admin.
- **first_name + last_name** (anagrafica, opzionali).
- **is_active**: disattivare = blocco login senza eliminare.

### Operazioni

| Azione | Note |
|---|---|
| **Crea utente** | Form: email + password (min 6 char) + role + tenant_id (se tenant_user). |
| **Edit anagrafica** | Cambia first_name / last_name. |
| **Reset password** | Imposti tu la nuova password. L'utente la cambiera' al login successivo (TODO: forzare cambio). |
| **Toggle attivo/disattivato** | Self-protection: non puoi disattivare te stesso. |
| **Elimina** | Self-protection: non puoi eliminare te stesso. |

### Quando creare un super-admin in piu'

Conviene mantenere almeno 2 super-admin (in caso uno perda l'accesso). Se ne
esiste uno solo e si perde l'accesso, il recovery passa dal DB (manuale).

### Note sicurezza

- Le password sono hashate con costo computazionale alto (lentezza apposita
  per resistere a brute force). Login lento di ~200ms = normale.
- Sessioni firmate con `ARGOS_SECRET` in env. Cambia `ARGOS_SECRET` =
  **invalida tutte le sessioni**, costringendo tutti a re-login.
- Non riusare password tra tenant. Il super-admin deve avere una password
  separata da quella usata su altri sistemi.

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

Pagina di **configurazione DB runtime**. Mostra:
- DSN attivo (mascherato).
- Latenza ping (ms).
- Numero connessioni in pool.
- Stato migrazioni alembic (`current revision`).

Da qui puoi (con cautela):
- Cambiare DSN al volo (richiede restart per recover completo).
- Promuovere il DB locale a remoto (deploy Neon).
- Vedere errori recenti del pool.

Per le migrazioni schema vere e proprie:
- **branch-per-cambio**: ogni modifica schema vive su una branch git.
- LLM-led tramite `python scripts/db.py new` per generare la migration.
- `python scripts/db.py migrate` per applicare al DB locale.
- `pwsh scripts/deploy_to_neon.ps1` per promuovere a Neon — **SEMPRE con
  conferma esplicita**, mai automatico.

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
