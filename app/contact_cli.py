"""B-002: mini-CLI per CRUD rapido dei contatti in `/inbox/contacts`.

Casella di testo nella pagina contatti per comandi terse, alternativa al giro
"apri edit → cambia campo → submit" per le modifiche piccole e frequenti.

Sintassi:
    update <id> <campo>=<valore>     corregge un campo (whitelist sotto)
    optout <id>                      segna opt-out (non più contattabile)
    reset  <id>                      re-contattabile (status=qualified)
    qualify <id> score=<0-10>        setta qualifier_score + status=qualified
    bulk-optout <id,id,id>           opt-out multiplo
    help                             lista comandi

Diviso in due funzioni: `parse_contact_command` è PURA (testabile senza DB);
`apply_contact_command` esegue gli effetti chiamando le funzioni `db.*` già
esistenti (le stesse usate da edit/optout/bulk). Tutte tenant-safe (gli helper
`db.*` filtrano per tenant nel WHERE), con guardia `get_contact` prima di agire.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Campi correggibili via `update <id> <campo>=<valore>`. Sottoinsieme della
# whitelist di `db.update_contact` — i canali di contatto che capita di
# correggere a mano. status/qualifier_score hanno comandi dedicati (reset/qualify).
UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "whatsapp",
    "email",
    "telegram_username",
    "display_name",
    "sitoweb",
    "notes",
})


@dataclass
class ParsedContactCmd:
    """Esito del parsing di un comando della mini-CLI contatti."""
    action: str  # update | optout | reset | qualify | bulk-optout | help | unknown
    contact_id: int | None = None
    contact_ids: list[int] = field(default_factory=list)
    field_name: str | None = None
    value: str | None = None
    score: int | None = None
    error: str | None = None


def _parse_int(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_contact_command(text: str) -> ParsedContactCmd:
    """Classifica un comando della mini-CLI. Puro, deterministico, no DB."""
    s = (text or "").strip()
    if not s:
        return ParsedContactCmd(action="unknown", error="Comando vuoto. Scrivi 'help'.")

    head, _, tail = s.partition(" ")
    action = head.strip().lower()
    tail = tail.strip()

    if action == "help":
        return ParsedContactCmd(action="help")

    if action in ("optout", "reset"):
        cid = _parse_int(tail)
        if cid is None:
            return ParsedContactCmd(action=action,
                                    error=f"Uso: {action} <id>  (es. {action} 4155)")
        return ParsedContactCmd(action=action, contact_id=cid)

    if action == "bulk-optout":
        ids = [i for i in (_parse_int(x) for x in tail.replace(" ", "").split(",")) if i is not None]
        if not ids:
            return ParsedContactCmd(action="bulk-optout",
                                    error="Uso: bulk-optout <id,id,id>  (es. bulk-optout 4155,4156)")
        return ParsedContactCmd(action="bulk-optout", contact_ids=ids)

    if action == "qualify":
        bits = tail.split(None, 1)
        cid = _parse_int(bits[0]) if bits else None
        if cid is None or len(bits) < 2:
            return ParsedContactCmd(action="qualify",
                                    error="Uso: qualify <id> score=<0-10>  (es. qualify 4155 score=8)")
        key, _, raw = bits[1].partition("=")
        if key.strip().lower() != "score":
            return ParsedContactCmd(action="qualify",
                                    error="Uso: qualify <id> score=<0-10>")
        score = _parse_int(raw.strip())
        if score is None or not (0 <= score <= 10):
            return ParsedContactCmd(action="qualify",
                                    error=f"score deve essere un intero 0-10 (ricevuto '{raw.strip()}').")
        return ParsedContactCmd(action="qualify", contact_id=cid, score=score)

    if action == "update":
        bits = tail.split(None, 1)
        cid = _parse_int(bits[0]) if bits else None
        if cid is None or len(bits) < 2 or "=" not in bits[1]:
            return ParsedContactCmd(
                action="update",
                error="Uso: update <id> <campo>=<valore>  "
                      f"(campi: {', '.join(sorted(UPDATABLE_FIELDS))})",
            )
        fname, _, val = bits[1].partition("=")
        fname = fname.strip().lower()
        val = val.strip()
        if fname not in UPDATABLE_FIELDS:
            return ParsedContactCmd(
                action="update",
                error=f"Campo '{fname}' non aggiornabile via CLI. "
                      f"Campi: {', '.join(sorted(UPDATABLE_FIELDS))}.",
            )
        if not val:
            return ParsedContactCmd(action="update",
                                    error=f"Valore mancante per '{fname}'.")
        return ParsedContactCmd(action="update", contact_id=cid, field_name=fname, value=val)

    return ParsedContactCmd(action="unknown",
                            error=f"Comando sconosciuto: '{action}'. Scrivi 'help'.")


HELP_TEXT = (
    "Comandi: update <id> <campo>=<valore> · optout <id> · reset <id> · "
    "qualify <id> score=<0-10> · bulk-optout <id,id,id>. "
    f"Campi update: {', '.join(sorted(UPDATABLE_FIELDS))}."
)


def apply_contact_command(text: str) -> tuple[bool, str]:
    """Parsa ed esegue il comando. Ritorna (ok, messaggio_flash).

    Tenant-safe: usa `db.get_contact` (filtrato per tenant del contesto) come
    guardia prima di modificare; gli helper `db.update_*` filtrano a loro volta
    nel WHERE. Un id di altro tenant → 'non trovato', nessuna scrittura.
    """
    from . import db

    p = parse_contact_command(text)
    if p.error:
        return False, p.error
    if p.action == "help":
        return True, HELP_TEXT

    if p.action in ("optout", "reset", "qualify", "update"):
        contact = db.get_contact(p.contact_id)
        if not contact:
            return False, f"Contatto #{p.contact_id} non trovato (o non tuo)."

    if p.action == "optout":
        db.update_contact_status(p.contact_id, "optedout", notes="Opt-out via CLI")
        return True, f"✓ contatto #{p.contact_id} → opt-out."

    if p.action == "reset":
        db.update_contact_status(p.contact_id, "qualified", notes="Reset via CLI (re-contattabile)")
        return True, f"✓ contatto #{p.contact_id} → re-contattabile (qualified)."

    if p.action == "qualify":
        db.update_contact(p.contact_id, {"qualifier_score": p.score, "status": "qualified"})
        return True, f"✓ contatto #{p.contact_id}: qualifier_score={p.score}, status=qualified."

    if p.action == "update":
        value: str | None = p.value
        if p.field_name == "telegram_username":
            value = value.lstrip("@")
        db.update_contact(p.contact_id, {p.field_name: value})
        return True, f"✓ contatto #{p.contact_id}: {p.field_name} aggiornato a '{value}'."

    if p.action == "bulk-optout":
        n = 0
        for cid in p.contact_ids:
            if db.get_contact(cid):  # guardia tenant per ciascuno
                db.update_contact_status(cid, "optedout", notes="Opt-out via CLI (bulk)")
                n += 1
        skipped = len(p.contact_ids) - n
        msg = f"✓ {n} contatti → opt-out."
        if skipped:
            msg += f" {skipped} ignorati (non trovati / non tuoi)."
        return (n > 0), msg

    return False, f"Comando sconosciuto. {HELP_TEXT}"
