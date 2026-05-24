"""Memoria sito (CRUD): gestione `site_patterns` + `site_playbooks`.

`site_patterns` = regex di pattern URL apprese dai runner per dominio.
`site_playbooks` = istruzioni operative cross-runner (Stage 2 knowledge transfer).

Pagina unica per ispezionare e cancellare entries selettive o l'intera memoria.
Utile prima di test "puliti da 0" o quando un sito cambia struttura.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, db_cloud
from ..templates import templates


router = APIRouter()


@router.get("/site_memory", response_class=HTMLResponse)
async def site_memory_list(
    request: Request,
    domain: str | None = None,
):
    """Lista pattern + playbook visibili al tenant corrente (o tutti per
    super_admin / tenant con `site_memory_shared=TRUE`).

    Il filtraggio per tenant_id avviene dentro `db.list_site_patterns` /
    `db.list_site_playbooks` via `_site_memory_tenant_filter`.
    """
    domain = (domain or "").strip().lower() or None

    patterns = db.list_site_patterns(registrable_domain=domain, limit=500)
    playbooks_raw = db.list_site_playbooks(registrable_domain=domain, limit=500)
    intelligence = db.list_site_intelligence(registrable_domain=domain, limit=500)
    policies = db.list_scraping_policies(active_only=False)

    # I playbook hanno il campo `playbook` JSON-serializzato: parsiamolo per la view.
    playbooks = []
    for r in playbooks_raw:
        pb_text = ""
        pb_blockers: list = []
        try:
            obj = json.loads(r.get("playbook") or "{}")
            pb_text = obj.get("text") or ""
            pb_blockers = obj.get("blockers") or []
        except Exception:
            pb_text = r.get("playbook") or ""
        playbooks.append({**r, "playbook_text": pb_text, "playbook_blockers": pb_blockers})

    # Domini distinti per il dropdown filtro
    all_domains = sorted({
        *(p.get("registrable_domain") for p in patterns if p.get("registrable_domain")),
        *(p.get("registrable_domain") for p in playbooks if p.get("registrable_domain")),
        *(i.get("registrable_domain") for i in intelligence if i.get("registrable_domain")),
    })

    # Info visibilita' per la banner-info nel template.
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    visibility_mode = "all"
    if not is_super_admin:
        # Lookup del flag del tenant corrente per mostrare il "perche'" all'utente.
        tenant_id = db.current_tenant_id()
        visibility_mode = "shared" if db._can_see_all_site_memory(tenant_id) else "isolated"

    # Per super-admin: map id -> nome parlante del tenant (mostrato nelle tabelle).
    tenant_names: dict[int, str] = {}
    if is_super_admin:
        try:
            for t in db_cloud.list_tenants():
                tenant_names[int(t["id"])] = t.get("name") or t.get("slug") or f"#{t['id']}"
        except Exception:
            pass

    return templates.TemplateResponse(
        request,
        "site_memory.html",
        {
            "patterns": patterns,
            "playbooks": playbooks,
            "intelligence": intelligence,
            "policies": policies,
            "filter_domain": domain or "",
            "all_domains": all_domains,
            "n_patterns": len(patterns),
            "n_playbooks": len(playbooks),
            "n_intelligence": len(intelligence),
            "n_policies": len(policies),
            "is_super_admin": is_super_admin,
            "visibility_mode": visibility_mode,
            "tenant_names": tenant_names,
        },
    )


@router.post("/site_memory/pattern/{pattern_id}/delete")
async def site_memory_delete_pattern(pattern_id: int, request: Request):
    db.delete_site_pattern(pattern_id)
    return RedirectResponse(url="/site_memory", status_code=303)


@router.post("/site_memory/playbook/{playbook_id}/delete")
async def site_memory_delete_playbook(playbook_id: int, request: Request):
    db.delete_site_playbook(playbook_id)
    return RedirectResponse(url="/site_memory", status_code=303)


@router.post("/site_memory/playbook/{playbook_id}/disable")
async def site_memory_disable_playbook(playbook_id: int):
    """Cambia status a 'disabled': il playbook resta in DB (audit) ma non
    viene piu' riapplicato nei prossimi run su quel dominio."""
    n = db.set_site_playbook_status(playbook_id, "disabled")
    return RedirectResponse(
        url=f"/site_memory?_msg={'disabilitato' if n else 'gia+disabilitato'}+playbook+%23{playbook_id}",
        status_code=303,
    )


@router.post("/site_memory/playbook/{playbook_id}/reactivate")
async def site_memory_reactivate_playbook(playbook_id: int):
    """Riattiva un playbook stale/disabled. Resetta `failures` a 0 cosi' il
    primo riuso non lo ri-stale-isca immediatamente."""
    n = db.set_site_playbook_status(playbook_id, "active")
    # Reset failures (best-effort, query separata)
    if n:
        try:
            with db.connect() as con:
                con.execute(
                    "UPDATE site_playbooks SET failures = 0, updated_at = %s "
                    "WHERE id = %s",
                    (db.now_iso(), playbook_id),
                )
        except Exception:
            pass
    return RedirectResponse(
        url=f"/site_memory?_msg={'riattivato' if n else 'non+trovato'}+playbook+%23{playbook_id}",
        status_code=303,
    )


