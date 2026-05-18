"""Loop ReAct. Tool web_search/fetch_url, conclude con finalize.

Dispatch LLM per `llm_provider` del progetto:
- `ollama` → /api/chat nativo (`chat()`)
- altri (openai/anthropic/gemini/grok/custom) → /v1/chat/completions (`chat_openai_compat()`)
"""
from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

from .. import db
from ..storage import write_result
from .llm_providers import resolve_api_key, resolve_base_url
from .ollama import chat, chat_openai_compat
from .prompts import SYSTEM_PROMPT, TOOLS_SPEC, build_user_prompt
from .tools.fetch_http import fetch_http
from .tools.search import web_search


LogFn = Callable[[str], None]


async def _llm_chat(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Dispatch sul provider LLM scelto nel progetto."""
    provider = (task.get("llm_provider") or "ollama").lower()
    model = task["model"]
    if provider == "ollama":
        return await chat(model=model, messages=messages, tools=tools, temperature=temperature)
    base_url = resolve_base_url(provider, task.get("llm_base_url"))
    api_key = resolve_api_key(provider, task.get("llm_api_key"))
    return await chat_openai_compat(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        tools=tools,
        temperature=temperature,
    )


def _domain_allowed(url: str, allowed: list[str], blocked: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if blocked and any(host == d or host.endswith("." + d) for d in blocked):
        return False
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        return False
    return True


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


async def _exec_tool(
    name: str,
    args: dict[str, Any],
    task: dict[str, Any],
    log: LogFn,
) -> tuple[str, str | None]:
    """Ritorna (observation_text, final_report_or_None)."""
    if name == "web_search":
        query = str(args.get("query", "")).strip()
        max_results = int(args.get("max_results") or 8)
        if not query:
            return ("Errore: query mancante.", None)
        log(f"web_search: {query!r} (max={max_results})")
        results = await web_search(
            query,
            max_results=max_results,
            allowed_domains=task.get("allowed_domains") or [],
            blocked_domains=task.get("blocked_domains") or [],
        )
        if not results:
            return ("Nessun risultato.", None)
        lines = [
            f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        ]
        return ("\n".join(lines), None)

    if name == "fetch_url":
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return (f"URL non valido: {url}", None)
        if not _domain_allowed(url, task.get("allowed_domains") or [], task.get("blocked_domains") or []):
            log(f"fetch_url BLOCCATO da policy domini: {url}")
            return (f"Dominio bloccato dalla policy: {url}", None)
        log(f"fetch_url: {url}")
        res = await fetch_http(url)
        if res.needs_browser:
            log(f"fetch_url: contenuto scarso, provo Playwright per {url}")
            try:
                from .tools.fetch_browser import fetch_browser
                res = await fetch_browser(url)
            except Exception as e:
                log(f"Playwright fallito: {e}")
        text = res.text or ""
        header = f"URL: {res.url}\nSTATUS: {res.status}\nTITLE: {res.title}\n---\n"
        return (header + text[:8000], None)

    if name == "finalize":
        report = str(args.get("report", "")).strip()
        if not report:
            return ("Errore: report mancante in finalize.", None)
        return ("OK, report ricevuto.", report)

    return (f"Tool sconosciuto: {name}", None)


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Esegue il loop ReAct fino a finalize o max_iterations. Ritorna il path del file salvato."""

    def log(line: str) -> None:
        db.append_job_log(job_id, line)

    db.update_job(job_id, status="running", started_at=db.now_iso())
    provider = (task.get("llm_provider") or "ollama").lower()
    log(
        f"Avvio agente per task #{task['id']} \"{task['name']}\" — "
        f"provider {provider} / modello {task['model']}"
    )

    user_prompt = build_user_prompt(
        objective=task["objective"],
        seed_queries=task.get("seed_queries") or [],
        allowed_domains=task.get("allowed_domains") or [],
        blocked_domains=task.get("blocked_domains") or [],
        max_iterations=int(task.get("max_iterations") or 10),
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    max_iter = int(task.get("max_iterations") or 10)
    final_report: str | None = None

    for step in range(1, max_iter + 1):
        log(f"--- step {step}/{max_iter} ---")
        try:
            msg = await _llm_chat(task, messages, tools=TOOLS_SPEC)
        except Exception as e:
            log(f"Errore LLM ({provider}): {e}")
            raise

        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""

        # rimetto in coda il messaggio dell'assistente
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        if not tool_calls:
            # nessun tool richiesto → trattiamo il content come finale
            log("Nessun tool_call: uso il contenuto come report finale.")
            final_report = content.strip() or "(nessun output)"
            break

        for idx, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = _parse_args(fn.get("arguments"))
            # OpenAI-compat richiede tool_call_id sul messaggio role=tool;
            # Ollama nativo lo tollera. Sintetizziamo un id se il provider non l'ha fornito.
            tool_call_id = tc.get("id") or f"call_{step}_{idx}"
            try:
                observation, final_report = await _exec_tool(name, args, task, log)
            except Exception as e:
                observation = f"Errore esecuzione tool {name}: {e}"
                log(observation)
            messages.append({
                "role": "tool",
                "name": name,
                "tool_call_id": tool_call_id,
                "content": observation[:12000],
            })
            if final_report:
                break

        if final_report:
            break
    else:
        log("Raggiunto max_iterations senza finalize. Forzo riassunto.")
        final_report = await _force_summary(task, messages, log)

    if not final_report:
        final_report = "(nessun report prodotto)"

    fmt = task.get("output_format") or "txt"
    path = write_result(task["id"], final_report, fmt)
    log(f"Report salvato: {path}")
    db.update_job(job_id, status="done", finished_at=db.now_iso(), result_path=path)
    return path


async def _force_summary(task: dict[str, Any], messages: list[dict[str, Any]], log: LogFn) -> str:
    """Se il modello non chiama finalize entro max_iterations, gli chiediamo un report con la sola cronologia."""
    log("Richiedo riassunto forzato (no tool).")
    closing = messages + [
        {
            "role": "user",
            "content": "Hai esaurito le iterazioni disponibili. Produci ORA il report finale completo "
            "in italiano sulla base di quanto raccolto, senza chiamare altri strumenti.",
        }
    ]
    try:
        msg = await _llm_chat(task, closing, tools=None, temperature=0.2)
        return (msg.get("content") or "").strip() or "(nessun output)"
    except Exception as e:
        log(f"Errore nel riassunto forzato: {e}")
        return "(errore nella generazione del report finale)"
