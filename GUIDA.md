# Guida utente — AgentScraper

> Come funziona l'app, cosa c'è dentro, come usarla. Con esempi reali.

---

## 1. Cos'è AgentScraper

AgentScraper è una **piattaforma locale single-user** per costruire pipeline agentiche di:

1. **Estrazione** dati da pagine web (singole o cataloghi),
2. **Qualificazione** dei contatti raccolti tramite LLM,
3. **Outreach** automatico (email + Telegram) ai contatti qualificati,
4. **Risposta automatica** alle replies tramite LLM.

Il tutto orchestrabile come **DAG di attività** (workflow) che si triggerano in cascata.

L'app gira interamente sul tuo computer — la web UI è su `http://127.0.0.1:8000`. Solo il traffico verso gli LLM (Ollama in locale o API esterne) e i fetch web/SMTP/IMAP/Telegram esce dalla tua macchina.

---

## 2. I concetti chiave

```
                                 ┌──────────────┐
                                 │   Workflow   │   (orchestrazione)
                                 │   (DAG di    │
                                 │   tasks)     │
                                 └──────┬───────┘
                                        │ contiene N edge
                                        ▼
        ┌──────────┐    edge   ┌──────────┐    edge   ┌──────────┐
        │  Task A  │──────────▶│  Task B  │──────────▶│  Task C  │
        │ (scraper)│           │(qualifier)│          │(outreach)│
        └─────┬────┘           └─────┬────┘           └─────┬────┘
              │ ogni run = 1 Job     │                      │
              ▼                      ▼                      ▼
         data/results/          data/results/          data/results/
         A/<ts>/profiles        B/<ts>/qualified       C/<ts>/outreach_log
                                                            │
                                                            ▼
                                                  ┌─────────────────┐
                                                  │  Channel email  │ → invio
                                                  │  Channel telegram│
                                                  └─────────────────┘
                                                            │ inbound
                                                            ▼
                                              ┌──────────────────────┐
                                              │ Contact + Thread +   │
                                              │ Message (in DB)      │
                                              └──────────┬───────────┘
                                                         ▼
                                                ┌──────────────┐
                                                │  Responder   │ ← un task
                                                │  (auto-reply)│
                                                └──────────────┘
```

| Concetto | Cos'è |
|---|---|
| **Task** (attività) | Una singola fase autonoma di un processo. Ha una sua modalità (`agent_mode`), un suo modello LLM, una sua configurazione. Si può lanciare da sola. |
| **Workflow** | Un contenitore nominato che orchestra più task in cascata, definendo gli **edge** del DAG. Quando lanci un workflow, parte dal task root e i downstream si triggerano automaticamente. |
| **Job** | Un'esecuzione concreta di un task. Ha uno stato (`queued`/`running`/`paused`/`done`/`error`/`cancelled`), un log, un report finale. |
| **Edge** | Connessione `Task A → Task B` dentro un workflow: quando A finisce con `done`, B parte automaticamente. Può passare un **artifact** (es. `profiles.jsonl`) come input di B. |
| **Channel** | Canale di messaggistica (`email` o `telegram`) configurato in `/settings`. Usato dai task `outreach` e `responder`. |
| **Contact** | Riga della tabella `contacts`: rappresenta una persona/entità raggiungibile (email, telegram, ecc.). Materializzata dal `qualifier` o `outreach` partendo dai `profiles.jsonl`. |
| **Thread** | Conversazione su un canale con un contact (es. tutti i messaggi email scambiati). |
| **Message** | Singolo messaggio inbound o outbound dentro un thread. |
| **Artifact** | File generato da un task — tipicamente un `.jsonl` (vedi sotto). È l'output di scraping (`profiles.jsonl`) o di qualificazione (`qualified.jsonl`). Viene passato come input ai task downstream. |

### 2.1 Cos'è un file `.jsonl`

Un file `.jsonl` ("**JSON Lines**", o NDJSON) è un formato testuale dove **ogni riga è un oggetto JSON valido**, separato dalle altre da un newline. Esempio di `profiles.jsonl`:

```jsonl
{"url": "https://alice.com", "email": "alice@x.it", "telegram": "@alice"}
{"url": "https://bob.com", "email": null, "telegram": "@bob"}
{"url": "https://carla.com", "email": "carla@y.com", "telegram": null}
```

3 righe = 3 oggetti = 3 profili. Vantaggi rispetto a un singolo array JSON:
- **Streaming**: leggi/scrivi una riga alla volta, niente bisogno di tenere in memoria tutto
- **Append-friendly**: aggiungi una riga in fondo senza riparsare il file
- **Resiliente**: se una riga è corrotta, le altre restano valide

In AgentScraper i `.jsonl` sono il **"currency" interno tra task**:
- task scraping (`bulk_extract`/`browser_use`/`auto_extract`) **producono** `profiles.jsonl`
- task downstream (`qualifier`/`outreach`/`responder`) **consumano** un `.jsonl` (e magari ne producono uno qualificato `qualified.jsonl`)

Tutti i `.jsonl` vivono in `data/results/<task_id>/<timestamp>/`.

### 2.2.0 Creare/modificare un task: il wizard a 5 step

Il form di creazione/modifica task (`/tasks/new`, `/tasks/<id>/edit`) è organizzato come **wizard a 5 step navigabili** invece che come un'unica pagina lunga. Vedi una stepper-bar in cima (cliccabile) e bottoni **◀ Indietro** / **Avanti ▶** / **✓ Crea task** in fondo.

| # | Step | Cosa contiene | Visibile per |
|---|---|---|---|
| 1 | 🎯 **Identità** | Nome, descrizione, modalità agente, obiettivo | sempre |
| 2 | 🔍 **Target & Schema** | Seed/URL, domini, schema di estrazione, crawler config (concorrenza, rate, depth, pattern) | scraping modes |
| 3 | 🧠 **LLM** | Tabella overview "Quali LLM" + 3 ruoli LLM: Main (obbligatorio), Discovery (opzionale, collassabile), Browser (opzionale, collassabile) | tutti tranne `outreach` |
| 4 | 🔄 **Pipeline I/O** | Input upstream (file picker), Outreach config, Responder system prompt | bulk/qualifier/outreach/responder/auto |
| 5 | 📋 **Pianificazione** | Output format, cron, valutazione personale | sempre |

**Step automaticamente skippati**: il wizard rileva quali step sono vuoti per la modalità scelta e li nasconde dalla stepper. Esempio: per `react` ti vedi solo Step 1, Step 2 (parziale), Step 3 e Step 5. Step 4 sparisce.

**Submit**: il bottone **✓ Crea task** (o **💾 Salva modifiche**) appare SOLO nell'ultimo step. Negli step intermedi vedi solo **Avanti ▶**. Click su un titolo della stepper-bar = jump diretto a quello step. Il form è un'unica request al server (niente upload parziali tra step).

**Sezioni collassabili** (`<details>` HTML nativi): tutte le sezioni "fieldset" del form sono retrattili e **partono CHIUSE per default** — click sul titolo per espandere quella che ti serve. Stato della singola sezione open/closed NON viene persistito tra reload.

### 2.2.1 Orchestrator: creare task/workflow da un brief

La pagina `/orchestrator` è una console per descrivere un obiettivo in linguaggio naturale e ottenere una **preview operativa**: task proposti, workflow DAG, artifact passati tra task e rischi.

Livelli di autonomia:

| Livello | Cosa può fare |
|---|---|
| **Consigliere** | propone il piano, senza creare nulla |
| **Builder** | crea task/workflow solo dopo conferma, ma non lancia job |
| **Supervisionato** | crea e può lanciare dopo conferma esplicita |
| **Autonomo controllato** | crea e lancia dopo conferma iniziale; outreach/responder richiedono comunque consenso dedicato |

Il planner funziona anche senza LLM esterno usando una strategia euristica locale. Se abiliti **Planner LLM**, l'orchestrator chiede a un modello OpenAI-compatible un piano JSON; se la chiamata fallisce, torna automaticamente al piano euristico. La API key del planner serve solo a generare il piano e non viene salvata nei task creati.

La colonna destra contiene una **chat persistente** salvata in DB. Usa lo stesso provider/modello configurato in Settings.

Toggle del composer:
- **Web** (per-messaggio): se attivo e il modello supporta tool-calling, l'orchestrator può usare `web_search` e `fetch_url` per recuperare contesto aggiornato.
- **Azioni** (per-messaggio): se attivo, l'orchestrator può **chiamare gli endpoint del progetto come tool** — `propose_plan`, `execute_plan`, `create_task`, `create_workflow`, `add_edge`, `start_job`, `start_workflow`, `update_asset_status`, `set_site_pattern_status`. Senza Azioni la chat è solo lettura/ragionamento.
- Allegati `+`: file `.txt`, `.md`, `.csv`, `.json/jsonl`, `.html`, `.pdf` (max 5 MB). I PDF vengono estratti server-side con `pypdf`. Gli allegati storici dei messaggi precedenti vengono ri-iniettati come contesto.

Tool sempre disponibili in chat (lettura, anche con Azioni OFF, se il modello supporta tool-calling): `list_tasks`, `get_task`, `list_workflows`, `list_jobs`, `get_job_status`, `list_extraction_templates`, `list_chat_models`, `list_assets`, `get_asset`, `list_site_patterns`.

Per outreach/responder serve sempre `confirm_risky=true` esplicito quando l'orchestrator chiama `start_job` / `start_workflow` / `execute_plan(run_now=true)`.

### 2.2 Come scegliere l'input `.jsonl` di un task

Quando crei un task della famiglia "Pipeline downstream" (qualifier/outreach/responder), o un `bulk_extract` con input pre-esistente, hai **3 modi per indicare il file di input**, nella sezione "📂 Input upstream" del form:

1. **① File generato da un task precedente** (dropdown): elenco di tutti i `.jsonl` in `data/results/`, ordinati per data più recente, con info `[task#X nome] timestamp/filename (N righe, KB)`. Click → il file viene selezionato.

2. **② Carica un file dal tuo computer** (file picker nativo del browser): per file `.jsonl` esterni — es. uno scaricato dal Downloads, ricevuto via email, esportato da un altro tool. Il file viene **caricato sul server** in `data/uploads/<timestamp>/<filename>` e selezionato automaticamente. Limiti: solo `.jsonl`/`.ndjson`, max 50 MB.

3. **③ Workflow edge**: se il task è downstream in un workflow con `pass_artifact='profiles.jsonl'` sull'edge, il file viene compilato **automaticamente** quando l'upstream finisce — non devi fare niente.

In ogni caso, dopo la selezione vedi un box verde **📁 File selezionato: \<path\>** con un bottone ✕ per rimuovere la selezione e ricominciare. Il path effettivo è gestito internamente come campo nascosto.

---

## 3. I 7 tipi di Task (`agent_mode`)

Quando crei un task, il campo **Modalità agente** determina cosa farà. Le 7 modalità si dividono in **2 famiglie**:

- **Scraping** (4 modalità): trovano ed estraggono dati dal web → producono `profiles.jsonl`
- **Pipeline downstream** (3 modalità): operano sui dati già estratti

### 3.0 Albero decisionale "quale modalità mi serve?"

```
Devo estrarre dati dal web?
├── SÌ — ho UN sito specifico che conosco bene
│   ├── Sito statico, HTML server-rendered, pattern URL chiari
│   │   (cataloghi, listini, directory) ─────────────► bulk_extract  (§3.3)
│   ├── Sito SPA / JS-heavy / login / scroll dinamico ─► browser_use   (§3.2)
│   └── Voglio solo riassumere info dal web ───────────► react         (§3.1)
│
├── SÌ — ho una LISTA di siti diversi
│   └── Lascia che il sistema scelga la strategia per ognuno
│       (con fallback automatico) ────────────────────► auto_extract  (§3.4)
│
├── SÌ — ho UN sito MA la struttura non è ovvia
│   (home non linka i target, listing nascoste,
│    navigazione multi-livello) ─────────────────────► site_explorer (§3.4.1)
│
└── NO — ho già un profiles.jsonl, devo lavorarci sopra
    ├── Filtrare/scorare i contatti via LLM ──────────► qualifier     (§3.5)
    ├── Mandare email/telegram ai contatti ───────────► outreach      (§3.6)
    └── Rispondere automaticamente ai messaggi ricevuti ► responder    (§3.7)
```

### 3.0.1 Tabella sintetica di confronto

#### Famiglia "Scraping"

| Modalità | Cosa fa | Velocità | Costo per 1000 URL | Quando |
|---|---|---|---|---|
| `react` | HTTP + DuckDuckGo, niente browser | rapido | $0.05-0.20 | Ricerche generiche, sintesi |
| `browser_use` | Pilota Chromium reale | LENTO (4-6h) | $5-10 (gpt-4o-mini) | Solo se HTML statico non basta |
| `bulk_extract` | HTTP + readability + 1 LLM/URL | veloce (5-10 min) | $0.20 cloud, **$0 locale** | Cataloghi statici, pattern URL chiari |
| `auto_extract` | Profiler + dispatch automatico | dipende | dipende dai siti | Lista eterogenea di siti |
| **`site_explorer`** | **Agente ReAct: LLM decide ogni step** | medio (5-15 min/sito) | **$0.05-0.20 per sito** | **Siti dove la struttura non è ovvia: scende automaticamente nelle listing giuste** |

