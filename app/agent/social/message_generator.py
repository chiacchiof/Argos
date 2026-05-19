"""Generatore di messaggi DM personalizzati via LLM.

Razionale: Instagram/TikTok detectano "mass DM" via Levenshtein distance fra
messaggi recenti dello stesso account. Template fissi tipo "Ciao {nome}, sono
interessato al tuo profilo, contattami..." vengono flaggati immediatamente.

Soluzione: per ogni target generiamo un messaggio UNICO via LLM, partendo
dalle info estratte del profilo (display_name, bio, post recenti, ecc.).

Architettura:
- Input: contact dict (display_name, source_url, raw_json con bio/foto/tags), template_intent (es. "outreach per ottimizzazione contenuti")
- LLM: Qwen 30b locale (gratis) di default, gpt-4o-mini come fallback
- Output: messaggio plain text ~80-200 caratteri, ITALIANO o ENGLISH a seconda del profilo

Costo Qwen locale: $0. Velocita': ~5-8s/messaggio su RTX 4090.
Costo gpt-4o-mini: ~$0.002/messaggio. Velocita': ~2s.
"""
from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..ollama import ollama_keep_alive

log = logging.getLogger(__name__)


# Etichette/heading che il modello a volte "rilasca" quando non rispetta il
# vincolo "solo testo del messaggio". Match case-insensitive su sottostringa.
# Coprire IT + EN perché i thinking models (qwen3.x, gpt-oss, deepseek-r1)
# tendono a ragionare in inglese anche con prompt italiano.
_INSTRUCTION_LEAK_KEYWORDS: tuple[str, ...] = (
    # IT
    "analizza", "analisi della richiesta", "obiettivo", "compito",
    "ruolo:", "target:", "piattaforma:", "informazioni profilo",
    "informazioni del profilo", "vincoli:", "stile:", "tono:",
    "passo 1", "passo 2", "passo 3", "fase 1", "fase 2",
    "lingua:", "lunghezza:", "max caratteri", "output:", "input:",
    "istruzioni:", "esempi:", "constraints:", "obiettivo dell'outreach",
    # EN
    "analyze the request", "analyze the prompt", "role:", "task:",
    "target:", "platform:", "profile info", "profile information",
    "step 1", "step 2", "step 3", "step-by-step", "let's think",
    "let me think", "first, ", "first,\n", "language:", "tone:",
    "constraints:", "output:", "instructions:", "context:", "audience:",
    "italian copywriter", "specialized in", "dm message for",
    "as instructed", "as requested", "your task is", "according to",
)


def _looks_like_instructions(text: str) -> tuple[bool, str | None]:
    """Riconosce output dove l'LLM ha rilasciato il proprio ragionamento /
    le istruzioni del prompt invece del messaggio finito.

    Trigger comuni dai thinking models (qwen3.x, gpt-oss, deepseek-r1):
      - liste markdown con `**Label:**` o `* *Label:*` (3+ run)
      - numerazione di step `1.`, `2.`, ...
      - keyword esplicite ("Analyze the Request", "Role:", "Target:")
      - prefisso meta ("Step 1:", "OUTPUT:", "Task:")

    Tornare (False, None) per output "puliti" che assomigliano a un messaggio
    umano genuino. Non perfetto — favoriamo i falsi negativi rari ai falsi
    positivi che bloccherebbero messaggi validi (es. un DM che cita "1." o
    contiene una breve frase tipo "ruolo importante").
    """
    if not text:
        return False, None
    t = text.strip()
    if not t:
        return False, None
    lower = t.lower()

    # 1) Run di `**Label:**` (markdown bold + colon). 3+ è cattivo segno: i
    #    DM umani usano al massimo un grassetto enfatico, mai 3+ etichette.
    label_runs = re.findall(r"\*\*[^*\n]{1,40}?\*\*\s*:", t)
    if len(label_runs) >= 3:
        return True, f"markdown_label_runs={len(label_runs)}"

    # 2) Bullet con singolo `*Label:*` (markdown italic + colon).
    star_label = re.findall(r"^\s*\*\s*\*[^*\n]{1,40}?\*\s*:", t, re.MULTILINE)
    if len(star_label) >= 3:
        return True, f"star_label_runs={len(star_label)}"

    # 3) Multipla numerazione step: 2+ righe con "1.", "2.", ...
    numbered = re.findall(r"^\s*\d+[\.\)]\s+\S", t, re.MULTILINE)
    if len(numbered) >= 2:
        return True, f"numbered_steps={len(numbered)}"

    # 4) Keyword red-flag: 2+ hit fra le tipiche etichette meta.
    keyword_hits = sum(1 for kw in _INSTRUCTION_LEAK_KEYWORDS if kw in lower)
    if keyword_hits >= 2:
        return True, f"red_flag_keywords={keyword_hits}"

    # 5) Prefisso meta esplicito alla prima riga.
    first_line = t.split("\n", 1)[0].strip().lower()
    if re.match(
        r"^(step\s*\d|output|input|task|role|target|platform|profile\s*info|"
        r"ruolo|compito|piattaforma|obiettivo|vincoli|istruzioni|esempi)\s*:",
        first_line,
    ):
        return True, "leading_meta_label"

    return False, None


