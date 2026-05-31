"""Primitive per la compilazione assistita di form su portali web.

Tre responsabilita', tenute separate per testabilita':

1. Sessione persistente del portale (storage_state Playwright su disco), in modo
   che il login si faccia UNA volta a mano e poi si riusi. Nessuna password nel DB:
   stesso modello delle sessioni social (data/portal_sessions/<key>.json).

2. `locate(page, field)` — trova l'elemento di un campo del form. Strategia ibrida
   auto-riparante: prova il selettore registrato; se non matcha (0 elementi o
   elemento non visibile) ritorna miss, cosi' il chiamante puo' invocare llm_remap.

3. `llm_remap(...)` — provider-agnostico (Ollama locale o modello remoto), stessa
   firma di runner_recon_social._llm_fill_schema: dato un riassunto del DOM dei campi
   compilabili + l'etichetta semantica cercata, l'LLM sceglie il selettore giusto.

`fill_form(page, fields, row_values, *, llm_cfg)` orchestra il tutto su una riga.
Riusa humanize.human_type/human_click per l'input "umano".
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...config import DATA_DIR
from ..social.humanize import human_click, human_type

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

PORTAL_SESSIONS_DIR = DATA_DIR / "portal_sessions"
PORTAL_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Sessione persistente del portale (storage_state Playwright)
# ---------------------------------------------------------------------------

def _safe_key(session_key: str | None) -> str:
    return "".join(c for c in (session_key or "") if c.isalnum() or c in "-_") or "default"


def portal_session_dir(session_key: str | None) -> Path:
    """Directory user_data_dir per il persistent context di una sessione portale.

    Si preferisce il persistent context (vs storage_state file) perche' molti
    portali usano IndexedDB / flussi SSO che lo storage_state non cattura — stesso
    motivo per cui recon_social usa launch_persistent_context. Il login manuale
    fatto una volta resta valido in questa dir finche' il portale non lo invalida.
    """
    return PORTAL_SESSIONS_DIR / _safe_key(session_key)


def portal_session_path(session_key: str) -> Path:
    """Path al file storage_state di una sessione portale (sanitizzato).

    Variante file-based (cookies + localStorage). Usata da chi preferisce lo
    storage_state al persistent context; il runner usa portal_session_dir.
    """
    return PORTAL_SESSIONS_DIR / f"{_safe_key(session_key)}.json"


def load_portal_session(session_key: str | None) -> dict | None:
    """Carica lo storage_state della sessione. None se assente/corrotto."""
    if not session_key:
        return None
    p = portal_session_path(session_key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("portal session corrupt for %s: %s", session_key, e)
        return None


async def save_portal_session(context, session_key: str) -> Path:
    """Salva lo storage_state del context per la sessione `session_key`."""
    p = portal_session_path(session_key)
    state = await context.storage_state()
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except (OSError, NotImplementedError):
        pass
    return p


# ---------------------------------------------------------------------------
# Modello di un campo della macro
# ---------------------------------------------------------------------------

# Le 4 fasi di una macro a processo (vedi runner loop). Ordine canonico.
PHASES = ("warmup", "activity", "return", "closing")


@dataclass
class MacroField:
    """Uno step della macro (campo da compilare O azione di navigazione).

    - selector: selettore registrato (CSS, o nome ruolo/testo a seconda di strategy)
    - semantic_label: etichetta umana usata dall'LLM per il fallback ("Partita IVA")
    - source: "column" (prendi il valore dalla colonna del foglio) | "const"
    - column_name / const_value: a seconda di source (solo per action=fill)
    - strategy: "css" | "role" | "text" — come interpretare `selector`
    - action: "fill" (input/textarea) | "click" (link/bottone/checkbox) | "submit"
    - phase: "warmup" | "activity" | "return" | "closing" — quando eseguire lo step
    """
    selector: str
    semantic_label: str = ""
    source: str = "column"
    column_name: str = ""
    const_value: str = ""
    strategy: str = "css"
    action: str = "fill"
    phase: str = "activity"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MacroField":
        phase = str(d.get("phase") or "activity").strip().lower()
        if phase not in PHASES:
            phase = "activity"
        return cls(
            selector=str(d.get("selector") or ""),
            semantic_label=str(d.get("semantic_label") or d.get("label") or ""),
            source=str(d.get("source") or "column"),
            column_name=str(d.get("column_name") or d.get("column") or ""),
            const_value=str(d.get("const_value") or ""),
            strategy=str(d.get("strategy") or "css"),
            action=str(d.get("action") or "fill"),
            phase=phase,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "semantic_label": self.semantic_label,
            "source": self.source,
            "column_name": self.column_name,
            "const_value": self.const_value,
            "strategy": self.strategy,
            "action": self.action,
            "phase": self.phase,
        }

    def value_for(self, row: dict[str, str] | None) -> str:
        """Il valore da scrivere per questo step. `row` può essere None (fasi
        warmup/closing senza riga): in tal caso una source=column resta vuota."""
        if self.source == "const":
            return self.const_value
        if not row:
            return ""
        return str(row.get(self.column_name, ""))


@dataclass
class FieldResult:
    """Esito della compilazione di un singolo campo."""
    label: str
    ok: bool
    detail: str = ""
    remapped_selector: str | None = None  # valorizzato se l'LLM ha ri-mappato


@dataclass
class FillResult:
    """Esito complessivo della compilazione di una riga."""
    fields: list[FieldResult] = field(default_factory=list)
    challenged: bool = False  # captcha/2FA rilevato → niente retry
    macro_updated: bool = False  # almeno un selettore e' stato ri-mappato

    @property
    def ok(self) -> bool:
        return not self.challenged and all(f.ok for f in self.fields)


# ---------------------------------------------------------------------------
# Locate: selettore registrato → fallback miss
# ---------------------------------------------------------------------------

def _build_locator(page: "Page", selector: str, strategy: str):
    """Costruisce un Locator Playwright dalla coppia (selector, strategy)."""
    if strategy == "role":
        # selector = "role:name" oppure solo "role"
        if ":" in selector:
            role, name = selector.split(":", 1)
            return page.get_by_role(role.strip(), name=name.strip())
        return page.get_by_role(selector.strip())
    if strategy == "text":
        return page.get_by_text(selector, exact=False)
    # default CSS / XPath (Playwright accetta entrambi in page.locator)
    return page.locator(selector)


async def locate(page: "Page", field: MacroField):
    """Ritorna (locator | None). None se 0 match o elemento non visibile.

    Non solleva: un selettore stantio e' un evento atteso (porta al fallback LLM).
    """
    if not field.selector:
        return None
    try:
        loc = _build_locator(page, field.selector, field.strategy).first
        count = await loc.count()
        if count == 0:
            return None
        if not await loc.is_visible():
            return None
        return loc
    except Exception as e:
        log.debug("locate fail for %r: %s", field.selector, e)
        return None


# ---------------------------------------------------------------------------
# DOM summary: estrae i campi compilabili per darli in pasto all'LLM
# ---------------------------------------------------------------------------

_DOM_FIELDS_JS = r"""
() => {
  const out = [];
  const els = document.querySelectorAll('input, textarea, select');
  for (const el of els) {
    const t = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue; // non visibile
    let label = '';
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) label = lab.textContent.trim();
    }
    if (!label && el.closest('label')) label = el.closest('label').textContent.trim();
    out.push({
      tag: el.tagName.toLowerCase(),
      type: t,
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      label: label.slice(0, 80),
    });
  }
  return out.slice(0, 60);
}
"""


async def dom_field_summary(page: "Page") -> list[dict[str, Any]]:
    """Lista compatta dei campi compilabili visibili nel DOM corrente."""
    try:
        return await page.evaluate(_DOM_FIELDS_JS)
    except Exception as e:
        log.debug("dom_field_summary fail: %s", e)
        return []


def _css_for_dom_field(f: dict[str, Any]) -> str | None:
    """Selettore CSS stabile per un campo del DOM summary (preferenza id>name)."""
    if f.get("id"):
        return f"#{f['id']}"
    if f.get("name"):
        tag = f.get("tag") or "input"
        return f"{tag}[name=\"{f['name']}\"]"
    return None


# ---------------------------------------------------------------------------
# llm_remap: l'LLM sceglie il campo giusto quando il selettore registrato fallisce
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "qwen3-coder:30b"
    enabled: bool = True


def _is_ollama(base_url: str) -> bool:
    return "11434" in base_url or (
        "/v1" in base_url
        and "openai.com" not in base_url
        and "anthropic.com" not in base_url
    )


async def llm_remap(dom_fields: list[dict[str, Any]], semantic_label: str,
                    *, cfg: LLMConfig) -> str | None:
    """Chiede all'LLM quale campo del DOM corrisponde a `semantic_label`.

    Ritorna un selettore CSS (ricavato da id/name del campo scelto) o None.
    Provider-agnostico: stessa logica di chiamata di
    runner_recon_social._llm_fill_schema (Ollama o remoto via base_url/api_key).
    """
    if not cfg.enabled or not dom_fields or not semantic_label:
        return None
    import httpx

    # Indicizza i campi: l'LLM sceglie per indice, noi ricostruiamo il selettore.
    compact = [
        {
            "i": i,
            "type": f.get("type"),
            "name": f.get("name"),
            "id": f.get("id"),
            "placeholder": f.get("placeholder"),
            "aria": f.get("aria"),
            "label": f.get("label"),
        }
        for i, f in enumerate(dom_fields)
    ]
    payload = {
        "model": cfg.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei un parser deterministico di form HTML. Ricevi (1) una "
                    "lista di campi di un form (con indice `i`) e (2) un'etichetta "
                    "semantica del dato da inserire. Scegli il campo che meglio "
                    "corrisponde all'etichetta. Rispondi SOLO con JSON "
                    '{"index": <numero>} oppure {"index": null} se nessun campo '
                    "corrisponde. Niente prosa."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"CAMPI:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
                    f"ETICHETTA: {semantic_label}\n\nRitorna il JSON ora."
                ),
            },
        ],
        "temperature": 0.0,
        "max_tokens": 60,
        "response_format": {"type": "json_object"},
    }
    if _is_ollama(cfg.base_url):
        payload["format"] = "json"

    raw = ""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {cfg.api_key}"},
            )
            r.raise_for_status()
            data = r.json()
            raw = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        log.warning("llm_remap HTTP fail: %s", e)
        return None

    idx = _parse_index(raw)
    if idx is None or not (0 <= idx < len(dom_fields)):
        return None
    return _css_for_dom_field(dom_fields[idx])


def _parse_index(raw: str) -> int | None:
    """Estrae l'indice dal JSON di risposta dell'LLM (tollerante <think>...)."""
    if not raw:
        return None
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    for candidate in (cleaned, raw):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and obj.get("index") is not None:
                return int(obj["index"])
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    m = re.search(r'"index"\s*:\s*(\d+)', cleaned)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Captcha / blocco: rilevamento best-effort per non ritentare all'infinito
# ---------------------------------------------------------------------------

_CHALLENGE_HINTS = (
    "recaptcha", "g-recaptcha", "h-captcha", "hcaptcha", "cf-turnstile",
    "captcha", "verify you are human", "verifica di non essere un robot",
)


async def detect_challenge(page: "Page") -> bool:
    """True se la pagina sembra mostrare un captcha / verifica anti-bot."""
    try:
        html = (await page.content()).lower()
    except Exception:
        return False
    return any(h in html for h in _CHALLENGE_HINTS)


# ---------------------------------------------------------------------------
# fill_form: orchestrazione su una riga
# ---------------------------------------------------------------------------

async def fill_form(page: "Page", fields: list[MacroField], row: dict[str, str],
                    *, llm_cfg: LLMConfig, speed_profile: str = "safe") -> FillResult:
    """Compila i campi del form per una riga. Auto-riparante via llm_remap.

    Non invia il form (il submit e' deciso dal runner in base a auto_submit).
    """
    result = FillResult()

    if await detect_challenge(page):
        result.challenged = True
        return result

    dom_fields: list[dict[str, Any]] | None = None  # lazy: solo se serve il remap

    for f in fields:
        label = f.semantic_label or f.column_name or f.selector
        value = f.value_for(row)
        if f.action == "fill" and value == "":
            # niente da scrivere per questo campo su questa riga
            result.fields.append(FieldResult(label=label, ok=True, detail="vuoto, saltato"))
            continue

        loc = await locate(page, f)
        remapped: str | None = None

        if loc is None and llm_cfg.enabled:
            if dom_fields is None:
                dom_fields = await dom_field_summary(page)
            new_selector = await llm_remap(dom_fields, f.semantic_label or f.column_name, cfg=llm_cfg)
            if new_selector:
                remapped = new_selector
                f.selector = new_selector
                f.strategy = "css"
                result.macro_updated = True
                loc = await locate(page, f)

        if loc is None:
            result.fields.append(FieldResult(
                label=label, ok=False,
                detail="campo non trovato (selettore stantio, remap fallito)",
                remapped_selector=remapped,
            ))
            continue

        try:
            # Selettore concreto per le helper humanize (che prendono una stringa).
            sel = f.selector if f.strategy == "css" else None
            if f.action == "click":
                if sel:
                    await human_click(page, sel)
                else:
                    await loc.click()
            else:
                if sel:
                    await human_type(page, sel, value, profile=speed_profile)
                else:
                    await loc.fill(value)
            result.fields.append(FieldResult(
                label=label, ok=True, remapped_selector=remapped,
            ))
        except Exception as e:
            result.fields.append(FieldResult(
                label=label, ok=False, detail=f"errore input: {e}",
                remapped_selector=remapped,
            ))

    return result


# ---------------------------------------------------------------------------
# run_steps: esegue una SEQUENZA di step (azioni miste) — base del runner a fasi
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    label: str
    action: str
    ok: bool
    detail: str = ""
    remapped_selector: str | None = None


@dataclass
class StepsResult:
    steps: list[StepResult] = field(default_factory=list)
    challenged: bool = False
    macro_updated: bool = False

    @property
    def ok(self) -> bool:
        return not self.challenged and all(s.ok for s in self.steps)


async def _settle_after_nav(page: "Page") -> None:
    """Dopo un click che può navigare/caricare, attende che la pagina sia stabile.
    Best-effort: non solleva se la pagina non naviga (click in-page)."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass


async def run_steps(page: "Page", steps: list[MacroField], row: dict[str, str] | None,
                    *, llm_cfg: LLMConfig, speed_profile: str = "safe") -> StepsResult:
    """Esegue una sequenza ordinata di step (fill / click / submit) su `page`.

    - `row` può essere None per le fasi senza riga (warmup/closing): gli step
      `fill` con source=column risultano vuoti e vengono saltati.
    - Auto-riparante: se `locate` fallisce su un fill, prova `llm_remap` (come
      `fill_form`). Per i `click`/`submit` di navigazione il remap non si applica
      (i selettori di link/bottoni sono catturati dal recorder, non inferiti).
    - Attesa: `locate` aspetta già visibilità; dopo un click si attende il load.

    Ritorna StepsResult con l'esito per-step. Si ferma alla prima `challenged`.
    """
    result = StepsResult()
    dom_fields: list[dict[str, Any]] | None = None

    for s in steps:
        label = s.semantic_label or s.column_name or s.selector or s.action

        if await detect_challenge(page):
            result.challenged = True
            return result

        value = s.value_for(row)
        if s.action == "fill" and value == "":
            result.steps.append(StepResult(label=label, action=s.action, ok=True, detail="vuoto, saltato"))
            continue

        loc = await locate(page, s)
        remapped: str | None = None

        # Auto-heal solo per i fill (i click di navigazione hanno selettori registrati).
        if loc is None and s.action == "fill" and llm_cfg.enabled:
            if dom_fields is None:
                dom_fields = await dom_field_summary(page)
            new_selector = await llm_remap(dom_fields, s.semantic_label or s.column_name, cfg=llm_cfg)
            if new_selector:
                remapped = new_selector
                s.selector = new_selector
                s.strategy = "css"
                result.macro_updated = True
                loc = await locate(page, s)

        if loc is None:
            result.steps.append(StepResult(
                label=label, action=s.action, ok=False,
                detail="elemento non trovato (selettore stantio)",
                remapped_selector=remapped,
            ))
            # Per i fill continuiamo (campo opzionale); per click/submit fermarsi
            # ha più senso (il percorso si è rotto), ma lasciamo decidere al runner
            # in base a StepsResult.ok. Qui proseguiamo per raccogliere tutti gli esiti.
            continue

        try:
            sel = s.selector if s.strategy == "css" else None
            if s.action in ("click", "submit"):
                if sel:
                    await human_click(page, sel)
                else:
                    await loc.click()
                await _settle_after_nav(page)
            else:  # fill
                if sel:
                    await human_type(page, sel, value, profile=speed_profile)
                else:
                    await loc.fill(value)
            result.steps.append(StepResult(label=label, action=s.action, ok=True, remapped_selector=remapped))
        except Exception as e:
            result.steps.append(StepResult(
                label=label, action=s.action, ok=False,
                detail=f"errore {s.action}: {e}", remapped_selector=remapped,
            ))

    return result
