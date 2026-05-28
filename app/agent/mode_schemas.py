"""Single source of truth degli agent_mode di Argos.

Per ciascuna modalità definisce:
- family: categoria UI per il raggruppamento del <select> agent_mode
- label / hint: testo nel select + tooltip esteso
- seed_kind: tipo logico del campo "seed" (None se la modalità non usa seed)
- seed_label / seed_hint: etichette UI per il campo seed (mostrate al posto
  del generico "Seed (una per riga, opzionale)")
- required / optional: nomi di campi TaskIn rilevanti per la modalità
- runner_status: production / beta / wip

`required ∪ optional ∪ UNIVERSAL_FIELDS` = campi ammessi per la modalità.
Tutti gli altri campi TaskIn sono "non rilevanti": se valorizzati dall'utente o
dall'orchestrator vengono segnalati come warning (non bloccano il salvataggio
per retro-compat con i task creati prima del redesign).

Importato da:
- `app.routes.tasks` per pilotare il context del form task_form.html
- `app.orchestrator` per validare le tool-call `create_task` del planner LLM
- `app.agent.runner_*` per sanity check del payload in ingresso
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


SeedKind = Literal[
    "search_queries",   # query DDG, testo libero (react)
    "seed_urls",        # lista URL (browser_use / bulk_extract / auto_extract)
    "start_url",        # UN solo URL home (site_explorer)
    "recon_targets",    # URL profili social o nomi friend (recon_social/url_driven)
    "target_handles",   # handle social degli account target (recon_social/follower_scrape)
    "anchor_profiles",  # profili FB di partenza opzionali per audience_discovery
]


Family = Literal["scraping", "recon", "audience", "processing", "outreach"]


# Campi sempre ammessi (identità, LLM main, output, scheduling, valutazione).
# Non vengono mai marcati come "non rilevanti" da `validate_payload`.
UNIVERSAL_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "objective",
    "agent_mode",
    "output_format",
    "cron",
    "rating",
    "notes",
    "status_tag",
    "model",
    "llm_provider",
    "llm_base_url",
    "llm_api_key",
    "llm_credential_id",
)


@dataclass(frozen=True)
class ModeSchema:
    mode: str
    family: Family
    label: str
    hint: str
    seed_kind: SeedKind | None
    seed_label: str | None
    seed_hint: str | None
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    runner_status: Literal["production", "beta", "wip"] = "production"

    def allowed_fields(self) -> frozenset[str]:
        return frozenset(UNIVERSAL_FIELDS) | frozenset(self.required) | frozenset(self.optional)


# ────────────────────────────────────────────────────────────────────────────
# Schemi per modalità. Ordine = ordine di apparizione nel <select> per famiglia.
# ────────────────────────────────────────────────────────────────────────────

MODE_SCHEMAS: dict[str, ModeSchema] = {
    "react": ModeSchema(
        mode="react",
        family="scraping",
        label="react — ricerca leggera HTTP+DDG",
        hint="Loop ReAct con DDG: trova info via HTTP. Veloce, niente browser.",
        seed_kind="search_queries",
        seed_label="Query di ricerca",
        seed_hint="Una query DuckDuckGo per riga. Se vuoto l'agente le deduce dall'obiettivo.",
        required=("objective",),
        optional=(
            "seed_queries",
            "allowed_domains",
            "blocked_domains",
            "max_iterations",
        ),
    ),
    "browser_use": ModeSchema(
        mode="browser_use",
        family="scraping",
        label="browser_use — scraping con browser reale (agentico)",
        hint="Pilota un browser reale (Chromium): scrolla, clicca, gestisce JS. Per scraping serio.",
        seed_kind="seed_urls",
        seed_label="Seed URL",
        seed_hint="Una URL completa per riga (es. <code>https://example.com/categoria/1</code>). Ognuna è una sessione browser-use indipendente.",
        required=("objective", "extraction_template"),
        optional=(
            "seed_queries",
            "extraction_schema",
            "allowed_domains",
            "blocked_domains",
            "max_iterations",
            "output_asset_type",
            "browser_llm_provider",
            "browser_llm_model",
            "browser_llm_credential_id",
        ),
    ),
    "bulk_extract": ModeSchema(
        mode="bulk_extract",
        family="scraping",
        label="bulk_extract — scraping massivo deterministico (lista URL)",
        hint="Scraping massivo deterministico (HTTP+readability+LLM). 1 chiamata LLM per URL. Per cataloghi grandi e siti statici.",
        seed_kind="seed_urls",
        seed_label="URL da processare",
        seed_hint="Una URL completa per riga. Verranno unite agli URL trovati nell'<code>Input artifact path</code> (se presente). Dedup automatico.",
        required=("objective", "extraction_template"),
        optional=(
            "seed_queries",
            "extraction_schema",
            "allowed_domains",
            "blocked_domains",
            "max_iterations",
            "input_artifact_path",
            "input_asset_filter",
            "output_asset_type",
            "bulk_concurrency",
            "bulk_rate_limit_per_sec",
            "bulk_extraction_method",
            "bulk_css_selectors",
            "crawler_enabled",
            "crawler_url_pattern",
            "crawler_max_depth",
            "max_discovery_retries",
            "refresh_policy_days",
            "discovery_llm_provider",
            "discovery_llm_model",
            "discovery_llm_credential_id",
        ),
    ),
    "auto_extract": ModeSchema(
        mode="auto_extract",
        family="scraping",
        label="auto_extract — agente decide la strategia per ogni sito",
        hint="Per ogni sito della lista, un profiler LLM decide la strategia (bulk_extract o browser_use) o salta se non promettente. Consigliato per liste eterogenee.",
        seed_kind="seed_urls",
        seed_label="Lista siti da analizzare",
        seed_hint="Una URL home per riga. Per ogni sito l'agente decide automaticamente la strategia (bulk_extract / browser_use / site_explorer / skip).",
        required=("objective", "extraction_template"),
        optional=(
            "seed_queries",
            "extraction_schema",
            "allowed_domains",
            "blocked_domains",
            "max_iterations",
            "output_asset_type",
            "bulk_concurrency",
            "bulk_rate_limit_per_sec",
            "bulk_extraction_method",
            "crawler_enabled",
            "crawler_url_pattern",
            "crawler_max_depth",
            "max_discovery_retries",
            "target_cap_per_site",
            "refresh_policy_days",
            "discovery_llm_provider",
            "discovery_llm_model",
            "discovery_llm_credential_id",
            "browser_llm_provider",
            "browser_llm_model",
            "browser_llm_credential_id",
        ),
    ),
    "site_explorer": ModeSchema(
        mode="site_explorer",
        family="scraping",
        label="site_explorer — LLM mappa il sito, runner estrae",
        hint="Mapping LLM (3-5 step) + Extraction runner-driven deterministico. Perfetto per siti listing→dettaglio (immobili, e-commerce, directory).",
        seed_kind="start_url",
        seed_label="URL iniziale",
        seed_hint="UN solo URL — la home o sezione alta del sito. Il runner mappa il sito da lì.",
        required=("objective", "extraction_template"),
        optional=(
            "seed_queries",  # UN solo URL (la prima riga)
            "extraction_schema",
            "allowed_domains",
            "blocked_domains",
            "max_iterations",
            "output_asset_type",
            "target_cap_per_site",
            "refresh_policy_days",
        ),
    ),
    "recon_social": ModeSchema(
        mode="recon_social",
        family="recon",
        label="recon_social — navigazione profili social (account loggato, READ-ONLY)",
        hint="Naviga profili social (FB/IG/TikTok) con un tuo account loggato in modalità READ-ONLY (no like/commenta/DM/follow). Vedi caveat GDPR/ToS.",
        seed_kind="recon_targets",
        seed_label="URL profili o handle target",
        seed_hint="Una URL profilo per riga (es. <code>https://www.facebook.com/mario.rossi</code>) <strong>oppure</strong> un handle (es. <code>@mario.rossi</code>). Per nomi-amici (resolved contro friend/following list) usa il campo dedicato sotto.",
        required=("objective", "recon_mode", "recon_social_account_id"),
        optional=(
            "seed_queries",
            "seed_queries_friends",
            "target_asset_ids",
            "extraction_template",
            "extraction_schema",
            "output_asset_type",
            "recon_hypothesis",
            "recon_max_targets_per_day",
            "recon_score_threshold",
            "refresh_policy_days",
            "speed_profile",
            "social_platform",
            "max_iterations",
        ),
    ),
    "audience_discovery": ModeSchema(
        mode="audience_discovery",
        family="audience",
        label="audience_discovery — esplora FB loggato cercando audience per brief NL",
        hint="Agente ReAct che pilota un account Facebook loggato per scoprire profili che matchano un brief NL (topic + demografia). Cerca su FB tramite barra ricerca, gruppi tematici, friends-of-friends partendo da anchor opzionali. READ-ONLY (no like/commenta/DM/follow).",
        seed_kind="anchor_profiles",
        seed_label="Anchor profili FB (opzionale)",
        seed_hint="URL o handle di profili FB da usare come <strong>punti di partenza</strong> per l'esplorazione (es. amici reali dell'account loggato che fanno parte dell'audience target). Se vuoto, l'agente parte solo dalla search bar e dai gruppi tematici dedotti dal brief. Es. <code>https://www.facebook.com/paolo.maugeri</code> o <code>@paolo.maugeri</code>.",
        required=("objective", "social_platform", "recon_social_account_id"),
        optional=(
            "seed_queries",  # anchor profiles (URL/handle), opzionale
            "target_asset_ids",  # asset anchor opzionali
            "recon_max_targets_per_day",  # cap audience da raccogliere
            "speed_profile",  # safe/balanced/aggressive (riusa recon)
            "refresh_policy_days",  # dedup profili già visti
            "extraction_template",
            "extraction_schema",
            "output_asset_type",
            "max_iterations",  # cap step LLM
            "recon_score_threshold",  # score minimo per save_match
        ),
        runner_status="beta",
    ),
    "qualifier": ModeSchema(
        mode="qualifier",
        family="processing",
        label="qualifier — LLM filtra/scora contatti",
        hint="Legge un profiles.jsonl e filtra i contatti via LLM, scorando 0-10.",
        seed_kind=None,
        seed_label=None,
        seed_hint=None,
        required=("objective",),
        optional=(
            "target_asset_ids",
            "input_artifact_path",
            "input_asset_filter",
            "extraction_template",
            "extraction_schema",
            "output_asset_type",
            "qualifier_destroy_mode",
        ),
    ),
    "outreach": ModeSchema(
        mode="outreach",
        family="outreach",
        label="outreach — invio email/telegram",
        hint="Manda email/telegram ai contatti qualified. Usa il template che definisci sotto.",
        seed_kind=None,
        seed_label=None,
        seed_hint=None,
        required=("message_template", "message_channels"),
        optional=(
            "target_asset_ids",
            "input_artifact_path",
            "input_asset_filter",
            "message_subject",
            "email_account_id",
            "telegram_bot_id",
            "outreach_filter_source_task_id",
            "outreach_filter_source_follower_of",
            "outreach_filter_tags",
        ),
    ),
    "outreach_social": ModeSchema(
        mode="outreach_social",
        family="outreach",
        label="outreach_social — DM via Instagram/TikTok/Facebook",
        hint="DM via Instagram/TikTok/Facebook con browser stealth + Playwright. LLM rephrase per messaggi personalizzati.",
        seed_kind=None,
        seed_label=None,
        seed_hint=None,
        required=("social_platform", "outreach_intent", "message_template_variants"),
        optional=(
            "target_asset_ids",
            "target_contact_ids",
            "social_account_id",
            "max_dms_per_run",
            "max_dms_per_session",
            "gap_between_dms_min",
            "gap_between_dms_max",
            "headed",
            "outreach_filter_source_task_id",
            "outreach_filter_source_follower_of",
            "outreach_filter_tags",
        ),
        runner_status="production",  # FB end-to-end live dal 2026-05-12
    ),
    "outreach_whatsapp": ModeSchema(
        mode="outreach_whatsapp",
        family="outreach",
        label="outreach_whatsapp — DM WhatsApp (Motore A browser + B Cloud API)",
        hint="DM WhatsApp con doppio motore: A (browser su web.whatsapp.com — cold outreach, viola ToS) + B (Meta Cloud API — opt-in + template).",
        seed_kind=None,
        seed_label=None,
        seed_hint=None,
        required=("outreach_intent", "message_template_variants"),
        optional=(
            "target_asset_ids",
            "target_contact_ids",
            "whatsapp_account_id",
            "whatsapp_api_config_id",
            "whatsapp_engine_preference",
            "whatsapp_dry_run",
            "max_dms_per_run",
            "max_dms_per_session",
            "gap_between_dms_min",
            "gap_between_dms_max",
            "headed",
            "outreach_filter_source_task_id",
            "outreach_filter_source_follower_of",
            "outreach_filter_tags",
        ),
    ),
    "responder": ModeSchema(
        mode="responder",
        family="outreach",
        label="responder — auto-reply LLM ai messaggi inbound",
        hint="Risponde automaticamente ai messaggi inbound usando un LLM. Opt-out detection inclusa.",
        seed_kind=None,
        seed_label=None,
        seed_hint=None,
        required=("responder_system_prompt",),
        optional=(
            "input_artifact_path",
        ),
    ),
}


# Ordine famiglie nel <select> agent_mode (top-down).
FAMILIES: tuple[Family, ...] = ("scraping", "recon", "audience", "processing", "outreach")


FAMILY_LABELS: dict[Family, str] = {
    "scraping": "Scraping & ricerca",
    "recon": "Recon social",
    "audience": "Audience discovery",
    "processing": "Processing",
    "outreach": "Outreach & responder",
}


def modes_by_family() -> dict[Family, list[ModeSchema]]:
    """Ritorna le modalità raggruppate per famiglia, mantenendo l'ordine di
    inserimento in MODE_SCHEMAS dentro ogni famiglia."""
    out: dict[Family, list[ModeSchema]] = {fam: [] for fam in FAMILIES}
    for schema in MODE_SCHEMAS.values():
        out[schema.family].append(schema)
    return out


def get_schema(mode: str) -> ModeSchema:
    if mode not in MODE_SCHEMAS:
        raise KeyError(
            f"Unknown agent_mode: {mode!r}. Known: {sorted(MODE_SCHEMAS.keys())}"
        )
    return MODE_SCHEMAS[mode]


# Default fallback per task pre-redesign salvati con campo seed semantically ambiguo.
DEFAULT_SEED_KIND_PER_FAMILY: dict[Family, SeedKind] = {
    "scraping": "search_queries",
    "recon": "recon_targets",
    "audience": "anchor_profiles",
    "processing": "search_queries",  # placeholder, qualifier non usa seed
    "outreach": "search_queries",     # placeholder, outreach* non usa seed
}


def validate_payload(payload: dict) -> tuple[list[str], list[str]]:
    """Valida un payload TaskIn rispetto allo schema della sua modalità.

    Ritorna (errors, warnings):
    - errors: agent_mode sconosciuto + campi `required` mancanti/vuoti.
      L'orchestrator deve rifiutare la `create_task` e chiedere al planner
      LLM di correggere la tool-call. Le route /tasks normali invece le
      mostrano nel form come errori di validazione (analoghi a Pydantic).
    - warnings: controlli semantici specifici per modalità (es. seed di
      recon_social che non sembra URL/handle → potrebbe essere intent
      audience). Non bloccano: l'utente decide.

    NB: NON segnaliamo "campo X valorizzato ma non rilevante" perché Pydantic
    popola tutti i campi default-non-vuoti (max_iterations=10, headed=1, ...)
    indipendentemente dalla modalità, e questo produrrebbe decine di warning
    rumorosi per ogni task. La rilevanza dei campi è documentata in
    `_PLANNER_MANUAL` e nello schema HTML del form.
    """
    errors: list[str] = []
    warnings: list[str] = []

    mode = payload.get("agent_mode")
    if not mode:
        errors.append("agent_mode mancante")
        return errors, warnings
    if mode not in MODE_SCHEMAS:
        errors.append(
            f"agent_mode sconosciuto: {mode!r}. Valori validi: {sorted(MODE_SCHEMAS.keys())}"
        )
        return errors, warnings

    schema = MODE_SCHEMAS[mode]

    for field_name in schema.required:
        v = payload.get(field_name)
        if _is_empty(v):
            errors.append(f"{mode}: campo richiesto '{field_name}' mancante o vuoto")

    # Controlli semantici per modalità.
    if mode == "recon_social":
        warnings.extend(_check_recon_social_seed(payload))

    return errors, warnings


def _check_recon_social_seed(payload: dict) -> list[str]:
    """Diagnostica bug ricorrente (task #49 fallito 2026-05-26): l'orchestrator
    mette in `seed_queries` di un task `recon_social/url_driven` keyword di
    shopping intent invece di URL/handle. Il runner chiama
    `search_user_by_name(keyword)` su FB che ritorna 0 match → job morto in
    zero secondi.

    Ritorna warning se almeno una entry di seed_queries non sembra né URL
    né handle. Lascia all'utente la decisione (potrebbe essere voluto)."""
    out: list[str] = []
    recon_mode = (payload.get("recon_mode") or "").strip() or "url_driven"
    if recon_mode != "url_driven":
        return out
    seeds = payload.get("seed_queries") or []
    if not isinstance(seeds, (list, tuple)):
        return out
    suspicious = [s for s in seeds if isinstance(s, str) and not _looks_like_url_or_handle(s)]
    if suspicious:
        sample = suspicious[0][:60]
        out.append(
            "recon_social/url_driven: seed_queries contiene voci che non sembrano "
            "URL profilo né handle social (es. "
            f"{sample!r}). Il runner cercherà letteralmente un utente con quel nome "
            "e probabilmente troverà 0 match. Se l'intent è 'trovare audience per "
            "topic/demografia', considera la modalità audience_discovery (beta). "
            "Se l'intent è 'profilare amici specifici', sposta i nomi in "
            "seed_queries_friends (resolved contro la friend list dell'account loggato)."
        )
    return out


def _looks_like_url_or_handle(s: str) -> bool:
    """Heuristica: True se la stringa è un URL completo (http/https) o un
    handle social (`@name`). False per testo libero/keyword di shopping."""
    if not s:
        return False
    s = s.strip()
    if s.startswith(("http://", "https://", "www.", "@")):
        return True
    # `facebook.com/x`, `instagram.com/x`, `tiktok.com/@x` senza schema
    lower = s.lower()
    if any(lower.startswith(d) for d in ("facebook.com/", "instagram.com/", "tiktok.com/")):
        return True
    return False


def _is_empty(v) -> bool:
    """True se il valore è considerato 'non valorizzato' dall'utente.
    Tratta None, stringhe vuote, liste/dict vuoti come 'mancante'. NON tratta
    0 / False / 0.0 come vuoto (sono valori validi per int/bool/float fields)."""
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    if isinstance(v, (list, tuple, dict, set)) and len(v) == 0:
        return True
    return False
