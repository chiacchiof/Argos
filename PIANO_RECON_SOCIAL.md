# Piano implementativo — `recon_social` (R1 + R2 + R3)

> **Scopo**: definire architettura, schema dati, tool whitelist, safety guards
> e sequenza step-by-step PRIMA di scrivere codice. Da rivedere e approvare
> insieme all'utente.
>
> **Stato**: bozza per revisione — nessun codice ancora scritto.
> **Effort totale stimato**: ~22-26h di sviluppo reale.

---

## 1. Contesto e scelte già fatte

Aggiungere a AgentScraper la modalità `recon_social` per **esplorare social
network con un account loggato** (Facebook, Instagram, TikTok) e:

- **R1 — URL-driven**: dato un set di URL profilo, navigali con sessione loggata,
  estrai schema (incluso contenuto gated dietro login) → `profiles.jsonl`.
- **R2 — Goal-driven (exploration)**: dato il MIO profilo + obiettivo NL
  ("trova amici interessati al sushi"), l'agente esplora la mia rete in
  modo **non invasivo** (no like/commenti/DM/friend-add) e produce un report
  classificato.
- **R3 — Multi-session resilient**: task long-running su giorni, con
  rate-limit aggressivo, pause anti-detection, checkpoint + resume, pool
  account rotation.

**Vincoli imprescindibili**:

1. **READ-ONLY su social**: l'agente NON deve mai cliccare like/commenta/share/DM/
   add-friend/follow. Action blacklist hard validata pre-click.
2. **Riuso infrastruttura `app/agent/social/`**: sessione loggata, humanize,
   stealth (patchright), account pool (con rotation in R3).
3. **Sessione persistente WA-style**: per FB/IG/TikTok usiamo
   `launch_persistent_context(user_data_dir)` invece di `new_context(storage_state)`
   per IndexedDB completo (alcuni dati FB sono in IndexedDB).

### Caveat etici/legali (in chiaro nell'UI)

⚠️ **Profilazione automatica della propria rete social** è zona grigia:
- **GDPR / ePrivacy**: anche se i contenuti sono "tuoi amici", classificare
  automaticamente per gusti/interessi è trattamento di dati personali. Se
  poi usi quei dati per outreach commerciale → serve base giuridica.
- **ToS Meta/TikTok**: tutte e 3 le piattaforme vietano automation. Rischio
  ban dell'account, perdita rete personale.
- **Etica**: stai osservando amici reali in modo che non hanno autorizzato.

AgentScraper fornisce solo lo strumento; la conformità legale e l'etica
sono responsabilità dell'utente. Disclaimer prominente in `/settings/social/accounts`
+ alert ad ogni run di `recon_social`.

---

## 2. Tre fasi incrementali

### R1 — URL-driven recon (~5-6h)

**Cosa**: nuovo `agent_mode = recon_social`. Input: lista URL + schema.
Per ogni URL:
1. Apri browser con sessione loggata (riusa `social_accounts`)
2. `goto_profile(url)` → naviga + aspetta DOM stabile (scroll lieve per lazy-load)
3. Extract testo principale, post recenti, bio, pagine like, gruppi visibili
4. LLM riempi schema utente → append `profiles.jsonl`
5. Pausa anti-bot (30-180s random) + next URL

**Quando usarlo**:
- Ho URL profili FB/IG che voglio leggere col mio login (vedere post non
  pubblici di amici, contenuto gated)
- Build report per N profili noti

**Output**: `profiles.jsonl` (uno per URL) + `report.md` + ingest in `contacts`
se lo schema include email/telegram/whatsapp.

**Non fa**: niente esplorazione autonoma, niente decisioni LLM su dove andare.

### R2 — Goal-driven exploration (~12-15h)

**Cosa**: nuovo `agent_mode = recon_social` con `recon_mode='exploration'`.
Input: obiettivo NL + il MIO profilo (account social loggato). L'agente
naviga la mia rete in autonomia ma controllata.

**Tool whitelist (read-only)** disponibili all'LLM:

| Tool | Cosa fa | Platform |
|---|---|---|
| `list_my_friends(limit, cursor)` | Pagina la lista amici/followers/following del mio profilo | FB, IG, TikTok |
| `list_my_groups()` | Gruppi/community a cui appartengo | FB |
| `goto_profile(url)` | Apre il profilo di un utente | tutti |
| `read_recent_posts(N)` | Estrae i primi N post visibili (testo + meta) | tutti |
| `read_liked_pages()` | Pagine "mi piace" del profilo aperto (se visibili) | FB |
| `read_joined_groups()` | Gruppi pubblici del profilo aperto | FB |
| `read_bio()` | Bio + info anagrafiche del profilo aperto | tutti |
| `search_my_feed(query, max_scroll)` | Cerca nel mio feed personale | FB, IG |
| `classify_target(profile_data, hypothesis)` | LLM mini-call: 0-10 score per ipotesi ("interessa il sushi") | tutti |
| `save_to_report(target_id, fields)` | Append a `recon_report.jsonl` | tutti |
| `done(summary)` | Termina la run con riepilogo | tutti |

**Action blacklist hard** (Playwright wrapper che valida ogni click):

```python
BLACKLIST_SELECTORS = [
    # FB
    '[aria-label*="Like"]', '[aria-label*="Mi piace"]',
    '[aria-label*="Commenta"]', '[aria-label*="Comment"]',
    '[aria-label*="Condividi"]', '[aria-label*="Share"]',
    '[aria-label*="Aggiungi"]', '[aria-label*="Add friend"]',
    '[aria-label*="Messaggio"]', '[aria-label*="Message"]',
    '[aria-label*="Segui"]', '[aria-label*="Follow"]',
    # IG
    'button:has-text("Segui")', 'button:has-text("Follow")',
    'svg[aria-label="Like"]', 'svg[aria-label="Comment"]',
    # TikTok
    'button:has-text("Following")', 'button[data-e2e="follow-button"]',
    # Generici
    'button:has-text("Send")', 'button:has-text("Invia")',
]

class SafeBrowser:
    """Wrapper Playwright che valida ogni click contro la blacklist."""
    async def safe_click(page, selector):
        for blocked in BLACKLIST_SELECTORS:
            if matches(selector, blocked) or page.locator(blocked).is_visible():
                if locator_overlaps(selector, blocked):
                    raise BlockedAction(f"Click bloccato: {blocked}")
        await page.click(selector)
```

**Flusso ReAct**:
```
goal: "trova fra i miei amici FB chi è interessato al sushi"

1. Agente: list_my_friends(limit=50) → [URL1, URL2, ..., URL50]
2. Per ogni URL (rate-limit 2-3 profili/min):
   2.1. Agente: goto_profile(URL)
   2.2. Agente: read_bio() → bio text
   2.3. Agente: read_recent_posts(20) → [post1, …, post20]
   2.4. Agente: read_liked_pages() → [page1, …]  (se visibili)
   2.5. Agente: classify_target({bio, posts, pages}, "sushi/cucina giapponese")
        → score 0-10 + reason
   2.6. Se score >= 6: save_to_report(target_id, {profile, score, reason, evidence})
3. After N profiles: done(summary="raccolti N target, di cui M score>=6")
```

**Stop conditions**: max_iterations (LLM steps), max profiles visited, time
budget, ban detection (FB challenge page rilevata).

**Output**: `recon_report.jsonl` (un record per target classificato) +
`recon_report.md` (top-K con score+evidence).

### R3 — Multi-session resilient (~5h)

**Cosa**: il task `recon_social` può durare giorni. Aggiungiamo:

1. **Checkpoint state**: ogni N step salva stato in tabella `recon_checkpoints`
   (visited URLs, frontier, processed targets, current account_id).
2. **Resume**: al rilancio (manuale o cron), legge l'ultimo checkpoint e
   riprende.
3. **Multi-account rotation**: usa N account social del pool per distribuire
   il carico (max 50 profili/giorno/account su FB; ban-safe).
4. **Pause anti-detection programmate**: tra sessioni di 30-60 min, pausa
   2-6 ore. Tra giornate, max 200-300 profili/account/giorno.
5. **Detection automatica ban/challenge**: se l'agente becca un checkpoint
   FB (pagina "verifica identità"), termina sessione, marca account
   `quarantine`, ruota a quello successivo.

