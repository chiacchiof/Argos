"""WebSocket realtime per i Fogli collaborativi.

Endpoint: /ws/sheets/{sheet_id}

NB: questo router NON ha la dependency HTTP get_current_user (le dependency di
APIRouter non si applicano in modo utile alle rotte websocket, e get_current_user
solleverebbe HTTPException con header di redirect inutili su WS). L'autenticazione
e il tenant scoping sono fatti a mano qui, perche' l'`auth_middleware` HTTP non
gira per gli upgrade WebSocket.

Sicurezza:
  - auth via cookie di sessione (get_optional_user_ws) -> close 4401 se assente;
  - Origin check (anti CSWSH: SameSite=lax + cookie non-HTTPS non basta) -> 4403;
  - ogni query DB passa tenant_id ESPLICITO (il ContextVar non si propaga in modo
    affidabile fuori dal middleware HTTP -> rischio cross-tenant);
  - lo stato (utente/is_active, foglio, progetto, ACL) e' RI-VALIDATO contro il DB
    all'apertura, su ogni `hello` (reconnect) e in modo throttlato (>=10s) prima di
    ogni `cell_patch`. Cosi' una revoca di permesso / disattivazione utente / cambio
    visibilita' chiude o blocca la sessione invece di restare valida fino alla
    disconnessione (vedi piano §WebSocket "controllare i permessi su ogni messaggio");
  - mai fidarsi dello user_id mandato dal client: si usa l'utente autenticato.

Protocollo: vedi docs/argos_fogli_collaborativi_plan.md §Protocollo WebSocket.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import db
from ..auth import get_optional_user_ws
from ..fascicoli import acl as facl
from ..fascicoli import db as fdb
from ..fascicoli import realtime
from ..fascicoli import sheets_db as sdb

log = logging.getLogger(__name__)

ws_router = APIRouter()

# Oltre questo gap di revisioni mandiamo uno snapshot completo invece delle patch
# mancanti (evita di inondare il client dopo una lunga disconnessione).
_SNAPSHOT_GAP_LIMIT = 200
# Throttle della ri-validazione DB sul percorso caldo (cell_patch).
_REVALIDATE_EVERY = 10.0  # secondi


class _WsClose(Exception):
    """Segnala che la connessione va chiusa con un codice + messaggio d'errore."""
    def __init__(self, code: int, code_str: str, message: str):
        self.code = code
        self.code_str = code_str
        self.message = message


