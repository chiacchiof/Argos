"""Argos Fascicoli — routes web.

Endpoint:
  GET  /fascicoli                                  -> lista utente
  GET  /fascicoli/settings                          -> form RootProject
  POST /fascicoli/settings                          -> set RootProject
  GET  /fascicoli/new                               -> form creazione
  POST /fascicoli                                   -> crea
  GET  /fascicoli/architect                         -> vista architect (tenant intero)
  GET  /fascicoli/{id}                              -> dettaglio
  POST /fascicoli/{id}/edit                         -> aggiorna titolo/descrizione/visibilita'
  POST /fascicoli/{id}/archive                      -> soft delete
  POST /fascicoli/{id}/restore                      -> ripristina
  POST /fascicoli/{id}/refresh                      -> re-sync filesystem
  POST /fascicoli/{id}/members                      -> add membro (User-Use sharing)
  POST /fascicoli/{id}/members/{user_id}/remove    -> remove membro

L'ordine di definizione e' importante: le rotte specifiche (/settings, /new,
/architect) DEVONO venire prima della rotta /{project_id} altrimenti FastAPI
le interpreterebbe come id.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .. import db_cloud
from ..auth import CurrentUser, get_current_user, require_architect_or_admin
from ..fascicoli import acl as facl
from ..fascicoli import db as fdb
from ..fascicoli import fs as ffs
from ..fascicoli import share as fshare
from ..fascicoli import sheets_db as sdb
from ..fascicoli import sheets_export as sx
from ..fascicoli import index_jobs
from ..fascicoli import rag as frag
from ..fascicoli import sync as fsync
from ..templates import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/fascicoli", dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_root_path(user: CurrentUser) -> Path | None:
    """RootProject configurata per l'utente sul PC corrente. None se non settata."""
    row = db_cloud.get_user(user.id)
    if not row:
        return None
    p = (row.get("root_project_path") or "").strip()
    return Path(p) if p else None


# ACL estratti in app/fascicoli/acl.py per condivisione con le route Fogli
# (vedi docs/argos_fogli_collaborativi_plan.md §Permessi). Alias retro-compatibili.
_can_edit_project = facl.can_edit_project
_can_manage_project = facl.can_manage_project


def _sheet_context_chunks(project_id: int, current_user: CurrentUser, *, max_total_chars: int = 12000) -> list[dict]:
    """Fogli collaborativi del fascicolo come 'fonti' per la chat RAG: il loro
    contenuto vive su DB (non nei file locali), quindi va iniettato a parte cosi'
    l'assistente puo' leggerli e ragionarci. Ogni foglio diventa uno pseudo-chunk
    {file, idx, text, score} compatibile con frag._build_messages."""
    out: list[dict] = []
    try:
        architect_view = current_user.is_architect or current_user.is_super_admin
        sheets = sdb.list_sheets(
            project_id=project_id, only_project=True,
            tenant_id=current_user.tenant_id, current_user_id=current_user.id,
            architect_view=architect_view,
        )
    except Exception:
        return out
    total = 0
    for sh in sheets:
        try:
            cells = sdb.get_cells(sh["id"], tenant_id=current_user.tenant_id)
        except Exception:
            continue
        txt = sx.to_prompt_text(cells, max_chars=4000)
        if not txt or txt == "(foglio vuoto)":
            continue
        out.append({"file": f"Foglio «{sh['title']}»", "idx": 0, "text": txt, "score": 1.0})
        total += len(txt)
        if total >= max_total_chars:
            break
    return out


# ---------------------------------------------------------------------------
# RootProject setup
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
async def fascicoli_settings_get(
    request: Request,
    first_time: int = 0,
    current_user: CurrentUser = Depends(get_current_user),
):
    row = db_cloud.get_user(current_user.id) or {}
    current_path = (row.get("root_project_path") or "").strip()
    default_suggestion = str(Path.home() / "Documents" / "Argos")
    return templates.TemplateResponse(
        request,
        "fascicoli_settings.html",
        {
            "current_path": current_path,
            "default_suggestion": default_suggestion,
            "first_time": bool(first_time),
        },
    )