#### Famiglia "Pipeline downstream"

| Modalità | Input | Output | Note |
|---|---|---|---|
| `qualifier` | `profiles.jsonl` da scraping | tabella `contacts` con score 0-10 + status `qualified`/`rejected` | 1 chiamata LLM per profilo |
| `outreach` | `contacts` con `status='qualified'` | thread + messaggi inviati via canale | Usa template (no LLM) |
| `responder` | inbox email/telegram | reply auto-generata e inviata | Auto-detect opt-out (STOP, unsubscribe) |

### 3.0.2 Regola d'oro: bulk_extract prima di tutto

**Prova sempre prima `bulk_extract`** se il sito target ha contenuto in HTML statico. È 50-100× più economico e veloce di `browser_use`. Passa a `browser_use` solo se: (a) il sito richiede JS per renderizzare il contenuto, (b) i dati sono dietro click/scroll, (c) il sito ha login obbligatorio. Se non sei sicuro, usa `auto_extract` che lo decide per te.

### 3.1 `react` — Ricerca leggera (HTTP + DuckDuckGo)

**Cosa fa**: un loop ReAct che chiama tre tool (`web_search`, `fetch_url`, `finalize`) usando solo richieste HTTP. Nessun browser. Veloce e leggero.

**Quando usarlo**:
- Ricerche giornalistiche / digest di news
- Sintesi di documentazione tecnica
- Pagine **statiche** o moderatamente dinamiche
- Quando ti basta un riassunto testuale, non dati strutturati

**Quando NON usarlo**:
- Siti SPA con tanto JS
- Cataloghi con scroll infinito
- Quando serve estrazione strutturata in `profiles.jsonl`

**Configurazione minima**:
- **Obiettivo**: descrizione testuale di cosa cercare
- **Seed query**: opzionali — se vuote, l'LLM le deduce dall'obiettivo
- **Max iterazioni**: 5–15 (è il numero totale di tool-call permessi)
- **Modello**: anche piccolo (`qwen3.5:latest` va bene)

**Output**: un singolo file `data/results/<task_id>/<timestamp>.txt` (o `.md`) con il report testuale.

**Esempio**:
- Nome: "Digest news AI italiane"
- Obiettivo: "Trova le 5 notizie più rilevanti della settimana sull'IA in Italia. Per ognuna riporta titolo, fonte, data e 2-3 righe di sommario."
- Seed: vuote
- Modello: `qwen3.5:latest`, Max iter: 10

---

### 3.2 `browser_use` — Browser reale (Playwright + Chromium)

