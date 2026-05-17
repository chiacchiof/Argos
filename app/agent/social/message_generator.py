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

log = logging.getLogger(__name__)


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

OUTPUT: solo il testo del messaggio, niente prefissi tipo "Messaggio:", niente commenti."""


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

    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": "Sei un copywriter italiano specializzato in outreach social. Scrivi messaggi DM che sembrano genuini, mai template."},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,  # piu' alta per variabilita' (vs 0.0 per JSON extract)
        "max_tokens": 200,
    }

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
