from __future__ import annotations

import json
import re
import uuid
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from . import db, jobs
from .agent.extraction_templates import get_schema, list_templates
from .agent.llm_providers import get_provider, resolve_api_key, resolve_base_url
from .agent.ollama import list_models as ollama_list_models, maybe_add_keep_alive
from .config import settings


AgentMode = Literal[
    "react",
    "browser_use",
    "bulk_extract",
    "auto_extract",
    "site_explorer",
    "qualifier",
    "outreach",
    "outreach_social",
    "outreach_whatsapp",
    "responder",
    "recon_social",
]

AutonomyLevel = Literal["advisor", "builder", "supervised", "autonomous"]
RiskLevel = Literal["low", "medium", "high"]

AUTONOMY_LEVELS: dict[str, dict[str, Any]] = {
    "advisor": {
        "label": "Consigliere",
        "description": "Propone un piano leggibile, senza creare o lanciare nulla.",
        "can_create": False,
        "can_run": False,
    },
    "builder": {
        "label": "Builder",
        "description": "Crea task e workflow solo dopo conferma; non lancia job.",
        "can_create": True,
        "can_run": False,
    },
    "supervised": {
        "label": "Supervisionato",
        "description": "Crea e puo lanciare workflow dopo conferma esplicita.",
        "can_create": True,
        "can_run": True,
    },
    "autonomous": {
        "label": "Autonomo controllato",
        "description": "Crea e lancia il piano dopo conferma iniziale; messaggistica richiede un consenso dedicato.",
        "can_create": True,
        "can_run": True,
    },
}

RISKY_AGENT_MODES = {"outreach", "outreach_social", "outreach_whatsapp", "responder"}

PLANNER_TOOL_MAX_LOOPS = 3

PLANNER_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_extraction_templates",
            "description": "Lista i template di estrazione disponibili (key, name, description). Usali per scegliere extraction_template nei task scraping.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": "Lista i modelli disponibili per il provider indicato (default: provider corrente del piano). Usalo per validare il campo 'model' di un task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "Chiave provider (ollama, openai, anthropic, ...). Omettilo per usare il provider corrente."}
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "Lista i task gia esistenti nel progetto (id, name, agent_mode, model). Usalo per evitare duplicati e mantenere coerenza naming.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Massimo task da ritornare", "default": 20}
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workflows",
            "description": "Lista i workflow gia esistenti (id, name, description). Usalo per capire quali pipeline sono gia in piedi.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


_PLANNER_MANUAL = """\
MANUALE OPERATIVO ARGOS (per pianificare task e workflow)

== Le 8 modalita di agente ==

[react]
Ricerca web leggera: DuckDuckGo + fetch HTTP + readability. Niente browser.
Quando: sintesi/mini-report da fonti aperte, ricerche generiche.
Campi rilevanti: objective, max_iterations (default 10), model (Ollama locale).
Output: report .md/.txt in data/results/. Niente profiles.jsonl.

[bulk_extract]
HTTP + readability + 1 LLM call per URL, opzionale crawler BFS statico dal seed con auto-detect pattern URL via 1 LLM discovery.
Quando: lista o pattern di URL noti su sito statico (cataloghi, listini, directory, e-commerce piccoli). Anche partendo dalla home con crawler ON.
Campi rilevanti: seed_queries (URL iniziali), extraction_template, allowed_domains, max_iterations (200), crawler_enabled+crawler_max_depth (3-5) se serve scoprire pagine partendo dalla home.
Output: profiles.jsonl.

[browser_use]
Browser Chromium reale via browser-use, supporta JS, login, scroll, click, anti-bot leggero. Lento (~4-6h per sito grande) e costoso (~$5-10).
Quando: il brief dice javascript/dinamico/login/scroll/click/cloudflare/anti-bot, oppure sai che HTTP statico non basta.
Campi rilevanti: seed_queries, extraction_template, allowed_domains, max_iterations (30+).
Output: profiles.jsonl.

[auto_extract]
Profiler iniziale (1 LLM call per sito) + dispatch automatico fra bulk_extract / site_explorer / browser_use / skip per ogni dominio. Fallback bidirezionale (bulk<->site_explorer, browser_use->site_explorer) se la strategia primaria produce 0 profili.
Quando: lista eterogenea di siti diversi, non sai a priori se sono statici/dinamici/multi-livello.
Campi rilevanti: seed_queries (lista URL siti), extraction_template, allowed_domains, max_iterations (200+).
Output: profiles.jsonl.

[site_explorer]
Mapping LLM (3-5 step) + Extraction runner-driven deterministico. Il LLM fa SOLO la fase di mapping: 1-3 fetch_page per capire la struttura, poi enqueue_listings([...]) e start_extraction(summary). Il runner pop-pa la queue e fa extract_target su ogni URL fresco (LLM extractor economico). Auto-discovery paginazione (?p=N, /page/N, /it/...) e dedup canonical cross-lingua.
Tool LLM aggiuntivo: discover_via_browser(url, scrolls, target_pattern_hint) per siti infinite-scroll — apre Chromium headless, scrolla N volte, raccoglie centinaia/migliaia di URL DOM-finali, accoda al direct_target_queue. Zero token LLM (deterministico), ~10-30s.
Auto-discovery FORZATA: se target_cap_per_site=0 OPPURE l'objective contiene una keyword-trigger ("tutti i profili", "tutti i target", "tutti gli annunci", "tutti i prodotti", "tutto il sito", "centinaia", "migliaia", "infinite scroll", "tutti i contatti", "tutta la lista"), il runner chiama discover_urls_via_scroll(seed, scrolls=30) PRIMA del primo turno LLM. Soluzione al caso in cui il LLM ignorerebbe l'infinite scroll.
Quando: siti listing→dettaglio multi-livello (yescasa, pcase: la home non linka i target), siti con sub-domain come slug profilo, siti infinite-scroll/lazy-load (camgirl, social feed, news feed). Anche unbounded ("indicizza tutto il sito").
Campi rilevanti: seed_queries (UN solo URL, la home o sezione alta), extraction_template (obbligatorio), max_iterations (30-50), target_cap_per_site (30-100 oppure 0 per unbounded — cap interno di sicurezza 5000), model (preferibilmente code-tuned: qwen3-coder:30b locale, oppure gpt-4o-mini cloud).
Output: profiles.jsonl.

[qualifier]
Legge profiles.jsonl in input, valuta ogni profilo via LLM, produce qualified.jsonl.
Quando: filtrare lead validi prima di outreach, scorare per pertinenza.
Campi rilevanti: input_artifact_path (collegato via edge upstream), objective (criteri di filtro), model.
Output: qualified.jsonl.

[outreach]   RISCHIOSO
Invia email/telegram ai contatti in qualified.jsonl rispettando opt-out.
Quando: il brief chiede esplicitamente messaggi/email/contattare/campagna.
Campi rilevanti: input_artifact_path, message_subject, message_template, message_channels (es. ["email"]).
Vincolo: SEMPRE preceduto da qualifier upstream, oppure warning esplicito + risk_level=high.

[responder]   RISCHIOSO
Auto-reply su messaggi inbound (email/telegram).
Quando: brief chiede di rispondere automaticamente a chi scrive.
Campi rilevanti: responder_system_prompt.
Vincolo: warning consenso obbligatorio + risk_level=high.

== Artifact convention sugli edge ==
- bulk_extract / auto_extract / browser_use / site_explorer -> qualifier   ::   pass_artifact = "profiles.jsonl"
- qualifier -> outreach                                                    ::   pass_artifact = "qualified.jsonl"
- qualifier -> responder                                                   ::   pass_artifact = "qualified.jsonl"
Per altre coppie pass_artifact puo essere null.

== Persistenza e re-run incrementali ==
Tutti i runner di scraping (bulk_extract / site_explorer / auto_extract) consultano db.has_recent_asset(url, ..., max_age_days=refresh_policy_days) PRIMA di chiamare l'LLM extractor su un URL.
- refresh_policy_days=0  -> "mai re-extract" (skip se asset esiste in DB, qualunque eta')
- refresh_policy_days=N  -> skip se asset esiste e updated_at entro N giorni (default 7)
- refresh_policy_days=-1 -> "sempre re-extract"
Il check usa source_url_canonical -> dedup cross-lingua (/it/ ≡ /en/) e cross-paginazione (?p=0 ≡ no-query).
Site_explorer e browser_use salvano un playbook in site_playbooks a fine job riuscito -> al run successivo sullo stesso dominio il LLM mapping legge la mappa pre-armata e risparmia 2-3 step.

== Regole di consistency ==
1. outreach o responder => SEMPRE almeno un warning di consenso esplicito + risk_level="high".
2. outreach senza qualifier upstream e ammesso solo se l'utente lo chiede esplicitamente; va dichiarato come warning.
3. browser_use solo se il brief menziona javascript/dinamico/login/scroll/click/cloudflare/anti-bot; altrimenti preferisci bulk_extract, site_explorer o auto_extract.
4. auto_extract per liste di siti diversi; bulk_extract per pattern URL chiari di un solo dominio statico; site_explorer per UN sito multi-livello con struttura non ovvia oppure infinite-scroll.
5. **site_explorer vs bulk_extract**: se il brief riguarda UN solo sito ma il pattern URL non e' ovvio (drill-down categoria>>annuncio, sub-domain per profilo, paginazioni numerate complesse), site_explorer. Se il brief contiene URL/seed gia' pronti e pattern lineare (es. "estrai i prodotti di shop.x.com/category/y"), bulk_extract con crawler ON.
6. **site_explorer infinite-scroll / unbounded**: se l'utente dichiara di voler "tutti i profili / tutti i target / tutti i prodotti / tutto il sito / centinaia di profili / migliaia di X" oppure menziona "infinite scroll" / "social feed" / "lazy load" / "scroll dinamico", produci un task site_explorer con target_cap_per_site=0 E mantieni nell'objective la keyword esatta scritta dall'utente (NON la riformulare in qualcosa di neutro tipo "estrai profili"). La keyword nell'objective attiva l'auto-discovery FORZATA via Chromium nel runner. Senza keyword il trigger non scatta e il runner vede solo il first-paint statico.
7. extraction_template: usa "profile_contacts" per lead/contatti/profili; "ecommerce_products" per prodotti; "real_estate" per case; "events" per eventi; "news_articles" per articoli; "job_listings" per lavoro. Se ambiguo, omettilo.
8. Niente API key nel JSON. Niente campi inventati. Usa solo i nomi di agent_mode esistenti.
9. Se il brief e ambiguo, fai un piano piccolo (1-2 task), non riempire di stadi inutili.
10. **Pipeline discovery+extract**: se il brief chiede di "estrarre" / "raccogliere lead" / "trovare profili" / "scrapare" MA non contiene URL ne' domini specifici (es. "trova le agenzie immobiliari di Acireale e estrai gli annunci"), preferisci una pipeline a 2 task:
   - Task 1: `react` con objective "scopri URL di siti pertinenti e produci un elenco".
   - Task 2: `auto_extract` con `seed_queries=[]` (l'utente popolera' manualmente dopo la run di react).
   - **NON creare un edge** tra i due task: react produce un report .md, non un file di seed; il passaggio degli URL e' manuale.
   - Aggiungi al piano un warning chiaro che spiega il passaggio manuale all'utente.

== Tool disponibili (chiamali se servono) ==
- list_extraction_templates() per scegliere il template giusto.
- list_models(provider) per validare il modello di un task.
- list_tasks(limit) per evitare duplicati e riusare naming coerenti.
- list_workflows() per capire le pipeline gia presenti.
"""


