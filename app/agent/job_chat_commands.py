"""B-001: parser deterministico dei comandi della chat in-running di un job.

Parser PURO (nessun accesso a DB / HTTP): prende il testo scritto dall'operatore
nella chat del job e lo classifica. La route `POST /jobs/{id}/chat` applica
l'intento risultante (vedi `app/routes/jobs.py`).

Sintassi comandi (tutto ciò che NON inizia con `/` è testo libero = suggerimento
live iniettato nel prompt LLM dai runner che lo supportano):

    /stop                       stop del job (hard)
    /pause                      sospende (solo agent_mode che supportano pause)
    /resume                     riprende da pausa
    /note <testo>               annota una riga nel log del job
    /skip                       salta il target/seed corrente (consumato dal runner)
    /set <asset_id> <campo> <valore>   corregge un campo di un asset
    /help                       lista comandi

Tenere il parser separato e puro permette di testarlo senza avviare l'app.
"""
from __future__ import annotations

from dataclasses import dataclass

# Campi asset correggibili live via /set. Sottoinsieme sicuro di quelli accettati
# da `db.update_asset` — i canali di contatto che capita di dover correggere
# durante un outreach (es. numero WhatsApp sbagliato, job#110).
SETTABLE_FIELDS: frozenset[str] = frozenset({
    "whatsapp",
    "email",
    "telegram_username",
    "telegram_chat_id",
    "display_name",
    "sitoweb",
})

# Comandi senza argomenti.
_NULLARY = frozenset({"stop", "pause", "resume", "skip", "help"})


@dataclass
class ParsedChat:
    """Esito del parsing di un messaggio della chat di un job.

    - `kind`: 'command' (inizia con `/`) | 'free_text' (suggerimento libero).
    - `command`: per kind='command', uno di stop|pause|resume|note|skip|set|help
      |unknown.
    - `error`: messaggio d'errore se la sintassi del comando è invalida (kind
      resta 'command', command='unknown' o quello riconosciuto ma malformato).
    """
    kind: str
    command: str | None = None
    note: str | None = None
    asset_id: int | None = None
    field_name: str | None = None
    value: str | None = None
    error: str | None = None


def parse_chat_input(body: str) -> ParsedChat:
    """Classifica un messaggio della chat. Non tocca DB. Deterministico."""
    text = (body or "").strip()
    if not text:
        return ParsedChat(kind="free_text", error="Messaggio vuoto.")
    if not text.startswith("/"):
        return ParsedChat(kind="free_text")

    # Comando: prima parola dopo lo slash, lowercase.
    rest = text[1:].lstrip()
    if not rest:
        return ParsedChat(kind="command", command="unknown",
                          error="Comando vuoto. Scrivi /help per la lista.")
    head, _, tail = rest.partition(" ")
    cmd = head.strip().lower()
    tail = tail.strip()

    if cmd in _NULLARY:
        if tail:
            # argomenti extra ignorati ma segnalati (non bloccante)
            return ParsedChat(kind="command", command=cmd,
                              error=f"Il comando /{cmd} non accetta argomenti (ignoro '{tail}').")
        return ParsedChat(kind="command", command=cmd)

    if cmd == "note":
        if not tail:
            return ParsedChat(kind="command", command="note",
                              error="Uso: /note <testo>")
        return ParsedChat(kind="command", command="note", note=tail)

    if cmd == "set":
        # /set <asset_id> <campo> <valore...>
        bits = tail.split(None, 2)
        if len(bits) < 3:
            return ParsedChat(
                kind="command", command="set",
                error="Uso: /set <asset_id> <campo> <valore>  "
                      f"(campi: {', '.join(sorted(SETTABLE_FIELDS))})",
            )
        raw_id, field_name, value = bits[0], bits[1].lower(), bits[2].strip()
        try:
            asset_id = int(raw_id)
        except ValueError:
            return ParsedChat(kind="command", command="set",
                              error=f"asset_id non valido: '{raw_id}' (atteso un numero).")
        if field_name not in SETTABLE_FIELDS:
            return ParsedChat(
                kind="command", command="set",
                error=f"Campo '{field_name}' non modificabile. "
                      f"Campi ammessi: {', '.join(sorted(SETTABLE_FIELDS))}.",
            )
        if not value:
            return ParsedChat(kind="command", command="set",
                              error="Valore mancante.")
        return ParsedChat(kind="command", command="set",
                          asset_id=asset_id, field_name=field_name, value=value)

    return ParsedChat(kind="command", command="unknown",
                      error=f"Comando sconosciuto: /{cmd}. Scrivi /help per la lista.")


HELP_TEXT = (
    "Comandi disponibili:\n"
    "• /stop — ferma il job\n"
    "• /pause — sospende (se la modalità lo supporta)\n"
    "• /resume — riprende\n"
    "• /note <testo> — annota nel log\n"
    "• /skip — salta il target/seed corrente\n"
    "• /set <asset_id> <campo> <valore> — corregge un asset "
    f"(campi: {', '.join(sorted(SETTABLE_FIELDS))})\n"
    "Qualsiasi altro testo viene passato all'agente come istruzione live."
)