def _origin_ok(websocket: WebSocket) -> bool:
    """True se l'Origin del WS corrisponde all'host dell'app (same-origin) o se
    Origin e' assente (client non-browser). Mitiga il CSWSH."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    host = websocket.headers.get("host", "")
    try:
        return urlparse(origin).netloc.lower() == host.lower()
    except Exception:
        return False


def _resolve_state(websocket: WebSocket, sheet_id: int):
    """Ri-valida contro il DB: utente (is_active), foglio (tenant+visibilita'),
    progetto agganciato, e i permessi. Solleva _WsClose se non piu' autorizzato.

    Ritorna (user, sheet, project, can_edit). Usato all'apertura, su hello e
    (throttlato) prima di ogni cell_patch."""
    user = get_optional_user_ws(websocket)  # rilegge sessione + DB + is_active
    if user is None:
        raise _WsClose(4401, "unauthorized", "Sessione non valida o scaduta.")
    architect_view = user.can_manage_architecture
    sheet = sdb.get_sheet(sheet_id, tenant_id=user.tenant_id,
                          current_user_id=user.id, architect_view=architect_view)
    if not sheet:
        raise _WsClose(4404, "not_found", "Foglio non trovato o non accessibile.")
    project = None
    if sheet.get("project_id"):
        project = fdb.get_project(sheet["project_id"], tenant_id=user.tenant_id,
                                  current_user_id=user.id, architect_view=architect_view)
    if not facl.can_open_sheet(sheet, project, user):
        raise _WsClose(4403, "forbidden", "Accesso al foglio negato.")
    can_edit = facl.can_edit_sheet_cells(sheet, project, user)
    return user, sheet, project, can_edit


@ws_router.websocket("/ws/sheets/{sheet_id}")
async def sheet_ws(websocket: WebSocket, sheet_id: int):
    await websocket.accept()

    # 1) Origin check (CSWSH)
    if not _origin_ok(websocket):
        await websocket.close(code=4403)
        return

    # 2) Auth + ACL iniziali (ri-validate contro il DB)
    try:
        user, sheet, project, can_edit = _resolve_state(websocket, sheet_id)
    except _WsClose as e:
        try:
            await websocket.send_json({"type": "error", "code": e.code_str, "message": e.message})
        except Exception:
            pass
        await websocket.close(code=e.code)
        return

    # 3) Tenant scoping difensivo (ContextVar per eventuali helper; le query
    #    passano comunque tenant_id ESPLICITO leggendo dall'utente ri-validato).
    t_tok = db.set_current_tenant(user.tenant_id)
    u_tok = db.set_current_user(user.id)
    conn: realtime.Connection | None = None
    last_check = time.monotonic()
    try:
        conn = realtime.Connection(websocket, user.id, user.email)
        await realtime.register(sheet_id, conn)
        await conn.send({"type": "presence", "users": realtime.presence_users(sheet_id)})

        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            # Ri-validazione: sempre su hello (reconnect), throttlata su cell_patch.
            if mtype == "hello" or mtype == "cell_patch":
                now = time.monotonic()
                if mtype == "hello" or (now - last_check) >= _REVALIDATE_EVERY:
                    try:
                        user, sheet, project, can_edit = _resolve_state(websocket, sheet_id)
                    except _WsClose as e:
                        try:
                            await conn.send({"type": "error", "code": e.code_str, "message": e.message})
                        except Exception:
                            pass
                        await websocket.close(code=e.code)
                        break
                    last_check = now

            if mtype == "hello":
                await _handle_hello(conn, sheet_id, user.tenant_id, msg)

            elif mtype == "cell_patch":
                if not can_edit:
                    await conn.send({"type": "error", "code": "forbidden",
                                     "message": "Non puoi modificare questo foglio."})
                    continue
                try:
                    result = sdb.apply_cell_patch(
                        sheet_id, msg.get("cells"),
                        tenant_id=user.tenant_id, actor_user_id=user.id,
                    )
                except sdb.SheetValidationError as exc:
                    await conn.send({"type": "error", "code": "bad_request", "message": str(exc)})
                    continue
                except sdb.SheetForbidden:
                    await conn.send({"type": "error", "code": "not_found", "message": "Foglio non trovato."})
                    break
                await realtime.broadcast_revision(
                    sheet_id,
                    revision=result["revision"],
                    actor_user_id=user.id,
                    cells=result["cells"],
                    origin_patch_id=msg.get("patch_id"),
                )

            elif mtype == "cursor":
                await realtime.broadcast_cursor(
                    sheet_id, user_id=user.id, email=user.email,
                    row=int(msg.get("row", 0)), col=int(msg.get("col", 0)),
                    selection=msg.get("selection"),
                    exclude_cid=conn.cid,
                )

            elif mtype == "ping":
                await conn.send({"type": "pong"})

            # tipi sconosciuti: ignorati (forward-compat)

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - difensivo
        log.warning("WS sheet=%s error: %s", sheet_id, exc)
    finally:
        if conn is not None:
            await realtime.unregister(sheet_id, conn)
        db.reset_current_user(u_tok)
        db.reset_current_tenant(t_tok)


async def _handle_hello(conn: realtime.Connection, sheet_id: int, tenant_id, msg: dict) -> None:
    """Invia lo stato iniziale o il recupero incrementale dopo reconnect.
    Chiamato SOLO dopo che _resolve_state ha confermato can_open (vedi loop).

    - last_revision < 0 / None / desync / gap troppo grande -> snapshot completo.
    - last_revision < head -> patch mancanti da project_sheet_revisions.
    - last_revision == head -> nessuna azione (sync).
    """
    head = sdb.get_head_revision(sheet_id, tenant_id=tenant_id)
    last = msg.get("last_revision")
    try:
        last = int(last)
    except (TypeError, ValueError):
        last = -1

    if last < 0 or last > head or (head - last) > _SNAPSHOT_GAP_LIMIT:
        cells = sdb.get_cells(sheet_id, tenant_id=tenant_id)
        await conn.send({
            "type": "snapshot",
            "sheet_id": sheet_id,
            "revision": head,
            "cells": cells,
            "users": realtime.presence_users(sheet_id),
        })
        return

    if last < head:
        revs = sdb.get_revisions_since(sheet_id, last, tenant_id=tenant_id)
        for r in revs:
            await conn.send({
                "type": "revision_patch",
                "sheet_id": sheet_id,
                "revision": r["revision"],
                "actor_user_id": r["actor_user_id"],
                "patch_id": None,
                "cells": (r["patch"] or {}).get("cells", []),
            })
        return

    # gia' allineato
    await conn.send({"type": "sync", "sheet_id": sheet_id, "revision": head})