_PLANNER_FEW_SHOT = """\
== Esempi brief -> plan (formato target) ==

ESEMPIO 1 (semplice scraping)
Brief: "Estrai prodotti da example-shop.com per costruire il catalogo"
Plan:
{
  "title": "Catalogo prodotti example-shop",
  "summary": "Estrazione bulk dello shop, output profiles.jsonl per export.",
  "risk_level": "low",
  "assumptions": ["Sito statico server-rendered."],
  "warnings": [],
  "run_after_create": false,
  "tasks": [{
    "key": "extract", "name": "Estrazione prodotti", "agent_mode": "bulk_extract",
    "objective": "Estrai pagine prodotto da example-shop.com.",
    "seed_queries": ["https://example-shop.com/"],
    "allowed_domains": ["example-shop.com"],
    "extraction_template": "ecommerce_products",
    "max_iterations": 200, "crawler_enabled": true, "crawler_max_depth": 3
  }],
  "edges": []
}

ESEMPIO 2 (pipeline con outreach: rischiosa)
Brief: "Cerca freelance designer su 3-4 portali italiani e mandagli un'email solo a quelli con portfolio decente"
Plan:
{
  "title": "Lead freelance designer italiani",
  "summary": "auto_extract -> qualifier -> outreach. Filtro qualita prima dell'invio.",
  "risk_level": "high",
  "assumptions": [
    "Portali freelance italiani: lista eterogenea, alcuni con JS.",
    "Schema profile_contacts cattura i campi pubblici dei freelance."
  ],
  "warnings": [
    "Il piano contiene outreach via email: serve consenso esplicito prima del lancio e rispetto opt-out."
  ],
  "run_after_create": false,
  "tasks": [
    {"key": "extract", "name": "Estrazione profili freelance", "agent_mode": "auto_extract",
     "objective": "Estrai profili di designer freelance italiani con contatti pubblici.",
     "seed_queries": ["https://portale-a.it/", "https://portale-b.it/"],
     "allowed_domains": ["portale-a.it", "portale-b.it"],
     "extraction_template": "profile_contacts", "max_iterations": 200},
    {"key": "qualifier", "name": "Qualifica designer con portfolio", "agent_mode": "qualifier",
     "objective": "Tieni solo profili con portfolio visibile e contatti email/whatsapp.",
     "max_iterations": 10},
    {"key": "outreach", "name": "Outreach email designer", "agent_mode": "outreach",
     "objective": "Invia email ai designer qualificati, rispettando opt-out.",
     "message_subject": "Una proposta per {display_name}",
     "message_template": "Ciao {display_name},\\n\\nho visto il tuo portfolio su {source_url}...",
     "message_channels": ["email"]}
  ],
  "edges": [
    {"from_key": "extract", "to_key": "qualifier", "pass_artifact": "profiles.jsonl"},
    {"from_key": "qualifier", "to_key": "outreach", "pass_artifact": "qualified.jsonl"}
  ]
}

ESEMPIO 3 (ricerca leggera)
Brief: "Voglio un report sulle ultime tendenze AI generativa"
Plan:
{
  "title": "Report tendenze AI generativa",
  "summary": "Ricerca web leggera con react + sintesi.",
  "risk_level": "low",
  "assumptions": ["Fonti pubbliche aperte, niente login."],
  "warnings": [],
  "run_after_create": false,
  "tasks": [{
    "key": "research", "name": "Ricerca tendenze AI", "agent_mode": "react",
    "objective": "Cerca e sintetizza le ultime tendenze AI generativa con fonti citate.",
    "max_iterations": 10
  }],
  "edges": []
}

ESEMPIO 4 (discovery + extract: nessun URL noto a priori)
Brief: "Trovami da solo i siti delle agenzie immobiliari di Acireale e poi estrai gli annunci con prezzo > 200000"
Plan:
{
  "title": "Lead immobiliari Acireale via discovery agenzie",
  "summary": "Step 1 react scopre URL di agenzie locali, step 2 auto_extract estrae gli annunci, step 3 qualifier filtra prezzo > 200k.",
  "risk_level": "low",
  "assumptions": [
    "Le agenzie immobiliari indipendenti pubblicano annunci su siti propri statici, accessibili senza login.",
    "Lo schema real_estate cattura prezzo/mq/citta/contatti agenzia."
  ],
  "warnings": [
    "Pipeline discovery+extract: lancia prima il task react. Quando completa, apri il report e copia gli URL trovati nel campo 'Seed URL' del task auto_extract a valle. Solo dopo lancia auto_extract."
  ],
  "run_after_create": false,
  "tasks": [
    {"key": "research", "name": "Discovery agenzie immobiliari Acireale",
     "agent_mode": "react",
     "objective": "Cerca e produci un elenco di URL di siti di agenzie immobiliari indipendenti che operano nella zona di Acireale. Per ogni fonte: URL canonico + breve nota di rilevanza.",
     "max_iterations": 10},
    {"key": "extract", "name": "Estrazione annunci dalle agenzie scoperte",
     "agent_mode": "auto_extract",
     "objective": "Estrai annunci immobiliari dai siti delle agenzie scoperte nello step di discovery.",
     "seed_queries": [],
     "extraction_template": "real_estate", "max_iterations": 200},
    {"key": "qualifier", "name": "Qualifica annunci sopra 200k",
     "agent_mode": "qualifier",
     "objective": "Tieni solo annunci con prezzo > 200000 EUR.",
     "max_iterations": 10}
  ],
  "edges": [
    {"from_key": "extract", "to_key": "qualifier", "pass_artifact": "profiles.jsonl"}
  ]
}
Nota: l'edge tra `research` e `extract` non esiste perche' react produce report.md, non un file di seed. L'utente popola manualmente seed_queries di `extract` dopo aver visto il report.

ESEMPIO 5 (site_explorer multi-livello con cap esplicito)
Brief: "Estrai gli annunci di case in vendita ad Acireale sopra 200k da yescasa.it"
Plan:
{
  "title": "Annunci yescasa.it Acireale > 200k",
  "summary": "site_explorer su un sito immobiliare multi-livello (categoria citta + paginazione) con cap target 50.",
  "risk_level": "low",
  "assumptions": ["Yescasa.it ha listing per citta e paginazione numerata, sito statico HTML."],
  "warnings": [],
  "run_after_create": false,
  "tasks": [{
    "key": "extract", "name": "Estrazione annunci Acireale",
    "agent_mode": "site_explorer",
    "objective": "Estrai annunci immobiliari di Acireale con prezzo > 200000 EUR.",
    "seed_queries": ["https://www.yescasa.it/"],
    "allowed_domains": ["yescasa.it"],
    "extraction_template": "real_estate",
    "max_iterations": 40,
    "target_cap_per_site": 50
  }],
  "edges": []
}

ESEMPIO 6 (site_explorer infinite-scroll / unbounded)
Brief: "Voglio indicizzare TUTTI i profili pubblici di mondocamgirls.com, e' un sito infinite scroll. Mi servono email/telegram/social di ogni profilo."
Plan:
{
  "title": "Indicizzazione completa profili mondocamgirls.com",
  "summary": "site_explorer unbounded su sito infinite-scroll: auto-discovery FORZATA via Chromium headless scrolla la home e raccoglie centinaia/migliaia di sub-domain profilo, poi runner estrae contatti.",
  "risk_level": "low",
  "assumptions": [
    "Il sito espone profili come sub-domain; HTTP statico mostra solo first-paint.",
    "Profili pubblici, niente login richiesto."
  ],
  "warnings": [
    "Modalita unbounded: costo proporzionale al numero di profili (~$0.005/profilo con gpt-4o-mini). 1000 profili ≈ $5."
  ],
  "run_after_create": false,
  "tasks": [{
    "key": "extract", "name": "Indicizzazione completa profili",
    "agent_mode": "site_explorer",
    "objective": "Estrai TUTTI i profili pubblici del sito (infinite scroll) con email/telegram/social/sitoweb.",
    "seed_queries": ["https://mondocamgirls.com/it/"],
    "allowed_domains": ["mondocamgirls.com"],
    "extraction_template": "profile_contacts",
    "max_iterations": 60,
    "target_cap_per_site": 0
  }],
  "edges": []
}
Nota: target_cap_per_site=0 attiva la modalita unbounded; le parole "TUTTI i profili" + "infinite scroll" nell'objective sono keyword-trigger che attivano l'auto-discovery FORZATA via Chromium nel runner. NON riformulare l'objective in modo neutro: la keyword esatta e' parte del meccanismo.
"""


