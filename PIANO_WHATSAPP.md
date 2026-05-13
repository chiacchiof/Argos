# Piano implementativo — `outreach_whatsapp` (Fase 1)

> **Scopo del documento**: definire architettura, schema dati, contratti API
> e sequenza di implementazione PRIMA di scrivere codice. Da rivedere con
> l'utente e approvare prima di iniziare la Fase 1.
>
> **Stato**: bozza per revisione — nessun codice ancora scritto.

---

## 1. Contesto e scelte già fatte

Aggiungere a AgentScraper la modalità `outreach_whatsapp` per inviare DM via
WhatsApp ai contatti `qualified` in tabella `contacts`. WhatsApp ha vincoli
unici che lo distinguono da email/telegram/social esistenti:

- Cold outreach (contatto senza opt-in) è **possibile solo via browser
  automation** su `web.whatsapp.com` — viola ToS Meta, rischio ban.
- API ufficiale Meta Cloud è **legale e scalabile** ma vincolata a contatti
  che hanno scritto al business number negli ultimi 24h, e a template
  pre-approvati.

### Scelte confermate dall'utente

| # | Scelta | Implicazione |
|---|---|---|
| 1 | **Doppio motore**: A (browser automation) + B (Meta Cloud API) | Engine selector decide per ogni contatto in base al consenso |
| 2 | **Modalità separata** `outreach_whatsapp` | Nuovo runner, NON sotto-caso di `outreach_social` |
| 3 | **LLM rephrase** del messaggio per ogni contatto | Riusa pattern di `message_generator.py` |

### Riuso dell'infrastruttura esistente

L'app ha già in [app/agent/social/](app/agent/social/) un'infrastruttura
platform-agnostic (per Instagram/TikTok/Facebook). WhatsApp si innesta
come terza piattaforma riusando:

- `crypto_creds.py` — `encrypt`/`decrypt` con `AGENTSCRAPER_SECRET`
- `humanize.py` — typing per-char, delays randomici
- `account_pool.py` — pool con rotazione
- `proxy_pool.py` — proxy opzionali
- `session_manager.py` — persistenza session su disk
- `message_generator.py` — LLM rephrase batch
- `platform_base.py` — interfaccia `SocialPlatform` (login, goto_profile, send_dm, check_health)

Non serve creare `app/whatsapp/`: aggiungiamo dentro `app/agent/social/`.

---

## 2. Architettura logica

```
┌────────────────────────────────────────────────────────────────┐
│  runner_outreach_whatsapp.py  (NUOVO)                          │
│                                                                │
│  1. Carica contacts.status='qualified' WITH whatsapp != null   │
│  2. Per ogni contatto:                                         │
│       engine = select_engine(contact)                          │
│            ├─ contact.whatsapp_consent='opt_in' → engine B     │
│            ├─ contact ha scritto nelle ultime 24h → engine B   │
│            └─ default → engine A (cold)                        │
│       message = llm_rephrase(template, contact)                │
│       result  = engine.send_dm(contact.whatsapp, message)      │
│       log_dm(result, contact, engine_label)                    │
│       update contacts.status='contacted'                       │
│  3. Report finale: .md + .jsonl                                │
└──────────────────────┬─────────────────────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
┌─────────────────────┐      ┌─────────────────────┐
│ Engine A — Browser  │      │ Engine B — API      │
│ social/whatsapp_    │      │ social/whatsapp_    │
│ browser.py          │      │ api.py              │
│                     │      │                     │
│ Playwright headed   │      │ httpx async POST    │
│ + stealth           │      │ Meta Cloud API      │
│ web.whatsapp.com    │      │ v17+ graph.facebook │
│                     │      │                     │
│ QR login persistito │      │ Template message    │
│ user_data_dir       │      │ (pre-approvati Meta)│
│ humanize typing     │      │                     │
│ rate-limit anti-ban │      │ 24h-window check    │
└─────────────────────┘      └─────────────────────┘
        │                             │
        ▼                             ▼
   ┌───────────────────────────────────────┐
   │  social_dm_log (riusato)              │
   │  + colonna `engine` ('A'|'B')         │
   └───────────────────────────────────────┘
```

---

## 3. Schema DB

### 3.1 Tabella esistente `social_accounts` — estensione (migrazione idempotente)

Riusiamo `social_accounts` per il pool del Motore A. Aggiungiamo:

```sql
-- Migrazione idempotente in db.init_db()
ALTER TABLE social_accounts ADD COLUMN phone_number TEXT;
  -- per WhatsApp: numero in formato E.164 (es. "+393331234567")
ALTER TABLE social_accounts ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'password';
  -- 'password' (instagram/tiktok) | 'qr_session' (whatsapp_browser) | 'api_token' (whatsapp_api)
ALTER TABLE social_accounts ADD COLUMN session_dir TEXT;
  -- path al user_data_dir di Playwright per persistenza session WA Web

-- Valore di `platform` per WhatsApp:
-- 'whatsapp_browser' → Motore A
-- (per Motore B usiamo whatsapp_api_config, vedi 3.2)
```

Quando l'utente aggiunge un account WhatsApp da Settings UI, scriviamo:
- `platform='whatsapp_browser'`
- `username=<label libera, es. "WA principale">`
- `phone_number='+393331234567'`
- `auth_method='qr_session'`
- `encrypted_password=NULL` (no password, login via QR)
- `session_dir='data/whatsapp_sessions/<uuid>'`

### 3.2 Nuova tabella `whatsapp_api_config` (Motore B)

```sql
CREATE TABLE IF NOT EXISTS whatsapp_api_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
    -- nome leggibile (es. "Numero business AGS")
  phone_number_id TEXT NOT NULL,
    -- ID Meta del numero registrato
  business_account_id TEXT NOT NULL,
    -- WABA ID
  app_id TEXT,
    -- App ID Meta (opzionale, solo per webhook)
  encrypted_access_token BLOB NOT NULL,
    -- access_token cifrato con AGENTSCRAPER_SECRET
  default_template_name TEXT,
    -- nome del template message di default (es. "lead_outreach_v1_it")
  default_template_language TEXT NOT NULL DEFAULT 'it',
  status TEXT NOT NULL DEFAULT 'active',
    -- 'active' | 'disabled' | 'rate_limited'
  daily_msg_cap INTEGER NOT NULL DEFAULT 250,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_whatsapp_api_config_status
  ON whatsapp_api_config(status);
```

### 3.3 Tabella esistente `social_dm_log` — estensione

```sql
ALTER TABLE social_dm_log ADD COLUMN engine TEXT;
  -- 'A_browser' | 'B_api' | NULL per record legacy IG/TikTok
ALTER TABLE social_dm_log ADD COLUMN api_config_id INTEGER
  REFERENCES whatsapp_api_config(id) ON DELETE SET NULL;
  -- popolato quando engine='B_api'; account_id è NULL in quel caso
```

Nota: `social_dm_log.account_id` è ora NULLABLE perché Motore B non usa
`social_accounts`. La FK rimane su `social_accounts(id)` con `ON DELETE SET NULL`.

### 3.4 Tabella esistente `contacts` — estensione

```sql
ALTER TABLE contacts ADD COLUMN whatsapp_consent TEXT NOT NULL DEFAULT 'cold';
  -- 'cold' | 'opt_in' | 'optedout'
ALTER TABLE contacts ADD COLUMN whatsapp_last_inbound_at TEXT;
  -- ISO-8601: ultima volta che il contatto ha scritto al business (per 24h-window)
```

Il valore `whatsapp_consent` viene popolato:
- A `'cold'` di default (=outreach iniziale via Motore A)
- A `'opt_in'` se l'utente lo marca manualmente da UI o se il contatto
  scrive al business number per primo (vedi flow Fase 3)
- A `'optedout'` se detecta keyword opt-out (STOP, rimuovimi, ecc.) nei
  messaggi inbound

---

## 4. Configurazione

### 4.1 Env vars (`.env.example`)

```bash
# WhatsApp Motore A (browser automation) — niente in env, tutto in DB
# WhatsApp Motore B (Meta Cloud API) — opzionale, può anche stare in DB
META_WHATSAPP_ACCESS_TOKEN=        # opzionale, fallback se non in DB
META_WHATSAPP_PHONE_NUMBER_ID=     # opzionale
META_WHATSAPP_BUSINESS_ACCOUNT_ID= # opzionale
META_WHATSAPP_VERIFY_TOKEN=        # per webhook futuro (Fase 3)

# Strategia engine selection
WHATSAPP_DEFAULT_ENGINE=auto       # 'A' | 'B' | 'auto'  (default: auto)

# Rate-limit anti-ban Motore A
WHATSAPP_A_MAX_PER_HOUR=30         # max DM/ora per account
WHATSAPP_A_PAUSE_MIN_SEC=30        # pausa minima tra DM
WHATSAPP_A_PAUSE_MAX_SEC=180       # pausa massima

# AGENTSCRAPER_SECRET (già esiste, riusato)
AGENTSCRAPER_SECRET=...            # per cifrare access_token Meta
```

