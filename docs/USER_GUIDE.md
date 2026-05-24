# Argos — Guida utente

Questa guida e' rivolta all'utente standard di un tenant. Spiega che cos'e' la
piattaforma, come si organizza il lavoro e come usare ogni pagina della UI.

Per le funzioni riservate al super-admin (creazione tenant, gestione utenti,
filtri cross-tenant) vedi [ADMIN_GUIDE.md](ADMIN_GUIDE.md).

---

## Indice

1. [Cos'e' Argos](#cose-argos)
2. [Login e ruoli](#login-e-ruoli)
3. [I 4 oggetti centrali](#i-4-oggetti-centrali)
4. [Tasks — come creare e lanciare un'attivita'](#tasks)
5. [Workflows — orchestrare task in cascata](#workflows)
6. [Assets — il database centralizzato](#assets)
7. [Qualified — selezionare audience dai risultati](#qualified)
8. [Outreach — email, WhatsApp, social DM](#outreach)
9. [Inbox — conversazioni con i contatti](#inbox)
10. [Social Accounts — gli account "sender"](#social-accounts)
11. [Orchestrator — la chat AI che pianifica](#orchestrator)
12. [Site Memory — la memoria del framework](#site-memory)
13. [Settings — chiavi LLM e account canali](#settings)
14. [Glossario](#glossario)

---

## Cos'e' Argos

Argos e' una piattaforma multi-tenant di **lead generation e outreach automatizzato**.
Estrae profili / dati strutturati da siti web (scraping intelligente), li
qualifica con un LLM, e li usa come audience per campagne di outreach via
email, WhatsApp o DM social.

Tutto e' organizzato in **tenant** (aziende isolate). I tuoi dati sono visibili
solo a te e ai membri del tuo tenant — mai ad altri tenant — salvo il pool
condiviso opzionale della [Site Memory](#site-memory).

Il flusso tipico:

```
1. Scraping     → ottieni asset (profili, link, contatti)
2. Qualifier    → un LLM marca gli asset utili come "qualified"
3. Outreach     → contatti gli asset qualified via email / WA / DM
4. Inbox        → leggi e rispondi alle reply
```

Tutto questo lo configuri come **task** (singole attivita') o come **workflow**
(catena di task in cascata).

---

## Login e ruoli

- Vai a `/login`, inserisci email + password fornite dal tuo super-admin.
- Dopo il login arrivi alla **dashboard** (`/`) con la lista dei tuoi task.
- Per cambiare password o uscire: link in alto a destra.

Esistono due ruoli:

| Ruolo | Cosa vede |
|---|---|
| **tenant_user** | Solo i dati del proprio tenant. La maggior parte degli utenti rientra qui. |
| **super_admin** | Tutti i tenant. Gestisce creazione tenant + utenti. Vedi [ADMIN_GUIDE.md](ADMIN_GUIDE.md). |

---

## I 4 oggetti centrali

| Oggetto | Cosa e' | Esempi |
|---|---|---|
| **Task** | Un'attivita' autonoma con un agente + un obiettivo | "Scrap profili tryst.link", "Qualifica come 'escort italiana'", "Manda DM Instagram" |
| **Workflow** | DAG di task in cascata (passaggi a catena) | Scrap → Qualifier → Outreach Email |
| **Asset** | Una riga di dato strutturato (profilo / contatto / link) prodotta da un task | Un profilo Instagram, un'email, una pagina aziendale |
| **Audience** | Un sottoinsieme di asset (filtrati / qualificati) usato per fare outreach | "Tutti gli asset con qualifier=ig_lifestyle_it e score>=7" |

---

## Tasks

URL: [`/`](/) o [`/?type=scraping`](/?type=scraping)

### Cos'e' un task

Un task e' un'unita' di lavoro indipendente. Ha:
- **agent_mode** — il "motore" che lo esegue (vedi tabella sotto).
- **provider + model** — quale LLM usa (es. `ollama` + `qwen3-coder:30b`,
  oppure `openai` + `gpt-4o-mini`).
- **input/target** — URL da scrapare, asset da qualificare, contatti a cui
  mandare messaggi, eccetera.
- **output** — un set di file in `data/results/{task_id}/{run}/`: un
  `report.md`, un `profiles.jsonl`, eventualmente un `todo.md`.

### Tipi di task (agent_mode)

| agent_mode | Scopo | Quando usarlo |
|---|---|---|
| `bulk_extract` | Estrae elenchi da pagine indice (es. `/escorts/milan` con tutti i profili in elenco) | Sito ben strutturato, lista visibile a primo colpo |
| `site_explorer` | Naviga il sito seguendo link, costruisce mappa, ne estrae profili | Sito complesso, scoperta automatica |
| `browser_use` | Esegue un agente AI con un browser reale (Playwright) | Sito con JS pesante, anti-bot, login richiesto |
| `auto_extract` | Decide automaticamente fra le 3 strategie sopra | Default quando non sai cosa scegliere |
| `recon_social` | Cerca handle social (IG/TT/FB/WA) per asset esistenti | Arricchimento contatti dopo lo scraping |
| `qualifier` | Marca asset come `qualified` / `rejected` in base a un prompt LLM | Filtrare asset utili dopo lo scraping |
| `outreach` | Manda email ai contatti qualificati | Campagne email |
| `outreach_whatsapp` | Manda WhatsApp (browser o API) | Campagne WA |
| `outreach_social` | Manda DM social (IG/TT/FB) | Campagne DM |
| `responder` | Risponde automaticamente alle reply in inbox (LLM) | Conversazioni autopilotate |

### Creare un task

1. Vai su [`/tasks/new`](/tasks/new).
2. Compila: **nome**, **agent_mode**, **provider/model**, **obiettivo** (o
   target URL / target asset_ids a seconda del mode).
3. Salva → arrivi alla pagina detail del task.
4. Clicca **▶ Esegui** per lanciarlo. Si crea un **job** (vedi "Jobs").

Quando lanci un task, viene avviato un job in background. La pagina detail
mostra in tempo reale i log + i file generati man mano.

### Pagina detail del task

URL: `/tasks/{id}`

Cosa trovi:
- Header con stato + ultimo job + rating
- Configurazione (modificabile via `/tasks/{id}/edit`)
- Cronologia run (link a ogni `report.md`)
- File generati (clic per visualizzarli)
- Comandi rapidi: **▶ Esegui**, **⏸ Interrompi job in corso**, **Modifica**

### Risultati di un task

URL: `/tasks/{id}/results`

Mostra l'elenco dei file generati dall'ultima run. I file `.jsonl` hanno un
viewer dedicato (paginazione + preview pretty-JSON).

### Filtri della lista task

Nella lista principale puoi filtrare per:

- **Tipo** (tab in alto): Scraping, Qualifier, Outreach, Responder, Altri.
- **Status_tag**: il task ha esito positivo / negativo / in corso.
- **Author**: "I miei task" (default) vs "Tutto il tenant".
- **Search**: ricerca testuale su nome + objective + agent_mode.

---

## Workflows

URL: [`/workflows`](/workflows)

### Cos'e' un workflow

Un workflow e' una **catena di task** collegati da edges (frecce). Quando un
task termina, eventuali task downstream connessi via edge vengono lanciati
**automaticamente** con gli artifact prodotti dal task precedente.

Esempio classico:

```
[Task Scrap]  →  [Task Qualifier]  →  [Task Outreach Email]
   (root)         (riceve asset_ids   (riceve asset_ids
                  scrappati)          qualified)
```

Ogni task del workflow resta comunque **lanciabile da solo** (il workflow non
"contiene" i task, li **collega**).

### Creare un workflow

1. Vai su [`/workflows/new`](/workflows/new), dai un nome.
2. Apri il workflow appena creato.
3. **Aggiungi task** come nodi (puoi creare un nuovo task o riusarne uno esistente).
4. **Connetti con un edge** trascinando dal nodo upstream al downstream.
5. Configura l'**artifact passing**: cosa passa al task successivo
   (es. "passa la lista di `asset_ids` qualificati" — il sistema sa quale colonna
   leggere dall'output del task upstream).

### Eseguire un workflow

- Pulsante **▶ Esegui workflow**: lancia i task root (quelli senza edge in
  ingresso). Il sistema scheduler avanza la catena automaticamente man mano
  che i task terminano.
- La cronologia dei run e' visibile sotto la sezione "Recent runs".

### Disabilitare un task / un edge

- **Toggle disabilita edge**: il task downstream non viene piu' triggerato
  automaticamente (utile per debug).
- **Disabilita workflow intero**: rimane in DB ma non si puo' lanciare.

---

## Assets

URL: [`/assets`](/assets)

### Cos'e' un asset

Un asset e' una riga di dato strutturato (profilo IG / pagina aziendale /
contatto / blog post). Lo producono i task di scraping/recon e li alimenta in
DB con `status='new'`.

Tipi di asset comuni:
- `ig_profile`, `tt_profile`, `fb_profile` — profili social
- `escort_profile`, `model_profile` — profili da directory
- `business_page` — pagine aziendali con email/telefono
- `web_url` — link generici

Un asset ha:
- **title** + **subtitle** + **profile_url**
- **status** (`new`, `qualified`, `rejected`, `archived`)
- **tags** (key/value coppie tipo `country:it`, `lang:it`, `qualifier_lifestyle:7`)
- **social_json** (handle social arricchiti, es. da `recon_social`)
- **outreach_status** + **email/whatsapp/telegram** (popolati dopo l'outreach)
- **raw_json** (il payload completo prodotto dal runner)

### Filtri della lista assets

In alto trovi:
- **Tipo** dropdown — filtra per asset_type.
- **Status** — new/qualified/rejected/archived.
- **Search** — match testuale su title + raw_json.
- **Tag filter widget** (sezione "Tag avanzati") — fino a 6 slot
  `tag_key/tag_value` in AND / OR / espressione custom.
- **has_contacts / has_social** — toggle "solo asset con almeno un contatto".

### Operazioni sugli asset

- **Apri detail** (clic sulla riga): vedi `raw_json` completo, social arricchiti,
  cronologia outreach.
- **Modifica**: cambia status, aggiungi tag manualmente, blacklisting
  (`outreach_status='optedout'`).
- **Bulk delete**: seleziona righe + tasto "elimina selezionati".
- **Export CSV**: scegli i campi da esportare (modale categorizzato).
- **Duplicati** (`/assets/duplicates`): candidati dedup proposti dal sistema,
  da unire o rifiutare uno a uno.

### Asset manuali

Puoi inserire un asset a mano da [`/assets/new`](/assets/new): utile per
inseire contatti VIP fuori-flusso che vuoi includere in una campagna.

---

## Qualified

URL: [`/qualified`](/qualified)

E' una **vista filtrata** di `/assets` che mostra solo i record con
`status='qualified'` (o `rejected` o `both`).

### Selezionare qualifier

Nella sidebar (o nel filtro top) selezioni uno o piu' qualifier:

- `lifestyle_it` — asset valutati positivi dal qualifier "lifestyle italiano"
- `business_owner` — asset valutati come imprenditori
- `escort_milano_it` — escort italiane con base Milano

Sono i **tag** che il task qualifier ha aggiunto agli asset. Ogni qualifier ha
uno **score 1-10** registrato come tag `qualifier_<slug>:<score>`.

### Creare un'audience da Qualified

Tre vie:

1. **Seleziono N righe + clicco "Crea task da selezione"** → si apre il form
   task con quelle audience-ids gia' popolate.
2. **Filtro la vista + "Seleziona tutti i filtrati" + crea task** → l'audience
   include tutti gli asset che matchano i filtri attivi (fino a 10000).
3. **Append a un task esistente**: "Aggiungi questi asset a un task outreach in
   corso" — utile per estendere una campagna senza ri-creare il task.

---

## Outreach

L'outreach non e' una pagina dedicata: si configura come **task** (`outreach`,
`outreach_whatsapp`, `outreach_social`). Vedi sopra in "Tasks".

### Email outreach (`outreach`)

Richiede:
- Almeno un **account email** configurato in [`/accounts/email`](/accounts/email)
  (SMTP/IMAP cifrato in Fernet).
- Asset target con campo `email` non vuoto + `outreach_status != optedout`.
- Un **prompt** per generare il testo del messaggio (oppure un template fisso).

Il runner:
- Personalizza il messaggio (LLM) per ogni asset.
- Manda via SMTP, registra in `social_dm_log` analogo + traccia il `message_id`.
- Apre un **thread inbox** per ogni invio (per ricevere reply).

### WhatsApp outreach (`outreach_whatsapp`)

Due varianti:

| Variante | Tab settings | Note |
|---|---|---|
| **API ufficiale** | `/accounts/messaging?tab=api` | Meta WhatsApp Cloud API — richiede token + numero verificato. |
| **Browser** | `/accounts/messaging?tab=browser` | WhatsApp Web pilotato con Playwright. Setup iniziale: scan QR code. Best-effort, piu' a rischio ban. |

### Social DM (`outreach_social`)

Manda DM Instagram / TikTok / Facebook usando account dedicati
([`/social/accounts`](/social/accounts)). Richiede warmup 4-6 settimane prima
di mettere in produzione, **mai usare account personali**.

---

## Inbox

URL: [`/inbox`](/inbox)

### Threads

Lista delle conversazioni in corso, una per (canale + contatto). Filtrabile per:
- **Canale**: email, telegram, whatsapp, instagram, ecc.
- **Status**: open, replied, opted_out, ignored.

Clicca un thread per aprire la **cronologia completa** dei messaggi scambiati,
con form di reply in fondo.

### Contatti (`/inbox/contacts`)

Directory dei contatti **uniti per identita'** (un contatto puo' avere email +
whatsapp + telegram + handle social). Lo crei a mano o e' generato
automaticamente dagli outreach.

Filtri:
- Status (active, opted_out, contacted), search per email/handle, source_domain.
- Tag filter (fino a 6 slot).
- score_min (per filtrare per qualifier score).

Da qui puoi:
- **Optout manuale** (l'asset non sara' piu' contattato).
- **Reset** (riabilita un contatto opted_out — usalo con cautela).
- **Edit** (correggere email/whatsapp/consent).
- **Delete singolo / bulk**.

### Responder

Se vuoi automatizzare le risposte, crea un task `responder` con un prompt:
quando arrivano nuove reply nei thread, il runner le legge, genera una risposta
con LLM e la invia.

---

## Social Accounts

URL: [`/social/accounts`](/social/accounts)

Gestisce gli account "sender" per outreach DM. **Solo IG / TT / FB** —
WhatsApp browser vive in `/accounts/messaging`.

Per ogni account memorizzi:
- **platform** + **username** + **password** (cifrata in Fernet).
- **daily_dm_cap** — limite invii al giorno (default 10, alzare gradualmente).
- **proxy_label** (opzionale) — etichetta del proxy associato.
- **notes** — appunti operativi.
- **owner_user_id** — chi e' il responsabile.

Stati possibili:
- `active` — pronto a essere usato.
- `quarantine` — sospeso (es. ban / login fallito).

Avvertenze:
- **Mai usare account personali**: rischio ban totale.
- **Warmup**: nuovi account devono fare prima azioni "umane" (login, like,
  follow) per 4-6 settimane PRIMA di iniziare DM in volume.
- **Daily cap basso**: 10/giorno il primo mese, salire lentamente fino a max
  30-50.

---

## Orchestrator

URL: [`/orchestrator`](/orchestrator)

### Cos'e'

L'orchestrator e' una **chat AI** che ti aiuta a pianificare il lavoro. Gli
dici cosa vuoi ottenere (es. "trovami 100 escort milanesi e mandagli un DM
Instagram"), e lui:
1. Capisce il tuo obiettivo.
2. Decide la strategia (con quali task / workflow / risorse).
3. Esegue verifiche tecniche pre-task (vedi sotto).
4. Crea i task / workflow necessari.
5. Lancia l'esecuzione e ti aggiorna sull'avanzamento.

### Pre-flight intelligence

Prima di creare un task di scraping, l'orchestrator fa:

| Step | Cosa fa |
|---|---|
| 1. `match_scraping_policies` | Controlla se l'URL e' in lista nera / lista bianca esplicita (vedi Site Memory). |
| 2. `get_site_intel` | Legge la storia per dominio: success/fail count, ultima strategia che ha funzionato, protezioni anti-bot rilevate. |
| 3. `inspect_url` | HEAD probe live: rileva Cloudflare/DataDome/Akamai e ti dice se il sito e' raggiungibile. |

Se uno di questi step trova un blocco (es. policy=skip, o intel=fail >3 volte,
o inspect=blocked da Cloudflare), l'orchestrator **te lo dice** e propone
un'alternativa invece di creare un task destinato a fallire.

### Tool che usa

L'orchestrator ha decine di tool "read" (per esplorare DB e config) e "write"
(per creare task / workflow / asset). E' un agente ReAct: ragiona, chiama un
tool, analizza, ragiona di nuovo, fino al risultato.

### Quando NON usare l'orchestrator

- Quando sai esattamente cosa vuoi creare: piu' veloce creare il task a mano
  da `/tasks/new`.
- Quando devi modificare configurazione fine (LLM model, prompt esatto, ecc.):
  meglio editare il task direttamente.

L'orchestrator e' il modo veloce di **iniziare**. Quando il flusso e'
consolidato, lavori sui task / workflow.

---

## Site Memory

URL: [`/site_memory`](/site_memory)

E' la **memoria persistente** che Argos accumula per ogni dominio scrappato.
Quattro tabelle:

| Tabella | Cosa contiene | Chi popola | Chi legge |
|---|---|---|---|
| **Site Patterns** | Regex di URL appresi (es. `tryst.link/escorts/*` e' pagina profilo) | `bulk_extract` + `site_explorer` | `bulk_extract` nei run successivi |
| **Site Playbooks** | Istruzioni testuali "ecco come si estrae da X" scritte da un agente potente | `browser_use` al primo successo | `site_explorer` e altri runner deboli (Stage 2 knowledge transfer) |
| **Site Intelligence** | Storia per dominio: success/fail, protezioni, ultima strategia OK | TUTTI i runner alla fine del job | **L'orchestrator** nel pre-flight |
| **Scraping Policies** | Regole `regex_dominio → action` (skip/warn/prefer_browser/...) | Tu manualmente, o il sistema (auto-promote) | **L'orchestrator** nel pre-flight |

### Visibilita' privata vs condivisa

Ogni riga di Intelligence e di Policy ha un flag **visibility**:
- 🔒 **private** (default): visibile solo al tuo tenant.
- 🌐 **shared**: visibile a tutti i tenant abilitati al pool community.

Diventi shared in due modi:
1. **Manuale**: clic sul toggle 🌐 nella riga.
2. **Automatico (community pool)**: quando **3 tenant indipendenti** hanno
   intelligence negativa sullo stesso dominio (almeno 1 fail e 0 success), il
   sistema flippa automaticamente le righe a `shared` e crea una policy
   `community` con `action='warn'`.

Per accedere alle righe `shared` di altri tenant, il super-admin deve aver
attivato il flag `site_memory_shared` sul tuo tenant
([ADMIN_GUIDE.md](ADMIN_GUIDE.md)).

### Quando cancellare

- **Sito cambia struttura**: i pattern vecchi diventano fuorvianti — cancellali
  per quel dominio.
- **Test "pulito da 0"**: vuoi vedere come si comporta l'agente senza memoria
  per testare un nuovo prompt → usa "Cancella tutto per {dominio}".
- **Reset totale** ("Svuota intera memoria sito"): drastico, usalo solo prima
  di un test di regressione completo.

### Policy manuali

Sotto "Scraping policies" puoi creare regole:
- `pokerstrategy\.com` → `skip` → l'orchestrator rifiutera' di creare task su quel dominio
- `youtube\.com` → `force_browser` → forza l'uso di `browser_use` anche se ad
  altri agenti sembra fattibile in altro modo
- `mondocamgirl\.com` → `force_skip` (red flag, abbiamo segnalato problemi su quel dominio)

Priority bassa = piu' importante (viene valutata prima). Default = 100.

---

## Settings

### `/settings`
Configurazione **orchestrator**: quale LLM usa per ragionare (planner) e
generare codice / piani. Default Ollama locale, ma puoi puntare a OpenAI /
Anthropic / Google se hai una chiave LLM configurata.

### `/accounts/email`
Account SMTP/IMAP per outreach email. Per ogni account: host + porta + creds
cifrate + daily_cap. Puoi avere piu' account per ruotare il sender.

### `/accounts/messaging`

Hub messaging con 3 tab:
- **api** — WhatsApp Cloud API (Meta Business).
- **browser** — WhatsApp Web pilotato con Playwright (no API, scan QR).
- **telegram** — bot Telegram (token + username).

### `/accounts/llm-keys`
Vault chiavi API LLM (OpenAI, Anthropic, Google, Mistral, OpenRouter, ecc.).
Le chiavi sono cifrate Fernet, mai loggate in chiaro. Per ogni chiave registri:
- provider + modello default
- daily_token_cap (opzionale)
- owner
- status (active / quarantine)

Quando crei un task, scegli quale chiave usare (dropdown). Senza chiave per il
provider richiesto, il task fallisce con un errore esplicito.

### `/social/accounts`
Vedi sopra in [Social Accounts](#social-accounts).

---

## Glossario

| Termine | Definizione |
|---|---|
| **tenant** | Un'azienda / cliente isolato. I dati di un tenant non sono visibili agli altri. |
| **super_admin** | Utente con accesso cross-tenant. Gestisce creazione tenant + utenti. |
| **tenant_user** | Utente normale. Vede solo il suo tenant. |
| **task** | Unita' di lavoro autonoma con un agente AI. |
| **workflow** | DAG di task in cascata. |
| **job** | Una singola esecuzione (run) di un task. |
| **asset** | Riga di dato strutturato (profilo / contatto / link). |
| **audience** | Set di asset selezionati per un outreach. |
| **agent_mode** | Il "motore" di un task (browser_use / bulk_extract / qualifier / ...). |
| **runner** | Il codice Python che esegue un agent_mode. |
| **provider/model** | LLM scelto per il task (ollama+qwen / openai+gpt-4o-mini / ...). |
| **qualifier** | Task che marca asset come qualified/rejected con score 1-10. |
| **outreach** | Invio messaggi (email / WA / DM) ai contatti. |
| **inbox thread** | Una conversazione (canale + contatto). |
| **site memory** | Memoria persistente per dominio (pattern, playbook, intelligence, policy). |
| **community pool** | Pool cross-tenant di intelligence/policy condivise (opt-in via flag). |
| **orchestrator** | Chat AI che pianifica + crea task automaticamente. |
| **artifact passing** | Meccanismo che permette al task downstream di ricevere output del task upstream in un workflow. |
| **playbook** | Istruzioni testuali "come si estrae da X" scritte da un agente potente per uno debole. |

---

Domande / proposte di miglioramento → apri una issue su GitHub.