class PlannedTask(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    agent_mode: AgentMode = "react"
    objective: str = Field(min_length=1)
    description: str | None = None
    seed_queries: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=10, ge=1, le=100000)
    model: str = "qwen3.5:latest"
    output_format: Literal["txt", "md", "both"] = "md"
    extraction_template: str | None = None
    extraction_schema: str | None = None
    llm_provider: str = "ollama"
    llm_base_url: str | None = None
    input_artifact_path: str | None = None
    message_subject: str | None = None
    message_template: str | None = None
    message_channels: list[str] = Field(default_factory=list)
    responder_system_prompt: str | None = None
    bulk_concurrency: int = 5
    target_cap_per_site: int = 30
    bulk_rate_limit_per_sec: float = 2.0
    crawler_enabled: bool = False
    crawler_max_depth: int = 3
    notes: str | None = None
    status_tag: str | None = "tuning"
    # Audience selection (outreach_*): id-list di destinatari espliciti.
    # target_asset_ids vince su target_contact_ids quando entrambi valorizzati.
    target_contact_ids: list[int] = Field(default_factory=list)
    target_asset_ids: list[int] = Field(default_factory=list)
    # Filtri alternativi alla selezione esplicita (si combinano in AND).
    outreach_filter_source_task_id: int | None = None
    outreach_filter_source_follower_of: str | None = None
    outreach_filter_tags: list[dict] = Field(default_factory=list)
    # Input asset filter (legge asset esistenti come input)
    input_asset_filter: dict | None = None
    output_asset_type: str | None = None
    # outreach_social
    social_platform: str | None = None
    social_account_id: int | None = None
    outreach_intent: str | None = None
    message_template_variants: str | None = None
    max_dms_per_run: int = 30
    max_dms_per_session: int = 5
    headed: int = 1
    gap_between_dms_min: float | None = None
    gap_between_dms_max: float | None = None
    # outreach_whatsapp
    whatsapp_engine_preference: Literal["auto", "force_A", "force_B"] = "auto"
    whatsapp_dry_run: int = 0
    whatsapp_account_id: int | None = None
    whatsapp_api_config_id: int | None = None
    # recon_social
    recon_mode: Literal["url_driven", "exploration", "follower_scrape"] | None = None
    recon_social_account_id: int | None = None
    recon_hypothesis: str | None = None
    recon_max_targets_per_day: int = 50
    recon_score_threshold: int = 6
    seed_queries_friends: list[str] = Field(default_factory=list)
    speed_profile: Literal["safe", "balanced", "aggressive"] = "safe"