### 4.2 Settings UI — nuova pagina `/settings/whatsapp`

Sezione dentro la pagina Settings esistente (o tab separato):

**Pannello Motore A — Account browser**
- Lista degli `social_accounts` con `platform='whatsapp_browser'`
- Per ogni account: label, phone_number, status (active/banned), DM oggi/totali, ultimo uso
- Bottone "➕ Aggiungi account WA":
  1. Form: label + phone_number
  2. Crea record DB con `status='pending_login'`
  3. Apre un modal con QR code (mostrato server-side via Playwright headed)
  4. Utente scansiona con telefono → status='active', salva session_dir
- Bottone "🗑️ Elimina" / "⏸ Disabilita" per account
- Test: bottone "📤 Invia DM di test" a un numero a scelta

**Pannello Motore B — API config**
- Lista record `whatsapp_api_config`
- Form per nuovo: label, phone_number_id, business_account_id, app_id, access_token, default_template_name, default_template_language
- Bottone "🧪 Test API" che fa una chiamata `GET /v17.0/<phone_number_id>` per validare il token
- Template management: form per registrare un nuovo template (proxy verso Meta Manager)

### 4.3 Task form — nuova modalità `outreach_whatsapp`

Form simile a `outreach` esistente, ma con campi specifici WhatsApp:

- **Modello messaggio** (textarea): template con placeholder `{display_name}`, `{source_url}`, `{phone}`, ecc.
- **Engine preference**: dropdown `auto` (default) | `force_A` (sempre browser) | `force_B` (sempre API)
- **Account WhatsApp** (Motore A): dropdown con account `platform='whatsapp_browser'`, status='active'. Multi-select se vuoi pool rotation.
- **API config** (Motore B): dropdown con `whatsapp_api_config.status='active'`. Single-select.
- **Template Meta** (solo Motore B): dropdown con i template registrati su Meta dell'API config selezionata.
- **Rate-limit override** (opzionale): max per ora, pause min/max — solo per Motore A.
- **Personalizzazione LLM**: checkbox "Personalizza messaggio per ogni contatto via LLM". Default ON.
- **Dry-run**: checkbox "Solo simulazione (no invio reale)". Default OFF.

---

## 5. Flussi operativi

### 5.1 Flow A — Cold outreach (Motore browser)

```
1. Carica accounts: db.list_social_accounts(platform='whatsapp_browser', status='active')
2. Se 0 account: errore "Nessun account WhatsApp configurato"
3. Carica contacts: status='qualified' WITH whatsapp != null AND whatsapp_consent != 'optedout'
4. Per ogni contatto (con rotazione account round-robin):
   a. engine_for_this = select_engine(contact)  # 'A' o 'B'
   b. message = llm_rephrase(template, contact)  # 1 chiamata Qwen locale
   c. account = rotate(active_accounts)
   d. result = await engine_A.send_dm(account, contact.whatsapp, message)
   e. db.insert_social_dm_log({
        account_id: account.id, engine: 'A_browser',
        contact_id, ok: result.ok, message, sent_at
      })
   f. db.update_contact_status(contact.id, 'contacted')
   g. await random_sleep(WHATSAPP_A_PAUSE_MIN_SEC, WHATSAPP_A_PAUSE_MAX_SEC)
   h. Se contatori orari > WHATSAPP_A_MAX_PER_HOUR per account, skippa quell'account fino al prossimo slot
5. Genera report.md + outreach_log.jsonl in data/results/<task_id>/<ts>/
```

### 5.2 Flow B — Opt-in follow-up (Motore API)

