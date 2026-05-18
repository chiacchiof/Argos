# Backlog AgentScraper

Feature da fare, ordinate per **valore × fattibilità**. Per ogni voce: cosa fa,
perché serve, stima sforzo, design ad alto livello.

---

## 🔝 P0 — Sblocca casi d'uso critici

### B-001 · Chat con LLM-in-running (human-in-the-loop su task attivo)

**Cosa**: durante l'esecuzione di un job (es. `outreach_whatsapp`, `browser_use`,
`auto_extract`), avere una **chat laterale per job_id** dove l'utente può:
- correggere informazioni live ("il numero di Chanel è sbagliato, usa +39...")
- iniettare suggerimenti per i prossimi step ("salta il prossimo contatto",
  "aggiungi 'PS: ci aggiorniamo lunedì' al template")
- chiedere status ("a che punto sei?", "perché hai scelto quel pattern URL?")

L'LLM legge i messaggi PRIMA del prossimo step deciso (DM successivo, fetch
URL, step browser_use) e li applica come *soft suggestions* o *hard overrides*
in base al tono.

**Perché sensato**:
- Oggi se sbagli un dato (esempio reale: numero WA errato, job#110) devi
  killare → correggere → rilanciare. Spreco completo del lavoro fatto.
- Permette di "guidare" un agente in modo molto più naturale di task pre-config.
- Pattern già visto in AutoGen, Claude Code stesso, Cursor agent mode.

**Design (high-level)**:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Tabella `job_chat_messages` (NUOVA)                                 │
│  id, job_id, direction (user|assistant), body, applied (bool),       │
│  created_at                                                          │
└──────────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Runner — checkpoint hooks                                           │
│  Prima di ogni "step rilevante" (= DM successivo, fetch URL,         │
│  decisione LLM importante) il runner chiama:                         │
│     suggestions = consume_pending_chat(job_id)                       │
│  Se c'è qualcosa, lo include nel prompt LLM come                     │
│  "ISTRUZIONI UTENTE LIVE (priorità su tutto)".                       │
│  Per i comandi deterministici (vedi sotto) li applica direttamente.  │
└──────────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  UI — pannello chat per job dashboard                                │
│  /jobs/<id>/chat (HTMX, auto-poll 3s).                               │
│  Textbox + lista messaggi. Ogni messaggio user marca `applied=False` │
│  finché il runner non lo legge → poi diventa `applied=True` con      │
│  reply assistant ("ho applicato: <descrizione>").                    │
└──────────────────────────────────────────────────────────────────────┘
```

**Due modalità di interpretazione del messaggio**:

1. **Comando strutturato** (parser deterministico, no LLM):
   - `/skip` → salta il prossimo target
   - `/set whatsapp 4155 +39...` → update contact 4155.whatsapp, ricarica dal runner
   - `/pause` / `/resume` / `/stop`
   - `/note <testo>` → annota nel log del job
   - `/use_template <id>` → cambia il template message in corsa
   - Pro: deterministico, veloce, sicuro
   - Contro: l'utente deve imparare la sintassi

2. **Linguaggio naturale** (LLM-parsed):
   - "il numero di chanel è 333..." → propone `/set whatsapp <id> +39333...`
     con bottone conferma
   - "rallenta un po', stai andando troppo veloce" → propone update di
     `bulk_rate_limit_per_sec` o pause min/max
   - Pro: naturale per l'utente
   - Contro: ambiguità, serve sempre conferma esplicita prima di applicare

**Sfide tecniche**:
- Cross-thread comm: runner gira in proactor thread (Windows), chat
  message arriva via HTTP. Soluzione: queue su DB con `applied` flag,
  runner polla in async loop ogni N step.
- Punti di interruzione: ogni runner deve definire i suoi checkpoint
  espliciti. Per `outreach_whatsapp` → tra un DM e l'altro. Per
  `browser_use` → tra step LLM. Per `bulk_extract` → tra URL processate.
- Stato corrente esposto al chat: il messaggio user deve poter leggere
  "stai per inviare DM a contact #4155 (Chanel, +39…)" così l'utente
  può decidere se intervenire. Soluzione: ogni job esporre `current_step`
  via DB.

**Effort stimato**: **~8-12h** (DB + 2-3 runners modificati + chat UI + JS
auto-poll + safety guard sui comandi destructive).

**Dipendenze**: nessuna. Indipendente dal resto del backlog.

**Test E2E target**:
- Lanci un task `outreach_whatsapp` con 5 DM. Dopo il 2°, scrivi nella chat
  "salta il 3° contatto, mandalo a una mail invece". Il runner skippa il 3°,
  marca contact.status='skipped_manual'. Continua con 4-5.

---

## 🚀 P1 — Quality of life

### B-013 / B-014 / B-015 · `recon_social` (R1 + R2 + R3) × IG + TikTok + FB

**Cosa**: nuovo agent_mode `recon_social` per esplorare social loggato in
3 fasi incrementali. Vedi piano dettagliato in [PIANO_RECON_SOCIAL.md](PIANO_RECON_SOCIAL.md).

| Sub | Cosa | Effort |
|---|---|---|
| **B-013 (R1)** | URL-driven recon: lista URL → extract con session loggata | 5-6h |
| **B-014 (R2)** | Exploration goal-driven: agente ReAct + tool whitelist + safety guards (blacklist click su like/commenta/DM/follow) | 12-15h |
| **B-015 (R3)** | Multi-session resilient: checkpoint, resume, pool account rotation, kill-switch | 5h |

**Decisione utente (2026-05-13)**: vuole tutti e tre, su tutte e 3 le
piattaforme target (IG + TikTok + FB; WA escluso = messaging).

**Caveat**: GDPR/ePrivacy + ToS Meta/TikTok = zona grigia. Disclaimer
prominente nell'UI obbligatorio.

**Totale**: 22-26h di sviluppo + design completo già scritto in piano.

---

### B-012 · Sender single-select uniforme per TUTTI gli outreach mode

**Cosa**: estendere il pattern di WhatsApp (single-select sender per task,
fail-fast su sender non-active) a tutti gli altri outreach mode.

**Decisione utente (2026-05-13)**: il task DEVE poter scegliere ESPLICITAMENTE
quale sender usare. Pool default come opzione (`NULL`), ma valore singolo
quando l'utente lo decide.

| Mode | Sender oggi | Cambia in |
|---|---|---|
| `outreach` (email) | `channel_config.email` singleton | Multi-account email → tabella `email_accounts` simile a `social_accounts` + FK `task.email_account_id` |
| `outreach` (telegram) | `channel_config.telegram` singleton (1 bot) | Multi-bot → tabella `telegram_bots` + FK `task.telegram_bot_id` |
| `outreach_social` | Pool TUTTI `social_accounts` con `platform=X, status=active` | FK `task.social_account_id` (single) + fail-fast se banned |
| `outreach_whatsapp` | ✅ già fatto (B-???) | — |

**Effort**:
- Email multi-account: ~3h (nuova tabella, SMTP config per-account, route Settings dedicata, runner filtro)
- Telegram multi-bot: ~2h (nuova tabella, route Settings, runner filtro)
- outreach_social FK + fail-fast: ~1.5h (FK su tasks, runner filtro, UI dropdown nel form)

Totale: ~6.5h se fatti tutti insieme.

**Dipendenze**: nessuna. Indipendente.

**Test target**:
- Crei 2 account email (acquisti@ + info@), task A usa "acquisti@", task B usa "info@", verifica che ogni task usi il sender giusto.
- Idem per telegram con 2 bot e per IG con 2 account.

---

### B-016 · Asset deduplication cross-task + UI merge conflitti (per-tenant) — ✅ CHIUSO (2026-05-18)

**Cosa**: oggi più task di scraping che scoprono lo stesso soggetto reale (es. la stessa persona/azienda con stesso numero WA ma URL/asset_type diversi) creano N record duplicati in `assets`. Il dedup attuale e' solo su `(source_url_canonical, asset_type)` dentro `upsert_asset` — non cattura match cross-canale.

**Proposta utente (2026-05-17)**: introdurre dedup basato su CHIAVI MULTIPLE con UI di gestione conflitti/merge. Asset univoci per tenant.

**Chiavi di match candidate** (normalizzate prima del confronto):

| Chiave | Normalizzazione | Forza signal |
|---|---|---|
| `source_url_canonical` | rimuovi tracking params, lowercase host, no trailing `/` (già esistente) | alta |
| `whatsapp` / `phone` | E.164 (`+39 333 1234567` → `+393331234567`) | **molto alta** (univoca per persona) |
| `email` | lowercase, strip dots Gmail | **molto alta** |
| `telegram_username` / `telegram_chat_id` | strip `@`, lowercase | molto alta |
| `social_json[platform=ig].url` → handle | extract `@handle` da `instagram.com/<handle>/` | alta |
| idem per `tiktok`, `facebook`, `onlyfans` | extract username dal path | alta |
| `title` + `source_domain` | normalize-trim + edit distance | bassa (solo come hint) |

Una coppia di asset e' "duplicate candidate" se condivide **>=1 chiave forte** (whatsapp/email/telegram/social handle) **OPPURE** **>=2 chiavi medie** (url canonico stesso path + title fuzzy match).

**Modalità di gestione** (decidere quale combinare):
1. **Auto-merge sicuro**: se match e' su chiave forte univoca (E.164 phone uguale, email uguale), merge automatico — basso rischio falsi positivi.
2. **Flag per review**: se match e' su chiavi medie o forti ma con discordanze su altri campi, mettere il nuovo asset in una coda `pending_merge`.
3. **Skip silenzioso**: il runner di scraping che trova un dup salta l'INSERT, logga in `dedup_log` per audit.

**Schema proposto**:
```
ALTER TABLE assets ADD COLUMN dedup_status TEXT;  -- 'unique' | 'duplicate_pending' | 'merged_into:<id>'
ALTER TABLE assets ADD COLUMN dedup_canonical_id BIGINT REFERENCES assets(id);  -- punta al "primary" della cluster

CREATE TABLE asset_dedup_candidates (
  id BIGSERIAL PRIMARY KEY,
  tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
  primary_asset_id BIGINT REFERENCES assets(id) ON DELETE CASCADE,
  candidate_asset_id BIGINT REFERENCES assets(id) ON DELETE CASCADE,
  match_keys JSONB,  -- es. [{"key":"whatsapp","value":"+393294257497"}, {"key":"email","value":"a@b.com"}]
  match_score REAL,  -- 0.0-1.0 (weighted sum delle chiavi)
  status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'merged' | 'rejected' | 'ignored'
  detected_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX idx_dedup_tenant_status ON asset_dedup_candidates(tenant_id, status);
```

Indici per match veloce:
```
CREATE INDEX idx_assets_whatsapp_norm ON assets(tenant_id, whatsapp) WHERE whatsapp IS NOT NULL;
CREATE INDEX idx_assets_email_norm ON assets(tenant_id, LOWER(email)) WHERE email IS NOT NULL;
CREATE INDEX idx_assets_tg_norm ON assets(tenant_id, telegram_username) WHERE telegram_username IS NOT NULL;
```

**UI proposta — pagina `/assets/duplicates`** (per tenant):

```
┌───────────────────────────────────────────────────────────────────────┐
│ 🔀 Conflitti di duplicazione asset                  [42 da risolvere] │
├───────────────────────────────────────────────────────────────────────┤
│ Filtri: [match strength: forte|medio|tutti] [asset_type] [tenant]     │
├───────────────────────────────────────────────────────────────────────┤
│ ┌─ Cluster #1 — match su whatsapp + email ─────────────────────────┐  │
│ │ Primary: #6251 Francesco Russo · ig_profile · qualified           │  │
│ │   whatsapp +393294257497 · email francesco@edg.com                │  │
│ │   created 2026-05-10 · 4 tag                                      │  │
│ │ Candidate: #7322 F. Russo · profile_contacts · new                │  │
│ │   whatsapp +393294257497 · email francesco@edg.com                │  │
│ │   created 2026-05-17 · 1 tag                                      │  │
│ │ Match keys: whatsapp(strong), email(strong) → score 0.95          │  │
│ │ [Merge into #6251 ✓] [Mantieni separati] [Ignora]                 │  │
│ │   ↳ merge regole:                                                 │  │
│ │     ☑ Preferisci campi più recenti se vuoti nel primary           │  │
│ │     ☑ Union tag (no overwrite valori esistenti)                   │  │
│ │     ☑ Union social_json                                           │  │
│ │     ☑ Update target_asset_ids in tasks: 7322 → 6251               │  │
│ │     ☑ Update social_dm_log.target_asset_id: 7322 → 6251           │  │
│ │     ☐ Eredita asset_tags del candidato (con prefix `from_7322_`)  │  │
│ └───────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Detection flow**:
1. `upsert_asset` (e endpoint `/assets/new`): dopo INSERT, esegue `find_dedup_candidates(new_id)` async → popola `asset_dedup_candidates` per i match trovati.
2. Job cron (o on-demand `/assets/duplicates/rescan`): full-table scan per asset esistenti pre-feature.

**Merge action** (idempotente, in transaction):
- Picks `primary_id`. Sposta riferimenti FK: `social_dm_log.target_asset_id`, `tasks.target_asset_ids` (rewrite JSON list), `asset_tags.asset_id` (con dedup di `(asset_id, tag_key)`).
- Union `social_json`, `raw_json` campi vuoti.
- Mark `candidate.dedup_status = 'merged_into:<primary_id>'`. NON cancellare il record (audit + ricostruibilità).

**Effort**: ~12-16h (schema + detection + UI + merge action + test). Da fare in 2 PR:
1. PR1 (~5h): schema + detection logic + chiamata in `upsert_asset` (no UI). Trova i duplicati ma non li risolve.
2. PR2 (~10h): UI `/assets/duplicates` + merge action + redirect FK + test E2E.

**Aperti / domande architetturali**:
- **Normalizzazione phone**: `phonenumbers` lib (E.164) per parsing affidabile (gestisce +39, 0039, 39, locali italiani con prefisso 3...). Aggiunge dipendenza ma necessaria.
- **Gmail dots**: `f.russo+spam@gmail.com` == `frusso@gmail.com`? Gmail-only equivalence, opzionale.
- **Whatsapp vs phone**: tenere come 2 campi separati o unificare? Oggi sono già 2 colonne ma spesso contengono lo stesso valore. Proposta: chiave dedup considera entrambi (match se WA(A) == phone(B)).
- **Tenant isolation**: dedup SOLO intra-tenant. Asset di tenant diversi non vanno mai mergiati anche se uguali.
- **Falsi positivi**: come gestire un numero WhatsApp condiviso (centralino aziendale) che appare su 30 asset? Soluzione: chiavi `weight` configurabile, e numeri "blacklist dedup" (es. numeri di centralino noti) skippano il match.
- **Performance**: con 100k+ asset, full scan ogni nuovo insert e' costoso. Index su chiavi normalizzate + query incrementale (`LIMIT 50` candidati per match).

**Test target**:
- Insert 2 asset stesso `whatsapp` → 1 candidate row creata.
- Insert 2 asset con stesso `source_url_canonical` ma `asset_type` diverso → candidate row (cross-type ora possibile).
- Merge action: candidate viene mergiato, `target_asset_ids` di un task viene riscritto, `social_dm_log` history conservata.
- Rescan idempotente: rieseguito non duplica candidate rows.

---

### B-011 · Gap anti-ban configurabile per task — ✅ CHIUSO (2026-05-17)

**Cosa**: oggi `random_gap_between_dms_min()` ritorna 8-30 min (hard-coded
in `humanize.py`). Su WhatsApp un task da 5 DM aspetta 40-150 min totali =
inusabile per test. Soluzione provvisoria: cap 2 min su WA in `engine.py`.

Soluzione definitiva: campo `gap_between_dms_min/max` nel task (default
sensato per platform: IG/TikTok 8-30, WA 1-3, dry-run 0.1-0.5).
UI: nuovi 2 input nella sezione configurazione outreach.

**Implementato**: `tasks.gap_between_dms_min/max REAL NULL` (NULL = default
platform da `humanize.DEFAULT_GAP_RANGE_MIN`); WA default abbassato a
0.15-0.35 min (9-21s) — account reale loggato. UI in entrambe le sezioni
outreach (social + WA). Doc in [GUIDA.md §17.9](GUIDA.md#179). Vedi commit.

---

### B-002 · Mini-CLI per CRUD contatti (`/inbox/contacts`)

**Cosa**: casella testo nella pagina `/inbox/contacts` per comandi rapidi:
- `update 4155 whatsapp=+393331234567`
- `optout 4155`
- `qualify 4155 score=8`
- `bulk-optout 4155,4156,4157`

**Perché**: scrivere 4 caratteri batte 5 click per modifiche piccole.

**Effort**: ~30 min. Parser deterministico, no LLM.

**Dipendenze**: nessuna.

---

### B-003 · Chat-CRUD via LLM (orchestrator)

**Cosa**: chatti con l'orchestrator in linguaggio naturale per modifiche al
DB (contatti, task, workflow). LLM legge la richiesta, propone struttura,
tu confermi.

Esempio:
```
User:  "qualifica tutti i contatti con email Gmail come opt_in"
LLM:   "Trovo 23 contatti contacts.email LIKE '%@gmail.com'.
        Applico whatsapp_consent='opt_in' a tutti?
        [✓ Applica]  [✗ Annulla]  [👁 Vedi lista]"
User:  [Applica] → 23 record aggiornati.
```

**Effort**: ~3-4h. Estendere `/orchestrator` con tool registry, sempre con
confirmation flow.

**Dipendenze**: nessuna (può precedere B-001 come "lite version").

---

### B-004 · WhatsApp inbound (Fase 2 piano)

Vedi [PIANO_WHATSAPP.md §12](PIANO_WHATSAPP.md). Implementa la lettura
inbound + integrazione con `responder` (auto-reply LLM).

**Decisione utente (2026-05-13)**: preferenza per **Motore B (webhook Meta)**,
NON Motore A (DOM polling). Motivi: legale, scalabile, niente browser sempre
aperto. Richiede però tunnel HTTPS pubblico per ricevere POST da Meta
(ngrok / Cloudflare Tunnel).

**Effort**: ~6-8h.

---

### B-005 · Media WhatsApp (immagini/audio/PDF)

Estendere `outreach_whatsapp` per allegati. Motore A: upload via DOM. Motore B:
`messaging_product.media` API.

**Effort**: ~3h. Documentato in PIANO_WHATSAPP.md §12.

---

## 🛠 P2 — Manutenzione tecnica

### B-006 · Refactor `_run_in_proactor_thread`

Oggi è in `app/jobs.py`. È usato da 4+ runners. Spostarlo in modulo dedicato
(`app/runtime/proactor.py`) con error handling più solido (timeout globali,
ripristino su crash del thread, dump del traceback).

**Effort**: ~2h. Beneficio: dispatcher più affidabile.

### B-007 · Test integration completi

Oggi smoke test sono solo unit. Aggiungere E2E per:
- workflow A → B → C con artifact passing
- outreach_whatsapp dry-run con 5 contatti finti
- recovery dopo kill di un job

**Effort**: ~4h. Riduce regressioni.

### B-008 · Encryption at rest credenziali

Oggi `social_accounts.encrypted_password` e `whatsapp_api_config.encrypted_access_token`
sono cifrati con Fernet (AGENTSCRAPER_SECRET). Altri campi sensibili (LLM API keys
in `tasks`) NO. Standardizzare: tutti i secret in DB → Fernet.

**Effort**: ~2h.

---

## ❄ P3 — Esperimenti / nice-to-have

### B-009 · UI dashboard analytics per outreach

- Grafici: DM/giorno per engine (A vs B), tasso di replies, tempo medio risposta
- Filtri: per task, per periodo, per status contatto
- Export CSV

**Effort**: ~6h.

### B-010 · Multi-workflow scheduler avanzato

Oggi i workflow si triggerano on-demand o via cron. Aggiungere:
- conditional triggers ("se task A produce ≥50 profili, lancia workflow X")
- chaining di workflow (W1 → W2 → W3)

**Effort**: ~5h.

---

## Roadmap suggerita

```
Settimana 1:  B-001 (chat in-running)  ← TOP PRIORITY (richiesta utente)
Settimana 2:  B-002 (mini-CLI) + B-003 (chat-CRUD)
Settimana 3:  B-004 (WA inbound) + B-005 (media)
Backlog:      B-006…B-010 manutenzione + nice-to-have
```