class PlannedEdge(BaseModel):
    from_key: str
    to_key: str
    pass_artifact: str | None = None


class OrchestratorPlan(BaseModel):
    title: str = "Piano orchestrator"
    summary: str
    autonomy_level: AutonomyLevel = "builder"
    risk_level: RiskLevel = "medium"
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tasks: list[PlannedTask] = Field(default_factory=list)
    edges: list[PlannedEdge] = Field(default_factory=list)
    run_after_create: bool = False
    planner_used: str = "heuristic"

    @property
    def has_risky_modes(self) -> bool:
        return any(t.agent_mode in RISKY_AGENT_MODES for t in self.tasks)


class ExecutionResult(BaseModel):
    created_task_ids: dict[str, int] = Field(default_factory=dict)
    workflow_id: int | None = None
    workflow_run_id: int | None = None
    started_jobs: list[int] = Field(default_factory=list)
    redirect_url: str
    message: str


URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+", re.IGNORECASE)
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)


def autonomy_meta(level: str) -> dict[str, Any]:
    return AUTONOMY_LEVELS.get(level, AUTONOMY_LEVELS["builder"])


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _extract_allowed_domains(text: str, urls: list[str]) -> list[str]:
    domains: list[str] = []
    for url in urls:
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if host and host not in domains:
            domains.append(host)
    if domains:
        return domains
    # fallback: only use bare domains if the prompt sounds like a site list
    lowered = (text or "").lower()
    if any(k in lowered for k in ("sito", "siti", "dominio", "url", "catalogo")):
        for m in DOMAIN_RE.finditer(text or ""):
            d = m.group(0).lower()
            if d not in domains:
                domains.append(d)
    return domains[:20]