```
1. Carica config: db.list_whatsapp_api_config(status='active')
2. Selezionata UNA config (single, no rotation)
3. Carica contacts: status='qualified' WITH whatsapp_consent='opt_in' AND
   whatsapp_last_inbound_at > now - 24h
4. Per ogni contatto:
   a. message = llm_rephrase(template, contact)
   b. result = await engine_B.send_template(config, contact.whatsapp,
                template_name, template_lang, [params])
       # NOTA: dentro 24h-window puoi anche mandare free-form text
       # Fuori dalla 24h-window puoi mandare solo template approvati
   c. log + update status (come Flow A)
   d. Niente rate-limit aggressivo, Meta gestisce throttling lato suo
```

### 5.3 Engine selector

```python
def select_engine(contact: dict, default: str = "auto") -> str:
    """Ritorna 'A' o 'B' per il contatto specifico."""
    if default == "force_A": return "A"
    if default == "force_B": return "B"
    # auto:
    consent = contact.get("whatsapp_consent", "cold")
    if consent == "optedout":
        raise SkipContact("opt-out registered")
    if consent == "opt_in":
        last_in = contact.get("whatsapp_last_inbound_at")
        if last_in and (now - parse(last_in)) < timedelta(hours=24):
            return "B"  # dentro 24h-window, può mandare free-form via API
        # Fuori 24h-window: può mandare solo template via B
        # → ancora B, ma usando template
        return "B"
    # cold: deve passare per A (API legale solo per opt-in)
    return "A"
```

---

## 6. Engine A — Browser automation

### 6.1 File: `app/agent/social/whatsapp_browser.py`

Estende `SocialPlatform` (interfaccia comune). Implementa:

```python
class WhatsAppBrowser(SocialPlatform):
    platform_id = "whatsapp_browser"

    async def login(self, page: Page, account: SocialAccount) -> bool:
        """Carica user_data_dir esistente. Se sessione scaduta, mostra QR e attende scan."""
        # 1. Se account.session_dir esiste e ha file di sessione validi, naviga
        #    a web.whatsapp.com — dovrebbe essere già loggato
        # 2. Se appare il QR code: salva PNG in data/whatsapp_sessions/<uuid>/qr.png,
        #    update social_accounts.status='pending_qr', notifica via job log
        # 3. Polling: ogni 5s controlla se il QR è stato scansionato (DOM cambia)
        # 4. Quando loggato, status='active', returns True

    async def goto_profile(self, page: Page, phone_number: str) -> bool:
        """Naviga a https://web.whatsapp.com/send?phone=<digits>&text=<empty>"""
        # 1. Strip + del numero, encode
        # 2. Aspetta il pulsante "Continua nella chat" o errore "Numero non WA"
        # 3. Click + attendi pannello chat aperto

    async def send_dm(self, page: Page, phone: str, message: str) -> SendResult:
        """Apre chat con phone, typa il messaggio, invia, verifica spunte."""
        # 1. await self.goto_profile(page, phone)
        # 2. Localizza editor input (selectors.WA_MESSAGE_INPUT)
        # 3. Click + humanize_type(message, delay_per_char=random.uniform(0.05, 0.18))
        # 4. Click invio (Enter o button)
        # 5. Aspetta 1-3s, verifica spunta:
        #    - 1 spunta grigia → sent
        #    - 2 spunte grigie → delivered
        #    - errore "Numero non valido" → SendResult(ok=False, reason="invalid_number")
        # 6. Estrai message_id dal DOM se possibile

    async def check_health(self, page: Page) -> HealthStatus:
        """Verifica stato sessione: ok / challenged / banned."""
        # - Se ban: pagina "Il tuo telefono ha bisogno di riconnettersi" o
        #   "Sei stato disconnesso" → status='banned'
        # - Se sessione scaduta (QR appare) → status='challenged'
```

### 6.2 File: `app/agent/social/whatsapp_selectors.py`

Tutti i selettori CSS WhatsApp Web in un unico file (fragili per design — aggiornabili senza toccare la logica):

```python
WA_QR_CANVAS = 'canvas[aria-label="Scan me!"]'
WA_QR_REFRESHED = '[data-testid="qr-refresh-button"]'
WA_MAIN_APP = '[data-testid="chat-list"]'
WA_MESSAGE_INPUT = 'div[contenteditable="true"][data-tab="10"]'
WA_SEND_BUTTON = 'button[data-testid="send"]'
WA_INVALID_NUMBER_MSG = 'div[role="alert"]:has-text("Il numero di telefono")'
WA_CHECKMARK_SENT = 'span[data-testid="msg-check"]'
WA_CHECKMARK_DELIVERED = 'span[data-testid="msg-dblcheck"]'
WA_LOGOUT_PROMPT = 'text="Sei stato disconnesso"'
```