def _is_ollama_endpoint(base_url: str) -> bool:
    """Heuristic per riconoscere endpoint Ollama (OpenAI-compat su :11434 o
    explicit 'ollama' nel path). Solo su Ollama possiamo passare `keep_alive`
    in payload — provider cloud (OpenAI, anthropic, ecc.) rifiuterebbero il
    campo extra con 400."""
    u = (base_url or "").lower()
    return ("11434" in u) or ("ollama" in u)


# Diverse "personalita'" per variare ulteriormente lo stile del messaggio.
# Il LLM riceve random uno di questi per produrre varianti naturali.
_TONE_VARIANTS: tuple[str, ...] = (
    "amichevole e diretto",
    "professionale ma caldo",
    "casual e curioso",
    "complimentoso e specifico (riferendoti a qualcosa che hai notato dal loro profilo)",
)


@dataclass
class MessageRequest:
    """Input per la generazione di un messaggio.

    `template_variants` sono esempi dell'utente per dare stile/tone all'LLM.
    Ognuno e' un messaggio "esempio buono", l'LLM ne prende ispirazione (NON
    copia letterale). Lasciare lista vuota = LLM genera da zero solo con `intent`.
    """
    target_display_name: str
    target_username: str
    target_platform: str  # "instagram" | "tiktok"
    target_profile_url: str
    target_raw_data: dict[str, Any]  # contenuto raw_json del contact
    intent: str  # frase user-provided che spiega lo scopo dell'outreach
    template_variants: list[str] | None = None  # esempi-style dell'utente (campo task)
    max_chars: int = 200
    language: str = "it"  # "it" | "en" — auto-detect dal raw_data se possibile