def _wants_any(text: str, keywords: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(k in t for k in keywords)


def _detect_extraction_template(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in (
        "immobil", "casa ", "case ", "appartament", "villa", "annuncio immobil",
        "annunci immobil", "real estate", "compravendita", "affitt",
    )):
        return "real_estate"
    if any(k in t for k in (
        "prodott", "ecommerc", "e-commerce", " shop", "catalog", "listino",
        "sku", "carrello", "amazon", "shopify",
    )):
        return "ecommerce_products"
    if any(k in t for k in (
        "evento", "eventi", "concerto", "concerti", "conferenza", "conferenze",
        "biglietti", "festival", "mostra",
    )):
        return "events"
    if any(k in t for k in (
        "articol", "blog", "news ", "post ", "post di", "rivista", "testata",
    )):
        return "news_articles"
    if any(k in t for k in (
        "lavoro", "lavori", "annunci di lavoro", "jobs ", "posizione apert",
        "vacancy", "assunzioni", "ricerca personale",
    )):
        return "job_listings"
    return "profile_contacts"


def _safe_key(base: str, used: set[str]) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or "task"
    candidate = key
    i = 2
    while candidate in used:
        candidate = f"{key}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def _edge_artifact(from_mode: str, to_mode: str) -> str | None:
    if from_mode in {"browser_use", "bulk_extract", "auto_extract"}:
        return "profiles.jsonl"
    if from_mode == "qualifier":
        return "qualified.jsonl"
    return None


def _default_model_for_mode(mode: str, provider: str, requested_model: str | None) -> str:
    if mode == "react":
        return settings.default_model
    return requested_model or settings.default_model


def _planned_task(
    *,
    key: str,
    name: str,
    agent_mode: AgentMode,
    objective: str,
    brief: str,
    urls: list[str],
    domains: list[str],
    provider: str,
    model: str | None,
    llm_base_url: str | None,
) -> PlannedTask:
    task_model = _default_model_for_mode(agent_mode, provider, model)
    llm_provider = "ollama" if agent_mode == "react" else provider
    is_extract_mode = agent_mode in {"browser_use", "bulk_extract", "auto_extract"}
    extraction_template = _detect_extraction_template(brief) if is_extract_mode else None
    extraction_schema = get_schema(extraction_template) if extraction_template else None
    max_iterations = 10
    if agent_mode == "browser_use":
        max_iterations = 30
    elif agent_mode in {"bulk_extract", "auto_extract"}:
        max_iterations = 200
    elif agent_mode in {"qualifier", "outreach", "responder"}:
        max_iterations = 10

    seed_queries: list[str] = []
    if is_extract_mode:
        seed_queries = list(urls)
        if not seed_queries and domains:
            seed_queries = [f"https://{d}" for d in domains[:5]]

    return PlannedTask(
        key=key,
        name=name,
        agent_mode=agent_mode,
        objective=objective,
        description=f"Creato da Orchestrator dal brief: {brief[:300]}",
        seed_queries=seed_queries,
        allowed_domains=domains if is_extract_mode else [],
        max_iterations=max_iterations,
        model=task_model,
        output_format="md",
        extraction_template=extraction_template,
        extraction_schema=extraction_schema,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url if llm_provider == "custom" else None,
        bulk_concurrency=5,
        target_cap_per_site=30,
        bulk_rate_limit_per_sec=2.0,
        crawler_enabled=agent_mode in {"bulk_extract", "auto_extract"} and bool(seed_queries),
        crawler_max_depth=3,
        notes="Creato da Orchestrator. Verifica schema, modelli e limiti prima di produzioni grandi.",
        status_tag="tuning",
    )


def build_heuristic_plan(
    brief: str,
    autonomy_level: AutonomyLevel,
    provider: str = "ollama",
    model: str | None = None,
    llm_base_url: str | None = None,
) -> OrchestratorPlan:
    text = brief.strip()
    urls = extract_urls(text)
    domains = _extract_allowed_domains(text, urls)
    used: set[str] = set()
    tasks: list[PlannedTask] = []

    wants_extract = _wants_any(
        text,
        (
            "estrai",
            "estrarre",
            "scrape",
            "scraping",
            "contatti",
            "lead",
            "profili",
            "catalogo",
            "prodotti",
            "annunci",
        ),
    )
    wants_qualifier = _wants_any(
        text,
        (
            "qualifica",
            "filtra",
            "filtrare",
            "scora",
            "score",
            "valuta",
            "validi",
            "selezion",
            "criterio",
            "criteri",
            "soglia",
            "tieni solo",
            "solo quelli",
            "solo quelle",
            "solo i ",
            "solo le ",
            "almeno",
            "massimo",
            "minimo",
            "maggiore di",
            "minore di",
            "sopra",
            "sotto",
            "prezzo >",
            "prezzo <",
            ">=",
            "<=",
        ),
    )
    wants_outreach = _wants_any(text, ("outreach", "email", "telegram", "contatta", "messaggi", "campagna"))
    wants_responder = _wants_any(text, ("rispondi", "reply", "auto-reply", "inbound", "posta in arrivo"))
    wants_research = _wants_any(text, ("cerca", "ricerca", "report", "analizza", "trova informazioni"))
    wants_discovery = _wants_any(
        text,
        (
            "scopri sit",
            "scopri font",
            "scopri portal",
            "trova sit",
            "trova font",
            "trova portal",
            "trova agenzi",
            "elenca sit",
            "elenca font",
            "elenca portal",
            "fonti pertinenti",
            "fonti rilevanti",
            "siti pertinenti",
            "siti tematici",
            "siti rilevanti",
            "raccogli sit",
            "raccogli font",
            "ricerca sit",
            "ricerca font",
            "cerca da solo",
            "trova da solo",
            "trova le fonti",
            "fonti web",
        ),
    )
    needs_discovery_pipeline = (
        (wants_extract or wants_discovery)
        and not urls
        and not domains
    )

    if wants_responder and not wants_extract:
        key = _safe_key("responder", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Responder automatico",
                agent_mode="responder",
                objective=text,
                description="Auto-reply orchestrata sui messaggi inbound.",
                model=model or settings.default_model,
                llm_provider=provider,
                llm_base_url=llm_base_url if provider == "custom" else None,
                responder_system_prompt=(
                    "Rispondi in italiano con tono professionale e sintetico. "
                    "Non inventare dati. Se il messaggio contiene opt-out, non rispondere."
                ),
                notes="Creato da Orchestrator. Modalita rischiosa: controlla bene prima di lanciare.",
                status_tag="tuning",
            )
        )
    elif needs_discovery_pipeline:
        # PIPELINE DISCOVERY + EXTRACT (2 task):
        # nessun URL/dominio nel brief → step 1 react cerca le fonti, step 2 auto_extract
        # estrae i dati dalle fonti scoperte. L'utente popola manualmente seed_queries
        # del task downstream dopo aver visto il report di react.
        research_key = _safe_key("research", used)
        tasks.append(
            PlannedTask(
                key=research_key,
                name="Discovery fonti",
                agent_mode="react",
                objective=(
                    "Cerca e produci un elenco di URL di siti/portali pertinenti per il seguente brief. "
                    "Per ogni fonte includi URL canonico e una breve nota di rilevanza. "
                    "Niente sintesi prosaica: serve una lista pulita.\n\nBrief: " + text
                ),
                description="Step 1: discovery delle fonti web (output: report con elenco URL).",
                seed_queries=[],
                max_iterations=10,
                model=settings.default_model,
                output_format="md",
                llm_provider="ollama",
                notes=(
                    "Step 1 della pipeline discovery+extract. Quando completa, "
                    "apri il report e copia gli URL nel campo seed_queries del task "
                    "'Estrazione dati dalle fonti scoperte'."
                ),
                status_tag="tuning",
            )
        )
        extract_key = _safe_key("extract", used)
        tasks.append(
            _planned_task(
                key=extract_key,
                name="Estrazione dati dalle fonti scoperte",
                agent_mode="auto_extract",
                objective=text,
                brief=text,
                urls=[],  # popolati manualmente dall'utente dopo la run di research
                domains=[],
                provider=provider,
                model=model,
                llm_base_url=llm_base_url,
            )
        )
    elif wants_extract or urls:
        mode: AgentMode = "auto_extract" if len(urls) != 1 else "bulk_extract"
        if _wants_any(text, ("browser", "javascript", "js", "dinamico", "click", "scroll")):
            mode = "browser_use"
        key = _safe_key("extract", used)
        tasks.append(
            _planned_task(
                key=key,
                name="Estrazione dati",
                agent_mode=mode,
                objective=text,
                brief=text,
                urls=urls,
                domains=domains,
                provider=provider,
                model=model,
                llm_base_url=llm_base_url,
            )
        )
    elif wants_research:
        key = _safe_key("research", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Ricerca web",
                agent_mode="react",
                objective=text,
                description="Ricerca leggera HTTP + DuckDuckGo creata da Orchestrator.",
                seed_queries=[],
                max_iterations=10,
                model=settings.default_model,
                output_format="md",
                llm_provider="ollama",
                notes="Creato da Orchestrator. Il runner react usa Ollama locale.",
                status_tag="tuning",
            )
        )
    else:
        key = _safe_key("research", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Analisi iniziale",
                agent_mode="react",
                objective=text,
                description="Task iniziale creato da Orchestrator quando il bisogno e ambiguo.",
                max_iterations=8,
                model=settings.default_model,
                output_format="md",
                llm_provider="ollama",
                notes="Brief ambiguo: usa questo task per raccogliere contesto, poi raffina il workflow.",
                status_tag="tuning",
            )
        )

    if wants_qualifier and tasks and tasks[-1].agent_mode != "responder":
        key = _safe_key("qualifier", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Qualifica contatti",
                agent_mode="qualifier",
                objective=(
                    "Valuta i profili estratti e tieni solo contatti coerenti con il brief: "
                    + text
                ),
                description="Filtro/scoring LLM dei profili estratti.",
                model=model or settings.default_model,
                llm_provider=provider,
                llm_base_url=llm_base_url if provider == "custom" else None,
                output_format="md",
                notes="Creato da Orchestrator. Ricevera profiles.jsonl dal task upstream.",
                status_tag="tuning",
            )
        )

    if wants_outreach:
        if not wants_qualifier and tasks and tasks[-1].agent_mode not in {"outreach", "responder"}:
            key = _safe_key("qualifier", used)
            tasks.append(
                PlannedTask(
                    key=key,
                    name="Qualifica contatti",
                    agent_mode="qualifier",
                    objective="Filtra lead contattabili e pertinenti prima dell'outreach.",
                    description="Filtro minimo aggiunto per sicurezza prima dell'outreach.",
                    model=model or settings.default_model,
                    llm_provider=provider,
                    llm_base_url=llm_base_url if provider == "custom" else None,
                    output_format="md",
                    notes="Creato da Orchestrator come guardrail prima dell'outreach.",
                    status_tag="tuning",
                )
            )
        key = _safe_key("outreach", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Outreach controllato",
                agent_mode="outreach",
                objective="Invia messaggi solo ai contatti qualificati, rispettando opt-out e limiti canale.",
                description="Invio messaggi creato da Orchestrator. Richiede canali configurati in Settings.",
                output_format="md",
                message_subject="Una proposta per {display_name}",
                message_template=(
                    "Ciao {display_name},\n\n"
                    "ho visto la tua pagina su {source_url} e penso ci sia spazio per migliorarne "
                    "presentazione e contenuti.\n\n"
                    "Se ti va, posso mandarti 2-3 spunti concreti.\n\n"
                    "A presto"
                ),
                message_channels=["email"],
                notes="Creato da Orchestrator. Rivedi subject/template e consenso prima di lanciare.",
                status_tag="tuning",
            )
        )

    edges: list[PlannedEdge] = []
    extract_modes = {"bulk_extract", "auto_extract", "browser_use"}
    for i in range(len(tasks) - 1):
        from_t = tasks[i]
        to_t = tasks[i + 1]
        # Pipeline discovery+extract: react -> auto_extract NON ha edge automatico,
        # il passaggio degli URL e' manuale (react produce report.md, non un file di seed).
        if from_t.agent_mode == "react" and to_t.agent_mode in extract_modes:
            continue
        edges.append(
            PlannedEdge(
                from_key=from_t.key,
                to_key=to_t.key,
                pass_artifact=_edge_artifact(from_t.agent_mode, to_t.agent_mode),
            )
        )

    warnings: list[str] = []
    if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks):
        warnings.append(
            "Il piano contiene messaggistica outbound/auto-reply: serve consenso esplicito prima del lancio."
        )
    if any(t.agent_mode == "react" for t in tasks) and provider != "ollama":
        warnings.append("I task react usano Ollama locale anche se il planner usa un provider remoto.")
    extract_tasks = [t for t in tasks if t.agent_mode in {"bulk_extract", "auto_extract", "browser_use"}]
    if needs_discovery_pipeline and extract_tasks and any(not t.seed_queries for t in extract_tasks):
        warnings.append(
            "Pipeline discovery+extract: lancia prima il task react per scoprire le fonti, "
            "poi copia gli URL dal report nel campo 'Seed URL' del task auto_extract a valle."
        )
    elif extract_tasks and any(not t.seed_queries for t in extract_tasks):
        warnings.append("Non ho trovato URL nel brief: compila seed URL prima di lanciare lo scraping.")
    elif extract_tasks and not urls and any(t.seed_queries for t in extract_tasks):
        warnings.append("Seed URL sintetizzati dai domini citati nel brief: rivedili prima di lanciare.")

    run_after_create = autonomy_level in {"supervised", "autonomous"}
    if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks):
        run_after_create = False

    return OrchestratorPlan(
        title=_make_title(text),
        summary=_make_summary(tasks, edges, autonomy_level),
        autonomy_level=autonomy_level,
        risk_level="high" if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks) else "medium",
        assumptions=_make_assumptions(tasks, urls),
        warnings=warnings,
        tasks=tasks,
        edges=edges,
        run_after_create=run_after_create,
        planner_used="heuristic",
    )