**Tabelle DB nuove**:
- `recon_runs` (id, task_id, started_at, status, target_count, last_checkpoint_at)
- `recon_checkpoints` (id, run_id, snapshot_json, created_at)
- `recon_visited` (run_id, target_url, visited_at, score, classified) → dedup

---

## 3. Architettura tecnica condivisa

### 3.1 Riuso esistente

| Componente | Riuso da |
|---|---|
| Login social + session persistente | `app/agent/social/whatsapp_browser.py` (pattern già fatto per WA), esteso a IG/TikTok/FB |
| Pool account + rotation | `app/agent/social/account_pool.py` |
| Humanize (typing, delay, scroll) | `app/agent/social/humanize.py` |
| Stealth (patchright) | `app/agent/social/engine.py` |
| Tool dispatcher ReAct (per R2) | Riusa pattern da `app/agent/runner_site_explorer.py` (già fa ReAct controllato) |
| LLM provider | `app/agent/llm_providers.py` |

### 3.2 Codice nuovo

```
app/agent/social/
  ├─ facebook_recon.py      Selettori + tool implementations per FB
  ├─ instagram_recon.py     Selettori + tool implementations per IG
  ├─ tiktok_recon.py        Selettori + tool implementations per TikTok
  ├─ safe_browser.py        Wrapper Playwright con blacklist action validation
  └─ recon_tools.py         Tool registry comune (list_my_friends, goto_profile, ecc.)
                            con dispatch platform-specific.

app/agent/
  ├─ runner_recon_social.py  Main runner. Stati: 'url_driven' (R1) e 'exploration' (R2).
  └─ recon_checkpoint.py     Logica checkpoint/resume (R3).
```

### 3.3 Schema DB

```sql
-- Run state per checkpoint/resume (R3)
CREATE TABLE IF NOT EXISTS recon_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  social_account_id INTEGER REFERENCES social_accounts(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'running',
    -- running | paused | done | error | quarantined
  started_at TEXT NOT NULL,
  last_active_at TEXT,
  finished_at TEXT,
  target_count INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS recon_checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES recon_runs(id) ON DELETE CASCADE,
  snapshot_json TEXT NOT NULL,
    -- contiene: frontier URLs, visited, processed targets, current step LLM, time spent
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recon_visited (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES recon_runs(id) ON DELETE CASCADE,
  target_url TEXT NOT NULL,
  target_platform TEXT NOT NULL,
  visited_at TEXT NOT NULL,
  classified INTEGER NOT NULL DEFAULT 0,
  score INTEGER,
  reason TEXT,
  UNIQUE(run_id, target_url)
);
CREATE INDEX IF NOT EXISTS idx_recon_visited_run ON recon_visited(run_id, visited_at);

-- Estensione tasks
ALTER TABLE tasks ADD COLUMN recon_mode TEXT;
  -- 'url_driven' (R1) | 'exploration' (R2)
ALTER TABLE tasks ADD COLUMN recon_social_account_id INTEGER
  REFERENCES social_accounts(id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN recon_hypothesis TEXT;
  -- per R2: la "domanda" ("trova chi ama il calcio")
ALTER TABLE tasks ADD COLUMN recon_max_targets_per_day INTEGER NOT NULL DEFAULT 50;
ALTER TABLE tasks ADD COLUMN recon_score_threshold INTEGER NOT NULL DEFAULT 6;
  -- score min per finire in report
```

---

## 4. Safety guards (R2 critico)

### 4.1 `SafeBrowser` wrapper

Ogni `page.click(...)` viene intercettato:

```python
async def safe_click(page: Page, selector: str | Locator):
    # 1. Verifica blacklist
    for blocked in BLACKLIST_SELECTORS:
        try:
            loc = page.locator(blocked).first
            if await loc.is_visible(timeout=200):
                bbox_blocked = await loc.bounding_box()
                bbox_target = await get_bbox(page, selector)
                if bboxes_overlap(bbox_blocked, bbox_target):
                    log_audit("BLOCKED_CLICK", selector, blocked)
                    raise BlockedActionError(f"Click bloccato (overlap con {blocked})")
        except (TimeoutError, AttributeError):
            continue
    # 2. Audit log (sempre)
    log_audit("CLICK", selector)
    await page.click(selector)
```