def _build_user_prompt(req: MessageRequest, tone: str) -> str:
    bio_snippet = ""
    if isinstance(req.target_raw_data, dict):
        for k in ("estratto", "bio", "meta_description"):
            v = req.target_raw_data.get(k)
            if isinstance(v, str) and v.strip():
                bio_snippet = v.strip()[:400]
                break

    # Esempi user-provided (campo task `message_template_variants`).
    # L'LLM li usa come "stile" / "tono" da imitare — NON come template letterale.
    examples_block = ""
    if req.template_variants:
        examples = [v.strip() for v in req.template_variants if v.strip()]
        if examples:
            examples_block = (
                "\n\nESEMPI DI STILE FORNITI DALL'UTENTE (ispirati, NON copiare letteralmente):\n"
                + "\n".join(f"  • {e}" for e in examples[:5])
            )

    return f"""Devi scrivere UN messaggio DM breve e umano per outreach commerciale.

TARGET:
- Nome: {req.target_display_name}
- Username: {req.target_username}
- Piattaforma: {req.target_platform}
- URL profilo: {req.target_profile_url}
{f'- Bio/Estratto: "{bio_snippet}"' if bio_snippet else '- (nessuna bio disponibile)'}

OBIETTIVO DELL'OUTREACH (definito dall'utente):
{req.intent}

STILE: {tone}
{examples_block}

VINCOLI ASSOLUTI:
- Lingua: {req.language} ({'italiano' if req.language == 'it' else 'english'})
- Massimo {req.max_chars} caratteri
- DEVE essere UN messaggio unico, naturale, NON sembrare template/copia-incollato
- NON usare frasi banali tipo "Ciao bellezza", "ti seguo da tempo"
- NON includere link, URL, email, "DM me", "check link in bio"
- Riferimento specifico se possibile (al nome, alla bio, al tipo di contenuto del profilo)
- NO emoji eccessive (max 1)
- Aprire con saluto naturale, chiudere con domanda aperta o invito leggero
- Se ESEMPI sono forniti sopra, segui IL TONO e LA LUNGHEZZA media ma cambia parole e struttura

OUTPUT (CRITICAL): la risposta deve essere ESCLUSIVAMENTE il testo del DM finito,
pronto da inviare. Niente analisi, niente "Step 1/2/3", niente bullet markdown,
niente etichette come "Role:", "Target:", "Platform:", "Profile Info:".
Niente "Messaggio:" come prefisso. Niente preamboli, niente commenti, niente
spiegazioni di cosa stai facendo. Se il destinatario leggesse la tua risposta
COSI' COME E', deve sembrargli un messaggio umano normale, non un'istruzione."""