async def build_plan(
    brief: str,
    autonomy_level: AutonomyLevel,
    provider: str = "ollama",
    model: str | None = None,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    use_llm: bool = False,
) -> OrchestratorPlan:
    fallback = build_heuristic_plan(brief, autonomy_level, provider, model, llm_base_url)
    if not use_llm:
        return fallback
    try:
        llm_plan = await _build_llm_plan(
            brief=brief,
            autonomy_level=autonomy_level,
            provider=provider,
            model=model or settings.default_model,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            fallback=fallback,
        )
        return llm_plan
    except Exception as e:
        fallback.planner_used = "heuristic_fallback"
        fallback.warnings.append(
            f"Planner LLM non riuscito ({type(e).__name__}: {str(e)[:180]}). Uso piano euristico."
        )
        return fallback


async def _build_llm_plan(
    *,
    brief: str,
    autonomy_level: AutonomyLevel,
    provider: str,
    model: str,
    llm_base_url: str | None,
    llm_api_key: str | None,
    fallback: OrchestratorPlan,
) -> OrchestratorPlan:
    base_url = resolve_base_url(provider, llm_base_url)
    api_key = resolve_api_key(provider, llm_api_key)
    prompt = _planner_prompt(brief, autonomy_level, fallback)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "Sei il Planner di Argos. Progetti task e workflow usando solo le modalita esistenti. "
                "Quando hai abbastanza contesto rispondi SOLO con il JSON del piano (niente prosa, niente markdown)."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 2200,
        "tools": PLANNER_TOOLS_SPEC,
        "tool_choice": "auto",
    }
    maybe_add_keep_alive(payload, base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url.rstrip('/')}/chat/completions"

    raw = ""
    tool_invocations = 0
    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(PLANNER_TOOL_MAX_LOOPS + 1):
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            message = data.get("choices", [{}])[0].get("message", {}) or {}
            tool_calls = message.get("tool_calls") or []
            content = (message.get("content") or "").strip()
            if not tool_calls:
                raw = content
                break
            normalized: list[dict[str, Any]] = []
            for call in tool_calls:
                cp = dict(call)
                cp.setdefault("id", f"call_{uuid.uuid4().hex[:8]}")
                normalized.append(cp)
            payload["messages"].append(
                {"role": "assistant", "content": content, "tool_calls": normalized}
            )
            for call in normalized:
                tool_name = (call.get("function") or {}).get("name") or ""
                args = _decode_planner_tool_args((call.get("function") or {}).get("arguments"))
                output = await _run_planner_tool(tool_name, args, provider=provider)
                payload["messages"].append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": tool_name,
                        "content": output[:8000],
                    }
                )
                tool_invocations += 1
        else:
            raise ValueError("planner LLM ha esaurito i loop tool senza JSON finale")

    if not raw:
        raise ValueError("planner LLM non ha prodotto JSON")
    parsed = _parse_json_object(raw)
    plan = _normalize_llm_plan(parsed, fallback, provider, model, llm_base_url)
    suffix = f" (tools={tool_invocations})" if tool_invocations else ""
    plan.planner_used = f"llm:{provider}/{model}{suffix}"
    return plan


def _decode_planner_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _run_planner_tool(name: str, args: dict[str, Any], *, provider: str) -> str:
    try:
        if name == "list_extraction_templates":
            return json.dumps(list_templates(), ensure_ascii=False, indent=2)
        if name == "list_models":
            target = (str(args.get("provider") or "").strip() or provider or "ollama")
            if target == "ollama":
                try:
                    models = await ollama_list_models()
                except Exception:
                    models = [settings.default_model]
            else:
                info = get_provider(target) or {}
                models = [m["id"] for m in (info.get("suggested_models") or [])]
            return json.dumps({"provider": target, "models": models}, ensure_ascii=False)
        if name == "list_tasks":
            limit = max(1, min(int(args.get("limit") or 20), 100))
            tasks = db.list_tasks()[:limit]
            slim = [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "agent_mode": t.get("agent_mode"),
                    "model": t.get("model"),
                    "status_tag": t.get("status_tag"),
                }
                for t in tasks
            ]
            return json.dumps(slim, ensure_ascii=False, indent=2)
        if name == "list_workflows":
            workflows = db.list_workflows()
            slim = [
                {"id": w.get("id"), "name": w.get("name"), "description": w.get("description")}
                for w in workflows
            ]
            return json.dumps(slim, ensure_ascii=False, indent=2)
        return f"Errore: tool sconosciuto {name!r}"
    except Exception as e:
        return f"Errore tool {name}: {type(e).__name__}: {e}"


