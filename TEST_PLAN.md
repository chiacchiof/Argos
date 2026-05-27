# TEST_PLAN.md — Copertura funzionale Argos

Documento operativo: traccia di test funzionali end-to-end che dimostrino la copertura del progetto. Ordinato per **uso reale previsto** (cosa l'utente effettivamente vuole far girare in produzione), non per importanza teorica.

Per il backlog tecnico esteso con stato corrente e dettagli implementativi, vedere il memory file `project_test_coverage_backlog.md`.

## Convenzioni

- 🟢 = validato sul campo (con job reali completati e DB popolato)
- 🟡 = validato parzialmente (smoke test ok, o validato in scenario limitato)
- 🔴 = non validato (codice presente ma mai eseguito end-to-end)
- ⬜ = task di test ancora da definire

Ogni sezione contiene:
- **Stato**
- **Cosa serve dimostrare** (criteri di accettazione)
- **Setup minimo** (account, env, dipendenze)
- **Step di esecuzione**

---

## 1. Recon social `follower_scrape` — Instagram 🟢

**Stato**: validato 2026-05-15 (job#147, 1258 follower enumerati su 3 target in 8m 20s; ~42s/profilo scraping individuale; 0 detection signals).

**Cosa serve dimostrare**: data un account IG target, enumera follower (cap configurabile) → materializza asset `ig_profile` + contact con tag derivati.

**Setup minimo**:
- 1 account IG sender (con `AGENTSCRAPER_SECRET` cifratura)
- `RECON_SOCIAL_DISABLED` non settato
- 1-3 handle IG target (account pubblici)

**Step**:
1. Crea task `recon_social` + `recon_mode=follower_scrape`, account sender, target handles nel seed
2. Lancia → verifica enumeration dei follower dal modale
3. Verifica scraping individuale → asset materializzati con `title` valido (non numerico, non URL)
4. Verifica tag derivati (`source_follower_of=@target`, `location`, `interests_inferred`, ecc.)

## 2. Recon social `follower_scrape` — Facebook 🔴

**Stato**: codice esiste in `app/agent/social/facebook_recon.py`, mai eseguito in modalità follower_scrape.

**Cosa serve dimostrare**:
- **2a. Ricerca per paletti**: dati zona geografica + interessi, FB ritorna lista di utenti pubblici corrispondenti; estrae nome + bio breve + canali di contatto.
- **2b. Ricerca partendo da amico** (analogo IG follower_scrape): dato un amico/friend FB, enumera i suoi friend/follower → materializza.

**Setup minimo**:
- 1 account FB sender (token sessione persistito in `data/social_sessions/`)
- Per 2a: definire una zona test + 1-2 interest tags
- Per 2b: 1 friend/page FB target

**Step**:
1. Crea task `recon_social` con `account_platform=facebook`, popolare seed
2. Per 2a: verificare che il flow di search by paletti funzioni (modulo `search_friend_via_friendlist` o equivalente)
3. Per 2b: scegliere recon_mode = friend list scrape
4. Asset materializzati con tag derivati

**Rischi noti**: FB cambia DOM frequentemente, friend list ha 2 layout (mobile vs desktop). I selettori in `facebook_recon.py` potrebbero richiedere refresh.

## 3. Recon social — TikTok 🔴

**Stato**: file `tiktok_recon.py` esiste ma zero validazione storica. Probabilmente non funziona out-of-the-box.

**Cosa serve dimostrare**:
- TT supporta enumeration "followers" o "following" di un creator?
- Search per hashtag/zona/keyword + extract profili

**Setup minimo**:
- 1 account TT sender
- 1-2 handle TT target

**Step**:
1. Smoke test del modulo (import + struttura selettori)
2. Login persistente TT
3. Lancia task recon → verifica primi 5-10 follower estratti
4. Verifica title + bio + canali contact pubblici

**Rischi noti**: TT ha anti-bot più forte di FB+IG combinati. Probabile che servano fix.

## 4. Outreach DM social — Instagram 🔴

**Stato**: codice `runner_outreach_social.py` supporta IG, ma memory documenta validation solo Facebook (2026-05-12 job#104). IG DM mai validato.

**Cosa serve dimostrare**: dato un set di contact con handle IG, il runner naviga al profilo → click DM → digita messaggio LLM-rephrased → invia.

**Setup minimo**:
- 1 account IG sender warmup ≥30gg
- Set di 3-5 contact target con `social_json` contenente IG handle
- `outreach_intent` + `message_template_variants`

**Step**:
1. Crea task `outreach_social`, account IG, target selezionati
2. Lancia → verifica:
   - Apertura profilo target OK
   - Trovato selector "Send message" / "Invia messaggio"
   - Modal DM si apre
   - Messaggio digitato carattere per carattere
   - Send confermato (post in `social_dm_log`)
3. Verifica nessun action_blocked / captcha durante run

**Rischi noti** (alti):
- IG è il più aggressive su anti-bot DM
- Selettore button DM cambia spesso
- Cold DM da account warmup raccomandato

## 5. Outreach DM social — Facebook 🟡

**Stato**: validato 2026-05-12 job#104 (memory `project_outreach_social_v0.md`). MA: il nuovo `outreach_filter_tags` multi-tag AND mai validato sul flow FB end-to-end.

**Cosa serve dimostrare**:
- Re-run del flow FB DM con set filtrato via `outreach_filter_tags=[{interests_inferred:fitness}, {location:Catania}]`
- Verifica che solo contact taggati con quei tag ricevono DM

**Setup minimo**: account FB già configurato + set di asset/contact con tag `interests_inferred` e `location`

**Step**:
1. Crea task `outreach_social` con `outreach_filter_tags` valorizzato (no `target_contact_ids` espliciti per testare il filtro)
2. Lancia in dry-run prima → verifica `plan` contiene SOLO i contact che matchano AND multipli
3. Run reale su sottoinsieme piccolo (3-5 DM)

## 6. Outreach DM social — TikTok 🔴

**Stato**: probabilmente non implementato o non validato. Dipende dal completamento del recon TT (sez. 3).

## 7. Outreach WhatsApp 🟡

**Stato**:
- Engine A (browser): validato in scenari limitati (template variants OK)
- Engine B (Meta Cloud API): integrato ma quasi sicuramente mai validato end-to-end (richiede setup template Meta approvato + numero verificato)

**Cosa serve dimostrare**:
- Engine A: invio DM via browser WhatsApp Web a numeri internazionali
- Engine B: invio messaggio approvato (template) o free-form (24h window) via Meta API

**Setup minimo per engine B**: numero WhatsApp Business verificato, app Meta approvata, template "hello world" preapprovato

## 8. Workflow DAG con cascata 🟡

**Stato**: smoke OK, ma scenario reale multi-step (recon → qualifier → outreach con artifact passing) mai testato end-to-end con pipeline complessa.

**Cosa serve dimostrare**:
1. Workflow con 3+ task collegati da edge
2. Task A produce `results/*.jsonl` → edge artifact_path passato a task B
3. Task B (qualifier) processa l'artifact e produce contact con status
4. Task C (outreach) parte automaticamente sui contact qualified
5. Stop e completa: trigger downstream funzionante

**Setup minimo**: 3 task pre-configurati + 1 workflow che li collega

## 9. Orchestrator (planner doppio + chat persistente) 🟡

**Stato**: usato casualmente, ma planner doppio (planner-fast + planner-deep) + autonomy throttle + artifact passing mai stressato con brief complesso.

**Cosa serve dimostrare**:
1. Brief reale ("scarica 100 follower di @ekipe_club, qualifica, contatta i fitness-interested in DM"): il planner genera plan corretto
2. User può approvare/rivedere il plan
3. Esecuzione plan: task creati e lanciati con cron sfalsati
4. Chat persistente: history visibile cross-conversation

## 10. Multi-tag filter — validazione end-to-end audience-driven ⬜

**Stato**: implementato in DB layer + 3 runner outreach. Verificato lato `count_contacts()`, MAI lato "DM realmente inviati solo ai filtrati".

**Cosa serve dimostrare**:
1. Crea task outreach con `outreach_filter_tags=[{interests_inferred:fitness}, {location:Catania}]`
2. Verifica via UI/DB: count contact matchanti
3. Lancia dry-run: il `plan` contiene SOLO i contact matchanti (no più, no meno)
4. Verifica social_dm_log dopo run reale: target_contact_id ∈ subset filtrato

## 11. Asset/contact CRUD + filtri 🟢

**Stato**: usato quotidianamente, validato.

**Cosa eventualmente da rivalidare**:
- Bulk delete con cascade su contact (selezione di 100+ asset)
- Import CSV su 1000+ righe (perf + dedup canonical)
- Filtri facet UI: combinazione `asset_type` + `tags` multipli + `status`

## 12. LLM provider — multi-provider 🟡

**Stato**:
- Ollama (locale): validato 🟢
- OpenAI, Anthropic, gemini, openrouter, lmstudio, vllm: nominalmente supportati, ma quanti effettivamente usati con task reali?

**Cosa serve dimostrare**: switch di `llm_provider` su un task e re-run → behavior coerente.

## 13. Schedulazione cron + scaling 🟡

**Stato**: validato per 1 task con cron. Mai testato pattern "5 task clonati con cron sfalsati che girano in batteria".

**Cosa serve dimostrare**:
1. Clona 1 task template 5 volte (target IG diversi)
2. Setta cron sfalsati (Lunedì 9, Martedì 10, ecc.)
3. Verifica APScheduler li registra tutti
4. A regime: 1 settimana di run, verifica nessun overlap, nessun crash, nessun ban account

---

## Ordine consigliato (priorità per uso reale)

In base ai workflow correnti dell'utente:

1. **IG outreach DM** (sez. 4) — naturale follow-up di job#147: dopo aver scrappato 1258 follower, contattare quelli qualified
2. **FB recon profondo** (sez. 2a + 2b) — analogo a IG
3. **Multi-tag filter end-to-end** (sez. 10) — validare il filtro audience prima di scalare l'outreach
4. **FB outreach DM con multi-tag** (sez. 5)
5. **TikTok recon** (sez. 3) — esplorazione, dipende da quanto serve
6. **Workflow cascata** (sez. 8) — pipeline completa scrape → qualify → outreach
7. **WhatsApp engine B** (sez. 7) — se serve canale legal-compliant
8. **TikTok DM** (sez. 6) — dopo recon TT
9. **Orchestrator brief complesso** (sez. 9)
10. **Schedulazione scaling** (sez. 13) — pattern produzione settimanale

---

## Note di rischio sintesi

| Area | Rischio | Note |
|---|---|---|
| IG follower_scrape | 🟢 Basso | Validato job#147 |
| FB recon profondo | 🟡 Medio | DOM FB cambia frequentemente |
| TikTok | 🔴 Alto | Codice presente, validazione zero |
| IG DM send | 🔴 Alto | Selettori DM IG fragili, anti-bot aggressive |
| FB DM send | 🟢 Basso | Validato 2026-05-12 |
| Multi-tag filter | 🟡 Medio | DB OK, send-time mai verificato |
| Workflow cascata | 🟡 Medio | Smoke OK; orchestrazione (recovery, finalize, artifact passing) coperta da test automatici (B-007). Scenario full-runner complesso ancora da validare a mano |
| WhatsApp engine B | 🔴 Alto | Setup Meta complicato, probabilmente non testato |

---

## Test automatici (CI) — complementari ai test manuali sopra

I test sopra sono **end-to-end manuali con runner reali** (LLM/browser/social). In `tests/` ci sono anche **test automatici deterministici** (pytest, DB isolato `agentscraper_test`) che coprono la logica di orchestrazione e i seam dove si annidano le regressioni, senza servizi esterni:

| File | Copre |
|---|---|
| `test_workflow_integration.py` (B-007) | recovery (`reconcile_orphan_jobs`, `watchdog_zombie_jobs`), policy `_maybe_finalize_workflow_run`, `find_workflow_roots`, artifact passing A→B (runner stubbato) |
| `test_secrets_encryption.py` (B-008) | cifratura at-rest LLM API key per-task (round-trip, idempotenza, fallback legacy, migrazione one-time) |
| `test_job_chat.py` (B-001) | chat in-running: parser comandi, coda `consume_pending_chat`, route |
| `test_contact_cli.py` (B-002) | mini-CLI contatti: parser + apply DB + route |

Esecuzione: `python -m pytest tests/ -q` (il conftest droppa/ricrea lo schema per test → isolamento totale; richiede il container Postgres dev attivo).