@router.post("/settings")
async def fascicoli_settings_post(
    request: Request,
    root_path: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    raw = (root_path or "").strip().strip('"').strip("'")
    if not raw:
        raise HTTPException(400, "Path vuoto.")
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
        resolved = str(path.resolve())
    except OSError as exc:
        raise HTTPException(400, f"Impossibile creare/accedere alla cartella: {exc}")
    db_cloud.update_user(current_user.id, root_project_path=resolved)
    log.info("user=%s set RootProject=%s", current_user.email, resolved)
    return RedirectResponse(url="/fascicoli", status_code=302)


# ---------------------------------------------------------------------------
# Architect view (deve essere prima di /{project_id})
# ---------------------------------------------------------------------------

@router.get("/architect", response_class=HTMLResponse)
async def fascicoli_architect_view(
    request: Request,
    current_user: CurrentUser = Depends(require_architect_or_admin),
):
    """Tutti i progetti del tenant, inclusi User-Use altrui e archiviati,
    con consumo spazio aggregato. Solo metadati, mai contenuti."""
    projects = fdb.list_projects(architect_view=True, include_archived=True)
    return templates.TemplateResponse(
        request,
        "fascicoli_architect.html",
        {"projects": projects},
    )


# ---------------------------------------------------------------------------
# New project form (deve essere prima di /{project_id})
# ---------------------------------------------------------------------------

@router.get("/new", response_class=HTMLResponse)
async def fascicoli_new_form(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    root = _user_root_path(current_user)
    if root is None:
        return RedirectResponse(url="/fascicoli/settings?first_time=1", status_code=302)
    return templates.TemplateResponse(
        request,
        "fascicoli_new.html",
        {"root_project_path": str(root)},
    )


# ---------------------------------------------------------------------------
# Lista + creazione
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def fascicoli_list(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    root = _user_root_path(current_user)
    if root is None:
        return RedirectResponse(url="/fascicoli/settings?first_time=1", status_code=302)
    projects = fdb.list_projects(current_user_id=current_user.id)
    projects = fsync.annotate_projects_with_local_state(projects, root)
    return templates.TemplateResponse(
        request,
        "fascicoli_list.html",
        {
            "projects": projects,
            "root_project_path": str(root),
        },
    )


@router.post("")
async def fascicoli_create(
    request: Request,
    title: str = Form(..., min_length=1),
    description: str = Form(""),
    visibility: str = Form("tenant"),
    current_user: CurrentUser = Depends(get_current_user),
):
    if current_user.tenant_id is None:
        raise HTTPException(403, "Solo utenti del tenant possono creare fascicoli.")
    root = _user_root_path(current_user)
    if root is None:
        raise HTTPException(400, "Configura la RootProject in /fascicoli/settings.")
    if visibility not in ("tenant", "user"):
        raise HTTPException(400, f"visibility non valida: {visibility}")

    tenant_row = db_cloud.get_tenant(current_user.tenant_id)
    tenant_slug = (tenant_row or {}).get("slug")

    project_uuid = ffs.new_project_uuid()
    try:
        folder_path = ffs.create_project_folder(
            root, title, project_uuid=project_uuid, tenant_slug=tenant_slug,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))

    project_id = fdb.create_project(
        title=title,
        description=description or None,
        visibility=visibility,
        folder_uuid=project_uuid,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    log.info(
        "created project id=%s uuid=%s folder=%s owner=%s",
        project_id, project_uuid, folder_path, current_user.email,
    )
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


# ---------------------------------------------------------------------------
# Dettaglio
# ---------------------------------------------------------------------------

@router.get("/{project_id}", response_class=HTMLResponse)
async def fascicoli_detail(
    request: Request,
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    # Architect/admin: usano la vista privilegiata per accedere a tutto.
    architect_view = current_user.is_architect or current_user.is_super_admin
    project = fdb.get_project(
        project_id,
        current_user_id=current_user.id,
        architect_view=architect_view,
    )
    if not project:
        raise HTTPException(404, "Fascicolo non trovato o non accessibile.")

    root = _user_root_path(current_user)
    folder = fsync.locate_project_folder(root, project["folder_uuid"]) if root else None
    project["local_folder"] = str(folder) if folder else None
    project["local_state"] = "completo" if folder else "monco"

    files = fdb.list_project_files(project_id)
    members = fdb.list_project_members(project_id)

    # Lista utenti del tenant per il selettore di sharing (esclude owner + esistenti).
    tenant_users: list[dict] = []
    if current_user.tenant_id is not None:
        all_users = db_cloud.list_users(tenant_id=current_user.tenant_id)
        existing_ids = {project["owner_user_id"]} | {m["user_id"] for m in members}
        tenant_users = [u for u in all_users if u["id"] not in existing_ids and u.get("is_active")]

    # Indice RAG + cronologia chat
    index_info = frag.index_summary(folder) if folder else {"ready": False, "n_chunks": 0, "n_files": 0, "model": None}

    # Conversazioni chat del fascicolo (max 20). Adotta eventuali messaggi legacy
    # (pre-conversazioni) in una conversazione 'Chat'.
    fdb.adopt_legacy_messages(project_id, user_id=current_user.id)
    conversations = fdb.list_conversations(project_id)
    _conv_q = request.query_params.get("conv")
    active_conv = None
    if _conv_q and _conv_q.isdigit():
        active_conv = next((c for c in conversations if c["id"] == int(_conv_q)), None)
    if active_conv is None and conversations:
        active_conv = conversations[0]
    active_conv_id = active_conv["id"] if active_conv else None
    chat_messages = (
        fdb.list_chat_messages(project_id, conversation_id=active_conv_id)
        if active_conv_id else []
    )
    # Stato del job di indicizzazione (in-memory): per la barra HTMX nel detail
    job_status = index_jobs.get_status(project_id)

    # Fogli collaborativi agganciati a questo fascicolo
    sheets = sdb.list_sheets(
        project_id=project_id, only_project=True,
        current_user_id=current_user.id, architect_view=architect_view,
    )
    for _s in sheets:
        _s["_can_manage"] = facl.can_manage_sheet(_s, project, current_user)

    return templates.TemplateResponse(
        request,
        "fascicoli_detail.html",
        {
            "project": project,
            "files": files,
            "members": members,
            "tenant_users": tenant_users,
            "can_edit": _can_edit_project(project, current_user),
            "can_manage": _can_manage_project(project, current_user),
            "sheets": sheets,
            "index_ready": index_info["ready"],
            "n_indexed": index_info["n_chunks"],
            "n_indexed_files": index_info["n_files"],
            "embed_model": index_info["model"],
            "chat_messages": chat_messages,
            "conversations": conversations,
            "active_conv_id": active_conv_id,
            "conv_limit": fdb.MAX_CONVERSATIONS_PER_PROJECT,
            # Stato del PC corrente vs progetto: serve al template per decidere
            # se mostrare l'upload (richiede root configurata) e diagnostica.
            "has_root_configured": root is not None,
            "user_root_path": str(root) if root else None,
            # Stato job di indicizzazione (in-memory): None se mai avviato,
            # dict con status/phase/chunks_done/chunks_total/... altrimenti.
            "index_status": job_status,
        },
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

@router.post("/{project_id}/edit")
async def fascicoli_edit(
    request: Request,
    project_id: int,
    title: str = Form(""),
    description: str = Form(""),
    visibility: str = Form(""),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_edit_project(project, current_user):
        raise HTTPException(403, "Non puoi modificare questo fascicolo.")

    can_manage = _can_manage_project(project, current_user)
    new_title = title.strip() or None
    # description: stringa vuota = clear (set NULL); non passare = preserve
    new_description = description if description is not None else None
    new_visibility = visibility if (visibility and can_manage) else None

    fdb.update_project(
        project_id,
        title=new_title,
        description=new_description,
        visibility=new_visibility,
    )
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


@router.post("/{project_id}/archive")
async def fascicoli_archive(
    request: Request,
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_manage_project(project, current_user):
        raise HTTPException(403)
    fdb.set_project_archived(project_id, True)
    return RedirectResponse(url="/fascicoli", status_code=302)


@router.post("/{project_id}/restore")
async def fascicoli_restore(
    request: Request,
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_manage_project(project, current_user):
        raise HTTPException(403)
    fdb.set_project_archived(project_id, False)
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


@router.post("/{project_id}/delete")
async def fascicoli_delete(
    request: Request,
    project_id: int,
    delete_files: int = Form(0),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Hard delete del fascicolo. `delete_files=1` rimuove anche la cartella
    fisica + tutti i file utente sul disco. `delete_files=0` mantiene la
    cartella sul disco (il fascicolo sparisce solo dal DB).

    Solo owner / architect / super-admin possono eliminare.
    """
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_manage_project(project, current_user):
        raise HTTPException(403, "Solo owner/architect/admin possono eliminare un fascicolo.")

    # 1) Cancella cartella fisica se richiesto (e se accessibile da questo PC).
    disk_deleted = False
    if delete_files:
        root = _user_root_path(current_user)
        folder = fsync.locate_project_folder(root, project["folder_uuid"]) if root else None
        if folder:
            try:
                disk_deleted = ffs.delete_project_folder(folder)
            except OSError as exc:
                # Continua col DB delete anche se la cartella non si può cancellare.
                log.warning("delete folder %s failed: %s", folder, exc)

    # 2) Cancella record DB (CASCADE pulisce files/members/chat).
    deleted = fdb.delete_project(project_id)
    # 3) Pulisci stato in-memory del job di indicizzazione.
    index_jobs.clear(project_id)

    log.info(
        "deleted project=%s by user=%s delete_files=%s disk_deleted=%s db_deleted=%s",
        project_id, current_user.email, delete_files, disk_deleted, deleted,
    )
    return RedirectResponse(url="/fascicoli", status_code=302)


@router.post("/{project_id}/files/delete")
async def fascicoli_file_delete(
    request: Request,
    project_id: int,
    relative_path: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Cancella un singolo file dal fascicolo: file fisico + record DB + chunks
    dall'indice RAG (se presenti).
    """
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_edit_project(project, current_user):
        raise HTTPException(403, "Non puoi cancellare file in questo fascicolo.")

    root = _user_root_path(current_user)
    folder = fsync.locate_project_folder(root, project["folder_uuid"]) if root else None
    if not folder:
        raise HTTPException(400, "Cartella non trovata su questo PC.")

    rel = (relative_path or "").strip()
    if not rel:
        raise HTTPException(400, "relative_path mancante.")

    # 1) File fisico (path-traversal safe).
    physical_ok = ffs.delete_file_in_project(folder, rel)
    # 2) Registro DB (sempre, anche se il file fisico non c'era già).
    fdb.delete_project_file(project_id, rel)
    # 3) Indice RAG.
    chunks_removed = frag.remove_file_from_index(folder, rel)

    log.info(
        "deleted file project=%s path=%s by=%s physical=%s chunks_removed=%d",
        project_id, rel, current_user.email, physical_ok, chunks_removed,
    )
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


@router.post("/{project_id}/refresh")
async def fascicoli_refresh(
    request: Request,
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Re-scan filesystem -> aggiorna `project_files`."""
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    root = _user_root_path(current_user)
    folder = fsync.locate_project_folder(root, project["folder_uuid"]) if root else None
    if not folder:
        raise HTTPException(400, "Cartella non trovata su questo PC (progetto monco).")
    summary = fsync.sync_project_from_disk(
        project_id, folder, actor_user_id=current_user.id,
    )
    log.info("refresh project=%s by=%s summary=%s", project_id, current_user.email, summary)
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


@router.post("/{project_id}/members")
async def fascicoli_members_add(
    request: Request,
    project_id: int,
    user_id: int = Form(...),
    role: str = Form("viewer"),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_manage_project(project, current_user):
        raise HTTPException(403)
    if role not in ("viewer", "editor"):
        raise HTTPException(400, f"role non valido: {role}")
    target = db_cloud.get_user(user_id)
    if not target or target.get("tenant_id") != current_user.tenant_id:
        raise HTTPException(400, "L'utente non appartiene al tuo tenant.")
    if user_id == project["owner_user_id"]:
        raise HTTPException(400, "L'owner ha gia' accesso completo.")
    fdb.add_project_member(project_id, user_id, role)
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


@router.post("/{project_id}/members/{user_id}/remove")
async def fascicoli_members_remove(
    request: Request,
    project_id: int,
    user_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_manage_project(project, current_user):
        raise HTTPException(403)
    fdb.remove_project_member(project_id, user_id)
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


# ---------------------------------------------------------------------------
# Upload file via browser (multipart + auto-sync)
# ---------------------------------------------------------------------------

# Estensioni accettate dall'upload UI. Coerenti con quelle gestite da
# `app.fascicoli.ingest.SUPPORTED` ma piu' permissive: un fornitore puo' caricare
# anche un PNG che diventa allegato (non indicizzato) — esiste nel registro,
# vede nel detail, semplicemente non finisce nell'indice RAG.
_UPLOAD_ALLOWED_EXT = {
    ".pdf", ".txt", ".md", ".markdown", ".eml",
    ".docx", ".xlsx", ".pptx", ".csv",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".zip",
}
_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per file


@router.post("/{project_id}/upload")
async def fascicoli_upload(
    request: Request,
    project_id: int,
    files: list[UploadFile] = File(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Carica uno o piu' file nella cartella fisica del progetto + sync.

    Se la cartella locale per questo progetto NON esiste ancora sul PC corrente
    (progetto monco) ma l'utente ha una RootProject configurata, la creiamo al
    volo con manifest che porta l'UUID del progetto. In questo modo qualsiasi
    PC del tenant puo' "adottare" il progetto caricando il primo file.
    """
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_edit_project(project, current_user):
        raise HTTPException(403, "Non puoi caricare file in questo fascicolo.")

    root = _user_root_path(current_user)
    if root is None:
        # Senza RootProject non sappiamo dove mettere i file -> setup
        return RedirectResponse(url="/fascicoli/settings?first_time=1", status_code=302)

    folder = fsync.locate_project_folder(root, project["folder_uuid"])
    if not folder:
        # Auto-rescue: crea una cartella nuova nella root col manifest del progetto.
        # Useremo il titolo come nome cartella (con dedup -2/-3 se collide).
        tenant_row = db_cloud.get_tenant(project["tenant_id"]) if project.get("tenant_id") else None
        tenant_slug = (tenant_row or {}).get("slug")
        try:
            folder = ffs.create_project_folder(
                root, project["title"],
                project_uuid=str(project["folder_uuid"]),
                tenant_slug=tenant_slug,
            )
            log.info(
                "auto-rescue: created folder %s for project=%s uuid=%s",
                folder, project_id, project["folder_uuid"],
            )
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise HTTPException(500, f"Impossibile creare la cartella locale: {exc}")
    folder_resolved = folder.resolve()

    saved = 0
    for upload in files:
        if not upload or not upload.filename:
            continue
        # Sanitize: prendi solo il basename (Path() defe il traversal cross-OS)
        safe_name = ffs.sanitize_folder_name(Path(upload.filename).name) or "file"
        ext = Path(safe_name).suffix.lower()
        if ext and ext not in _UPLOAD_ALLOWED_EXT:
            log.warning("upload skip %s (ext %s non ammessa)", upload.filename, ext)
            continue
        # Risolvi collisioni: -2, -3, ...
        target = folder / safe_name
        n = 2
        while target.exists():
            stem = Path(safe_name).stem
            target = folder / f"{stem}-{n}{ext}"
            n += 1
        # Path traversal safety: target deve essere dentro folder_resolved.
        try:
            target.resolve().relative_to(folder_resolved)
        except ValueError:
            log.warning("upload skip %s (path traversal)", upload.filename)
            continue
        # Stream-write con limite di size
        written = 0
        try:
            with target.open("wb") as f:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _UPLOAD_MAX_BYTES:
                        f.close()
                        target.unlink(missing_ok=True)
                        log.warning("upload skip %s (>50MB)", upload.filename)
                        written = -1
                        break
                    f.write(chunk)
        except OSError as exc:
            log.warning("upload write failed for %s: %s", upload.filename, exc)
            continue
        if written >= 0:
            saved += 1

    # Re-sync filesystem -> DB cosi' i nuovi file appaiono in tabella subito.
    summary = fsync.sync_project_from_disk(project_id, folder, actor_user_id=current_user.id)
    log.info("upload project=%s saved=%d sync=%s", project_id, saved, summary)
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


# ---------------------------------------------------------------------------
# Indexing (chunking + embedding via Ollama)
# ---------------------------------------------------------------------------

@router.post("/{project_id}/index", response_class=HTMLResponse)
async def fascicoli_index(
    request: Request,
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Avvia l'indicizzazione in background. La richiesta torna SUBITO con
    un fragment HTMX che mostra la barra di avanzamento; la barra polla
    `/index/status` ogni 1.5s per aggiornarsi.

    Se gia' un job e' in corso per questo progetto, lo riusa (idempotente).
    """
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_edit_project(project, current_user):
        raise HTTPException(403)
    root = _user_root_path(current_user)
    folder = fsync.locate_project_folder(root, project["folder_uuid"]) if root else None
    if not folder:
        raise HTTPException(400, "Cartella non trovata su questo PC.")

    started = index_jobs.start(project_id, folder, actor_user_id=current_user.id)
    log.info("/index project=%s started=%s", project_id, started)

    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        return templates.TemplateResponse(
            request,
            "_index_progress.html",
            {"project_id": project_id, "status": index_jobs.get_status(project_id)},
        )
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


@router.get("/{project_id}/index/status", response_class=HTMLResponse)
async def fascicoli_index_status(
    request: Request,
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Fragment HTMX con lo stato attuale del job di indicizzazione.
    Pollato dalla barra di avanzamento ogni ~1.5s. Quando `status == 'done'`
    o `'error'` il fragment smette di pollare (vedi template)."""
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "_index_progress.html",
        {"project_id": project_id, "status": index_jobs.get_status(project_id)},
    )


# ---------------------------------------------------------------------------
# Chat Q&A (RAG)
# ---------------------------------------------------------------------------

def _project_share_response(request: Request, project: dict, current_user: CurrentUser):
    members = fdb.list_project_members(project["id"])
    member_role = {m["user_id"]: m["role"] for m in members}
    tenant_users = db_cloud.list_users(tenant_id=current_user.tenant_id) if current_user.tenant_id else []
    ctx = fshare.build_share_context(
        kind="project", title=project["title"], base=f"/fascicoli/{project['id']}",
        visibility=project.get("visibility", "tenant"),
        owner_user_id=project.get("owner_user_id"),
        member_role=member_role, tenant_users=tenant_users,
    )
    return templates.TemplateResponse(request, "share_modal.html", {"share": ctx})


def _load_project_for_manage(project_id: int, current_user: CurrentUser) -> dict:
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    if not _can_manage_project(project, current_user):
        raise HTTPException(403, "Non puoi gestire la condivisione di questo fascicolo.")
    return project


@router.get("/{project_id}/share", response_class=HTMLResponse)
async def fascicoli_share_get(
    request: Request, project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    project = _load_project_for_manage(project_id, current_user)
    return _project_share_response(request, project, current_user)


@router.post("/{project_id}/share", response_class=HTMLResponse)
async def fascicoli_share_set(
    request: Request, project_id: int,
    user_id: int = Form(...), role: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = _load_project_for_manage(project_id, current_user)
    if role == "none":
        fdb.remove_project_member(project_id, user_id)
    elif role in ("viewer", "editor"):
        # no condivisione cross-tenant: l'utente deve appartenere al tenant
        target = db_cloud.get_user(user_id)
        if not target or target.get("tenant_id") != current_user.tenant_id:
            raise HTTPException(400, "Utente non valido per questo tenant.")
        fdb.add_project_member(project_id, user_id, role)
    else:
        raise HTTPException(400, "Ruolo non valido.")
    return _project_share_response(request, project, current_user)


@router.post("/{project_id}/share/visibility", response_class=HTMLResponse)
async def fascicoli_share_visibility(
    request: Request, project_id: int,
    visibility: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = _load_project_for_manage(project_id, current_user)
    if visibility not in ("tenant", "user"):
        raise HTTPException(400, "visibility non valida.")
    fdb.update_project(project_id, visibility=visibility)
    project["visibility"] = visibility
    return _project_share_response(request, project, current_user)


@router.post("/{project_id}/conversations")
async def fascicoli_conversation_new(
    project_id: int,
    title: str = Form("Nuova chat"),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    try:
        cid = fdb.create_conversation(project_id, title=title or "Nuova chat", user_id=current_user.id)
    except fdb.ConversationLimitError as exc:
        # torna al fascicolo con messaggio d'errore
        return RedirectResponse(url=f"/fascicoli/{project_id}?conv_err={quote(str(exc))}", status_code=302)
    return RedirectResponse(url=f"/fascicoli/{project_id}?conv={cid}", status_code=302)


@router.post("/{project_id}/conversations/{conversation_id}/rename")
async def fascicoli_conversation_rename(
    project_id: int,
    conversation_id: int,
    title: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    fdb.rename_conversation(conversation_id, title, project_id)
    return RedirectResponse(url=f"/fascicoli/{project_id}?conv={conversation_id}", status_code=302)


@router.post("/{project_id}/conversations/{conversation_id}/delete")
async def fascicoli_conversation_delete(
    project_id: int,
    conversation_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    fdb.delete_conversation(conversation_id, project_id)
    return RedirectResponse(url=f"/fascicoli/{project_id}", status_code=302)


@router.post("/{project_id}/chat")
async def fascicoli_chat(
    request: Request,
    project_id: int,
    message: str = Form(..., min_length=1),
    conversation_id: str = Form(""),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Risponde in streaming via Server-Sent Events.

    Eventi SSE (separati da `\\n\\n`, body JSON):
      - `{"delta": "...testo..."}`     ad ogni chunk dall'LLM
      - `{"done": true, "html": "...", "citations": [...]}`  finale, con HTML
        markdown gia' renderizzato server-side e citazioni
      - `{"error": "..."}`             se qualcosa esplode

    Persistenza: il messaggio user viene salvato in DB SUBITO (prima dello
    streaming). Il messaggio assistant viene salvato al termine dello stream,
    quindi se il client si scollega a meta' rispondiamo "a perdere".
    """
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    root = _user_root_path(current_user)
    folder = fsync.locate_project_folder(root, project["folder_uuid"]) if root else None
    if not folder:
        raise HTTPException(400, "Cartella non trovata su questo PC.")

    msg = message.strip()
    if not msg:
        raise HTTPException(400, "message vuoto")

    # Risolvi la conversazione: usa quella indicata o creane una nuova (titolo
    # dal primo messaggio), rispettando il limite di 20 per fascicolo.
    conv_id: int | None = None
    if conversation_id and conversation_id.isdigit():
        conv = fdb.get_conversation(int(conversation_id), project_id)
        if conv:
            conv_id = conv["id"]
    if conv_id is None:
        try:
            conv_id = fdb.create_conversation(project_id, title=msg[:60], user_id=current_user.id)
        except fdb.ConversationLimitError as exc:
            raise HTTPException(400, str(exc))

    # Snapshot history PRIMA di aggiungere il nuovo user msg (cosi' history
    # non include il messaggio attuale, evita duplicazione nel prompt).
    history = fdb.list_chat_messages(project_id, conversation_id=conv_id, limit=20)
    fdb.add_chat_message(project_id, conversation_id=conv_id, role="user", content=msg, user_id=current_user.id)

    # Retrieve UNA VOLTA: i chunks usati per il prompt sono gli stessi citati.
    chunks = frag.retrieve(folder, msg, k=5)
    # I FOGLI del fascicolo vivono su DB (non nei file): iniettali come fonti
    # cosi' l'assistente puo' leggerli e ragionarci.
    chunks = _sheet_context_chunks(project_id, current_user) + chunks
    citations = [{"file": c["file"], "score": round(c["score"], 4)} for c in chunks]

    async def event_stream():
        full_text = ""
        try:
            async for delta in frag.answer_stream(
                folder, msg, top_k=5, history=history, chunks=chunks,
            ):
                full_text += delta
                payload = json.dumps({"delta": delta}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except Exception as exc:
            log.exception("chat stream failed for project=%s", project_id)
            payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            return

        # Persisti la risposta assistant
        try:
            fdb.add_chat_message(
                project_id, conversation_id=conv_id, role="assistant",
                content=full_text,
                citations=citations,
            )
        except Exception:
            log.exception("failed to persist assistant message for project=%s", project_id)

        # Render markdown server-side per il done event
        from ..markdown_render import render_markdown
        try:
            html = render_markdown(full_text)
        except Exception:
            log.exception("markdown render failed")
            html = None

        done_payload = {"done": True, "citations": citations, "conversation_id": conv_id}
        if html is not None:
            done_payload["html"] = html
        yield f"data: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # No-buffer hints per proxy intermedi (uvicorn -> proxy -> browser).
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{project_id}/chat/clear")
async def fascicoli_chat_clear(
    request: Request,
    project_id: int,
    conversation_id: str = Form(""),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = fdb.get_project(
        project_id, current_user_id=current_user.id,
        architect_view=current_user.is_architect or current_user.is_super_admin,
    )
    if not project:
        raise HTTPException(404)
    cid = int(conversation_id) if conversation_id and conversation_id.isdigit() else None
    fdb.clear_chat_messages(project_id, conversation_id=cid)
    dest = f"/fascicoli/{project_id}" + (f"?conv={cid}" if cid else "")
    return RedirectResponse(url=dest, status_code=302)
