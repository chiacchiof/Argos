"""Permessi condivisi tra route Fascicoli e Fogli collaborativi.

Estratti da `app/routes/fascicoli.py` per evitare duplicazione (vedi
`docs/argos_fogli_collaborativi_plan.md` §Permessi).

Tutte le funzioni sono PURE: prendono `dict` gia' tenant-scoped (caricati con le
query di `app/fascicoli/db.py`, che filtrano per tenant) + il `CurrentUser`
autenticato lato server. NON ricevono mai un `user_id` arbitrario dal client.

Modello fogli (deciso con l'utente 2026-05-29): i fogli sono strumenti
collaborativi del tenant, creabili e gestibili sia DENTRO un fascicolo
(`project_id` valorizzato) sia STANDALONE (`project_id` NULL). Il tenant deve
poterli sempre aprire da qualsiasi postazione (sorgente di verita' su DB).

  - apri/leggi  : architect/super_admin sempre; foglio agganciato => segue la
                  visibilita' del progetto; standalone => 'tenant' a tutti, 'user'
                  solo al creatore.
  - modifica    : architect/super_admin sempre; agganciato => editor/owner del
                  progetto; standalone 'tenant' => ogni membro del tenant
                  (operatore incluso); standalone 'user' => solo creatore.
  - gestisci    : (rename/archive/delete/visibilita') architect/super_admin,
                  creatore, oppure chi puo' gestire il progetto agganciato.
"""
from __future__ import annotations

from typing import Any

from ..auth import CurrentUser
from . import db as fdb
from . import sheets_db as sdb


# ---------------------------------------------------------------------------
# Progetti (fascicoli) — estratti da routes/fascicoli.py
# ---------------------------------------------------------------------------

def can_edit_project(project: dict, user: CurrentUser) -> bool:
    """Owner / architect / super-admin / editor (project_users role='editor')."""
    if project["owner_user_id"] == user.id:
        return True
    if user.is_architect or user.is_super_admin:
        return True
    for m in fdb.list_project_members(project["id"]):
        if m["user_id"] == user.id and m["role"] == "editor":
            return True
    return False


def can_manage_project(project: dict, user: CurrentUser) -> bool:
    """Permessi piu' forti: cambio visibilita', archive, gestione membri.
    Solo owner / architect / super-admin (gli editor NON possono)."""
    if project["owner_user_id"] == user.id:
        return True
    if user.is_architect or user.is_super_admin:
        return True
    return False


# ---------------------------------------------------------------------------
# Fogli collaborativi
# ---------------------------------------------------------------------------

def can_open_sheet(sheet: dict, project: dict | None, user: CurrentUser) -> bool:
    """Puo' aprire e vedere il foglio (lettura + realtime read-only).

    `project` e' il record del fascicolo agganciato GIA' filtrato per
    visibilita'/tenant (None se il foglio e' standalone oppure se il progetto
    non e' visibile all'utente)."""
    if user.is_super_admin or user.is_architect:
        return True
    if sheet.get("project_id"):
        # Agganciato: visibile solo se il progetto e' visibile all'utente.
        return project is not None
    # Standalone.
    if sheet.get("visibility") == "tenant":
        return True
    if sheet.get("created_by_user_id") == user.id:
        return True
    # foglio privato condiviso esplicitamente con l'utente (qualsiasi ruolo)
    return sdb.sheet_member_role(sheet["id"], user.id) is not None


def can_edit_sheet_cells(sheet: dict, project: dict | None, user: CurrentUser) -> bool:
    """Puo' modificare le celle (inviare cell_patch)."""
    if user.is_super_admin or user.is_architect:
        return True
    if sheet.get("project_id"):
        return project is not None and can_edit_project(project, user)
    if sheet.get("visibility") == "tenant":
        return True  # tenant-collaborativo: ogni membro del tenant modifica
    if sheet.get("created_by_user_id") == user.id:
        return True
    # foglio privato: editabile solo da chi e' stato condiviso come 'editor'
    return sdb.sheet_member_role(sheet["id"], user.id) == "editor"


def can_manage_sheet(sheet: dict, project: dict | None, user: CurrentUser) -> bool:
    """Rename / archive / delete / cambio visibilita'."""
    if user.is_super_admin or user.is_architect:
        return True
    if sheet.get("created_by_user_id") == user.id:
        return True
    if sheet.get("project_id") and project is not None:
        return can_manage_project(project, user)
    return False
