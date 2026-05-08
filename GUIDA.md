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

**Sezioni collassabili** (`<details>` HTML nativi): tutte le sezioni "fieldset" del form sono retrattili — click sul titolo per chiudere/aprire. Default: aperte le sezioni essenziali, chiuse le opzionali (es. "Configura Discovery LLM (avanzato)" si apre solo se ci sono già valori salvati). Stato della singola sezione open/closed NON viene persistito tra reload.

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
| **`auto_extract`** | **Profiler + dispatch automatico** | dipende | dipende dai siti | **Lista eterogenea di siti** |

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

1. **Profiler LLM** (`app/agent/site_profiler.py`): fa fetch della home, calcola signals deterministiche (text-to-html ratio, link patterns, login forms, JS-heaviness, lingua) e con UNA chiamata LLM "capable" produce un JSON:
   ```
   {strategy: "bulk_extract" | "browser_use" | "http_llm_guided" | "skip",
    promising: "yes" | "maybe" | "no",
    reason: "...", target_hint: "...", expected_yield: 0-N}
   ```
2. **Dispatch**: instrada al runner corrispondente (con la stessa configurazione del task — schema, modello, browser_llm, ecc.)
3. **Fallback automatico**: se la strategia primaria produce 0 profili e non era già `browser_use`, ritenta UNA volta con `browser_use` (cap a 1 fallback per sito per non bruciare costi)
4. **Aggrega** tutti gli output in un unico `profiles.jsonl` consolidato + report.

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

**Consigli**:
- `react`: `llama3.1:8b` o `mistral:latest` (ollama) bastano
- `browser_use`: idealmente `gpt-4o-mini` (~$0.10/run) o `gpt-oss:20b` se vuoi restare locale
- `bulk_extract`: per l'**Extraction** usa modelli **senza thinking mode** — `llama3.1:8b`, `mistral:latest`, `gpt-oss:20b` (ollama) o `gpt-4o-mini` (cloud). **Evita `qwen3*`, `qwen3.5*`, `qwen3-coder*`, `deepseek-r1*`**: il thinking mode brucia tutti i token e ritornano `content` vuoto. Per la **Discovery** (1 sola chiamata) usa pure un modello capace come `gpt-4o-mini`.
- `qualifier`: `gpt-4o-mini` o anche locale (qualifier è leggero, una chiamata per profilo)
- `responder`: medio-grande (la qualità della risposta scritta a un essere umano conta)
- `outreach`: non usa LLM (è puro template fill + send)

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

## 9. Casi d'uso completi

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

## 10. Comandi utili

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

## 11. Troubleshooting

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

## 12. Considerazioni etiche e legali

⚠️ **AgentScraper è dual-use**. È legittimo per content audit, monitoraggio competitor, ricerca, lead generation B2B. È invece **rischioso** per:

- **Scraping di dati personali identificabili** senza base giuridica (GDPR art. 6). Il Garante italiano ha emesso un provvedimento specifico nel 2024 sul web scraping. Anche dati pubblicamente visibili richiedono interesse legittimo documentato per essere raccolti sistematicamente.
- **Outreach commerciale automatizzato** verso contatti scrappati. CAN-SPAM (US) richiede unsubscribe; ePrivacy (UE) richiede consenso preventivo per consumer e legitimate interest documentato per B2B.
- **Auto-reply LLM senza review** rischia di mandare risposte inappropriate. Mitigazioni built-in: opt-out detection automatica, history thread sempre passata. Ma l'ultima riga la firmi tu.
- **ToS dei provider** (Gmail, Telegram, OpenAI): l'invio massivo automatico con identità non chiare può chiuderti l'account.

L'app fornisce gli strumenti tecnici. La conformità legale e l'etica sono responsabilità tua.

---

## 13. Cosa NON fa (ancora)

- **WhatsApp**: scartato (Meta Cloud API troppo costosa/burocratica per single-user)
- **Webhook pubblici**: solo polling. Per webhook reali servirebbe tunneling HTTPS (ngrok/cloudflare).
- **A/B testing template outreach**: si fa duplicando il task
- **Drip campaigns** (sequenze nel tempo): per ora outreach manda tutto in batch
- **Diff fra run successivi** (es. "solo nuovi profili"): da costruire come task custom
- **Visualizzazione DAG grafica**: solo lista testuale
- **Multi-utente / autenticazione**: è single-user locale; per uso condiviso servirebbe auth + isolamento DB

Il piano è allineabile a tutto questo — basta chiedere.