### 4.2 Audit log

Ogni step LLM + ogni interazione DOM finisce in:
- `recon_audit_log.jsonl` (file della run): step LLM, tool chiamato, selettori
  cliccati/blacklisted, screenshot riferimento
- `data/results/<task_id>/<ts>/screenshots/` (1 screenshot ogni 5 step LLM)

L'utente può rivedere TUTTO ex-post: "esattamente cosa ha fatto l'agente
sul mio account?".

### 4.3 Kill switch globale

Env var `RECON_SOCIAL_DISABLED=1` → ogni task `recon_social` rifiuta di
partire. Per emergenze.

### 4.4 Tool whitelist hardcoded

L'LLM riceve la lista tool come parte del system prompt. Anche se "hallucina"
un nome di tool non in whitelist, il dispatcher rifiuta con errore esplicito
e il loop ReAct continua senza eseguire azioni invasive.

---

## 5. Configurazione utente nel task form

Nuovo Step nel wizard task: **"🔍 Recon social"**. Visible solo se
`agent_mode == 'recon_social'`.

```
🔍 Recon social
  Modalità: ● url_driven  ○ exploration
  Account social: [▼ dropdown N account loggati]
  Obiettivo / ipotesi (solo R2): [textarea]
  Max profili/giorno/account: [50]
  Score minimo per report: [6]
  ☐ Disable kill-switch (advanced, sconsigliato)
```

**Per R1 (url_driven)**: seed = lista URL profili.
**Per R2 (exploration)**: niente seed, l'agente parte dal proprio profilo.

---

## 6. Output utente

### Per ogni run

```
data/results/<task_id>/<ts>/
  ├─ recon_report.jsonl    Un record per target classificato
  ├─ recon_report.md       Top-K markdown ordinato per score
  ├─ recon_audit_log.jsonl Audit di TUTTE le azioni DOM
  └─ screenshots/          Screenshot ogni 5 step LLM
```

### Esempio `recon_report.md` (R2)

```markdown
# Recon FB — interessati al sushi

Obiettivo: "trova fra i miei amici FB chi è probabilmente interessato al sushi"

Profili scansionati: 187 (3 sessioni in 5 giorni)
Profili sopra soglia (≥6/10): 12

| Rank | Profile | Score | Evidence |
|---|---|---|---|
| 1 | https://fb.com/marco.rossi | 9 | 4 post recenti su ristoranti giapponesi, like a pagina "Sushi Milano" |
| 2 | https://fb.com/giulia.bianchi | 8 | check-in al ristorante "Iyo" 2 volte, foto sashimi |
| ... | ... | ... | ... |
```

---

## 7. Sequenza di implementazione

### Fase 1 — R1 (URL-driven recon) ~5-6h

1. **DB migrations** (~30m): `tasks.recon_mode`, `recon_social_account_id`, `recon_max_targets_per_day`, `recon_score_threshold`
2. **`safe_browser.py`** (~1h): wrapper Playwright + blacklist (anche se R1 ha bisogno minimo, lo creiamo già)
3. **Recon platform modules** (~2h): `facebook_recon.py`, `instagram_recon.py`, `tiktok_recon.py` con selettori + funzione `extract_profile_data(page)` per ognuno
4. **`runner_recon_social.py`** (~1.5h): mode `url_driven`: loop su seed URLs, fetch+extract+LLM schema, profiles.jsonl
5. **AgentMode Literal + dispatcher jobs.py** (~10m)
6. **UI task form** (~30m): dropdown account, recon_mode select, hint
7. **Smoke test** (~30m): 3-5 URL FB con account loggato → report

### Fase 2 — R2 (Exploration goal-driven) ~12-15h

8. **`recon_tools.py`** (~2h): registry tools con dispatch platform-specific
9. **Tool implementations FB** (~3h): `list_my_friends`, `read_recent_posts`, `read_liked_pages`, `read_joined_groups`, ecc.
10. **Tool implementations IG** (~2h): `list_my_followers`, `read_posts`, `read_bio`
11. **Tool implementations TikTok** (~1.5h): `list_my_following`, `read_videos`, `read_bio`
12. **ReAct loop** in runner (~2h): system prompt + tool dispatcher + LLM call loop
13. **Audit log + screenshots** (~1h): scrittura jsonl + cattura periodica
14. **Safety guards completi** (~1.5h): blacklist completa, kill-switch env, validator pre-click
15. **UI exploration mode** (~30m): textarea hypothesis, hint platform-specific

