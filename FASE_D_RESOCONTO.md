# FASE D — Multi-platform follower_scrape (FB + TikTok)

> Lavoro fatto mentre dormivi. Test E2E reale **NON eseguito** (mi mancava la password di chifer81@hotmail.com).

## ✅ Cosa è stato fatto

### 1. Refactor runner ([runner_recon_social.py](app/agent/runner_recon_social.py))

**Prima**:
```python
if recon_mode == "follower_scrape":
    if account_platform != "instagram":
        error("solo IG supportato")  # HARD-BLOCK
    # Codice hardcoded ig_entry filter + instagram_recon.* chiamate dirette
```

**Adesso**:
- Dispatch dinamico via `_RECON_BY_PLATFORM` dict (IG/FB/TT)
- Verifica `hasattr(module, "enumerate_followers_of_target")` — se manca, error chiaro
- Estrazione handle dal `social_json` parametrica su `account_platform` (non più hardcoded "instagram")
- Helper extractor per la URL: prova `_ig_username_from_url`, `_tt_handle_from_url`, `_fb_handle_from_url`, `_normalize_fb_profile_url`
- Regex handle permissiva per FB (a-z 0-9 . _ -, max 80) vs IG/TT (a-z 0-9 . _, max 30)
- Log warning chiaro: `⚠️ follower_scrape su <plat>: implementazione BETA`

### 2. Facebook scraper ([facebook_recon.py](app/agent/social/facebook_recon.py))

Aggiunti **3 nuovi simboli**:

- `_fb_handle_from_url(url)`: estrae username o id numerico (per `profile.php?id=X`)
- `_fb_followers_url(target)`: costruisce l'URL della lista follower (username o numeric id)
- `enumerate_followers_of_target(page, safe, handle, *, cap, jlog, debug_dir)`: 
  - Naviga a `/<username>/followers` (o `profile.php?id=X&sk=followers`)
  - Detect pagine errore (privacy/inesistente/login required) via `_is_fb_error_page`
  - 4 selettori CSS multipli (fallback): `div[role="main"] a[role="link"]`, `div[aria-label*="ollower"]`, ecc.
  - Filtri: skip foto/posts/comment links, skip non-profile URLs, skip target stesso
  - Scroll loop fino al cap (max 8 scrolls, 3 stagnant rounds = stop)
  - Screenshot debug per troubleshooting selettori
  - Output: `[{handle, display_name, profile_url}, ...]`

**Limiti noti** (documentati nel docstring):
- Privacy "amici" → ritorna `[]`
- 2FA/checkpoint → goto fail (gestito)
- "People you may know" potrebbe contaminare → filtraggio per URL normalizzato

### 3. TikTok scraper ([tiktok_recon.py](app/agent/social/tiktok_recon.py))

Aggiunto **1 nuovo simbolo**:

- `enumerate_followers_of_target`: 
  - Naviga a `https://www.tiktok.com/@<handle>`
  - 5 selettori CSS per il link "Followers" (data-e2e prioritari)
  - Aspetta modale `div[role="dialog"]` o `data-e2e="follower-list"`
  - Scroll **dentro la modale** (TikTok ha virtual scroll: `container.scrollTop = container.scrollHeight`)
  - Filtri: `_tt_handle_from_url`, skip target stesso
  - Stessa interfaccia di FB/IG

**Limiti noti**:
- TikTok ha anti-bot aggressivo: captcha frequente
- Modale virtual scroll può essere flaky
- Selettori `data-e2e` cambiano spesso

### 4. UI ([task_form.html](app/templates/task_form.html))

Dropdown `recon_mode` aggiornato:
- "follower_scrape — per ogni account IG nel seed..." (vecchio testo IG-only)
- ➡️ "follower_scrape — scarica i follower di account target (**IG ✅** / **FB 🚧 beta** / **TikTok 🚧 beta**)"

Help text aggiornato:
- Era "solo Instagram, account loggato richiesto"
- ➡️ "Supporto attuale: Instagram (produzione), Facebook e TikTok (beta, selettori best-effort)"

### 5. Task di test creati nel DB

| Task ID | Nome | Platform | Social Account | Note |
|---|---|---|---|---|
| **#46** | `[TEST] Follower scrape FB - Paolo & Carlotta` | facebook | id=4 (chifer81@hotmail.com) | **PRONTO** per il test |
| **#47** | `[TEST] Follower scrape TikTok - Paolo & Carlotta` | tiktok | NESSUNO | Manca social_account_id TT |

Config comune:
- `agent_mode`: recon_social
- `recon_mode`: follower_scrape
- `model`: qwen3.5:latest (Ollama locale)
- `llm_provider`: ollama
- `recon_max_targets_per_day`: 20 (cap basso per test rapidi)
- `seed_queries`: `paolo maugeri\ncarlotta castoro`
- `output_asset_type`: `fb_follower_test` / `tt_follower_test`

### 6. Smoke test architetturale: **6/6 verde**