@router.post("/site_memory/domain/delete")
async def site_memory_delete_domain(domain: str = Form("")):
    """Cancella tutti i pattern + playbook di un singolo dominio."""
    domain = (domain or "").strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="domain mancante")
    n_pat = db.delete_site_patterns_by_domain(domain)
    n_pb = db.delete_site_playbooks_by_domain(domain)
    return RedirectResponse(
        url=f"/site_memory?_msg=cancellati+{n_pat}+pattern+e+{n_pb}+playbook+per+{domain}",
        status_code=303,
    )


@router.post("/site_memory/flush_all")
async def site_memory_flush_all(confirm: str = Form("")):
    """Svuota completamente la memoria sito. Richiede confirm='YES'."""
    if confirm.strip().upper() != "YES":
        raise HTTPException(status_code=400, detail="confirm='YES' richiesto")
    res = db.truncate_site_memory()
    parts = [f"{v}+{k.replace('_','+')}" for k, v in res.items()]
    return RedirectResponse(
        url=f"/site_memory?_msg=svuotata+memoria:+{'+'.join(parts)}",
        status_code=303,
    )


# ===========================================================================
# Site intelligence: delete singolo record (l'upsert e' automatico post-job)
# ===========================================================================

@router.post("/site_memory/intelligence/{intel_id}/delete")
async def site_memory_delete_intelligence(intel_id: int):
    """Cancella una riga di intelligence (es. per resettare la storia di un
    sito che ha temporaneamente avuto problemi)."""
    n = db.delete_site_intelligence(intel_id)
    return RedirectResponse(
        url=f"/site_memory?_msg={'cancellata' if n else 'non+trovata'}+intel+%23{intel_id}",
        status_code=303,
    )


@router.post("/site_memory/intelligence/{intel_id}/toggle-visibility")
async def site_memory_toggle_intel_visibility(intel_id: int):
    """Toggle visibility (private <-> shared) di una riga intelligence.
    'shared' = la rendi visibile a tutti i tenant nel pool community."""
    rows = db.list_site_intelligence()
    cur = next((r for r in rows if int(r["id"]) == intel_id), None)
    if not cur:
        raise HTTPException(status_code=404, detail="intel non trovata")
    new_vis = "private" if cur.get("visibility") == "shared" else "shared"
    n = db.set_site_intelligence_visibility(intel_id, new_vis)
    return RedirectResponse(
        url=f"/site_memory?_msg=intel+%23{intel_id}+{'condivisa+nel+pool' if (n and new_vis=='shared') else 'resa+privata'}",
        status_code=303,
    )


@router.post("/site_memory/policy/{policy_id}/toggle-visibility")
async def site_memory_toggle_policy_visibility(policy_id: int):
    """Toggle visibility policy (private <-> shared)."""
    policies = db.list_scraping_policies(active_only=False)
    cur = next((p for p in policies if int(p["id"]) == policy_id), None)
    if not cur:
        raise HTTPException(status_code=404, detail="policy non trovata")
    new_vis = "private" if cur.get("visibility") == "shared" else "shared"
    n = db.set_scraping_policy_visibility(policy_id, new_vis)
    return RedirectResponse(
        url=f"/site_memory?_msg=policy+%23{policy_id}+{'condivisa+nel+pool' if (n and new_vis=='shared') else 'resa+privata'}",
        status_code=303,
    )


# ===========================================================================
# Scraping policies: CRUD manuale (regole match → action)
# ===========================================================================

@router.post("/site_memory/policy/create")
async def site_memory_create_policy(
    match_pattern: str = Form(""),
    action: str = Form("skip"),
    match_kind: str = Form("domain_regex"),
    reason: str = Form(""),
    priority: str = Form("100"),
):
    """Crea una nuova policy manuale dal form UI."""
    if not match_pattern.strip():
        raise HTTPException(status_code=400, detail="match_pattern richiesto")
    try:
        pid = db.create_scraping_policy(
            match_pattern=match_pattern.strip(),
            action=action.strip(),
            match_kind=match_kind.strip() or "domain_regex",
            reason=(reason.strip() or None),
            source="manual",
            priority=int(priority) if str(priority).strip().lstrip("-").isdigit() else 100,
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/site_memory?_msg=errore+policy:+{e}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/site_memory?_msg=creata+policy+%23{pid}",
        status_code=303,
    )


@router.post("/site_memory/policy/{policy_id}/toggle")
async def site_memory_toggle_policy(policy_id: int):
    """Toggle active flag della policy."""
    policies = db.list_scraping_policies(active_only=False)
    cur = next((p for p in policies if int(p["id"]) == policy_id), None)
    if not cur:
        raise HTTPException(status_code=404, detail="policy non trovata")
    new_active = not bool(cur.get("active"))
    db.update_scraping_policy(policy_id, active=new_active)
    return RedirectResponse(
        url=f"/site_memory?_msg=policy+%23{policy_id}+{'attivata' if new_active else 'disattivata'}",
        status_code=303,
    )


@router.post("/site_memory/policy/{policy_id}/delete")
async def site_memory_delete_policy(policy_id: int):
    """Cancella una policy."""
    n = db.delete_scraping_policy(policy_id)
    return RedirectResponse(
        url=f"/site_memory?_msg={'cancellata' if n else 'non+trovata'}+policy+%23{policy_id}",
        status_code=303,
    )