def _planner_prompt(brief: str, autonomy_level: str, fallback: OrchestratorPlan) -> str:
    return (
        f"{_PLANNER_MANUAL}\n"
        f"{_PLANNER_FEW_SHOT}\n"
        "== Istruzioni per QUESTO brief ==\n"
        "Brief utente:\n"
        f"{brief}\n\n"
        f"Livello autonomia richiesto: {autonomy_level}\n\n"
        "Procedura:\n"
        "1. Se serve, chiama list_extraction_templates / list_models / list_tasks / list_workflows per validare le scelte.\n"
        "2. Produci un OrchestratorPlan in JSON valido (vedi formato negli esempi).\n"
        "3. Quando hai finito, rispondi SOLO con il JSON del piano (niente prosa).\n\n"
        "Piano euristico di partenza che puoi migliorare (non e obbligatorio seguirlo):\n"
        + fallback.model_dump_json(exclude={"planner_used"})
    )


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]+\}", raw or "")
    if not m:
        raise ValueError("LLM non ha restituito JSON")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON planner non e un oggetto")
    return obj


def _normalize_llm_plan(
    raw: dict[str, Any],
    fallback: OrchestratorPlan,
    provider: str,
    model: str,
    llm_base_url: str | None,
) -> OrchestratorPlan:
    tasks: list[PlannedTask] = []
    used: set[str] = set()
    fallback_by_key = {t.key: t for t in fallback.tasks}

    for idx, item in enumerate(raw.get("tasks") or []):
        if not isinstance(item, dict):
            continue
        mode = item.get("agent_mode") or item.get("mode") or "react"
        if mode not in AgentMode.__args__:  # type: ignore[attr-defined]
            continue
        key = _safe_key(str(item.get("key") or f"task_{idx+1}"), used)
        base = fallback_by_key.get(str(item.get("key") or ""))
        urls = [str(x).strip() for x in (item.get("seed_queries") or []) if str(x).strip()]
        domains = [str(x).strip().lower() for x in (item.get("allowed_domains") or []) if str(x).strip()]
        if base:
            data = base.model_dump()
            data.update(item)
            data["key"] = key
            data["agent_mode"] = mode
            data["seed_queries"] = urls or base.seed_queries
            data["allowed_domains"] = domains or base.allowed_domains
        else:
            data = _planned_task(
                key=key,
                name=str(item.get("name") or f"Task {idx + 1}"),
                agent_mode=mode,
                objective=str(item.get("objective") or fallback.summary),
                brief=fallback.summary,
                urls=urls,
                domains=domains,
                provider=provider,
                model=model,
                llm_base_url=llm_base_url,
            ).model_dump()
            data.update(item)
            data["key"] = key
            data["agent_mode"] = mode

        if data.get("extraction_template") and not data.get("extraction_schema"):
            data["extraction_schema"] = get_schema(data["extraction_template"])
        data["model"] = data.get("model") or _default_model_for_mode(mode, provider, model)
        data["llm_provider"] = "ollama" if mode == "react" else (data.get("llm_provider") or provider)
        if data["llm_provider"] != "custom":
            data["llm_base_url"] = None
        tasks.append(PlannedTask(**data))

    if not tasks:
        tasks = fallback.tasks

    valid_keys = {t.key for t in tasks}
    edges: list[PlannedEdge] = []
    for e in raw.get("edges") or []:
        if not isinstance(e, dict):
            continue
        from_key = str(e.get("from_key") or "")
        to_key = str(e.get("to_key") or "")
        if from_key in valid_keys and to_key in valid_keys and from_key != to_key:
            edges.append(
                PlannedEdge(
                    from_key=from_key,
                    to_key=to_key,
                    pass_artifact=e.get("pass_artifact") or None,
                )
            )
    if len(tasks) > 1 and not edges:
        edges = [
            PlannedEdge(
                from_key=tasks[i].key,
                to_key=tasks[i + 1].key,
                pass_artifact=_edge_artifact(tasks[i].agent_mode, tasks[i + 1].agent_mode),
            )
            for i in range(len(tasks) - 1)
        ]

    risk = raw.get("risk_level")
    if risk not in {"low", "medium", "high"}:
        risk = "high" if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks) else fallback.risk_level

    warnings = [str(x) for x in (raw.get("warnings") or []) if str(x).strip()]
    if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks) and not warnings:
        warnings.append("Il piano contiene messaggistica: richiedi consenso prima di lanciare.")

    tasks, warnings = _validate_llm_plan(tasks, edges, warnings)

    return OrchestratorPlan(
        title=str(raw.get("title") or fallback.title)[:120],
        summary=str(raw.get("summary") or fallback.summary),
        autonomy_level=fallback.autonomy_level,
        risk_level=risk,
        assumptions=[str(x) for x in (raw.get("assumptions") or fallback.assumptions)],
        warnings=warnings or fallback.warnings,
        tasks=tasks,
        edges=edges,
        run_after_create=bool(raw.get("run_after_create", fallback.run_after_create)),
        planner_used="llm",
    )


def _validate_llm_plan(
    tasks: list[PlannedTask],
    edges: list[PlannedEdge],
    warnings: list[str],
) -> tuple[list[PlannedTask], list[str]]:
    out_warnings = list(warnings)
    seen = {w.strip() for w in out_warnings if isinstance(w, str)}

    def warn(msg: str) -> None:
        if msg in seen:
            return
        out_warnings.append(msg)
        seen.add(msg)

    tasks_by_key = {t.key: t for t in tasks}

    for t in tasks:
        if t.agent_mode in {"bulk_extract", "auto_extract", "browser_use"} and not t.seed_queries:
            warn(f"Task '{t.key}' ({t.agent_mode}): manca seed URL, da completare prima del lancio.")

    for t in tasks:
        if t.agent_mode == "outreach":
            upstream_qualifier = any(
                e.to_key == t.key
                and (tasks_by_key.get(e.from_key) and tasks_by_key[e.from_key].agent_mode == "qualifier")
                for e in edges
            )
            if not upstream_qualifier:
                warn(
                    f"Task '{t.key}' (outreach): nessun qualifier upstream, rischio invio a contatti non filtrati."
                )

    valid_templates = {tpl["key"] for tpl in list_templates()}
    fixed: list[PlannedTask] = []
    for t in tasks:
        if t.extraction_template and t.extraction_template not in valid_templates:
            warn(
                f"Task '{t.key}': extraction_template '{t.extraction_template}' sconosciuto, rimosso."
            )
            data = t.model_dump()
            data["extraction_template"] = None
            data["extraction_schema"] = None
            fixed.append(PlannedTask(**data))
        else:
            fixed.append(t)
    return fixed, out_warnings


