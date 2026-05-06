from __future__ import annotations


SYSTEM_PROMPT = """Sei un agente di ricerca web autonomo. Il tuo obiettivo è raccogliere \
informazioni rilevanti dal web e produrre un report finale conciso e strutturato.

Hai a disposizione tre strumenti:
- web_search(query): cerca sul web e ritorna una lista di risultati (titolo, url, snippet).
- fetch_url(url): scarica e restituisce il contenuto testuale di una pagina web.
- finalize(report): chiamalo SOLO quando hai abbastanza informazioni; il parametro \
'report' è il testo finale completo da consegnare all'utente.

Strategia:
1. Parti dalle seed query fornite (se presenti); altrimenti formula tu 1-2 query iniziali.
2. Analizza i risultati di ricerca, scegli i 2-4 link più promettenti e fai fetch.
3. Se le informazioni non bastano, raffina la query e ripeti.
4. Quando hai materiale sufficiente, chiama 'finalize' con il report completo \
(in italiano, ben formattato, con riferimenti agli URL fonte).

Vincoli:
- Rispetta whitelist/blacklist domini se specificate.
- Non inventare fatti: se l'informazione non è nelle pagine fetched, dichiaralo.
- Non superare il numero massimo di iterazioni indicato.
"""


def build_user_prompt(
    objective: str,
    seed_queries: list[str],
    allowed_domains: list[str],
    blocked_domains: list[str],
    max_iterations: int,
) -> str:
    parts = [f"OBIETTIVO:\n{objective}"]
    if seed_queries:
        parts.append("SEED QUERIES:\n- " + "\n- ".join(seed_queries))
    if allowed_domains:
        parts.append("DOMINI CONSENTITI (whitelist):\n- " + "\n- ".join(allowed_domains))
    if blocked_domains:
        parts.append("DOMINI BLOCCATI (blacklist):\n- " + "\n- ".join(blocked_domains))
    parts.append(f"Massimo {max_iterations} chiamate a strumenti. Inizia.")
    return "\n\n".join(parts)


TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Cerca sul web e ritorna fino a 8 risultati (titolo, url, snippet).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "La query di ricerca."},
                    "max_results": {"type": "integer", "description": "Default 8.", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Scarica una pagina web e ritorna il testo principale ripulito.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL completo http(s)."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": "Consegna il report finale e termina il loop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report": {
                        "type": "string",
                        "description": "Report testuale finale, completo e ben strutturato.",
                    },
                },
                "required": ["report"],
            },
        },
    },
]