async def generate_message(
    req: MessageRequest,
    *,
    llm_base_url: str = "http://localhost:11434/v1",
    llm_api_key: str = "",
    llm_model: str = "qwen3-coder:30b",
    timeout: int = 120,
) -> str:
    """Genera un messaggio DM personalizzato via LLM.

    Ritorna stringa pulita (no markdown, no quote, max max_chars).
    """
    tone = random.choice(_TONE_VARIANTS)
    user_prompt = _build_user_prompt(req, tone)

    payload: dict[str, Any] = {
        "model": llm_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei un copywriter italiano specializzato in outreach social. "
                    "Scrivi messaggi DM che sembrano genuini, mai template.\n\n"
                    "REGOLA #1 — ZERO META-OUTPUT: la tua risposta DEVE essere "
                    "solo e soltanto il testo che il destinatario leggera'. "
                    "Mai 'Analizza la richiesta', 'Role:', 'Task:', 'Target:', "
                    "'Platform:', 'Profile Info:', 'Step 1:', 'OUTPUT:'. Mai "
                    "elenchi puntati o numerati. Mai markdown con etichette in "
                    "grassetto come **Label:**. Mai spiegare cosa stai per scrivere. "
                    "Solo il messaggio finito, come se lo stessi scrivendo tu su "
                    "WhatsApp/IG. Se non hai abbastanza info per personalizzare, "
                    "scrivi un saluto breve generico ma umano (3-4 righe max), "
                    "non rifiutarti."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,  # piu' alta per variabilita' (vs 0.0 per JSON extract)
        "max_tokens": 200,
    }
    # Ollama-only: keep_alive estende la permanenza del modello in VRAM dopo
    # la risposta. Senza questo flag il default Ollama e' 5min → modelli grandi
    # vengono scaricati tra job consecutivi causando cold-start ripetuti.
    if _is_ollama_endpoint(llm_base_url):
        payload["keep_alive"] = ollama_keep_alive()

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{llm_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {llm_api_key}"} if llm_api_key else {},
        )
        r.raise_for_status()
        data = r.json()
        msg_obj = data.get("choices", [{}])[0].get("message", {}) or {}
        raw = (msg_obj.get("content", "") or "").strip()
        # Alcuni modelli "thinking" (gpt-oss, qwen thinking, deepseek-r1) mettono
        # la risposta in `reasoning` / `reasoning_content` e lasciano content="".
        # Fallback su quei campi.
        if not raw:
            reasoning = (msg_obj.get("reasoning_content") or msg_obj.get("reasoning") or "").strip()
            if reasoning:
                # Prendi l'ultimo paragrafo non vuoto del reasoning come fallback.
                paragraphs = [p.strip() for p in reasoning.split("\n\n") if p.strip()]
                if paragraphs:
                    raw = paragraphs[-1]

    # Pulizia: rimuovi virgolette wrap, markdown, prefissi tipo "Messaggio:"
    raw = raw.strip("\"'`")
    raw = re.sub(r"^(messaggio|message|testo)\s*:\s*", "", raw, flags=re.IGNORECASE)
    raw = raw.strip("\"'`")
    # Rimuovi link/URL eventuali (vincolo violato dall'LLM)
    raw = re.sub(r"https?://\S+", "", raw).strip()
    raw = re.sub(r"\b(?:DM\s*me|link\s*in\s*bio|check\s*bio)\b", "", raw, flags=re.IGNORECASE).strip()
    # Limita lunghezza
    if len(raw) > req.max_chars:
        raw = raw[: req.max_chars].rsplit(" ", 1)[0] + "…"
    # Se DOPO pulizia la stringa e' ancora vuota, e' un errore vero
    # (modello thinking che non ha messo testo nel content / reasoning, oppure
    # cleanup ha rimosso tutto). Solleva cosi' che generate_batch lo logga.
    if not raw:
        raise ValueError(
            "LLM ha risposto vuoto (probabile modello 'thinking' senza output testuale "
            "nel campo content; prova un modello chat/instruct tipo qwen2.5:instruct)"
        )

    # Guardrail anti instruction-leak: bocca i casi in cui il modello ha
    # mandato la sua catena-di-ragionamento o ha echo'd le istruzioni del
    # prompt invece del messaggio finale. Se non fermassimo, il runner
    # spedirebbe DAVVERO quel testo al destinatario (succede gia' con
    # qwen3.5 thinking + Sebastiano Arcidiacono, 2026-05-19).
    is_leak, leak_reason = _looks_like_instructions(raw)
    if is_leak:
        raise ValueError(
            f"LLM ha emesso un output meta/ragionamento invece del messaggio "
            f"({leak_reason}). Cambia modello (es. qwen2.5:instruct, mistral) "
            f"o irrigidisci il prompt. Output bloccato per non inviare istruzioni al destinatario."
        )
    return raw


async def generate_batch(
    requests: list[MessageRequest],
    *,
    llm_base_url: str = "http://localhost:11434/v1",
    llm_api_key: str = "",
    llm_model: str = "qwen3-coder:30b",
    concurrency: int = 1,
) -> list[tuple[MessageRequest, str | None, str | None]]:
    """Genera messaggi per una lista di target. Concurrency=1 di default
    (Ollama serializza comunque, no benefit da parallelismo).

    Ritorna lista di tuple `(req, msg, err)`:
      - msg=str e err=None su successo
      - msg=None e err=str con la causa su fallimento (per logging upstream)
    """
    import asyncio
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(req):
        async with sem:
            try:
                msg = await generate_message(req, llm_base_url=llm_base_url, llm_api_key=llm_api_key, llm_model=llm_model)
                return req, msg, None
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                # httpx.HTTPStatusError espone request/response — include status + body short
                resp = getattr(e, "response", None)
                if resp is not None:
                    try:
                        body = resp.text[:200] if hasattr(resp, "text") else ""
                    except Exception:
                        body = ""
                    err = f"HTTP {resp.status_code}: {body}"
                log.warning("message generation failed for %s: %s", req.target_username, err)
                return req, None, err

    return await asyncio.gather(*[_one(r) for r in requests])