**Cosa fa**: pilota un Chromium reale tramite [browser-use](https://github.com/browser-use/browser-use). L'LLM "vede" la pagina, clicca, scrolla, gestisce cookie banner / age gate, attende JS. Per ogni pagina valida estrae dati strutturati secondo lo **schema di estrazione** del task e li scrive riga-per-riga in `profiles.jsonl`.

**Quando usarlo**:
- Cataloghi/listing di profili o prodotti
- Siti SPA che richiedono interazione (click, scroll, paginazione)
- Tutto quello che `react` non riesce a fare

**Configurazione**:
- **Obiettivo**: descrizione di cosa cercare (l'LLM lo combina con lo schema)
- **Seed query**: una **lista di URL** completi (uno per riga). Ogni URL è una sessione browser-use indipendente con `max_iterations` step ciascuna.
- **Max iterazioni**: **per seed**, non totale. Con 5 seed e max 30 → 150 step totali.
- **Modello**: meglio frontier (`gpt-4o-mini`, `claude-haiku-4-5`); locale ≤20B fa fatica.
- **Schema di estrazione**: scegli un template (`profile_contacts`, `ecommerce_products`, ecc.) e modificalo nella textarea. Lo schema dice all'agente quali campi estrarre per ogni pagina.
- **Whitelist/blacklist domini**: per non far andare l'agente fuori scope.

**Output (dual: file + DB)**:

1. **File system** (artefatti immutabili della run): `data/results/<task_id>/<timestamp>/`
   - `report.md` — riepilogo
   - `profiles.jsonl` — UNA riga JSON per ogni pagina-profilo estratta (consolidato cross-seed)
   - `seed_NN_<dominio>/...` — file dell'agente per-seed (debug)

2. **Database** (stato applicativo, query-friendly): tabella `contacts`
   - Ogni riga di `profiles.jsonl` con email o telegram diventa un **contatto in DB** con `status='new'`
   - Visibile immediatamente in `/inbox/contacts` senza dover lanciare un qualifier
   - Se il contatto esiste già (matching email/telegram), lo `status` corrente viene **preservato** (non torna a `new` se era `qualified`/`contacted`/`optedout`)
   - Idempotente: re-eseguire lo scraper non duplica i contatti

Questo dual-storage segue il pattern ETL: **file = artefatto immutabile della specifica run, DB = stato corrente del sistema**. Il qualifier successivo (se nel workflow) aggiorna `status` a `qualified`/`rejected`. Se invece salti il qualifier, puoi lanciare outreach diretto sui contatti `new`.

**Esempio**:
- Nome: "Scraping pagine prodotto Wineshop X"
- Obiettivo: "Estrai tutti i prodotti vino disponibili con prezzo e descrizione"
- Seed:
  ```
  https://wineshop-x.it/categoria/rossi
  https://wineshop-x.it/categoria/bianchi
  ```
- Provider: OpenAI, Modello: `gpt-4o-mini`, Max iter: 30
- Schema: `ecommerce_products` (modifica se serve)

---

### 3.3 `bulk_extract` — Scraping massivo deterministico (lista URL)

**Cosa fa**: per ogni URL della lista, fa fetch HTTP → estrae il testo principale (readability) → chiama l'LLM una volta con lo schema → salva il JSON in `profiles.jsonl`. **Niente loop agentico**, niente browser, niente decisioni step-by-step. Concorrenza configurabile + rate limit per host.

**Quando usarlo**:
- Quando hai già una **lista di URL** (da un `browser_use` upstream con scope "discovery", o copiata a mano)
- Cataloghi grandi (centinaia–migliaia di URL) dove `browser_use` brucerebbe troppi step
- Pagine **statiche** o moderatamente dinamiche (HTML server-side rendered)
- Quando vuoi **velocità + costo basso** (10-30× più veloce di `browser_use`)

**Quando NON usarlo**:
- Pagine SPA che richiedono JS pesante per renderizzare il contenuto (usa `browser_use`)
- Quando NON hai una lista di URL e devi prima scoprirla agenticamente

**Configurazione**:
- **Seed**: URL da processare, una per riga (oppure)
- **Input artifact path**: file `profiles.jsonl` da un task upstream (legge il campo `url` di ogni riga). Le due fonti si **uniscono** + dedup automatico.
- **Domini consentiti/bloccati**: safety filter post-merge
- **Max URL**: cap di sicurezza (default 1000, max 100k). Se la lista combinata è più lunga, viene troncata.
- **Schema di estrazione**: stesso template/textarea dei task `browser_use`
- **Modello LLM**: provider qualsiasi. Usa modelli **economici** (`gpt-4o-mini`, `claude-haiku-4-5`, `gemini-2.5-flash`) — niente decisioni complesse, solo "estrai questi campi da questo testo".
- **Configurazione bulk** (nuovo fieldset):
  - **Concorrenza**: URL in parallelo (default 5, max ~10 per evitare di stressare il server target)
  - **Rate limit per host**: req/secondo (default 2.0). Se il sito ha solo un dominio, questo è il throttle effettivo.
  - **Strategia estrazione**: `llm_per_page` (default, 1 chiamata LLM per URL) o `css_selectors` (avanzato, futuro: zero LLM, mapping campo→selettore)
  - **🕷️ Crawler dal seed (opzionale)**: vedi sotto

#### Crawler dal seed (BFS deterministico + auto-detect pattern)

Se vuoi partire da una **home/listing** e scoprire automaticamente tutti gli URL prodotto del sito (senza compilarli a mano), abilita il crawler. Il flusso è:

1. **(facoltativo, default ON)** Una sola chiamata LLM analizza la home: vede i link presenti raggruppati per pattern strutturale (es. `/catalogue/{slug}/index.html`, `/page-{int}.html`) e, conoscendo lo schema target, ritorna la **regex del path** delle pagine target.
2. **BFS deterministico**: il runner naviga il sito senza LLM, segue tutti i link interni fino a `Max profondità`, raccogliendo gli URL il cui path matcha la regex.
3. **Bulk extraction** classico sugli URL discovered (1 chiamata LLM per URL).

**Campi della UI**:
- **Abilita crawler dal seed** (checkbox): attiva/disattiva il flusso
- **URL pattern** (regex Python, opzionale): se vuoto → auto-detect via LLM (consigliato). Se compilato → bypassa l'LLM e usa la tua regex direttamente. Es. `^/catalogue/[^/]+/index\.html$`.
- **Max profondità link-following**: hop massimi dal seed (default 3). Per cataloghi con molte pagine paginate aumenta a 5-7.
- **Max URL totali** (riusa il campo "Max iterazioni"): cap di sicurezza globale (default 1000).

**Esempio per `books.toscrape.com`** (1 task, niente workflow):

- Modalità: `bulk_extract`
- Seed: `https://books.toscrape.com/`
- Schema: `ecommerce_products`
- Provider: `openai`, Modello: `gpt-4o-mini`
- Max URL: 1000
- ☑ Abilita crawler dal seed
- URL pattern: *(vuoto = auto-detect)*
- Max profondità: 3

Click ▶ Esegui ora. Nel job log vedrai:
```
crawler: auto-detect pattern via LLM (1 chiamata)...
✅ pattern auto-detected: '^/catalogue/[^/]+/index\.html$'
crawler depth 1/3: 1 URL da esplorare
crawler depth 2/3: 71 URL da esplorare
crawler depth 3/3: 350 URL da esplorare
crawler ha esplorato ~450 pagine, scoperto 1000 URL target
URL finali da processare con LLM extraction: 1000
progress: 50/1000 (47 ok, 3 failed)
...
Run completata: 985 estratti, 15 falliti, 985 ingest. Report: ...
```

Tempo totale: ~6-10 min. Costo: 1 LLM (auto-detect) + 1000 LLM (extraction) ≈ $0.20-0.40 con `gpt-4o-mini`.

**Quando il crawler NON funziona bene**:
- L'auto-detect produce una regex troppo stretta o troppo larga → controlla nel log il pattern proposto, ricopialo nel campo **URL pattern** e aggiustalo
- Il sito ha JS che genera link dinamicamente (i link appaiono solo dopo render) → usa `browser_use` invece
- Il sito blocca scraping con anti-bot → idem

#### 🧠 Mix di modelli LLM (capable + cheap)

Quando il crawler è abilitato, `bulk_extract` fa **2 tipi di chiamate LLM** con difficoltà molto diverse:

| Fase | N° chiamate | Difficoltà | Cosa serve |
|---|---|---|---|
| **Discovery** (auto-detect URL pattern) | 1 sola | 🔥 Alta — ragionare sulla struttura del sito + scegliere regex | modello capace |
| **Extraction** (riempire schema da testo pagina) | N (= URL discovered) | 🟢 Bassa — "vedo questo testo, riempi il JSON" | modello qualsiasi (anche locale gratis) |

Il task ha quindi **due slot LLM separati**:

- **Modello principale** (campo "Modello" nella sezione "Modello LLM"): usato per le N chiamate di Extraction. Conviene economico/locale.
- **Modello discovery** (campi "Discovery — Provider" e "Discovery — Modello", visibili solo per `bulk_extract`): usato SOLO per la chiamata di auto-detect. Conviene capace. Se vuoti, riusa il modello principale.

**Esempio ottimale per `books.toscrape.com`** (1000 libri, costo minimizzato):

| Slot | Provider | Modello | Chiamate | Costo |
|---|---|---|---|---|
| Discovery | `openai` | `gpt-4o-mini` | 1 | ~$0.0001 |
| Extraction (principale) | `ollama` | `llama3.1:8b` | 1000 | $0 (locale) |
| **Totale** | mix | | 1001 | **~$0.0001** |

Confronta con tutto OpenAI: $0.30. Risparmio: 99.97%.

> ⚠️ **Modelli Ollama da NON usare per l'Extraction**: tutti quelli con **thinking mode** acceso di default (`qwen3:*`, `qwen3.5:*`, `qwen3-coder:*`, `deepseek-r1:*`). Su Ollama OpenAI-compat il "ragionamento" finisce nel campo `reasoning` e brucia tutti i `max_tokens`, lasciando `content` vuoto → risultato: **0 estrazioni, 100% fallimenti**. Non c'è modo affidabile di disabilitarlo via API (`/no_think`, `think:false`, `chat_template_kwargs.enable_thinking=false` sono tutti ignorati su questi modelli).
>
> **Modelli Ollama testati e funzionanti per Extraction**: `llama3.1:8b` (rapido, qualità decente, può allucinare su pagine vuote), `mistral:latest` (rapido, JSON pulito), `gpt-oss:20b` (più qualitativo, un po' più lento). Per pagine "vuote" o ambigue, `gpt-4o-mini` resta la scelta più affidabile contro le allucinazioni.
>
> Verifica veloce: se nel job vedi `0 ok, N failed` e in `errors.jsonl` trovi `"raw_response": ""` su tutte le righe, è quasi sempre questo problema. Cambia modello.

**Quando NON splittare**:
- Cataloghi piccoli (≤50 URL): la differenza di costo è zero, lascia un solo modello.
- Pagine complesse (siti senza struttura uniforme): l'Extraction richiede a volte ragionamento → usa modello capable per entrambi.
- Macchina senza Ollama veloce: meglio rimanere su API (la latenza locale potrebbe essere più alta della rete).

**Come si aziona nel form**:

1. Compila il "Modello LLM" principale come sempre (es. `ollama` + `qwen3-coder:30b`)
2. Spunta "Abilita crawler dal seed"
3. Nel riquadro 🧠 "Quali LLM vengono usati e per cosa" che appare sotto, leggi i ruoli
4. Compila i 3 campi "Discovery — ..." SOLO se vuoi splittare:
   - **Discovery — Provider LLM**: es. `openai`
   - **Discovery — Modello**: es. `gpt-4o-mini`
   - **Discovery — API key** (campo password): solo se il provider scelto richiede chiave **e** non l'hai messa in env var. Se la metti in env (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ecc.) puoi lasciare vuoto. Se la compili qui, viene salvata nel DB del task (gitignorato).

   Se lasci tutti e 3 vuoti, il modello principale fa entrambe le cose.

Nel job log vedrai a inizio run:
```
🧠 Mix LLM attivo:
  • Discovery (auto-detect pattern, 1× chiamata): openai/gpt-4o-mini
  • Extraction (per URL, N× chiamate): ollama/qwen3-coder:30b
```

Se manca, significa che stai usando un solo modello per tutto.

**Output (dual: file + DB)**:
- `data/results/<task_id>/<ts>/profiles.jsonl` — UNA riga per URL processata con successo
- `data/results/<task_id>/<ts>/errors.jsonl` — log di URL falliti con motivo
- `data/results/<task_id>/<ts>/report.md` — riepilogo (totali, ok, fail, ingest)
- Tabella `contacts` (DB) — ingest automatico se lo schema include `email`/`telegram`

**Performance attesa** (esempio 1000 URL su `books.toscrape.com`):
- Concorrenza 5, rate limit 2/s → ~3-5 min totali
- 1000 chiamate LLM × `gpt-4o-mini` (~600 token/req) ≈ **$0.10-0.30 totali**
- Confronto: `browser_use` per gli stessi 1000 libri richiederebbe ~5000 step LLM ≈ $5-10 e ~5h

**Esempio — pipeline completa per `books.toscrape.com`**:

1. **Task A `BookDiscovery`** (`browser_use`, max_iter=50)
   - Obiettivo: "Naviga la paginazione del catalogo. Per ogni pagina-elenco estrai SOLO l'URL canonico di ogni libro"
   - Schema: `{"url": "URL completo della pagina-libro"}`
   - Output: `profiles.jsonl` con righe `{"url": "..."}`

2. **Task B `BookExtract`** (`bulk_extract`)
   - Input artifact: profiles.jsonl di A (passato via edge DAG)
   - Schema: template `ecommerce_products` (full)
   - Concorrenza: 5, rate: 2/s
   - Modello: `gpt-4o-mini`

3. **Workflow** edge A→B con `pass_artifact=profiles.jsonl`. Click ▶ Esegui workflow → A produce ~1000 URL in 5 min → B li processa in 3 min.

---

### 3.4 `auto_extract` — Il dispatcher "intelligente" per liste eterogenee

**Cosa fa**: ricevi una **lista di siti diversi** e ti chiedi "quale strategia per ognuno?". `auto_extract` decide DA SOLO. Per ogni URL della lista:

1. **Profiler LLM** (`app/agent/site_profiler.py`): fa fetch della home, calcola signals deterministiche (text-to-html ratio, link patterns, login forms, JS-heaviness, lingua, `has_recurring_target_pattern`) e con UNA chiamata LLM "capable" produce un JSON:
   ```
   {strategy: "bulk_extract" | "site_explorer" | "browser_use" | "skip",
    promising: "yes" | "maybe" | "no",
    reason: "...", target_hint: "...", expected_yield: 0-N}
   ```
2. **Dispatch**: instrada al runner corrispondente (con la stessa configurazione del task — schema, modello, browser_llm, ecc.). Per `browser_use` cap `max_iterations` a 25; per `site_explorer` cap a 50.
3. **Fallback automatico bidirezionale**: se la strategia primaria produce 0 profili, ritenta UNA volta con la strategia complementare (cap 1 fallback per sito):
   - `bulk_extract` → `site_explorer` (agente ReAct: trova listing nascoste, drill-down)
   - `site_explorer` → `bulk_extract` (caso raro: pattern semplice non rilevato)
   - `browser_use` → `site_explorer` (agente HTTP intelligente, più rapido di un secondo browser)
4. **Aggrega** tutti gli output in un unico `profiles.jsonl` consolidato + report.

**Matrice di scelta del profiler (post-2026-05-09)**:

| Segnali del sito | Strategia |
|---|---|
| `text_to_html_ratio<0.03` E body raw vuoto E nessun pattern ricorrente → vero JS-render | `browser_use` |
| `has_recurring_target_pattern=True` E ≥10 URL del pattern target sulla home | `bulk_extract` |
| HTML decente ma pattern target non chiaro / multi-livello / sub-domini come slug | **`site_explorer`** |
| Sito off-topic (title/contenuto non c'entra con l'obiettivo) | `skip` |
| Paywall completo / login obbligatorio per i dati | `skip` |
| HTTP 401/403/429 sul profiler (anti-bot blocca lo User-Agent) | `browser_use` (UA realistico + JS, prima di rinunciare) |

**Quando usarlo**:
- Hai una lista di **N siti diversi** (B2B lead gen, monitoraggio, audit) e non vuoi creare un task `bulk_extract` o `browser_use` per ognuno
- Non sai a priori se i siti sono statici, JS-heavy, o non scrappabili affatto
- Vuoi che il sistema **salti automaticamente** i siti off-topic (il profiler identifica "questo sito non c'entra con il tuo obiettivo" e mette `skip`)

**Quando NON usarlo**:
- Hai un solo sito che conosci bene → meglio `bulk_extract` o `browser_use` direttamente, è più prevedibile
- Hai bisogno di output deterministico (es. confronto con run precedenti): l'auto-detect del profiler può variare leggermente tra run

**Configurazione**:
- **Lista siti** (campo "Seed", una URL per riga): es. `https://sito1.com`, `https://sito2.com`, ...
- **Schema**: lo schema dei dati che vuoi estrarre — il profiler lo USA per giudicare se il sito è "promettente"
- **Obiettivo**: in italiano, descrive cosa cerchi. **Il profiler lo legge e lo usa per decidere skip/process**. Es: *"Trovare profili pubblici con email/telegram di freelance professionisti"*. Se metti uno schema dei prodotti e nell'obiettivo dici "voglio scarpe", il profiler scarterà siti di mobili.
- **Modello principale** (per Extraction): es. `gpt-4o-mini` o `llama3.1:8b` locale
- **Discovery LLM** (opzionale): per l'auto-detect del pattern URL nei siti `bulk_extract` — viene riusato anche dal profiler se compilato
- **Browser LLM** (opzionale): per i siti instradati a `browser_use` (sia primaria che fallback) — vedi §4.1
- **Crawler config + max_iterations**: applicate ai siti instradati a `bulk_extract`

**Output**:
- `data/results/<task_id>/<ts>/profiles.jsonl` — tutti i profili aggregati da TUTTI i siti
- `data/results/<task_id>/<ts>/auto_extract_report.json` — strutturato, una entry per sito con strategia/profili/reason
- `data/results/<task_id>/<ts>/report.md` — markdown leggibile con tabella per sito
- I sub-job (uno per ogni strategia/sito) restano nei loro timestamp accanto, per ispezionare il dettaglio

**Esempio per una lead gen B2B**:
- Lista siti: 50 URL di directory professionisti
- Obiettivo: *"Trovare freelance italiani con email pubblica visibile sulla pagina-profilo"*
- Schema: `profile_contacts`
- Modello: `gpt-4o-mini`

Il sistema processerà i 50 siti, scarterà quelli off-topic (che non ospitano profili pubblici), tenterà bulk_extract per ognuno, e fa fallback su browser_use solo dove necessario. Costo tipico: ~$0.30-2 per 50 siti.

**Costo del profiling**: 1 chiamata LLM per sito (~1500 token IN + 200 OUT) ≈ **$0.0003 per sito** con `gpt-4o-mini`. Una lista di 100 siti ti costa 3 centesimi solo per il triage iniziale. Trascurabile.

**Limiti noti**:
- Il profiler usa un User-Agent generico → siti con anti-bot stretto (Wikipedia, alcuni LinkedIn-style) ritornano HTTP 403 e vengono `skip`. Per gestirli serve `browser_use` esplicito o un UA realistico.
- Il `target_hint` ritornato dal profiler è solo indicativo: il discovery LLM dei sub-runner lo ricalcola (con il proprio retry loop) — niente single-point-of-failure.
- `http_llm_guided` non è ancora implementato → fallback su `bulk_extract` se il profiler lo sceglie.

---

### 3.4.1 `site_explorer` — Agente ReAct vero per navigazione di siti

**Cosa fa**: a differenza di `bulk_extract` (pipeline rigida discovery+crawler) e `auto_extract` (dispatcher di sub-runner), `site_explorer` usa un **LLM agentico** per decidere step-per-step come navigare un sito, esattamente come farebbe un umano:

> "Apro la home. Vedo nel menu un link `Vendita case` con sotto-link per zona fra cui Acireale. Quella è ovviamente la sezione che mi interessa. Apro `vendita-case/acireale/`. Vedo 30 schede annuncio. Provo a estrarne una per verificare il pattern. OK, sono pagine annuncio. Estraggo le altre. Pagine successive del paginatore? Se sì, vado avanti."

**Quando usarlo**:
- Siti dove `bulk_extract` con discovery automatica fallisce perché la home **non linka direttamente** le pagine target (es. yescasa.it, pcase.it: gli annunci sono dentro `/vendita-case/<citta>/`, non sulla home).
- Siti con struttura non ovvia / multi-livello / dove il pattern URL non si capisce a prima vista.
- Quando vuoi essere SICURO che l'agente esplori l'obiettivo dichiarato nel `objective` invece di vagare alla cieca.

**Architettura ReAct con 3 tool**:
- `fetch_page(url)` → ritorna `{title, n_internal_links, link_patterns: [{pattern, count, examples}], text_preview}`. Output compatto pensato per LLM (no HTML grezzo).
- `extract_target(url)` → fetch + LLM extractor con il template del task. Ritorna `{ok: true, asset_summary: "..."}` se la pagina è un target valido (post-`_has_minimal_data_for`), `{ok: false, reason: "..."}` se non lo è. Permette al LLM di **verificare in vivo** se un URL è un target, prima di sprecare 200 chiamate su URL fasulle.
- `done(reason)` → termina graceful.

**DOM enrichment automatico (2026-05-10)**: dopo che il LLM extractor produce il JSON, prima della validazione, il runner applica `_enrich_obj_from_dom(html, template)` che cerca nel raw HTML **pattern di contatto canonici** e popola i campi che il LLM ha lasciato vuoti:

| Campo | Pattern riconosciuti |
|---|---|
| `email` | `mailto:...` (preferito), o email inline nel testo se il LLM non ne ha trovata |
| `whatsapp` | `wa.me/<numero>`, `api.whatsapp.com/send?phone=<numero>` |
| `telegram` | `t.me/<handle>` |
| `social` | `instagram.com/<handle>`, `tiktok.com/@<handle>`, `twitter.com / x.com / facebook.com / youtube.com / linkedin.com/in/<...>`, `onlyfans.com / linktr.ee` |

Filtri anti-spam: vengono scartate email su domini palesemente di servizio (`sentry.io`, `wixpress.com`, `example.com`, `*.local`) e local-part chiaramente automatici (`noreply`, `no-reply`, `do-not-reply`, `donotreply`).

**Razionale**: i LLM piccoli (anche i coder 7B) tendono a "leggere" il testo ma a saltare gli `href` e gli attributi HTML. Un mailto del profilo molto spesso non finisce nel JSON estratto. Il DOM enrichment recupera quei dati gratis in O(1) regex sul main-content che il runner ha già in mano dopo `fetch_page`. Il LLM resta autoritativo (non sovrascrive mai un campo già pieno), il regex riempie solo i buchi.

**Scope dell'enrichment (2026-05-10, rev. 2)**: il regex viene applicato all'HTML grezzo **privato di header/footer/nav** (zone "globali del sito"). Il pre-processing avviene tramite [`_strip_global_chrome`](app/agent/runner_site_explorer.py): `selectolax` rimuove i tag semantici (`<header>`, `<footer>`, `<nav>`), gli ARIA roles (`banner`, `navigation`, `contentinfo`) e le class names comuni (`.site-header`, `.site-footer`, `.main-nav`, ...).

Razionale: avevamo prima provato a usare il `summary_html` di Readability (main-content), ma Readability spesso butta i blocchi link social del profilo perché non sono "narrativa" — perdevamo i contatti veri del singolo profilo (es. `t.me/MissGiadina`). All'opposto, il raw HTML grezzo include footer/header del sito → false attribuzioni (`info@mondocamgirls.com`, `twitter/MondoCamGirls`). La via di mezzo "raw HTML privato delle zone globali" tiene tutto il body del profilo (incluse sidebar e blocchi link) e taglia solo le zone notoriamente di sistema.

**Filtro anti-brand**: in più, ogni handle social estratto viene confrontato col primo segmento del registrable_domain del sito ([`_is_brand_handle`](app/agent/runner_site_explorer.py)). Esempio: su `mondocamgirls.com`, l'handle `MondoCamGirls` viene scartato (è il brand), mentre `MissGiadina` viene tenuto. Email con dominio uguale a quello del sito (`info@mondocamgirls.com` su `mondocamgirls.com`) sono pure scartate. Doppia rete di sicurezza contro le attribuzioni globali.

**Soglia `is_complete` per template (2026-05-10)**: il flag `is_complete` ritornato da `extract_target` non significa "tutti i campi-chiave popolati" (definizione troppo stretta che era impossibile da raggiungere su profili reali). Significa "soglia minima sufficiente":

| Template | `is_complete=true` quando |
|---|---|
| `profile_contacts` | `display_name` + ALMENO 1 fra `email/whatsapp/telegram/social/sitoweb` |
| `real_estate` | `prezzo_eur` + (`citta` o `indirizzo`) + (`categoria` o `tipo`) |
| `ecommerce_products` | `name` + `price_amount` |
| `events` | `title` + (`start_datetime` o (`city` E `venue`)) |
| `news_articles` | `title` + (`author` o `published_at`) |
| `job_listings` | `title` + `company` |

**Importante**: anche con `is_complete=false`, l'asset viene SALVATO (perché ha già passato la validazione `_has_minimal_data_for`). `is_complete` serve solo a guidare il LLM su _quando_ tentare drill-down e a sbloccare il pattern learning sotto. Il prompt è esplicito: "INCOMPLETO ≠ scartabile, vai avanti — il qualifier filtrerà dopo".

**Pattern learning sub-pagina (2026-05-10)**: il runner traccia un `learned_subpath` per-job. Funziona così:

1. L'agente fa `extract_target(alice.example.com/)` → `is_complete=false` (mancano email/social).
2. L'agente esplora, fa `fetch_page(alice.example.com/social-links/)`, poi `extract_target(alice.example.com/social-links/)` → `is_complete=true`.
3. **Il runner deriva**: il path "/social-links" è ciò che ha trasformato un incompleto in completo → `learned_subpath = "/social-links"`. Log: `💡 PATTERN IMPARATO: target completi su questo sito vivono in '/social-links/'`.
4. Per i profili successivi (bob, charlie, ...), ogni `fetch_page` o `extract_target` su una URL che non contiene già `/social-links` riceve nel proprio output un campo `hint` che dice all'agente: "vai diretto a `<profile_url>/social-links/`, evita di esplorare la home del profilo".
5. L'agente legge l'hint e va dritto al subpath utile per gli altri profili. Risultato: ~1 step in meno per profilo, su un sito da 25 profili sono 25 step risparmiati (cioè spesso la differenza fra "raggiunge il cap target" e "sfora `max_iterations` con metà cap fatto").

Il subpath imparato è **per-job** (resetta a fine task) e si "blocca" sul primo che funziona. Gestisce siti omogenei (tutti i profili in `/social-links/`); non gestisce siti misti (alice→`/social-links/`, charlie→`/contatti/`) — in quei casi resta valida la strategia di esplorazione del LLM.

Filtri sul subpath: i segmenti puramente numerici sono scartati (es. `/12345` non viene mai imparato come pattern: è un ID, non una sezione).

Il LLM tiene memoria implicita degli URL visitati e degli asset estratti tramite il context window (gpt-4o-mini regge facilmente 30 step).

**Configurazione del task**:

| Campo | Cosa metterci |
|---|---|
| `seed_queries` | **Un solo URL** per task: la home del sito o una sezione di alto livello. Non serve la listing precisa: il LLM la trova. |
| `extraction_template` | Obbligatorio: `real_estate`, `ecommerce_products`, ecc. Lo schema viene incluso nel system prompt. |
| `objective` | **CRUCIALE**: scrivilo specifico ("annunci immobiliari Acireale > 200k", non "annunci immobiliari"). Il LLM usa l'obiettivo per decidere quali sezioni esplorare. |
| `model` | Vedi sezione "Scelta del modello" sotto. **Importante**: i modelli code-tuned (qwen3-coder, deepseek-coder) battono i chat per questo loop. |
| `max_iterations` | Cap step LLM. Default 30. Aumentare a 50 per siti grandi. |
| `target_cap_per_site` | **Cap target per sito** (default 30, max 200). Quando l'agente arriva a N asset estratti, termina. Campo dedicato nel form (sezione "Configurazione site_explorer"). |
| `llm_provider` | OpenAI o Ollama vanno entrambi bene. La differenza vera è il modello, non il provider. |

> **Nota (2026-05-10)**: prima del 2026-05-10 il `target_cap_per_site` era riusato dal campo `bulk_concurrency`, creando ambiguità (concorrenza HTTP vs cap target). Ora `bulk_concurrency` significa SOLO concorrenza HTTP per `bulk_extract`, e `target_cap_per_site` è un campo separato nel DB e nel form. La migrazione è idempotente (`ALTER TABLE ADD COLUMN ... DEFAULT 30`): i task pre-2026-05-10 ereditano automaticamente il default 30.

**Scelta del modello (lezione dal campo, 2026-05-09)**:

L'aspettativa intuitiva era "modello chat = ragionamento naturale = vince". È sbagliata. Risultati reali sul medesimo task (yescasa.it, brief "annunci Acireale > 200k"):

| Modello | Step | Asset estratti | Note |
|---|---|---|---|
| `qwen3.5:latest` (~7B chat) | 9 | **0** | Si è perso al 9° step emettendo testo invece di tool_call. Non è mai arrivato a `extract_target`. |
| `qwen3-coder:30b` (30B code-tuned) | 11 | **2** | Ha capito subito la struttura, fatto il drill-down corretto, estratto 2 annunci. (Si è fermato presto — vedi reminder "non chiudere a 2" nel system prompt.) |
| `gpt-4o-mini` (OpenAI) | tipicamente 25-30 | 15-25 | Riferimento di robustezza. ~$0.05-0.20 per sito. |

**Perché i coder vincono qui**: tool-calling è essenzialmente "emetti JSON conforme a uno schema". I modelli code-tuned sono allenati a farlo a occhi chiusi (è il loro mestiere). I modelli chat su loop ReAct di 10+ step tendono a:
- emettere `tool_calls` malformati che il dispatcher salta;
- generare risposte in linguaggio naturale ("ora dovrei chiamare fetch_page...") **invece** del tool_call vero;
- "perdere il filo" del context dopo 5-10 turni.

**Raccomandazione operativa**:
- **Locale**: `qwen3-coder:30b` (se hai HW per 30B). In subordine: `deepseek-coder:6.7b` o `llama3.1:8b`.
- **Cloud**: `gpt-4o-mini` (riferimento), `gpt-4o` per siti molto difficili.
- **Sconsigliati per site_explorer**: modelli chat <8B (qwen3.5:latest, mistral:7b). Per **chat conversazionale** generale (es. orchestrator chat) restano invece la scelta giusta.

**Costo tipico**:
- Step LLM: ~$0.001-0.003 con gpt-4o-mini.
- Step `extract_target`: 1 fetch HTTP + 1 chiamata LLM extractor (~$0.005).
- **Totale per sito**: 30 step × $0.005 medio = **$0.05-0.20 per sito**, decisamente meno di `browser_use` ($5-10).

**Output**: `profiles.jsonl` (compatibile con qualifier downstream) + `report.md` con la cronologia dei passi.

**Esempio: yescasa.it brief "annunci Acireale > 200k"**:
- Step 1: `fetch_page(https://www.yescasa.it/)` → vede link `/vendita-case/<citta>/` (tante città).
- Step 2: il LLM ragiona "Acireale è in Sicilia, cerco /vendita-case/acireale/" → `fetch_page(https://www.yescasa.it/vendita-case/acireale/)`.
- Step 3: vede 30 link `/annuncio/<id>/` → prova `extract_target` su uno → `ok: true, asset_summary: "appartamento · Acireale · €250000"`.
- Step 4-25: itera `extract_target` sugli altri annunci della listing.
- Step 26: `done(reason: "raggiunto cap di 25 target")`.

**Cosa vedi nei log durante l'esecuzione** (logging dettagliato):

Ogni step del loop ReAct produce una riga compatta con tool + URL + outcome:
```
📄 step 1: fetch_page(https://www.yescasa.it/) → 47 link, 5 pattern, top: yescasa.it/{slug}/{slug} (30 URL) "Yescasa - Annunci Immobiliari"
📄 step 2: fetch_page(https://www.yescasa.it/vendita-case/acireale/) → 28 link, 2 pattern, top: yescasa.it/annuncio/{int} (24 URL)
✅ step 3: extract_target(https://www.yescasa.it/annuncio/658565/) → ok: appartamento · Acireale · €250000 [totale: 1/25]
✅ step 4: extract_target(https://www.yescasa.it/annuncio/657550/) → ok: villa · Acireale · €380000 [totale: 2/25]
⛔ step 5: extract_target(https://www.yescasa.it/agenzie-immobiliari/) → no: campi-chiave del template real_estate tutti vuoti
💭 step 6: Ho già estratto 2 annunci, vado avanti sulla listing principale.
📄 step 6: fetch_page(https://www.yescasa.it/vendita-case/acireale/?p=2) → 24 link, 1 pattern...
🏁 step 30: done → raggiunti 25 target sulla listing acireale
```

I prefissi:
- `📄` fetch_page: riassunto title + n. link + top pattern
- `✅` extract_target ok: asset estratto, sintesi del contenuto
- `⛔` extract_target no: motivo del fallimento (es. campi vuoti)
- `🏁` done: motivazione finale dell'agente
- `💭` thought: i "pensieri" del modello quando emette content + tool_call insieme
- `↩` se il modello smette di chiamare tool (problema, vedi "Scelta del modello" sopra)

**Few-shot nel system prompt**:

Al modello viene mostrato un esempio concreto di "traiettoria buona" all'interno del system prompt:
```
ESEMPIO DI TRAIETTORIA (per orientarti, NON copiare gli URL):
  step 1: fetch_page(home) → vede link /vendita-case/{citta}/
  step 2: fetch_page(/vendita-case/acireale/) → vede 25 link /annuncio/{int}
  step 3-N: extract_target sui restanti URL
  step N+1: done(...)
```

E due regole anti-fallimento osservate sul campo:
- **"DEVI sempre emettere un tool_call ad ogni turno (mai solo testo)"** — per i modelli chat che tendono a passare in modalità prosa.
- **"NON fermarti dopo aver estratto solo 2-3 target se la listing ne contiene di più"** — per i coder che tendono a chiudere troppo presto.

**Limiti**:
- Anche `site_explorer` NON supera anti-bot (Cloudflare, hCaptcha) di portali grandi.
- Se il sito è **completamente JS-rendered** e l'HTML iniziale è vuoto, l'agente vede una pagina vuota e termina; ricorri a `browser_use`.
- Costa più di `bulk_extract` (in cambio di intelligenza vera).
- Su siti **enormi e malstrutturati** può girare in tondo: il cap di `max_iterations` lo ferma comunque.

**Quando preferirlo agli altri**:
- ❌ `react`: site_explorer è specializzato per estrazione da un **singolo sito**, react fa ricerca multi-sito via DDG.
- ✅ vs `bulk_extract`: usa site_explorer quando la home non linka i target o quando bulk_extract estrae spazzatura.
- ✅ vs `browser_use`: usa site_explorer quando il sito è statico (HTML completo a fetch HTTP). Se il sito richiede JS, browser_use.
- 🔄 **vs `auto_extract`**: dal 2026-05-09 `auto_extract` include `site_explorer` come **terza strategia** scelta dal profiler, oltre a `bulk_extract` e `browser_use`. Quindi puoi anche metterti su `auto_extract` con una lista mista di siti, e il profiler decide caso-per-caso (bulk per pattern chiari, site_explorer per struttura non ovvia, browser_use solo per JS pesante, skip per siti irrecuperabili). Vedi §3.4 per la nuova matrice di scelta del profiler.

---

### 3.5 `qualifier` — Filtro/scoring contatti via LLM

**Cosa fa**: legge un `profiles.jsonl` (di solito prodotto da un task `browser_use` upstream) e per ogni riga chiede a un LLM "questo profilo è valido per outreach? scora 0-10". Materializza i contatti in tabella `contacts` con `status='qualified'` o `'rejected'`.

**Quando usarlo**:
- Quando vuoi togliere falsi positivi dallo scraping (pagine listing finite per errore, profili senza contatti utili, duplicati)
- Quando vuoi assegnare uno **score di priorità** ai contatti da contattare per primi

**Configurazione**:
- **Obiettivo**: criterio di valutazione, in italiano (es. "tieni solo profili con email pubblica e descrizione narrativa di almeno 100 caratteri")
- **Input artifact path**: percorso al `profiles.jsonl` upstream. Se il task è dentro un workflow con edge che ha `pass_artifact=profiles.jsonl`, viene passato automaticamente.
- **Modello**: piccolo va bene (`qwen3.5:latest`, `gpt-4o-mini`)

**Output**: cartella con:
- `qualified.jsonl` — solo i profili approvati, arricchiti con `_qualifier_score` e `_qualifier_reason`
- `rejected.jsonl` — i profili scartati con la motivazione
- `report.md` — totali

**Inserimenti DB**: i contatti finiscono nella tabella `contacts` con `status='qualified'` o `'rejected'`.

**Esempio**:
- Nome: "Qualifier wineshop leads"
- Obiettivo: "Tieni solo prodotti con prezzo > 15€ e descrizione che menziona almeno una varietà di uva. Scarta gli altri."
- Modello: `gpt-4o-mini`

---

### 3.5 `outreach` — Invio messaggi (email/telegram)

**Cosa fa**: legge i contatti `qualified` (o `new`) dalla tabella `contacts`, instanzia il `message_template` con placeholder per ogni contatto, e invia su uno o più canali. Aggiorna `contact.status='contacted'`.

**Quando usarlo**:
- Dopo aver scrappato + qualificato i contatti, per il primo invio
- Anche per invii ripetuti su contatti già esistenti (puoi resettarli a `qualified` da `/inbox/contacts`)

**Configurazione**:
- **Obiettivo**: nota descrittiva (l'agente non lo usa attivamente in questa modalità)
- **Subject email**: oggetto, può contenere placeholder come `{display_name}`, `{source_domain}`
- **Message template** (textarea): corpo del messaggio. Placeholder supportati:
  - `{display_name}` — nome del contatto
  - `{source_url}` — URL della pagina da cui è stato scrappato
  - `{source_domain}` — host (es. `example.com`)
  - `{email}`, `{telegram_username}`
- **Canali messaggio**: virgola-separati (`email`, `telegram`, oppure `email,telegram`)
- **Seed query**: opzionalmente una riga con un id numerico → filtro sui contatti che hanno quel `source_task_id`

**Vincoli**:
- Il **canale email** richiede SMTP configurato in `/settings`.
- Il **canale telegram** richiede bot token + che il contatto abbia già un `telegram_chat_id` (cioè abbia scritto al bot per primo). Telegram non permette invii cold ai bot.
- Il sistema rispetta il `rate_limit_per_minute` configurato in `/settings`.

**Output**: cartella con:
- `outreach_log.jsonl` — una riga per ogni messaggio inviato/fallito
- `report.md` — totali

**Esempio**:
- Nome: "Outreach wineshop"
- Subject: "Una proposta per {display_name}"
- Template:
  ```
  Ciao {display_name},
  
  ho visto la vostra selezione su {source_url} e mi è piaciuta molto.
  Mi occupo di ottimizzazione di schede prodotto e posso aiutarvi a
  migliorare le conversioni del 20-30%.
  
  Posso mandarvi un audit gratuito della vostra pagina?
  
  Grazie,
  Mario
  ```
- Canali: `email`

---

### 3.6 `responder` — Risposta automatica via LLM

**Cosa fa**: prende tutti i messaggi inbound (email/telegram) **non ancora processati**, per ognuno genera una reply via LLM usando il `responder_system_prompt`, e la invia. **Detection automatica di opt-out**: se il messaggio contiene parole come `STOP`, `unsubscribe`, `disiscrivi`, `rimuovimi`, `opt-out`, `non contattarmi`, il sistema marca il contatto come `optedout` e NON risponde.

**Quando usarlo**:
- Subito dopo aver lanciato un `outreach`, per gestire le risposte che arrivano
- Schedulato con cron (es. `*/15 * * * *` ogni 15 minuti) per gestione automatica continua

**Configurazione**:
- **Responder system prompt**: il system prompt dell'LLM. Deve definire tono, scopo, eventuali domande standard. Esempio:
  ```
  Sei un assistente cordiale che risponde a email/messaggi commerciali in italiano.
  Tono: professionale ma diretto.
  Scopo: confermare interesse e proporre una call la prossima settimana.
  Se l'utente fa una domanda specifica, rispondi nel merito; altrimenti
  chiedi 2 slot disponibili per una breve call di 15 minuti.
  Mai promettere prezzi o sconti senza conferma.
  ```
- **Modello**: medio-grande (`gpt-4o-mini` o superiore)

**Output**:
- Inserimenti in `messages` con `direction='out'` e `llm_generated=1`
- Aggiornamenti su `contact.status` e `thread.status`

⚠️ **Caveat**: l'auto-reply LLM **senza review umana** può produrre risposte inappropriate. Mitigazioni built-in: opt-out detection, history del thread sempre passata al modello (così tiene il contesto). Ma il rischio finale è tuo.

**Esempio**:
- Nome: "Auto-responder wineshop"
- System prompt: vedi sopra
- Modello: `gpt-4o-mini`
- Cron: `*/30 * * * *` (ogni 30 min)

---

## 4. I 6 provider LLM

Selezionabili per task dal selettore "Provider LLM" nel form. Le API key vanno in `.env` (preferito) o nel campo del task (memorizzate in DB).

| Provider | Quando | Costo | Qualità per agentic |
|---|---|---|---|
| **`ollama`** (default) | Tutto in locale, nessun cost | gratis (elettricità) | Modelli ≤ 20B → fragili su JSON tool-calling. OK per task semplici. |
| **`openai`** | Best-in-class per browser-use/qualifier/responder | $0.05–1/run | Eccellente |
| **`anthropic`** | Alternativa OpenAI, scrittura più "pulita" per email outreach | simile OpenAI | Eccellente |
| **`grok`** | xAI, opzione alternativa | basso | Buono ma meno testato |
| **`gemini`** | Google, contesto enorme (utile su pagine lunghe) | basso | Buono |
| **`custom`** | Qualsiasi endpoint OpenAI-compat (es. proxy aziendale, llama.cpp server) | dipende | dipende |

**Consigli per agent_mode**:
- `react`: `llama3.1:8b` o `mistral:latest` (ollama) bastano
- `browser_use`: idealmente `gpt-4o-mini` (~$0.10/run) o `gpt-oss:20b` se vuoi restare locale
- `bulk_extract`: per l'**Extraction** usa modelli **senza thinking mode** — `llama3.1:8b`, `mistral:latest`, `gpt-oss:20b` (ollama) o `gpt-4o-mini` (cloud). **Evita `qwen3*`, `qwen3.5*`, `qwen3-coder*`, `deepseek-r1*`**: il thinking mode brucia tutti i token e ritornano `content` vuoto. Per la **Discovery** (1 sola chiamata) usa pure un modello capace come `gpt-4o-mini`.
- **`site_explorer`**: agent loop ReAct multi-step → preferisci modelli **code-tuned** robusti sul tool-calling: **`qwen3-coder:30b`** (locale, raccomandato), `gpt-4o-mini` (cloud, riferimento). I chat <8B (`qwen3.5:latest`, `mistral:7b`) **falliscono spesso**: emettono prosa invece di tool_call dopo qualche step. Vedi §3.4.1 per i benchmark reali.
- `qualifier`: `gpt-4o-mini` o anche locale (qualifier è leggero, una chiamata per profilo)
- `responder`: medio-grande (la qualità della risposta scritta a un essere umano conta)
- `outreach`: non usa LLM (è puro template fill + send)

**Regola generale (lezione dal campo)**:
- **Agent loop multi-step con tool-calling** (`site_explorer`, `browser_use`, chat orchestrator con Azioni ON): preferisci **modelli code-tuned** o cloud capable. Il modello deve emettere JSON conforme a uno schema turno dopo turno; i coder lo fanno meglio dei chat di pari taglia.
- **Ragionamento aperto / sintesi prosaica** (chat orchestrator senza Azioni, `react`, `responder`): preferisci modelli **chat generalisti**.
- **Estrazione strutturata one-shot** (`bulk_extract` extraction, `qualifier` judging): qualunque modello ragionevole va bene, evita il thinking-mode.

**Setup chiavi** (in `.env`):
```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
XAI_API_KEY=xai-...
GEMINI_API_KEY=AIzaSy...
```

### 4.1 I 3 ruoli LLM in un task: Main / Discovery / Browser

Per `bulk_extract` e `auto_extract` un task può avere **fino a 3 LLM separati**, ognuno per un ruolo diverso. Questo permette di **mixare un modello capace per i task difficili con un modello locale gratis per i task ripetitivi**.

| Slot | Quando viene chiamato | N° chiamate per task | Modello consigliato |
|---|---|---|---|
| **Main / Extraction** | 1 volta per ogni URL processato (legge testo pagina + schema → JSON estratto) | **N** (= URL discovered) | Locale gratis (`llama3.1:8b`, `gpt-oss:20b`) o `gpt-4o-mini` se vuoi affidabilità |
| **Discovery** (opzionale) | 1 volta all'inizio per scegliere il pattern URL target nel crawler. In `auto_extract` viene riusato anche dal **profiler** (1 chiamata per sito) | 1 + numero siti | Capace: `gpt-4o-mini` (ottimo cost/quality, ~$0.0003 per chiamata) |
| **Browser** (opzionale) | Solo per `browser_use` o per i siti che `auto_extract` instrada al browser. Tool-calling complesso + visione | M chiamate (browser-use steps) | **Capable obbligatorio**: `gpt-4o-mini` minimo, meglio `gpt-4o`. Modelli ≤ 8B falliscono il tool-calling complesso. |

**Quando lasciare vuoti gli slot Discovery / Browser**: se il main è già adeguato per quel ruolo. Esempio: se main = `gpt-4o-mini`, lasciare vuoti Discovery e Browser → tutti e 3 i ruoli usano lo stesso. Costo unico, configurazione minima.

**Quando splittarli**: quando il main è locale gratis ma fallisce sui task complessi. Esempio:
- Main: `ollama/llama3.1:8b` (per le N chiamate di extraction, gratis)
- Discovery: `openai/gpt-4o-mini` (per la scelta del pattern URL — 1 chiamata, capable)
- Browser: `openai/gpt-4o-mini` (per i siti che richiedono Playwright)

**API key**: ogni slot ha il proprio campo password nel form. Se compili, viene salvata nel DB del task; altrimenti viene letta dall'env var del provider (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ecc.).

---

## 5. Template di estrazione (per `browser_use`)

Sotto il fieldset "Schema di estrazione" del task `browser_use`, scegli un template e modificalo. I template sono in [app/agent/extraction_templates.py](app/agent/extraction_templates.py):

| Template | Per cosa | Campi tipici |
|---|---|---|
| `profile_contacts` (default) | Profili di persone (modelle, freelance, professionisti) | username, email, whatsapp, telegram, social, sitoweb |
| `ecommerce_products` | Pagine prodotto e-commerce | sku, name, price_amount, availability, images, rating |
| `real_estate` | Annunci immobiliari | tipo (vendita/affitto), prezzo, mq, locali, agenzia |
| `events` | Eventi (concerti, conferenze) | start_datetime, venue, ticket_url, organizer |
| `news_articles` | Articoli giornalistici / blog post | title, author, published_at, summary, tags |
| `job_listings` | Annunci di lavoro | title, company, location, salary, apply_url |
| `custom` | Vuoto, scrivi tu | — |

Il template descrive due cose all'LLM:
1. **Come riconoscere la pagina** (es. "URL del tipo `/product/<id>`, mostra UN singolo prodotto con prezzo")
2. **Quali campi estrarre** (schema JSON commentato)

Se il template di default non corrisponde al tuo caso, **modifica la textarea**. È solo testo che viene iniettato nel prompt dell'LLM — più chiaro e specifico = miglior risultato.

---

## 6. Workflow e pipeline DAG

Un **workflow** è un grafo orientato di task collegati da edge. Quando uno upstream finisce, il downstream parte automaticamente.

### Creare un workflow

1. Vai su `/workflows` → `+ Nuovo workflow` → dai un nome (es. "Lead generation wineshop")
2. Sulla pagina dettaglio del workflow, aggiungi edge nel form "Aggiungi edge":
   - **Da** (upstream): il task che produce qualcosa
   - **A** (downstream): il task che consuma
   - **Artifact da passare**: il file relativo alla run dir di A che deve diventare l'input di B (es. `profiles.jsonl`)
3. Ripeti per ogni step della pipeline.

### Esecuzione

Click `▶ Esegui workflow` → il sistema:
1. Trova i **task root** (quelli senza edge in ingresso in questo workflow)
2. Crea un `workflow_run` (record in DB con stato `running`)
3. Lancia un job per ogni root, taggandolo col `workflow_run_id`
4. Quando un job finisce con `done`, segue gli edge → crea i job downstream con lo stesso `workflow_run_id`
5. Eventuali edge passano gli artifact aggiornando `task.input_artifact_path` del downstream prima di lanciarlo

### Cycle detection

Non puoi creare loop. Se provi `A→B→C→A` il sistema rifiuta con messaggio chiaro. La cycle detection è **scoped per workflow** — A→B in WF1 e B→A in WF2 sono OK perché non chiudono un loop dentro lo stesso workflow.

### Riusabilità task

Lo stesso task può apparire in più workflow. Sulla pagina del task vedi "Questo task fa parte di N workflow: ..." con i link. Modificando il task, il cambiamento riguarda **tutti** i workflow che lo usano.

---

## 7. Canali Email e Telegram

Configurabili in `/settings`. Le credenziali sensibili vanno preferibilmente in `.env`.

### Email

**Setup**:
1. Su `/settings` → sezione 📧 Email
2. SMTP host (es. `smtp.gmail.com`), port (587 con STARTTLS o 465 con SSL), user, From address
3. **Password**: in `.env` come `SMTP_PASSWORD`. Per Gmail: NON la password Google ma una **App Password** dedicata ([qui](https://myaccount.google.com/apppasswords))
4. IMAP host/user simili (per Gmail: `imap.gmail.com:993`); password in `IMAP_PASSWORD` (spesso uguale a SMTP)
5. Spunta "Canale abilitato"
6. Click "Test invio SMTP" → se arriva la mail di test sei a posto.

**Polling**: ogni 60 secondi un job APScheduler legge le mail nuove (UNSEEN) dalla casella IMAP, le parsa, matcha sul campo `From`, crea/aggiorna `contact` + `thread` + `message(direction='in', status='received')`.

### Telegram

**Setup**:
1. Apri Telegram, scrivi a [@BotFather](https://t.me/BotFather) → `/newbot` → segui le istruzioni → ottieni il **token** (formato `12345:ABCdef...`)
2. Mettilo in `.env` come `TELEGRAM_BOT_TOKEN`
3. Su `/settings` → sezione 💬 Telegram → spunta "Canale abilitato" → salva
4. Click "Test invio" → inserisci il **chat_id** del tuo account (lo ottieni scrivendo prima al bot e poi guardando i log inbound di AgentScraper).

**Polling**: ogni 30s `getUpdates` Telegram → ogni messaggio inbound diventa un `contact` (con `telegram_chat_id` salvato) + thread + message.

**Vincolo importante**: il bot Telegram **non può iniziare conversazioni**. Per inviare un messaggio outbound a un utente, quell'utente deve avergli scritto **almeno una volta**. Quindi un task `outreach` su Telegram funziona solo verso contatti che hanno già `telegram_chat_id` popolato.

---

## 8. Inbox e auto-reply

`/inbox` mostra tutti i thread di conversazione. Click su un thread → cronologia messaggi + form di reply manuale + bottone Opt-out.

`/inbox/contacts` mostra i contatti raggruppati per stato (`new`, `qualified`, `rejected`, `contacted`, `replied`, `optedout`). Da qui puoi:
- Filtrare per stato
- Mettere un contatto in opt-out manuale (🚫)
- Riportarlo a `qualified` (↩️) per re-includerlo in nuovi outreach

L'auto-reply funziona solo se hai un task `responder` che gira (manualmente o via cron).

---

## 9. Asset, tag e memoria pattern

A partire dall'iterazione 2026-05-09 il modello dati e il runtime hanno **due strati nuovi** che generalizzano il vecchio "tutto è un contatto" e dotano il sistema di una memoria di apprendimento per dominio.

### 9.1 Asset come modello dati generalizzato

Ogni riga di `profiles.jsonl` prodotta da un runner viene ingestata in tabella `assets`, indipendentemente dal template di estrazione:

| Asset type | Da cosa nasce | Tag derivati tipici |
|---|---|---|
| `real_estate` | template `real_estate` (case/ville) | `tipo` (vendita/affitto), `categoria`, `citta`, `price_band`, `mq_band`, `locali`, `classe_energetica` |
| `ecommerce_products` | template `ecommerce_products` | `category`, `brand`, `availability`, `price_band`, `rating_band` |
| `events` | template `events` | `category`, `city`, `country`, `is_free`, `availability` |
| `news_articles` | template `news_articles` | `category`, `author`, `topic`, `length_band` |
| `job_listings` | template `job_listings` | `company`, `location`, `remote_policy`, `employment_type`, `experience_level`, `salary_band` |
| `profile_contacts` | template `profile_contacts` | `contact` (email/whatsapp/telegram), `social`, `source_domain`, `lang` |

I tag sono derivati **dichiarativamente** (niente LLM) dal modulo [`app/agent/asset_tags.py`](app/agent/asset_tags.py) tramite `derive_tags(asset_type, raw_json)`. Sono indicizzati in tabella `asset_tags(asset_id, tag_key, tag_value)` con vincolo UNIQUE.

L'asset nasce con `status='new'` e può essere promosso a `qualified|rejected|archived` (manualmente da UI o tramite il qualifier downstream / chat orchestrator).

**`profile_contacts` resta speciale**: continua a essere ingestato anche in tabella `contacts` (con filtro email/telegram) per alimentare outreach. Per tutti gli altri template, l'ingest in `contacts` viene saltato (filtro "no email/telegram" non si applica più erroneamente a immobili/prodotti/eventi/articoli/lavoro).

### 9.2 La pagina `/assets`

`📦 Assets` nella nav. Fornisce:
- **Filtro tipo**: dropdown con tutti gli `asset_type` presenti in DB + count.
- **Filtro stato**: `new | qualified | rejected | archived`.
- **Filtri tag a faceting**: per ogni `tag_key` rilevante per il tipo selezionato, top 30 valori con count cliccabili. Multi-tag funziona come AND.
- **Tabella**: `id | tipo | titolo | dominio | tag (mini-chip) | stato | task origine`.
- Detail asset: `raw_json` completo, lista tag, form di promozione stato, link al task/job di origine.

Esempi di query URL:
- `/assets?asset_type=real_estate&tags=citta:Acireale,price_band:200-300k`
- `/assets?asset_type=job_listings&tags=remote_policy:remote,salary_band:60-90k`

### 9.3 Memoria pattern per dominio (`site_patterns`)

`bulk_extract` ora memorizza in DB i pattern URL "target" che ha imparato per ogni dominio.

Tabella `site_patterns`:
| Colonna | Significato |
|---|---|
| `registrable_domain` | es. `yescasa.it` |
| `pattern` | forma simbolica, es. `www.yescasa.it/annuncio/{int}` |
| `regex` | regex generata da `_pattern_to_regex` |
| `asset_type` | template per cui il pattern è valido (`real_estate`, ecc.) |
| `status` | `candidate` → `confirmed` → `rejected` |
| `hits` | URL del crawler che hanno matchato il pattern |
| `successes` / `failures` | quanti URL matchati hanno prodotto un'estrazione valida |

**Flusso**:
1. **All'inizio del crawler** in `runner_bulk_extract`: query `find_site_patterns(domain, asset_type, status='confirmed')`. Se trovato → riusa, salta la discovery LLM. **Risparmio**: ~2-8s + 1 chiamata LLM per dominio.
2. **Quando il discovery LLM trova un pattern accettato dal sanity check**: salvato come `candidate` (idempotente per `(domain, pattern)`).
3. **A fine run** (DOPO l'ingest in `assets`): `record_pattern_run(pattern_id, hits, successes, failures)` con contatori **post-validation**.
4. **Promozione automatica**: `maybe_promote_pattern` verifica le soglie:
   - candidate → confirmed: `successes >= 3` E `successes/(successes+failures) >= 0.4`.
   - confirmed → candidate (retrocesso): se `(successes+failures) >= 5` E ratio < 0.2.

**Cosa conta come `success` (importante!)**:
- `success` = **asset realmente valido** che è entrato in tabella `assets` dopo aver superato il filtro `_has_minimal_data_for` ([runner_browseruse.py](app/agent/runner_browseruse.py)).
- `failure` = URL processato dal pattern che ha prodotto un asset scartato (campi-chiave del template tutti vuoti) o ha fallito l'extraction LLM.
- **NON** è "extraction LLM ha emesso JSON parseable": un pattern che produce 200 JSON tutti vuoti conta 200 failures, non 200 successes. Questo evita che pattern fasulli (es. quelli che matchano pagine indice anziché annunci) vengano promossi a `confirmed` e poi riusati.

I log del runner mostrano:
- `📌 memoria DB: riuso pattern confermato per 'yescasa.it' [hits=20 successes=15]` → riuso.
- `📌 memoria DB: salvato pattern come 'candidate' (id=N)` → primo apprendimento.
- `📌 memoria DB: pattern id=N -> status='confirmed' (post-validation: 12 successes / 3 failures)` → promozione.

**Tool chat per ispezione/cleanup**: `list_site_patterns(domain?)` per leggere, `set_site_pattern_status(pattern_id, 'rejected')` per scartare manualmente un pattern fasullo (vedi 9.6).

### 9.3.1 Discovery multi-step: drill-down nelle listing intermediarie

I siti reali raramente espongono i link agli annunci/prodotti **direttamente dalla home**. Tipicamente la home ha solo un menu di sezioni (`/vendita-case/`, `/categoria/`, `/account/`, ecc.) e i target stanno **un livello più giù**, dentro le listing per zona/categoria (es. `/vendita-case/acireale/` linka i singoli annunci `/annuncio/<id>/`).

Il discovery del runner [`runner_bulk_extract.py`](app/agent/runner_bulk_extract.py) lavora su **due passate**:

1. **Pass 1 — discovery sul seed** (come prima): il LLM analizza i link interni del seed e propone un pattern target. Sanity check sui link diretti del seed: `n_match`.
2. **Pass 2 — drill-down nelle listing** (nuovo): se `n_match < 3` (pattern dubbio dal seed), il runner identifica fra i link del seed delle "candidate listing pages" e ci scende dentro:
   - **Step 2.1 — euristica keyword** [`_identify_candidate_listings`](app/agent/runner_bulk_extract.py): seleziona top 6 candidate URL dal sample del seed con uno score basato su:
     - **+3** se l'URL contiene una keyword di listing (`vendit`, `annunci`, `case`, `categori`, `catalog`, `prodott`, `elenco`, `directory`, `ricerc`, `comuni`, `regioni`, `zone`, ...).
     - **+2** se il pattern strutturale dell'URL ha ≥5 URL nel sample (pattern ricorrente).
     - **+1** se il path è corto (≤3 segmenti).
   - **Step 2.2 — rerank LLM** [`_rerank_listings_via_llm`](app/agent/runner_bulk_extract.py): chiede al modello di riordinare i 6 candidate secondo la coerenza semantica con l'**obiettivo del task** + lo **schema target**. Il modello vede una stringa tipo:
     > "OBIETTIVO: estrai annunci immobiliari Acireale > 200k. SCHEMA: real_estate (prezzo, mq, città, ...). Quali tra questi 6 URL sono LISTING che linkano i target?"
     
     Ritorna ordine top-N. Costo: 1 chiamata LLM extra per sito (solo quando il drill-down si attiva). Se il rerank fallisce silenziosamente, ricade sull'ordine euristico keyword.
   - **Step 2.3 — visita top 4** dopo rerank: il runner fa GET, estrae i link interni e ri-chiama il discovery LLM su ognuno.
   - Se trova un pattern con `n_match` migliore di quello del seed: lo adotta, **aggiunge la listing al seed** (così il crawler la visita), e aggiorna la memoria pattern in DB.
   - Si ferma appena trova un pattern con `n_match >= 5` (fortemente confermato).

Esempio concreto (yescasa.it, brief "annunci immobiliari Acireale"):
- Pass 1: home `https://www.yescasa.it`, link diretti tipo `/account/accedi/`, `/calcola-mutuo/`, `/vendita-case/<citta>/`. LLM propone pattern dubbio (perché gli annunci concreti `/annuncio/<id>/` non sono linkati direttamente).
- Pass 2.1: `_identify_candidate_listings` filtra top 6 (keyword euristica): `/vendita-case/acireale/`, `/vendita-case/catania/`, `/calcola-mutuo/`, `/agenzie-immobiliari/<citta>/`, ecc.
- Pass 2.2: `_rerank_listings_via_llm` con obiettivo "annunci Acireale" → mette `/vendita-case/acireale/` in cima (semanticamente più coerente di "calcola-mutuo" o "agenzie").
- Pass 2.3: visita `vendita-case/acireale/`, vede link `/annuncio/<id>/`. LLM propone pattern `www.yescasa.it/{slug}/{int}` con `n_match >> 5`.
- Pattern adottato. Listing aggiunta come seed. Crawler estrae i veri annunci.

Log diagnostici attivati nel job log:
```
🔍 pattern dal seed debole (1 match). Esploro 4 candidate listing (LLM-ranked)...
   listing candidate: https://www.yescasa.it/vendita-case/acireale/
     → pattern 'www.yescasa.it/{slug}/{int}': matcha 18/47 link della listing
✅ pattern migliorato dalla listing https://www.yescasa.it/vendita-case/acireale/: ... (18 match)
➕ listing aggiunta come seed: https://www.yescasa.it/vendita-case/acireale/
📌 memoria DB: pattern aggiornato dopo drill-down
```

**Costo**: 1 chiamata LLM extra per il rerank + fino a 4 fetch HTTP + 4 chiamate LLM aggiuntive per la discovery, **solo quando** il pattern dal seed è debole. Se il seed è già buono (`n_match >= 3`), il drill-down è skippato — costo zero.

### 9.3.2 Qualifier ora opera su `assets`, non su `profiles.jsonl` raw

Il runner [`runner_qualifier.py`](app/agent/runner_qualifier.py) ora ha **due sorgenti di input**, in ordine di priorità:

1. **Sorgente primaria — tabella `assets`** (default in workflow):
   - Il runner cerca i task **upstream** via `db.list_edges(to_task_id=<qualifier_task_id>)`.
   - Per ogni task upstream, carica `db.list_assets(source_task_id=src, status='new', limit=10000)`.
   - Valuta SOLO gli asset post-validation (non più 90% di pagine indice).
   - A ogni judgment, scrive `qualifier_score` + `status` direttamente sull'asset (`db.update_asset_qualifier`).
   - I qualified vengono anche scritti in `qualified.jsonl` per outreach/responder downstream (compat).

2. **Fallback — `input_artifact_path` (profiles.jsonl)**: se non ci sono asset upstream (es. task standalone, task lanciato manualmente senza workflow, dati importati da file esterno), il runner ricade sul comportamento legacy.

Il log mostra esplicitamente quale sorgente sta usando:
```
Sorgente: tabella `assets` (task upstream [22]): 18 asset 'new' da valutare.
```
oppure:
```
Sorgente: profiles.jsonl fallback (`/data/results/22/.../profiles.jsonl`).
```

**Conseguenze pratiche per la configurazione del task**:
- Se il qualifier è dentro un workflow (collegato via edge a un task upstream `auto_extract`/`bulk_extract`/`browser_use`): non serve impostare `input_artifact_path`. Il runner pesca dagli `assets`.
- Se il qualifier è standalone o vuoi forzare un file specifico: imposta `input_artifact_path` nel form del task. Il fallback parte solo se la sorgente primaria è vuota.

**Effetto sui numeri**: i contatori `qualified` / `rejected` adesso sono **onesti** — sono la frazione su asset reali validi, non su righe spazzatura. Prima del fix, un run poteva mostrare "qualified=42" su 401 ma 388 dei 401 erano pagine indice scartate dal validator; ora `qualified=N` significa N asset realmente promossi a `status='qualified'` in tabella `assets`.

### 9.3.3 Validation di completezza all'ingest

Prima di scrivere un `asset` in DB, [`_ingest_to_assets`](app/agent/runner_browseruse.py) verifica che il `raw_json` abbia almeno i **campi minimi** del template:

| Template | Campi minimi (almeno uno true) |
|---|---|
| `real_estate` | `prezzo_eur`, `metri_quadri`, `locali`, `indirizzo`, `agenzia` |
| `ecommerce_products` | `name` o `price_amount` |
| `events` | `title` o `start_datetime` |
| `news_articles` | `title` E almeno uno tra `author`, `published_at`, `summary` |
| `job_listings` | `title` E almeno uno tra `company`, `apply_url` |
| `profile_contacts` | `display_name`, `username`, `email`, `whatsapp`, `telegram` |
| `generic` | sempre accettato |

Le righe scartate vengono loggate come `⏭️ N righe scartate (campi-chiave del template '...' tutti vuoti)`. Risultato: niente più asset-fantasma da pagine indice processate per errore.

### 9.4 Cambio strategia automatico (cascading) e anti-loop

`auto_extract` adesso fa **cascading 3-via** quando una strategia produce 0 profili. Massimo 1 retry per sito (cap 1 fallback per non bruciare costi).

| Strategia primaria (profiler) | Fallback automatico | Razionale |
|---|---|---|
| `bulk_extract` → 0 profili | `site_explorer` | Pattern URL non chiaro: serve un agente che esplori semanticamente |
| `site_explorer` → 0 profili | `browser_use` | Caso "JS pesante non rilevato": l'agente ReAct su HTTP non vede contenuto, salgo a browser reale |
| `browser_use` → 0 profili o timeout | `site_explorer` | Browser pesante è inutile, agente ReAct su HTTP è più rapido |
| `skip` | nessun fallback | Decisione del profiler (sito off-topic) |

> **Nota (2026-05-09)**: il fallback di `site_explorer` ora va a `browser_use` (non più a `bulk_extract`). Razionale: se un agente ReAct con LLM tool-calling **non riesce a estrarre nulla** da un sito, è quasi certo che il problema è JS-render o anti-bot, non un pattern bulk-friendly che il LLM non ha colto. Saltare a un crawler deterministico è quasi sempre tempo sprecato.

La logica vive in [`runner_auto_extract.py`](app/agent/runner_auto_extract.py).

Il **profiler** distribuisce le 4 strategie con questa logica (vedi anche §3.4):
- **`browser_use`** SOLO se vero JS-render (`text_to_html_ratio<0.03` E body raw quasi vuoto E nessun pattern ricorrente).
- **`bulk_extract`** se `has_recurring_target_pattern=True` (≥10 URL target sul sample della home, es. `/annuncio/{int}`, `/product/{slug}`).
- **`site_explorer`** in tutti gli altri casi con HTML decente: pattern non chiaro, struttura multi-livello, sub-domini come slug, ecc. È la **scelta predefinita per siti "non banali"**.
- **`skip`** se off-topic o paywall completo.

**Anti-loop**: per evitare che gli agenti girino all'infinito, il runner applica cinture di sicurezza per modalità:
1. `max_iterations` cappato per i sub-job che hanno semantica "step LLM" invece di "URL processati":
   - `browser_use`: cap 25 step (prima ereditava 200 step di auto_extract → loop di minuti).
   - `site_explorer`: cap **min 50 / max 200** step (`max(50, min(inherited, 200))`). Il default ReAct è 30, ma se l'utente sa di voler estrarre molti profili (es. `target_cap_per_site=100`) può alzare `max_iterations` del task fino a 200. Cap minimo 50 protegge da impostazioni sbagliate (es. `max_iterations=10`).
2. Browser-use `step_timeout=180s` (default). Step più lento di 3 min = abort.
3. `asyncio.wait_for` esterno a `agent.run()` con timeout `max(180, max_steps*15+60)`. Su TimeoutError salva quanto raccolto e passa al seed successivo.
4. Site_explorer: validation completezza degli asset post-extract (vedi §9.3.3) impedisce ingest di JSON vuoti.

### 9.5 Pulsante Stop davvero affidabile

Click "Stop" su un job (sia entry-point sia sub-job) ora:
1. Scrive `control_signal='stop'` in DB.
2. Risolve il task asyncio in `_active_jobs[job_id]` (anche per i sub-job, che ora si registrano: in [`runner_browseruse.run_agent`](app/agent/runner_browseruse.py) e [`runner_bulk_extract.run_agent`](app/agent/runner_bulk_extract.py) si fa `register_subjob` all'ingresso, `unregister_subjob` in `finally`).
3. Chiama `task.cancel()` cross-thread → propagazione `CancelledError` alla prossima `await` interna → httpx chiude TCP → OpenAI interrompe la generation lato server (best effort).

Per browser_use specificamente, `register_should_stop_callback` viene cablato a `db.get_control_signal(job_id) == 'stop'`: browser-use lo invoca **tra una step e l'altra**. Quindi anche senza task.cancel(), basta che il control_signal vada a 'stop' e l'agent termina graceful entro 1 step (~10s).

Il timeout della singola chiamata LLM è ora `60s` esplicito sul `ChatOpenAI`: limita la finestra di esposizione su completion in volo dopo un Stop.

### 9.6 Tool chat orchestrator per asset e pattern

Quando il toggle Azioni è ON, l'orchestrator può anche:
- `list_assets({asset_type, status, tags: ["citta:Acireale","price_band:200-300k"], limit})`
- `get_asset({asset_id})` — dettaglio + tag + raw_json.
- `update_asset_status({asset_id, status, notes})`
- `list_site_patterns({registrable_domain, status, limit})`
- `set_site_pattern_status({pattern_id, status})` — utile per `'rejected'` un pattern sbagliato.

Esempi da chat:
- "elenca gli annunci immobiliari ad Acireale sopra 200k" → l'orchestrator chiama `list_assets(asset_type='real_estate', tags=['citta:Acireale','price_band:200-300k'])`.
- "ho già un pattern confirmed per yescasa.it?" → `list_site_patterns(registrable_domain='yescasa.it')`.

### 9.7 Modalità d'uso aggiornate (best practice)

Linee guida pratiche dopo i fix di affidabilità. Seguile per evitare i tre problemi tipici (asset spazzatura, pattern fasulli in memoria, qualifier che valuta rumore).

#### Configurare bene un task scraping (`bulk_extract` / `auto_extract` / `site_explorer`)

| Cosa | Suggerimento |
|---|---|
| **Seed URL** | Preferisci sempre la **listing page della tua zona/categoria** (`https://sito.it/vendita/acireale/`), non la home (`https://sito.it/`). Quando proprio non sai la listing, lascia comunque la home: il drill-down LLM-ranked (9.3.1, e su `site_explorer` il navigatore ReAct) prova a scendere automaticamente. |
| **`extraction_template`** | Sceglilo coerente coi dati che ti aspetti: `real_estate` per annunci immobili, `ecommerce_products` per shop, ecc. La validation post-ingest (9.3.3) usa questo per scartare le pagine "spazzatura". Se metti il template sbagliato, asset validi vengono scartati. |
| **`crawler_enabled`** | Lascialo **ON** quando vuoi che il sito venga esplorato in profondità (solo `bulk_extract`). Per `site_explorer` non si applica: il LLM esplora step-per-step. |
| **`max_iterations`** | Per `bulk_extract` / `auto_extract`: 100-200 (cap totale di URL processati). Per `site_explorer`: 30-50 (cap step LLM). Per `browser_use`: 25 (oltre, entra in loop). |
| **`bulk_concurrency`** | **Solo `bulk_extract`**: concorrenza HTTP fetch (URL paralleli). 3-5 default. Se i siti rispondono lenti, abbassa a 2. Non si applica a `site_explorer`. |
| **`target_cap_per_site`** | **Solo `site_explorer` / `auto_extract`→`site_explorer`**: massimo asset estratti per sito (default 30, max 200). Alza a 50-100 per directory grandi. Non si applica a `bulk_extract` (che processa la lista intera). |
| **Modello** | `gpt-4o-mini` è ottimo rapporto qualità/costo. Per `site_explorer` locale preferisci modelli code-tuned (`qwen3-coder`, `deepseek-coder`): battono i chat sui loop di tool-calling. Per tasks `react` resta su Ollama locale (gratis). |

#### Configurare bene un workflow `extract → qualifier`

1. Crea il task `extract` (`auto_extract` o `bulk_extract`) con seed e template come sopra.
2. Crea il task `qualifier`:
   - `agent_mode = qualifier`
   - `objective` = istruzioni specifiche per il filtro (es. "Tieni solo annunci con prezzo > 200000 EUR E località in provincia di Catania (Acireale, Catania, Aci Castello, ...)").
   - `input_artifact_path`: **lascia vuoto** se è dentro a un workflow con edge dall'extract. Il qualifier ora pesca direttamente dalla tabella `assets` (vedi 9.3.2).
   - Modello: anche un Ollama locale tool-capable va bene per il judging.
3. In `/workflows/<id>`, aggiungi un edge `extract → qualifier` (con `pass_artifact='profiles.jsonl'` opzionale, retrocompat).
4. Lancia il workflow. Il qualifier ora opererà SOLO sugli asset validati (post-`_has_minimal_data_for`), non sulle pagine indice spazzatura.

#### Quando rilanciare/cancellare un pattern dalla memoria

La memoria pattern impara progressivamente. Comportamento atteso:
- **Primo run su un dominio**: pattern salvato come `candidate`, hits/successes accumulati.
- **Run successivi**: se `successes >= 3` E ratio `successes/(successes+failures) >= 0.4` → promosso a `confirmed`. Da quel momento il discovery LLM viene saltato per quel dominio (risparmio tempo+$).
- **Pattern fallato**: se confirmed e poi `(successes+failures) >= 5` con ratio < 0.2 → retrocesso automaticamente a `candidate`.

**Manualmente**: se ti accorgi che un pattern è proprio sbagliato (es. punta a pagine `/account/` invece che ad annunci), apri la chat orchestrator (con Azioni ON) e di' "metti `rejected` il pattern X di yescasa.it". L'LLM chiamerà `set_site_pattern_status`. Da quel momento il pattern non verrà più riusato.

#### Quando lanciare un task standalone (senza workflow)

Pratico per: test rapidi, riusare un `profiles.jsonl` esistente, valutare un set di profili importati da fuori.

- Task `bulk_extract` standalone: imposta `seed_queries`, lascia `crawler_enabled=on`, lancialo.
- Task `qualifier` standalone: imposta `input_artifact_path` (puoi caricarlo dalla UI o sceglierlo dal dropdown). Il fallback profiles.jsonl si attiva solo in questo caso.
- Task `outreach` / `responder`: sempre richiedono task upstream (qualifier) o dati importati.

#### Stop pulito di un job che gira male

Se un job `auto_extract` o `bulk_extract` sta producendo solo rumore (lo vedi nei progress: `0 ok / X failed`), **clicca Stop sul sub-job** o sul job parent dalla UI:
- Il control_signal viene scritto in DB.
- Il task asyncio viene cancellato (Fix sub-job in `_active_jobs` sempre attivo).
- Browser-use viene fermato dalla callback `register_should_stop_callback` entro pochi secondi (non aspetta la fine della seed).
- httpx chiude il TCP a OpenAI: il completion in volo viene interrotto sul server (best effort, qualche cent di token già emessi possono essere fatturati).

---

## 10. Casi d'uso completi

### Caso 1 — Lead generation B2B end-to-end

**Obiettivo**: generare lead per il tuo servizio di ottimizzazione contenuti, contattando i proprietari di wineshop indipendenti italiani.

**Setup**:

1. **Task A — `Scraper wineshop directory`** (`agent_mode=browser_use`)
   - Provider: OpenAI, Modello: `gpt-4o-mini`
   - Seed: URL della directory (es. una lista di wineshop italiani)
   - Schema: `ecommerce_products` modificato per estrarre anche `email_proprietario` se presente nel footer
   - Max iter per seed: 30

2. **Task B — `Qualifier wineshop`** (`agent_mode=qualifier`)
   - Modello: `gpt-4o-mini`
   - Obiettivo: "Tieni solo i siti che hanno un'email pubblica visibile e mostrano almeno 10 prodotti. Scarta marketplace generalisti."

3. **Task C — `Outreach wineshop IT`** (`agent_mode=outreach`)
   - Subject: "Audit gratuito per {display_name}"
   - Template: vedi esempio in §3.4
   - Canali: `email`

4. **Task D — `Responder commerciale`** (`agent_mode=responder`)
   - System prompt: "Sei un commerciale cortese, italiano, conciso. Se l'utente è interessato proponi una call. Se chiede prezzi rispondi 'preferisco discuterli in call'. Se non è interessato, ringrazia e chiudi."
   - Modello: `gpt-4o-mini`
   - Cron: `*/15 * * * *` (controlla replies ogni 15 min)

5. **Workflow `Lead generation wineshop`**:
   - Edge A→B con `pass_artifact=profiles.jsonl`
   - Edge B→C con `pass_artifact=qualified.jsonl`
   - (D è schedulato a parte via cron)

6. **Esecuzione**: Click ▶ Esegui workflow → A scrappa → B qualifica → C invia email. D controlla risposte automaticamente in background.

### Caso 2 — Audit competitor (un singolo task)

**Obiettivo**: capire come 3 competitor strutturano le loro landing page.

**Setup**:
- **Task `Audit landing competitor`** (`agent_mode=browser_use`)
  - Seed:
    ```
    https://competitor1.com/
    https://competitor2.com/
    https://competitor3.com/
    ```
  - Schema: `custom`, scrivi tu i campi che ti interessano (h1, CTA principale, value proposition, social proof, ecc.)
  - Modello: `gpt-4o-mini`
  - Max iter: 10 per seed (la home è poco)
- **Niente workflow**: lancialo direttamente con ▶ Esegui ora.
- **Output**: `data/results/<task>/<ts>/profiles.jsonl` con 3 righe (una per competitor) + `report.md`.

### Caso 3 — News digest schedulato

**Obiettivo**: ricevere ogni mattina un digest di notizie tecnologiche italiane via email a te stesso.

**Setup**:
1. **Task `Digest tech news`** (`agent_mode=react`)
   - Obiettivo: "Trova le 5 notizie tech italiane più rilevanti delle ultime 24h. Per ognuna: titolo, fonte, URL, 3 righe di sommario."
   - Modello: `qwen3.5:latest` (locale, gratis)
   - Cron: `0 8 * * *` (ogni mattina alle 8)

2. **Task `Email digest a me`** (`agent_mode=outreach`)
   - Subject: "Tech news IT — {source_domain}"  *(non importa, sarà uguale ogni giorno)*
   - Message template: usa il `{...}` solo se vuoi; altrimenti testo fisso.
   - Seed query: id numerico del Task 1 → filtra contatti generati da quel task
   - **Trick per inviare a te stesso**: pre-popola un contact con la tua email tramite il responder o manualmente (un'opzione futura sarà "destinatari hardcoded").

3. **Workflow `Daily digest`**: edge `Digest news` → `Email digest a me`

4. Cron sul task 1 lancia tutto ogni mattina alle 8.

> Nota: il task 2 è un workaround perché outreach legge dalla tabella `contacts`. Per scenari "notifica a me stesso" ha più senso usare lo step ReAct e leggere il report finale via /tasks o vita una mail manuale dal task. Una versione futura potrebbe avere un `notifier` task dedicato.

### Caso 4 — Outreach multi-lingua

**Obiettivo**: stessi contatti, due messaggi (italiano e inglese) a seconda della lingua del sito.

**Setup**:
1. **Task A — `Scraping cataloghi multinazionali`** (`browser_use`)
2. **Task B — `Qualifier IT`**: tieni solo `lang='it'` dalle pagine
3. **Task C — `Qualifier EN`**: tieni solo `lang='en'`
4. **Task D — `Outreach italiano`** (template in italiano)
5. **Task E — `Outreach english`** (template in inglese)

**Workflow `Multi-lingua`**:
- Edge A→B (`pass_artifact=profiles.jsonl`)
- Edge A→C (`pass_artifact=profiles.jsonl`)  *(stesso input, due qualifier diversi)*
- Edge B→D (`pass_artifact=qualified.jsonl`)
- Edge C→E (`pass_artifact=qualified.jsonl`)

**Diagramma**:
```
       A (scraper)
      / \
     B   C   (qualifier IT / qualifier EN)
     |   |
     D   E   (outreach IT / outreach EN)
```

Click ▶ Esegui workflow → A parte, poi B e C in parallelo, poi D e E in parallelo. Niente di nuovo da configurare nel runtime — il DAG fa il suo lavoro.

### Caso 5 — Monitoraggio pagine + alert

**Obiettivo**: ogni giorno controlla se nuove pagine prodotto sono apparse su un sito monitorato; se sì, mandami un'email di alert.

**Setup**:
1. **Task A — `Monitor wineshop`** (`browser_use` o `react` se le pagine sono semplici)
   - Cron: `0 7 * * *` (alle 7 ogni giorno)
   - Schema: estrai URL + title di tutte le pagine prodotto
2. **Task B — `Alert manuale`** (`outreach`)
   - Solo se ci sono nuovi profili rispetto al giorno prima → invia email a te stesso

> Nota: il "diff con il giorno prima" attualmente NON è automatico. Per ora ogni run riproduce tutto. Per implementarlo serve un piccolo `differ` task custom (futuro).

---

## 11. Comandi utili

```powershell
# Avvio (con auto-reload)
agentscraper

# Test
pytest

# Reset DB (dati persi)
del data\agentscraper.db
agentscraper   # ricrea

# Pulizia cache Playwright (~150MB)
playwright install chromium --force
```

URL principali:
- `http://127.0.0.1:8000/` — lista task
- `/tasks/new` — crea task
- `/workflows` — lista workflow
- `/inbox` — conversazioni
- `/inbox/contacts` — contatti
- `/settings` — canali email/telegram

---

## 12. Troubleshooting

**Job rimane "running" per ore senza progredire**
- Click ⏹ Stop (hard cancel del task asyncio)
- Se il bottone non c'è (job marcato come `dead`), c'è un bottone "💀 Chiudi job morto"
- Riavvia uvicorn → al boot tutti i job orfani vengono marcati `error` automaticamente

**Browser-use con Ollama qwen3.5 non estrae nulla**
- Modelli ≤20B fanno fatica con il JSON tool-calling complesso di browser-use. Switcha a `gpt-4o-mini` o `gpt-oss:20b` (locale) o `claude-haiku-4-5`.

**`bulk_extract`: tutte le URL falliscono con `raw_response: ""`**
- È quasi certo un modello Ollama con **thinking mode** attivo (qwen3*, qwen3.5*, qwen3-coder*, deepseek-r1*). Il modello scrive nel campo `reasoning`, mai in `content`, e brucia tutti i `max_tokens` ragionando. Cambia il "Modello" del task in `llama3.1:8b`, `mistral:latest`, `gpt-oss:20b` o `gpt-4o-mini`. Vedi §3.3 per il dettaglio.

**`bulk_extract` con `llama3.1:8b` allucina dati su pagine vuote**
- Modelli da 8B parametri tendono a "completare" lo schema anche se la pagina non contiene i dati (es. inventa un titolo libro sulla home page). Mitigazioni: 1) usa un modello più grande (`gpt-oss:20b` locale o `gpt-4o-mini` cloud), 2) escludi gli URL "indice" dalla lista (home, categoria, paginazione), 3) post-filtra `profiles.jsonl` scartando righe con campi-chiave mancanti.

**`auto_extract` mette `skip` su tutti i siti**
- Il profiler decide sulla base di `objective` + `extraction_schema`. Se il tuo objective è generico ("estrai dati") e lo schema è generico, il profiler non capisce cosa cerchi e tende a `skip`. Soluzioni: 1) scrivi un objective specifico in italiano (chi/cosa/dove cerchi), 2) compila lo schema con campi precisi, 3) verifica che i siti seed siano raggiungibili (HTTP 200 senza UA filter — alcuni anti-bot bloccano lo UA generico del profiler → `skip` per HTTP 403).

**`auto_extract` con HTTP 403 sul profiler**
- Wikipedia, ResearchGate, LinkedIn e altri grandi player bloccano user agent generici → il profiler riceve 403 → `skip`. Workaround: crea un task `browser_use` esplicito per quei siti specifici (Playwright passa indenne). In futuro: UA realistico configurabile.

**SMTP test fallisce con "auth"**
- Per Gmail/Outlook NON usare la password normale. Crea una **App Password** dedicata.
- Verifica che `SMTP_PASSWORD` in `.env` sia stata letta (riavvia uvicorn dopo modifiche a `.env`).

**Telegram bot non riceve messaggi**
- Hai scritto al bot per primo? È un vincolo di Telegram.
- `getUpdates` polling è ogni 30s; aspetta un po'.

**"Edge crea un ciclo nel DAG di questo workflow"**
- Stai cercando di creare un loop. Es. A→B esiste, e ora vuoi B→A. Non si può. La cycle detection è scoped per workflow, quindi puoi avere B→A in un ALTRO workflow.

**`profiles.jsonl` non viene passato al downstream**
- Sull'edge devi specificare il nome del file in **Artifact da passare** (es. `profiles.jsonl`, `qualified.jsonl`). L'edge default non passa nulla.

---

## 13. Considerazioni etiche e legali

⚠️ **AgentScraper è dual-use**. È legittimo per content audit, monitoraggio competitor, ricerca, lead generation B2B. È invece **rischioso** per:

- **Scraping di dati personali identificabili** senza base giuridica (GDPR art. 6). Il Garante italiano ha emesso un provvedimento specifico nel 2024 sul web scraping. Anche dati pubblicamente visibili richiedono interesse legittimo documentato per essere raccolti sistematicamente.
- **Outreach commerciale automatizzato** verso contatti scrappati. CAN-SPAM (US) richiede unsubscribe; ePrivacy (UE) richiede consenso preventivo per consumer e legitimate interest documentato per B2B.
- **Auto-reply LLM senza review** rischia di mandare risposte inappropriate. Mitigazioni built-in: opt-out detection automatica, history thread sempre passata. Ma l'ultima riga la firmi tu.
- **ToS dei provider** (Gmail, Telegram, OpenAI): l'invio massivo automatico con identità non chiare può chiuderti l'account.

L'app fornisce gli strumenti tecnici. La conformità legale e l'etica sono responsabilità tua.

---

## 14. Cosa NON fa (ancora)

- **WhatsApp**: scartato (Meta Cloud API troppo costosa/burocratica per single-user)
- **Webhook pubblici**: solo polling. Per webhook reali servirebbe tunneling HTTPS (ngrok/cloudflare).
- **A/B testing template outreach**: si fa duplicando il task
- **Drip campaigns** (sequenze nel tempo): per ora outreach manda tutto in batch
- **Diff fra run successivi** (es. "solo nuovi profili"): da costruire come task custom
- **Visualizzazione DAG grafica**: solo lista testuale
- **Multi-utente / autenticazione**: è single-user locale; per uso condiviso servirebbe auth + isolamento DB

Il piano è allineabile a tutto questo — basta chiedere.
