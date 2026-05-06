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

---

## 3. I 5 tipi di Task (`agent_mode`)

Quando crei un task, il campo **Modalità agente** determina cosa farà. Ci sono 5 modalità: 2 per scraping, 1 per qualificazione, 1 per outreach, 1 per risposta.

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

### 3.3 `qualifier` — Filtro/scoring contatti via LLM

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

### 3.4 `outreach` — Invio messaggi (email/telegram)

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

### 3.5 `responder` — Risposta automatica via LLM

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
- `react`: `qwen3.5:latest` o `llama3.1:8b` (ollama) bastano
- `browser_use`: idealmente `gpt-4o-mini` (~$0.10/run) o `gpt-oss:20b`/`qwen3-coder:30b` se vuoi restare locale
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