def execute_plan(
    plan: OrchestratorPlan,
    *,
    run_now: bool = False,
    confirm_risky: bool = False,
) -> ExecutionResult:
    meta = autonomy_meta(plan.autonomy_level)
    if not meta["can_create"]:
        raise ValueError("Questo livello di autonomia non consente modifiche al progetto.")
    if run_now and not meta["can_run"]:
        raise ValueError("Questo livello di autonomia consente la creazione, ma non il lancio dei job.")
    if run_now and plan.has_risky_modes and not confirm_risky:
        raise ValueError(
            "Il piano contiene outreach/responder: conferma esplicitamente la messaggistica prima del lancio."
        )

    created: dict[str, int] = {}
    for task in plan.tasks:
        data = _task_to_db_payload(task)
        created[task.key] = db.create_task(data)

    workflow_id: int | None = None
    workflow_run_id: int | None = None
    started_jobs: list[int] = []

    if plan.edges:
        workflow_id = db.create_workflow(
            plan.title,
            f"Creato da Orchestrator. Autonomy={plan.autonomy_level}. {plan.summary[:400]}",
        )
        for edge in plan.edges:
            from_id = created.get(edge.from_key)
            to_id = created.get(edge.to_key)
            if not from_id or not to_id:
                continue
            db.create_edge(
                from_task_id=from_id,
                to_task_id=to_id,
                workflow_id=workflow_id,
                pass_artifact=edge.pass_artifact,
                enabled=True,
            )
        if run_now:
            result = jobs.start_workflow(workflow_id)
            workflow_run_id = int(result["workflow_run_id"])
            started_jobs = [int(j) for j in result.get("started_jobs") or []]
    elif created:
        task_id = next(iter(created.values()))
        if run_now:
            started_jobs = [jobs.start_job(task_id)]

    jobs.reload_schedules()

    if workflow_id:
        redirect_url = f"/workflows/{workflow_id}"
        message = f"Creato workflow #{workflow_id} con {len(created)} task."
    else:
        only_id = next(iter(created.values())) if created else 0
        redirect_url = f"/tasks/{only_id}" if only_id else "/"
        message = f"Creato task #{only_id}."
    if started_jobs:
        message += f" Avviati job: {', '.join(str(x) for x in started_jobs)}."

    return ExecutionResult(
        created_task_ids=created,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        started_jobs=started_jobs,
        redirect_url=redirect_url,
        message=message,
    )


def _task_to_db_payload(task: PlannedTask) -> dict[str, Any]:
    return {
        "name": task.name,
        "description": task.description,
        "objective": task.objective,
        "seed_queries": task.seed_queries,
        "allowed_domains": task.allowed_domains,
        "blocked_domains": task.blocked_domains,
        "max_iterations": task.max_iterations,
        "model": task.model,
        "output_format": task.output_format,
        "cron": None,
        "agent_mode": task.agent_mode,
        "extraction_template": task.extraction_template,
        "extraction_schema": task.extraction_schema,
        "llm_provider": task.llm_provider,
        "llm_base_url": task.llm_base_url,
        "llm_api_key": None,
        "input_artifact_path": task.input_artifact_path,
        "message_template": task.message_template,
        "message_subject": task.message_subject,
        "message_channels": task.message_channels,
        "responder_system_prompt": task.responder_system_prompt,
        "bulk_concurrency": task.bulk_concurrency,
        "target_cap_per_site": task.target_cap_per_site,
        "bulk_rate_limit_per_sec": task.bulk_rate_limit_per_sec,
        "bulk_extraction_method": "llm_per_page",
        "bulk_css_selectors": None,
        "crawler_enabled": task.crawler_enabled,
        "crawler_url_pattern": None,
        "crawler_max_depth": task.crawler_max_depth,
        "discovery_llm_provider": None,
        "discovery_llm_model": None,
        "discovery_llm_api_key": None,
        "max_discovery_retries": 3,
        "browser_llm_provider": None,
        "browser_llm_model": None,
        "browser_llm_api_key": None,
        "rating": None,
        "notes": task.notes,
        "status_tag": task.status_tag,
        # Audience selection (outreach_*)
        "target_contact_ids": list(task.target_contact_ids),
        "target_asset_ids": list(task.target_asset_ids),
        "outreach_filter_source_task_id": task.outreach_filter_source_task_id,
        "outreach_filter_source_follower_of": task.outreach_filter_source_follower_of,
        "outreach_filter_tags": list(task.outreach_filter_tags),
        "input_asset_filter": task.input_asset_filter,
        "output_asset_type": task.output_asset_type,
        # outreach_social
        "social_platform": task.social_platform,
        "social_account_id": task.social_account_id,
        "outreach_intent": task.outreach_intent,
        "message_template_variants": task.message_template_variants,
        "max_dms_per_run": task.max_dms_per_run,
        "max_dms_per_session": task.max_dms_per_session,
        "headed": task.headed,
        "gap_between_dms_min": task.gap_between_dms_min,
        "gap_between_dms_max": task.gap_between_dms_max,
        # outreach_whatsapp
        "whatsapp_engine_preference": task.whatsapp_engine_preference,
        "whatsapp_dry_run": task.whatsapp_dry_run,
        "whatsapp_account_id": task.whatsapp_account_id,
        "whatsapp_api_config_id": task.whatsapp_api_config_id,
        # recon_social
        "recon_mode": task.recon_mode,
        "recon_social_account_id": task.recon_social_account_id,
        "recon_hypothesis": task.recon_hypothesis,
        "recon_max_targets_per_day": task.recon_max_targets_per_day,
        "recon_score_threshold": task.recon_score_threshold,
        "seed_queries_friends": list(task.seed_queries_friends),
        "speed_profile": task.speed_profile,
    }


def _make_title(brief: str) -> str:
    cleaned = re.sub(r"\s+", " ", brief).strip()
    if not cleaned:
        return "Piano orchestrator"
    return (cleaned[:70] + ("..." if len(cleaned) > 70 else "")).capitalize()


def _make_summary(tasks: list[PlannedTask], edges: list[PlannedEdge], autonomy_level: str) -> str:
    modes = " -> ".join(t.agent_mode for t in tasks)
    if edges:
        return f"Creo una pipeline {modes} con passaggio artifact automatico. Autonomia: {autonomy_level}."
    return f"Creo un task {tasks[0].agent_mode if tasks else 'react'}. Autonomia: {autonomy_level}."


def _make_assumptions(tasks: list[PlannedTask], urls: list[str]) -> list[str]:
    out = []
    if any(t.agent_mode in {"bulk_extract", "auto_extract", "browser_use"} for t in tasks):
        out.append("Lo scraping parte dagli URL trovati nel brief; puoi modificarli nel task prima del lancio.")
        out.append("Lo schema iniziale usa il template profile_contacts se il brief parla di lead/contatti.")
    if not urls and any(t.agent_mode in {"bulk_extract", "auto_extract", "browser_use"} for t in tasks):
        out.append("Non sono stati rilevati URL: il task andra completato con seed URL.")
    if any(t.agent_mode == "outreach" for t in tasks):
        out.append("I canali email/Telegram devono essere configurati in Settings prima dell'esecuzione.")
    return out