- ✅ 3 moduli social importati (`facebook_recon`, `instagram_recon`, `tiktok_recon`)
- ✅ `enumerate_followers_of_target` presente e async in tutti e 3
- ✅ Handle extractors funzionano: `carlotta.castoro` correttamente estratto da URL FB/TT/IG
- ✅ `_RECON_BY_PLATFORM` mappa tutti e 3
- ✅ Runner si importa senza syntax errors
- ✅ `task_form.html` parsa OK

---

## ⚠️ Cosa NON è stato fatto (richiede te)

### Test E2E reale FB

**Bloccato** dalla mancanza della password di `chifer81@hotmail.com`.

### Setup social_account TikTok

Il tenant non ha un social_account TikTok registrato. Per testare TT serve:
1. Registrare account TT via `/social/accounts` (architect UI)
2. Loggare il browser headed la prima volta (Argos salva cookie)
3. Rifare il task #47 con `recon_social_account_id` valorizzato

---

## 📋 Checklist mattina (5-15 minuti)

### Per FB (priorità alta — Paolo e Carlotta sono FB amici tuoi):

```
1. Avvia Argos (uvicorn --reload)
2. Login come architect
3. Vai su /tasks/46 (TEST FB)
4. Clicca "▶ Esegui"
5. Apri /jobs/<job_id>/log e segui i log live
```

### Cosa aspettarti nel log:

**Caso A — Funziona al primo colpo**:
```
✓ goto https://www.facebook.com/paolo.maugeri/followers
✓ main container visibile
📥 Enumero follower di @paolo.maugeri su facebook (cap=20)...
  scroll 1/8: 8 follower (cap=20)
  scroll 2/8: 16 follower (cap=20)
  scroll 3/8: 20 follower (cap=20)
  ✓ enumerati 20 follower di @paolo.maugeri
[poi: estrazione bio di ogni follower con LLM Ollama]
```

**Caso B — Login required (probabile)**:
```
❌ pagina inaccessibile per @paolo.maugeri (privacy / inesistente / login required)
```
→ Devi PRIMA fare login a FB nel browser headed Argos. Argos usa session_manager.py con cookie persistenti.

**Caso C — Selettori CSS errati (~30% probabilità)**:
```
⚠️ main container non trovato — fallback su body
  scroll 1/8: 0 follower (cap=20)
  ⏹ stop: 3 scroll senza nuovi follower
  ✓ enumerati 0 follower di @paolo.maugeri
```
→ Apri lo screenshot in `data/recon_runs/<job_id>/follower_lists/fb_followers_initial_*.png`. 
→ Inspect DOM con DevTools sulla pagina /paolo.maugeri/followers e annota i nuovi selettori.
→ Mandami il dump HTML del container `div[role="main"]` e aggiusto i selettori in 5 min.

**Caso D — Anti-bot/captcha**:
```
❌ goto fail: Page.goto: net::ERR_ABORTED
```
→ FB ha lanciato challenge. Login manuale + retry.

### Per TikTok (priorità bassa):

1. Registrare social_account TT (architect UI > /social/accounts)
2. Una volta registrato, edit task #47 → metti `recon_social_account_id` = <id-tt>
3. Lancia come per FB

---

## 🚨 Rischi documentati

- **TOS violation**: FB e TT vietano scraping autenticato. Rischio ban account chifer81. Mitigazione: cap basso (20), pause randomizzate fra target (20-60s), modalità READ-ONLY.
- **DOM instability**: Meta/ByteDance aggiornano DOM ogni 2-4 settimane. I selettori che ho scritto sono basati su quello che ricordo del DOM 2024/2025 ma POTREBBERO ESSERE GIA' CAMBIATI. La cosa più probabile è che 1-2 selettori vadano aggiustati alla prima esecuzione.
- **Selettori CSS untested**: NON ho potuto verificare i selettori contro il DOM reale. Best-effort.

---

## 📁 File modificati

```
app/agent/runner_recon_social.py    [refactor dispatch ~50 righe]
app/agent/social/facebook_recon.py  [+200 righe: enumerate + helpers]
app/agent/social/tiktok_recon.py    [+200 righe: enumerate]
app/templates/task_form.html        [rename dropdown + help text]
```

## 🎯 Status finale FASE D

| Sub-task | Status |
|---|---|
| Refactor runner dispatch | ✅ Done |
| Codice FB scraper | ✅ Done (best-effort, untested) |
| Codice TT scraper | ✅ Done (best-effort, untested) |
| UI rename + badge beta | ✅ Done |
| Task di test in DB | ✅ Done (#46 FB, #47 TT) |
| Smoke architetturale | ✅ 6/6 verde |
| Test E2E FB reale | ❌ **TUO COMPITO** (5-15 min) |
| Test E2E TikTok | ❌ Manca social_account TT |

Buon risveglio. Per la demo: l'architettura è multi-platform e UI mostra le 3 piattaforme con badge "beta" su FB/TT. Se ti chiedono di dimostrare FB live, prima fai il test step "Caso B/C/D" e mandami il log se qualcosa rompe.
