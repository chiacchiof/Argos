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
  - permesso di edit verificato all'apertura E su ogni cell_patch;
  - mai fidarsi dello user_id mandato dal client: si usa l'utente autenticato.

Protocollo: vedi docs/argos_fogli_collaborativi_plan.md §Protocollo WebSocket.
"""
from __future__ import annotations

import logging
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


def _origin_ok(websocket: WebSocket) -> bool:
    """True se l'Origin del WS corrisponde all'host dell'app (same-origin) o se
    Origin e' assente (client non-browser)."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    host = websocket.headers.get("host", "")
    try:
        return urlparse(origin).netloc.lower() == host.lower()
    except Exception:
        return False


@ws_router.websocket("/ws/sheets/{sheet_id}")
async def sheet_ws(websocket: WebSocket, sheet_id: int):
    await websocket.accept()

    # 1) Origin check (CSWSH)
    if not _origin_ok(websocket):
        await websocket.close(code=4403)
        return

    # 2) Auth via sessione
    user = get_optional_user_ws(websocket)
    if user is None:
        await websocket.close(code=4401)
        return

    tenant_id = user.tenant_id
    architect_view = user.can_manage_architecture

    # 3) Tenant scoping difensivo (ContextVar per eventuali helper + explicit nelle query)
    t_tok = db.set_current_tenant(tenant_id)
    u_tok = db.set_current_user(user.id)
    conn: realtime.Connection | None = None
    try:
        # 4) Carica foglio + progetto, verifica visibilita'/ACL
        sheet = sdb.get_sheet(sheet_id, tenant_id=tenant_id, current_user_id=user.id,
                              architect_view=architect_view)
        if not sheet:
            await websocket.send_json({"type": "error", "code": "not_found",
                                       "message": "Foglio non trovato."})
            await websocket.close(code=4404)
            return
        project = None
        if sheet.get("project_id"):
            project = fdb.get_project(sheet["project_id"], tenant_id=tenant_id,
                                      current_user_id=user.id, architect_view=architect_view)
        if not facl.can_open_sheet(sheet, project, user):
            await websocket.send_json({"type": "error", "code": "forbidden",
                                       "message": "Accesso negato."})
            await websocket.close(code=4403)
            return
        can_edit = facl.can_edit_sheet_cells(sheet, project, user)

        # 5) Registra nella stanza + presenza
        conn = realtime.Connection(websocket, user.id, user.email)
        await realtime.register(sheet_id, conn)
        # invia subito la presenza corrente a QUESTO client (register ha gia'
        # fatto broadcast, ma l'onmessage del client potrebbe non essere pronto)
        await conn.send({"type": "presence", "users": realtime.presence_users(sheet_id)})

        # 6) Loop messaggi
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            if mtype == "hello":
                await _handle_hello(conn, sheet_id, tenant_id, msg)

            elif mtype == "cell_patch":
                if not can_edit:
                    await conn.send({"type": "error", "code": "forbidden",
                                     "message": "Non puoi modificare questo foglio."})
                    continue
                try:
                    result = sdb.apply_cell_patch(
                        sheet_id, msg.get("cells"),
                        tenant_id=tenant_id, actor_user_id=user.id,
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