### Fase 3 — R3 (Multi-session) ~5h

16. **DB tables `recon_runs/checkpoints/visited`** (~30m)
17. **Checkpoint logic** in runner (~2h): ogni N step salva snapshot
18. **Resume da checkpoint** (~1.5h): comando "▶ Riprendi" sul job error/paused
19. **Multi-account rotation** (~1h): se account corrente raggiunge daily_cap, ruota

---

## 8. Fuori scope (futuro)

- **Twitter/X recon**: API V2 a pagamento, automation browser severamente
  detectata. Non implementiamo per ora.
- **LinkedIn recon**: ToS LinkedIn molto restrittivo + ban veloce. Skip.
- **Recon su WhatsApp**: WA è messaging, niente "profili" navigabili. N/A.
- **Network graph analysis**: dati "chi conosce chi" potenzialmente utili
  ma fuori scope (Fase 4).

---

## 9. Test plan

### R1
- [ ] FB: 5 URL profili amici loggato → estratto bio + ultimi 3 post per ognuno
- [ ] IG: 5 URL profili pubblici (no login required) → estratto username + bio + N post
- [ ] TikTok: 5 URL utenti → estratto username + bio + 3 video titoli
- [ ] Account banned: errore esplicito, niente fallback silenzioso

### R2
- [ ] FB: "trova amici che amano il calcio" — 100 profili, top-10 con evidence
- [ ] Safety: durante la run, dump dei click validati vs blacklist (deve avere 0 click blacklist tentati)
- [ ] Audit log completo e leggibile
- [ ] Screenshots ogni 5 step

### R3
- [ ] Run da 300 target su 2 account: distribuiti round-robin, max 50/account/giorno
- [ ] Kill processo a metà run → restart → resume da ultimo checkpoint
- [ ] Detection challenge FB → account marcato quarantine → continua sull'altro account
- [ ] Kill-switch env: `RECON_SOCIAL_DISABLED=1` → task partono e si chiudono subito con errore

---

## 10. Decisioni minori assunte (non bloccanti)

1. **Scope iniziale platform**: FB, IG, TikTok. WhatsApp escluso (è messaging).
2. **Pause anti-detection**: 30-180s tra profili, 2-6h tra sessioni, max 200 profili/giorno/account FB.
3. **LLM cost**: R2 costoso (~$0.10-0.50 per run di 100 profili con gpt-4o-mini per classify). Usabile anche con Ollama gpt-oss:20b se l'utente preferisce gratis.
4. **Storage**: gli screenshot occupano spazio. Default: 1 ogni 5 step LLM + 1 al goto_profile. Configurabile.
5. **Privacy**: il `recon_report.jsonl` contiene dati personali dei contatti. Auto-cleanup dopo 30 giorni opzionale (env `RECON_REPORT_TTL_DAYS=30`).

---

## 11. Domande aperte per l'utente

1. **Priorità di partenza**: vuoi che inizi da R1 (FB+IG+TikTok in una passata) o preferisci R1 solo FB prima e poi le altre? Single-platform è più focalizzato e meno rischio bug.

2. **Cost vs gratis**: per il classifier LLM (R2 `classify_target`) preferisci default Ollama gpt-oss:20b (gratis ma più lento) o gpt-4o-mini (~$0.20 per run di 100 profili, più affidabile)?

3. **Audit log su filesystem**: OK occuparci di ~10-50 MB per run (screenshots + audit jsonl)?

4. **GDPR**: hai consenso esplicito dei tuoi amici per profilare? Se NO il rischio legale esiste ma l'app fornisce solo lo strumento. Confermi che procediamo a tuo rischio?

5. **Quando iniziare a codare**: dopo che hai letto il piano, mi dici "ok parti con Fase 1" e procedo. Oppure preferisci aggiustare il piano prima.

---

**Quando hai risposto, procedo con Fase 1 (R1 - URL-driven recon, ~5-6h).**
