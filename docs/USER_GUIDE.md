# Argos — Guida utente

Questa guida copre **due profili di utente tenant**:

1. **Operator** (`tenant_user`) — utente non tecnico. Vede una dashboard
   semplificata con gli agenti pre-confezionati, una toolbox con
   programmazione e cronologia, e una chat assistente. Sezione dedicata:
   [UI Operator](#ui-operator).

2. **Architect** (`tenant_architect`) — utente tecnico che costruisce gli
   agenti, configura LLM, workflow, asset. Sezioni "classiche": Tasks,
   Workflows, Assets, Qualified, Outreach, Inbox, Site Memory, Fascicoli e Fogli.

Per le funzioni riservate al super-admin (creazione tenant, gestione utenti,
filtri cross-tenant) vedi [ADMIN_GUIDE.md](ADMIN_GUIDE.md).

---

## Indice

1. [Cos'e' Argos](#cose-argos)
2. [Login e ruoli](#login-e-ruoli)
3. [UI Operator — dashboard semplificata](#ui-operator)
4. [I 4 oggetti centrali](#i-4-oggetti-centrali)
5. [Tasks — come creare e lanciare un'attivita'](#tasks)
6. [Pubblicare un task / workflow come agente Operator](#pubblicare-come-agente)
7. [Workflows — orchestrare task in cascata](#workflows)
8. [Assets — il database centralizzato](#assets)
9. [Qualified — selezionare audience dai risultati](#qualified)
10. [Outreach — email, WhatsApp, social DM](#outreach)
11. [Inbox — conversazioni con i contatti](#inbox)
12. [Social Accounts — gli account "sender"](#social-accounts)
13. [Orchestrator — la chat AI che pianifica](#orchestrator)
14. [Site Memory — la memoria del framework](#site-memory)
15. [Fascicoli e Fogli — archivi documentali e spreadsheet](#fascicoli-e-fogli)
16. [Settings — chiavi LLM e account canali](#settings)
17. [Glossario](#glossario)

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
- Dopo il login arrivi alla **dashboard** appropriata al tuo ruolo
  (vedi sotto).
- Per cambiare password o uscire: clicca l'avatar in alto a destra → "Esci".

Esistono **3 ruoli**:

| Ruolo | URL post-login | Cosa vede |
|---|---|---|
| **`tenant_user`** (operator) | `/home` | UI semplificata: griglia di "agenti" pronti, toolbox con stato live + agenda + storia, chat assistente. Non puo' creare task ne' modificare configurazioni. |
| **`tenant_architect`** | `/` | UI completa attuale: tasks, workflows, assets, qualified, inbox, social, llm-keys, site_memory, settings. Costruisce gli agenti che gli operator useranno. |
| **`super_admin`** | `/admin` | Tutto + gestione tenant + gestione utenti + filtri cross-tenant. Vedi [ADMIN_GUIDE.md](ADMIN_GUIDE.md). |

Sotto al login le sezioni si dividono:

- **Sei un Operator?** Salta direttamente a [UI Operator](#ui-operator).
- **Sei un Architect?** Le sezioni che seguono (Tasks, Workflows, Assets, ecc.)
  sono per te. Importante anche: [Pubblicare un task come agente Operator](#pubblicare-come-agente)
  per esporre i task agli operator del tuo tenant.

---

<a id="ui-operator"></a>
## UI Operator — dashboard semplificata

URL: [`/home`](/home) (atterraggio automatico al login per gli operator)

### Filosofia

L'operator non costruisce gli agenti — li **usa**. La UI Operator non mostra
mai termini come "task", "workflow", "agent_mode", "LLM", "credenziali". Tutto
e' presentato come "agenti" gia' pronti, organizzati per categoria, che si
"avviano" con un click + eventuali parametri.

### Layout

La dashboard ha due "plance" (canvas) affiancate:

```
+-------------------------------------+----------------------+
| Hero "Ciao, X. Cosa posso fare?"   |  🛠 STRUMENTI        |
+-------------------------------------+  [In corso][Agenda]  |
| AGENTI DISPONIBILI                  |          [Storia]    |
|                                     |                      |
| Ricerca contatti                    | (tab corrente)       |
| [agente] [agente] [agente] →        |                      |
|                                     |                      |
| Contatto e messaggi                 |                      |
| [agente] [agente]                   |                      |
|                                     |                      |
| Analisi e qualifica                 |                      |
| [agente] →                          |                      |
+-------------------------------------+----------------------+
                                                  [💬 Chiedi] ← FAB chat
```

- **Canvas sinistra**: agenti raggruppati per categoria (Ricerca contatti,
  Contatto e messaggi, Arricchimento dati, Analisi e qualifica, Risposte
  automatiche, Altri). Ogni categoria mostra fino a 3 card alla volta;
  per vedere le altre **scorri orizzontalmente** la riga (drag, swipe,
  mouse wheel orizzontale, frecce tastiera dopo focus).
- **Canvas destra (Strumenti)**: pannello sticky con 3 tab descritti sotto.

### Card "agente"

Ogni card mostra:
- Icona + categoria (badge TASK in violetto o WORKFLOW in indaco)
- Nome e descrizione user-friendly (decisi dall'architect quando pubblica)
- Pulse blu in alto-sinistra se ci sono esecuzioni in corso
- 2 bottoni: **Avvia** (lancia subito) e **Dettagli** (apre modal read-only)

Click sulla card (fuori dai bottoni) → apre comunque il modal Dettagli.

### Lancio agente

1. Click **Avvia** → modal con (eventuali) parametri richiesti
2. Compila i campi obbligatori (se previsti)
3. Click **Avvia agente** → toast verde di conferma in basso a destra
4. L'agente compare nel pannello **Strumenti → In corso** entro 3 secondi

Alcuni agenti possono partire **senza parametri** — in quel caso il modal
ha solo "Avviare {nome}?" con conferma.

### Strumenti — Tab 1: In corso

Mostra i job che stanno girando nel tuo tenant:

- Nome agente + icona
- **Timer durata** che si aggiorna ogni secondo (formato MM:SS)
- **Barra di progresso** (indeterminate se non ci sono metriche, oppure
  determinate con % e label di step)
- **Stat snippet**: ultima riga rilevante del log (es. "12 contatti trovati")
- Stato pill: `in esecuzione` / `in coda` / `in pausa`
- Bottone **Dettagli** → modal con cronologia log + file generati

Sotto, sezione **"Completati di recente"** con le ultime 3 esecuzioni
terminate (ultimi 24h):

- Badge stato (`✓ Completato` verde / `✗ Errore` rosso / `⊘ Annullato`)
- Durata totale
- Bottone **Risultati** → modal con file generati

Polling automatico ogni 3 secondi. Quando un job termina mentre stai sulla
pagina, ricevi un **toast notification** in basso a destra (`✓ Test Ricerca
Web completato`).

### Strumenti — Tab 2: Agenda

Lista degli agenti **pianificati** (con cron expression valorizzata).
Per ciascuno:
- Nome agente
- **Prossima esecuzione** in linguaggio naturale (es. "oggi alle 09:00",
  "domani alle 14:00", "lun alle 09:00", "fra 3 giorni"). Calcolata con
  croniter.
- Bottone **Modifica** → modal scheduling pre-popolato

Bottone in fondo: **+ Pianifica un agente** → modal per nuovo schedule:

1. Scegli quale agente pianificare (dropdown)
2. Scegli il preset cron friendly:
   - "Ogni giorno alle 9:00"
   - "Lun-Ven alle 9:00"
   - "Lunedì alle 14:00"
   - "Ogni 6 ore"
   - "Il 1 del mese a mezzanotte"
3. Oppure scrivi manualmente la cron expression nel campo (validata server-side)
4. Click **Salva** → APScheduler attiva automaticamente la pianificazione

Per **rimuovere** una pianificazione: apri il modal Modifica → click
**Rimuovi pianificazione** (rosso). Lo schedule viene cancellato (cron=NULL)
e l'agente non parte piu' automaticamente.

### Strumenti — Tab 3: Storia

Statistiche degli ultimi **7 giorni**:

- KPI in alto: **N esecuzioni totali** e **% successo**
- Bar chart: 7 colonne (1 per giorno, Lun-Dom) con stack di esecuzioni
  completate (verde) ed errori (rosso). Hover su una barra per vedere il
  dettaglio del giorno.

Utile per capire a colpo d'occhio se la macchina sta lavorando bene oppure
ci sono giornate con tanti errori da indagare.

### Chat assistente (drawer)

In basso a destra c'e' un **FAB rotondo "💬 Chiedi"**. Click → drawer
slide-in da destra con la chat. Caratteristiche:

- L'assistente sa cosa puoi fare nella dashboard, gli agenti disponibili,
  e ti aiuta a scegliere quello giusto per il tuo obiettivo.
- **Non puo' creare nuovi agenti** ne' modificare configurazioni — quello
  e' lavoro dell'architect.
- Chat **persistente** in DB: chiudi il drawer, riapri, ritrovi la
  conversazione.
- **Minimizza** col chevron `>` in alto: il drawer scompare ma il FAB
  riappare per riaprirlo. Stato salvato in localStorage.
- ESC chiude il drawer.
- Suggerimenti pronti per cominciare ("Che agenti ho a disposizione?",
  "Quale agente uso per trovare contatti?", "Come vedo i risultati?").

### Nav top-bar Operator

- **Dashboard** → `/home`
- **Lead** → `/leads` (tutti i contatti/profili estratti)
- **Qualificati** → `/leads/qualified` (audience pronte per outreach)
- **Messaggi** → `/messages` (inbox del tenant)
- Avatar a destra → dropdown con tuo nome + email + tenant + **Esci**

### Pagina Lead (`/leads` e `/leads/qualified`)

Vista **read-only** dei contatti/profili generati dagli agenti del tenant.
Stessi filtri della pagina architect:

- **Ricerca testuale** full-text (nome, email, titolo)
- **Tipo asset** (ig_profile, business_page, ecc.)
- **Stato** (nuovo, qualificato, scartato, archiviato)
- **Score minimo** per qualificati (1-10)
- **Filtro qualifier** (quale qualifier ha marcato l'asset)
- **Solo con contatti** / **Solo con social** (toggle)
- **Tag avanzati**: 6 slot key=value in AND o OR, con autocomplete delle
  chiavi tag in uso (collassabile in `▸ Filtri avanzati per tag`)

Ogni lead e' una card con:
- Titolo + sottotitolo + tipo
- Chip per ogni canale contatto (email, WhatsApp, Telegram, link profilo)
- Badge stato colorato
- Data di creazione
- Bottone **Opt-out** → con conferma, marca il lead come "da non contattare"
  (outreach_status='optedout'). I lead opt-out vengono mostrati con opacita'
  ridotta + badge `⊘ Opt-out` e non vengono mai piu' contattati dagli agenti
  di outreach.

Paginazione 60 per pagina, preserva i filtri.

### Pagina Messaggi (`/messages`)

Inbox semplificata: thread di conversazione con i contatti raggiunti dagli
agenti outreach. Filtri minimi: canale (email/WA/Telegram/IG/...) + stato
(aperti/con risposta/opt-out).

Click su un thread → cronologia completa + form di reply.

### Risultati di un agente (modal dettagli job)

Click **Dettagli** o **Risultati** su un job (live o completato) → modal
con:

- Header: badge stato + durata + numero esecuzione
- **Box errore** rosso se il job e' in stato error
- **Lista file generati**: report.md, profiles.jsonl, qualified.csv, log...
  - Bottone **Apri** per i file visualizzabili (md/json/csv/txt/log)
  - Bottone **Download** per altri formati
- **Tail del log**: ultime 20 righe del log per debug visivo

Click **Apri** su un file → modal viewer dedicato:

- **Markdown** → reso a HTML (titoli, code blocks, tabelle, link)
- **JSON / JSONL** → pretty-printed con highlighting
- **CSV** → tabella con sticky header
- **TXT / log** → testo monospace
- Bottoni in fondo: `← Torna ai dettagli` e `Scarica`

Tutto questo riusa gli stessi viewer della pagina architect ma in stile
operator (modal in-page, no sidebar).

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

Nella lista dei task, quelli pubblicati come agente Operator mostrano un
badge `🤖 agente` accanto al nome (idem per workflow nella lista
[`/workflows`](/workflows)).

---

<a id="pubblicare-come-agente"></a>
## Pubblicare un task / workflow come agente Operator

I tuoi colleghi con ruolo `tenant_user` (operator) **non vedono** la lista
task. Vedono solo la dashboard [`/home`](/home) con gli **"agenti pubblicati"**.
Per esporre un task o workflow agli operator, devi pubblicarlo esplicitamente.

### Come pubblicare

1. Apri il detail del task: `/tasks/{id}` (o del workflow: `/workflows/{id}`)
2. Cerca il box `▸ Non pubblicato` sopra "⚙️ Configurazione" — espandilo
3. ☑ Spunta **"Mostra questo task come agente nella dashboard operator"**
4. Compila i 5 campi della "carta d'identita' Operator":

| Campo | Esempio | Note |
|---|---|---|
| **Nome visibile** | "Trova clienti farmacia" | Quello che l'operator legge sulla card. User-friendly, niente gergo. |
| **Categoria** | "Ricerca contatti" | Dropdown: Ricerca contatti / Contatto e messaggi / Arricchimento dati / Analisi e qualifica / Risposte automatiche / Altri |
| **Icona** | 🎯 Target | Dropdown con icone Lucide preconfezionate |
| **Descrizione** | "Cerca farmacie nella citta indicata con email pubbliche" | 1-2 frasi senza gergo tecnico, max 240 char |
| **Parametri richiesti al lancio (JSON)** | (vedi sotto) | Schema dei campi che l'operator deve compilare prima del lancio |

5. Click **Salva pubblicazione** → badge `✓ Pubblicato come agente`
6. Per ritirare: bottone **Ritira pubblicazione** sotto il form (rosso)

### Schema parametri JSON

Il campo "Parametri richiesti al lancio" e' una lista JSON che descrive i
campi del modal di Avvio. Esempio:

```json
[
  {
    "name": "city",
    "type": "text",
    "label": "Citta",
    "required": true,
    "placeholder": "es. Milano"
  },
  {
    "name": "max_results",
    "type": "number",
    "label": "Numero massimo risultati",
    "default": "50"
  },
  {
    "name": "country",
    "type": "select",
    "label": "Paese",
    "options": ["it", "fr", "de"],
    "default": "it"
  }
]
```

Tipi supportati al MVP: `text`, `textarea`, `number`, `select` (con
`options`), `checkbox`.

**Lascia `[]`** (lista vuota) se l'agente non ha parametri runtime — il
modal mostrera' solo "Avviare {nome}?" e basta.

### Workflow

Per i workflow il flusso e' identico (stessa form nella tab Configurazione
del detail workflow). L'icona di default suggerita e' `🔀 Workflow` ma puoi
sceglierne un'altra. I workflow pubblicati appaiono nella griglia Operator
con badge `WORKFLOW` (indaco) invece di `TASK` (violetto).

### Esecuzione dal lato Operator

Quando l'operator clicca **Avvia** sulla card:

1. Il modal di lancio mostra solo i campi che hai dichiarato in `agent_input_schema`.
2. L'operator compila + click "Avvia agente".
3. Il sistema crea un job. **Importante**: il job eredita `created_by_user_id`
   dal task (cioe' DA TE, l'architect), non dall'operator. Cosi' il runner
   usa le tue credenziali LLM e i tuoi account email/social, non quelli
   dell'operator (che ne ha zero).
4. L'operator vede il progresso nel pannello "Lavoro in corso".
5. A fine job, l'operator vede toast `✓ Completato` o `✗ Errore`.

I file generati (`report.md`, `profiles.jsonl`, ecc.) sono nella stessa
cartella `data/results/{task_id}/` come per le tue esecuzioni.

### Tip

Mantieni la libreria dei tuoi agenti pubblicati **piccola e curata**.
Meglio 5 agenti pubblicati che fanno bene una cosa che 50 agenti
sperimentali. Gli agenti pubblicati sono il "menu" che presenti
all'operator: vanno scelti con attenzione.

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

Dalla v1.5 legge **tutto il dominio del tenant**: oltre a task/workflow/job,
anche i **Fascicoli** (metadati + ricerca RAG nei documenti) e i **Fogli**
(contenuto tabellare), più un `get_tenant_overview` con i conteggi complessivi.
Restano **esclusi dai contenuti** `asset` e `qualified` (per volume): di questi
l'orchestrator vede solo i conteggi. Così puoi chiedergli di progettare task e
workflow basandosi su ciò che è scritto nei tuoi fascicoli e fogli.

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

<a id="fascicoli-e-fogli-quick"></a>
> 💡 Oltre allo scraping, il tenant ha due archivi documentali — **Fascicoli**
> e **Fogli** — descritti nella sezione [Fascicoli e Fogli](#fascicoli-e-fogli).

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

<a id="fascicoli-e-fogli"></a>
## Fascicoli e Fogli

Dalla v1.4 Argos affianca allo scraping **due archivi documentali** del tenant,
indipendenti dalla pipeline lead-gen.

### Fascicoli (`/fascicoli`)

Un **fascicolo** è un dossier di documenti (PDF, DOCX, TXT, email `.eml`) con
**ricerca semantica** (RAG). I file vivono **sul tuo PC**, dentro la cartella
*RootProject* che configuri una volta in [`/fascicoli/settings`](/fascicoli/settings);
su DB stanno solo i metadati (titolo, descrizione, elenco file, condivisione) —
**il contenuto dei documenti non lascia mai il PC**. Puoi:
- creare un fascicolo, trascinarci dentro file, e farli **indicizzare** per la ricerca;
- aprire la **chat sul fascicolo**: fai domande e l'assistente risponde citando i documenti;
- aprire un file direttamente dal PC.

Un fascicolo può risultare **completo** (cartella presente su questo PC →
documenti leggibili) o **monco** (solo metadati, cartella non presente qui).

### Fogli (`/fascicoli/sheets`)

Un **foglio** è uno spreadsheet collaborativo in tempo reale (stile Google
Sheets). A differenza dei fascicoli, **il contenuto delle celle vive su DB**,
quindi è sempre leggibile/modificabile anche da un altro PC e si presta alla
collaborazione multi-utente. Un foglio può essere standalone o agganciato a un
fascicolo. Esportabile in CSV/XLSX (anti formula-injection).

### Condivisione

Sia fascicoli sia fogli hanno una **visibilità**: `tenant` (tutti gli utenti del
tenant) o `user` (privati dell'owner + persone aggiunte). La modale di
condivisione permette di aggiungere persone con livello lettura/scrittura e
l'opzione "Tutti (lettura)". L'**architetto** ha supervisione sull'intero tenant
(vede tutti i fascicoli e fogli, anche quelli `user` di altri utenti dello
stesso tenant); l'isolamento **fra tenant diversi** resta sempre garantito.

### L'orchestrator li legge (v1.5)

La chat [Orchestrator](#orchestrator) sa leggere fascicoli e fogli: chiedi
«cosa c'è nel fascicolo X», «riassumi il foglio Clienti», oppure «qualifica i
lead elencati nel foglio Prospect» e userà i loro contenuti per ragionare e
impostare i task. (Il contenuto RAG dei fascicoli richiede stato *completo*.)

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
| **tenant_architect** | Utente tecnico del tenant. Costruisce task/workflow, pubblica agenti, configura LLM. Vede UI completa a `/`. |
| **tenant_user** (operator) | Utente non tecnico del tenant. Vede solo gli agenti pubblicati dall'architect. UI semplificata a `/home`. |
| **agente pubblicato** | Task o workflow marcato `is_published_agent=TRUE` con metadata user-friendly (nome, descrizione, icona, parametri JSON). E' quello che vede l'operator nella dashboard. |
| **toolbox** | La canvas destra della dashboard operator. 3 tab: In corso (live jobs), Agenda (scheduling cron), Storia (analytics 7gg). |
| **task** | Unita' di lavoro autonoma con un agente AI. |
| **workflow** | DAG di task in cascata. |
| **job** | Una singola esecuzione (run) di un task. |
| **asset** | Riga di dato strutturato (profilo / contatto / link). Per l'operator si chiama "lead". |
| **audience** | Set di asset selezionati per un outreach. |
| **opt-out** | Marca un asset come `outreach_status='optedout'`: nessun agente outreach lo contatta piu'. Bottone su ogni card lead. |
| **cron** | Espressione di scheduling ricorrente (es. `0 9 * * *` = ogni giorno alle 9:00). Settata sul task da Agenda Operator o da task detail Architect. |
| **agent_mode** | Il "motore" di un task (browser_use / bulk_extract / qualifier / ...). Nascosto all'operator. |
| **runner** | Il codice Python che esegue un agent_mode. |
| **provider/model** | LLM scelto per il task (ollama+qwen / openai+gpt-4o-mini / ...). |
| **qualifier** | Task che marca asset come qualified/rejected con score 1-10. |
| **outreach** | Invio messaggi (email / WA / DM) ai contatti. |
| **inbox thread** | Una conversazione (canale + contatto). |
| **site memory** | Memoria persistente per dominio (pattern, playbook, intelligence, policy). |
| **fascicolo** | Dossier di documenti (PDF/DOCX/TXT/email) con ricerca semantica RAG. File locali sul PC, metadati su DB. Stato `completo` (cartella presente) o `monco` (solo metadati). |
| **foglio** | Spreadsheet collaborativo realtime; contenuto celle su DB. Standalone o agganciato a un fascicolo. |
| **RootProject** | Cartella sul PC, configurata in `/fascicoli/settings`, sotto cui risiedono le cartelle dei fascicoli. |
| **community pool** | Pool cross-tenant di intelligence/policy condivise (opt-in via flag). |
| **orchestrator** | Chat AI che pianifica + crea task automaticamente. Per operator: solo lettura, no creazione. |
| **artifact passing** | Meccanismo che permette al task downstream di ricevere output del task upstream in un workflow. |
| **playbook** | Istruzioni testuali "come si estrae da X" scritte da un agente potente per uno debole. |
| **FAB** | Floating Action Button: il bottone tondo "💬 Chiedi" in basso a destra che apre la chat assistente dell'operator. |
| **canvas** | Plancia di lavoro: i container con background + bordo + ombra che separano sezioni della dashboard operator (sinistra: agenti, destra: toolbox). |

---

Domande / proposte di miglioramento → apri una issue su GitHub.