### 6.3 Anti-ban

- Rate-limit configurabile: default 30 msg/ora/account
- Pause randomiche tra invii: 30-180s
- Humanize typing: per-char delay random 50-180ms
- Pool rotation: round-robin tra account `status='active'`
- Pre-flight check: `await check_health(page)` prima di OGNI invio; se challenged/banned, skippa account
- Daily cap: rispetto `social_accounts.daily_dm_cap` (default 100 per WA, vs 10 default IG)
- **Skip dei contatti senza spunta verde**: dopo l'invio, se dopo 30s la spunta non appare → log `ok=False, reason='not_delivered'` e proseguo

---

## 7. Engine B — Meta Cloud API

### 7.1 File: `app/agent/social/whatsapp_api.py`

NON estende `SocialPlatform` (non c'è browser). Classe autonoma:

```python
class WhatsAppAPI:
    """HTTP client per Meta WhatsApp Cloud API v17.0+."""

    def __init__(self, config: dict):
        self.phone_number_id = config["phone_number_id"]
        self.token = decrypt(config["encrypted_access_token"])
        self.base_url = f"https://graph.facebook.com/v17.0/{self.phone_number_id}"

    async def send_template(self, to: str, template_name: str,
                            lang: str, components: list[dict]) -> SendResult:
        """Invia un template message pre-approvato."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": lang},
                "components": components,  # placeholder values
            }
        }
        # POST /messages, parse response.messages[0].id → message_id

    async def send_text(self, to: str, body: str) -> SendResult:
        """Free-form text — funziona SOLO dentro 24h-window dopo che il contatto
        ha scritto al business number. Altrimenti Meta ritorna error code 131056."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }

    async def list_templates(self) -> list[dict]:
        """GET /<WABA_ID>/message_templates — per popolare il dropdown UI."""
```

### 7.2 Template management

Meta richiede che i template siano **pre-approvati** prima dell'uso. Il piano
NON include un editor di template (uso Meta Business Manager web). Il task
form mostra solo i template già approvati nel WABA selezionato.

### 7.3 24h-window logic

```python
def can_send_freeform(contact: dict) -> bool:
    last_in = contact.get("whatsapp_last_inbound_at")
    if not last_in:
        return False
    return (now_utc() - parse_iso(last_in)) < timedelta(hours=24)
```

---

## 8. LLM rephrase

Riuso di `app/agent/social/message_generator.py`. Funzione:

```python
generate_batch(
    template_str: str,
    contacts: list[dict],
    llm_provider: str = "ollama",
    llm_model: str = "qwen3.5:latest",
) -> list[str]  # un messaggio per ogni contatto
```

Per WhatsApp, vincoli aggiuntivi:
- Messaggi corti (max ~600 char): WA censura testi lunghi/spam-like
- No link nei primi 3 messaggi (Meta penalizza link a freddo)
- No emoji eccessivi (3-5 max)
- Tono colloquiale italiano (configurabile per lingua del contatto)

**Per Motore B con template message**: niente LLM rephrase del corpo
(il template è fisso). LLM personalizza SOLO i parametri `{{1}}`, `{{2}}`
del template (es. nome destinatario, link offerta).

---

## 9. UI dettagli aggiuntivi

### 9.1 Dashboard job WhatsApp

Mostra in real-time:
- Account in uso (Motore A): label + DM oggi/cap
- Distribuzione engine: % messaggi A vs B
- Spunte: tot sent, tot delivered, tot failed, tot opted-out
- Last error (se rate-limited o banned)

### 9.2 Warning UI

Banner permanente nel form `outreach_whatsapp` (e anche nel pannello Settings):

> ⚠️ **WhatsApp Web automation (Motore A) viola i ToS Meta.**
> L'uso massivo per cold outreach può comportare **ban del numero**. Per
> uso intensivo a contatti senza opt-in il rischio è alto. AgentScraper non
> ti protegge da queste conseguenze. Procedi a tuo rischio.
>
> Il Motore B (API ufficiale) richiede opt-in dei contatti ed è limitato a
> template pre-approvati. Per cold outreach legale non è applicabile.

### 9.3 Conferma esplicita

Bottone "▶ Esegui ora" del task `outreach_whatsapp` chiede conferma modal
con riepilogo: "Stai per mandare N messaggi a M contatti via Motore A
(rischio ban) / B (legale). Procedere?"

---

## 10. Sicurezza credenziali

- `whatsapp_api_config.encrypted_access_token` cifrato con
  `AGENTSCRAPER_SECRET` via `social/crypto_creds.py`
- `social_accounts.session_dir` (file Playwright user_data_dir): NON cifrato,
  ma in cartella gitignored. Eventuale wipe alla disattivazione account.
- QR PNG temporaneo (visible nella UI durante login): cancellato dopo scan
- Access token Meta su file system: solo se presente in `.env` (fallback);
  preferire DB cifrato

---

## 11. Sequenza di implementazione (Fase 1 MVP)

In ordine cronologico:

### Step 1 — DB migrations (~30 min)
- Aggiungere colonne a `social_accounts`: `phone_number`, `auth_method`, `session_dir`
- Aggiungere colonna `engine` + `api_config_id` a `social_dm_log`
- Aggiungere colonne a `contacts`: `whatsapp_consent`, `whatsapp_last_inbound_at`
- Creare tabella `whatsapp_api_config`
- Smoke test: init_db() su DB esistente non rompe nulla

### Step 2 — Crypto + DB helpers (~30 min)
- Funzioni `db.insert_whatsapp_api_config()`, `list_whatsapp_api_config()`, ecc.
- Helper `db.update_contact_whatsapp_consent(contact_id, consent)`
- Test unit per cifratura access_token

### Step 3 — Engine A core (~3h)
- `app/agent/social/whatsapp_selectors.py` (lista selettori)
- `app/agent/social/whatsapp_browser.py` (classe `WhatsAppBrowser`)
- Smoke test manuale: login con un numero, invio singolo DM a se stesso

### Step 4 — Engine B core (~2h)
- `app/agent/social/whatsapp_api.py` (classe `WhatsAppAPI`)
- Smoke test con curl/httpx contro graph.facebook.com (con un access_token sandbox Meta)
- Implementazione: `send_template`, `send_text`, `list_templates`

### Step 5 — Runner `outreach_whatsapp` (~2h)
- `app/agent/runner_outreach_whatsapp.py`
- Engine selector
- Loop principale con rate-limit, humanize, rotation
- Integrazione `message_generator` per LLM rephrase
- Logging in `social_dm_log`
- Pause/stop unificato via `runner_control`

### Step 6 — Dispatcher in `jobs.py` (~15 min)
- Aggiungere `mode == 'outreach_whatsapp'` al dispatcher
- Update `AgentMode` Literal in `models.py`

### Step 7 — Settings UI (~2h)
- Nuova pagina `/settings/whatsapp` con due pannelli (Motore A, Motore B)
- Form per nuovo account WA browser (con QR code modal)
- Form per nuova API config
- Test connection per entrambi

### Step 8 — Task form `outreach_whatsapp` (~1h)
- Nuovo `agent_mode` nel dropdown
- Step 4 (Pipeline I/O) del wizard mostra: template messaggio, engine preference,
  account WA dropdown, API config dropdown, template dropdown, rate-limit, dry-run
- Caveat banner

### Step 9 — Test E2E manuale (~1h)
- Lancia task con 2 contatti di test (numeri reali tuoi)
- Verifica: messaggio ricevuto, log in DB, status='contacted'

### Step 10 — Documentazione (~1h)
- GUIDA.md §3.6.2 `outreach_whatsapp` (cosa fa, quando usarlo, caveat)
- README.md sezione "WhatsApp setup" con QR-login walk-through
- `.env.example` aggiornato

**Totale stimato Fase 1: ~13 ore di sviluppo**

---

## 12. Cose esplicitamente FUORI scope di Fase 1

- **Inbound da Motore A**: leggere reply DOM polling su web.whatsapp.com
  → Fase 3 (rischio ban aumenta con sessione persistente lungo termine)
- **Inbound da Motore B via webhook Meta**: richiede tunnel pubblico HTTPS
  (ngrok/Cloudflare Tunnel) → Fase 2
- **Template editor / approval flow**: gestione su Meta Business Manager
- **WhatsApp Business app (non API)**: NON supportato
- **Gruppi WhatsApp**: solo DM 1-a-1
- **Media (immagini, audio, doc)**: solo testo. Media in Fase 2.
- **Multi-device sync issues**: WhatsApp ha bug noti con multi-device,
  AgentScraper assume un solo telefono pairing per account
- **Verifica numero esistente prima dell'invio**: WhatsApp non espone API
  pubblica per "is this number on WhatsApp?". Il check lo fa il browser
  navigando a `wa.me/<num>` e leggendo l'errore (Motore A) o tentando
  l'invio e gestendo error 131026 (Motore B).

---

## 13. Test plan per Fase 1

### Test funzionali
- [ ] Creazione account WA browser via Settings (QR-login funzionante)
- [ ] Invio DM singolo via Motore A a numero di test → ricevuto
- [ ] Invio DM singolo via Motore B a numero di test (con opt-in) → ricevuto
- [ ] Personalizzazione LLM funziona per 5 contatti diversi
- [ ] Rate-limit rispettato: 30 msg/ora non sforato
- [ ] Account rotation: con 2 account, alternanza round-robin osservabile in `social_dm_log`
- [ ] Dry-run: nessun invio reale, log con `ok=True, reason='dry_run'`
- [ ] Stop hard durante esecuzione: job termina entro 5s, contatti non processati saltati

### Test robustezza
- [ ] Numero invalido: log con `ok=False, reason='invalid_number'`, contatto NON marcato contacted
- [ ] Sessione WA scaduta a metà run: detection + skip account + log warning
- [ ] Meta API rate-limit (429): backoff esponenziale, retry max 3
- [ ] Stop in mezzo a un batch LLM rephrase: cleanup graceful

### Test sicurezza
- [ ] `AGENTSCRAPER_SECRET` mancante in `.env`: errore esplicito all'avvio
- [ ] Access_token Meta non leggibile in chiaro dal DB
- [ ] Session_dir Playwright in `.gitignore`

---

## 14. Decisioni minori già assunte (non sono blocking ma le elenco per trasparenza)

1. **Rate-limit Motore A**: 30 msg/ora, pause 30-180s, daily cap 100/account.
   Modificabili sia da env vars sia per-task.
2. **Pool rotation**: round-robin tra `status='active'`. Esclusione automatica
   di account con `status='banned'` o `rate_limited'`.
3. **Cifratura access_token**: stesso `AGENTSCRAPER_SECRET` di `outreach_social`.
4. **Sessione QR**: persistente in `data/whatsapp_sessions/<account_uuid>/`.
   Cancellata alla disattivazione dell'account da Settings.
5. **Contatti senza WhatsApp**: saltati silenziosamente con log
   `"⏭️ N righe scartate (no whatsapp number)"`.
6. **Opt-out detection**: keyword "STOP", "rimuovimi", "unsubscribe" nei
   messaggi inbound → `whatsapp_consent='optedout'`. Implementato in Fase 2
   (inbound).
7. **Compatibilità con `responder`**: se Fase 2 (inbound) è attiva,
   `responder` può gestire anche WhatsApp.

---

## 15. Domande aperte per l'utente

Prima di iniziare la Fase 1, vorrei conferma su:

1. **Tabella DB**: preferisci `whatsapp_accounts` separata o `social_accounts` esteso?
   Il piano propone **estensione** (riuso) ma posso fare tabella separata se ti
   sembra più pulito.

2. **Pagina Settings**: tab dentro `/settings` esistente o pagina dedicata
   `/settings/whatsapp`? Il piano propone **pagina dedicata** per separare il
   warning bene.

3. **QR-login flow**: server-side via Playwright headed (il browser si apre
   sul TUO desktop e scansioni il QR direttamente) — OK? Alternativa più
   complessa: screenshot del QR e mostrarlo nella web UI per scansione da
   un altro dispositivo.

4. **Fase 1 include Motore B completo o solo skeleton**?
   - Skeleton: tabella DB + UI Settings + stub `send_template` raise NotImplementedError
   - Completo: implementazione `send_template` + `send_text` funzionante
   Il piano propone **Motore B completo** (è solo HTTP, è un altro paio di ore).
   Ma richiede che tu abbia un account Meta Business già configurato.

5. **Tempistiche**: vuoi che procedo subito con Step 1-2 (DB) appena confermi,
   o preferisci aspettare di aver risposto alle domande sopra prima di toccare codice?

---

**Quando hai risposto a queste 5 domande, procedo con l'implementazione step by step.**
