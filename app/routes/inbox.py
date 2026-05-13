"""Inbox: lista thread, dettaglio thread con cronologia messaggi, opt-out manuale."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..channels import email as ch_email
from ..channels import telegram as ch_telegram
from ..templates import templates


router = APIRouter()


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_list(
    request: Request,
    channel: str | None = None,
    status: str | None = None,
):
    threads = db.list_threads(channel=channel, status=status, limit=200)
    return templates.TemplateResponse(
        request,
        "inbox_list.html",
        {
            "threads": threads,
            "filter_channel": channel or "",
            "filter_status": status or "",
        },
    )


_CONTACTS_PAGE_SIZE = 100


@router.get("/inbox/contacts", response_class=HTMLResponse)
async def inbox_contacts(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    channel: str | None = None,
    source_domain: str | None = None,
    score_min: str | None = None,  # str perche' il <select> manda "" quando "qualsiasi"
    page: str | int = 1,
    per_page: str | int = _CONTACTS_PAGE_SIZE,
):
    # Parsing tollerante page/per_page
    try:
        page = int(str(page).strip() or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(str(per_page).strip() or _CONTACTS_PAGE_SIZE)
    except (TypeError, ValueError):
        per_page = _CONTACTS_PAGE_SIZE
    status_clean = (status or "").strip() or None
    q_clean = (q or "").strip() or None
    channel_clean = (channel or "").strip().lower() or None
    domain_clean = (source_domain or "").strip().lower() or None
    # Parsing tollerante: "" / non-int → None, altrimenti clamp [0..10]
    score_clean: int | None = None
    if score_min:
        try:
            v = int(str(score_min).strip())
            if 0 <= v <= 10:
                score_clean = v
        except (TypeError, ValueError):
            pass
    per_page = max(10, min(int(per_page or _CONTACTS_PAGE_SIZE), 500))
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page

    total = db.count_contacts(
        status=status_clean,
        search=q_clean,
        channel=channel_clean,
        source_domain=domain_clean,
        score_min=score_clean,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page

    contacts = db.list_contacts(
        status=status_clean,
        search=q_clean,
        channel=channel_clean,
        source_domain=domain_clean,
        score_min=score_clean,
        limit=per_page,
        offset=offset,
    )

    # Per dropdown filtri: domini source noti
    available_domains = db.list_contact_source_domains(limit=50)

    # Querystring base (preserva filtri attivi nei link di paginazione)
    from urllib.parse import urlencode
    qs_dict = {}
    if status_clean: qs_dict["status"] = status_clean
    if q_clean: qs_dict["q"] = q_clean
    if channel_clean: qs_dict["channel"] = channel_clean
    if domain_clean: qs_dict["source_domain"] = domain_clean
    if score_clean is not None: qs_dict["score_min"] = score_clean
    if per_page != _CONTACTS_PAGE_SIZE: qs_dict["per_page"] = per_page
    qs_base = urlencode(qs_dict)

    return templates.TemplateResponse(
        request,
        "inbox_contacts.html",
        {
            "contacts": contacts,
            "filter_status": status_clean or "",
            "filter_q": q_clean or "",
            "filter_channel": channel_clean or "",
            "filter_source_domain": domain_clean or "",
            "filter_score_min": score_clean if score_clean is not None else "",
            "available_domains": available_domains,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "offset": offset,
            "qs_base": qs_base,
        },
    )


@router.post("/inbox/contacts/add")
async def inbox_contacts_add(
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    telegram_username: str = Form(""),
    whatsapp: str = Form(""),
    sitoweb: str = Form(""),
    instagram_url: str = Form(""),
    tiktok_url: str = Form(""),
    facebook_url: str = Form(""),
    source_url: str = Form(""),
    source_domain: str = Form(""),
    notes: str = Form(""),
    status: str = Form("qualified"),
):
    """Inserisce manualmente un contatto outreach. Almeno UN canale (email,
    telegram, whatsapp, sitoweb, social) deve essere valorizzato — altrimenti
    rifiuta perche' non sarebbe contattabile.
    """
    import json as _json

    # Costruisci social array da campi separati
    socials: list[dict[str, str]] = []
    for platform, url in (
        ("instagram", instagram_url),
        ("tiktok", tiktok_url),
        ("facebook", facebook_url),
    ):
        url = (url or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            socials.append({"platform": platform, "url": url})

    # Validazione: almeno UN canale
    has_channel = bool(
        (email or "").strip()
        or (telegram_username or "").strip()
        or (whatsapp or "").strip()
        or (sitoweb or "").strip()
        or socials
    )
    if not has_channel:
        raise HTTPException(
            status_code=400,
            detail="Almeno un canale di contatto (email/telegram/whatsapp/sitoweb/social) deve essere valorizzato.",
        )

    # Infer source_domain da source_url se non specificato
    sd = (source_domain or "").strip().lower() or None
    if not sd and source_url:
        from urllib.parse import urlparse
        try:
            sd = (urlparse(source_url).hostname or "").lower() or None
        except Exception:
            sd = None
    # Default source: manuale
    if not sd:
        sd = "manual"

    payload = {
        "source_url": (source_url or "").strip() or f"manual:{(email or telegram_username or whatsapp or 'unnamed').strip()}",
        "source_domain": sd,
        "display_name": (display_name or "").strip() or None,
        "email": (email or "").strip() or None,
        "telegram_username": (telegram_username or "").strip().lstrip("@") or None,
        "whatsapp": (whatsapp or "").strip() or None,
        "sitoweb": (sitoweb or "").strip() or None,
        "social": socials if socials else None,
        "raw_json": _json.dumps({
            "_manual_entry": True,
            "display_name": display_name,
            "notes": notes,
        }, ensure_ascii=False),
        "status": (status or "qualified").strip() or "qualified",
    }
    cid = db.upsert_contact(payload)
    # Status (upsert_contact non lo gestisce direttamente, lo settiamo)
    db.update_contact_status(cid, payload["status"], notes=notes.strip() or None)
    return RedirectResponse(url=f"/inbox/contacts?flash=Contatto+%23{cid}+aggiunto", status_code=303)


@router.get("/inbox/{thread_id}", response_class=HTMLResponse)
async def inbox_thread(request: Request, thread_id: int):
    thread = db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread non trovato")
    contact = db.get_contact(thread["contact_id"])
    messages = db.list_messages(thread_id)
    return templates.TemplateResponse(
        request,
        "inbox_thread.html",
        {"thread": thread, "contact": contact, "messages": messages},
    )


@router.post("/inbox/{thread_id}/reply", response_class=HTMLResponse)
async def inbox_thread_reply(
    request: Request,
    thread_id: int,
    body: str = Form(""),
):
    thread = db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread non trovato")
    body = body.strip()
    if not body:
        return RedirectResponse(url=f"/inbox/{thread_id}", status_code=303)
    contact = db.get_contact(thread["contact_id"])
    if not contact:
        raise HTTPException(status_code=404, detail="contatto non trovato")

    try:
        if thread["channel"] == "email":
            subject = thread.get("subject") or "(senza oggetto)"
            in_reply_to = thread.get("external_id")
            msg_id = await ch_email.send_email(
                contact["email"], f"Re: {subject}" if not subject.lower().startswith("re:") else subject,
                body, in_reply_to=in_reply_to,
            )
            db.insert_message(thread_id, "out", body, llm_generated=False,
                              external_id=msg_id, status="sent", sent_at=db.now_iso())
        elif thread["channel"] == "telegram":
            chat_id = contact.get("telegram_chat_id") or thread.get("external_id")
            if not chat_id:
                raise RuntimeError("chat_id non disponibile")
            msg_id = await ch_telegram.send_message(chat_id, body)
            db.insert_message(thread_id, "out", body, llm_generated=False,
                              external_id=msg_id, status="sent", sent_at=db.now_iso())
        db.touch_thread(thread_id)
        db.update_thread_status(thread_id, "replied")
        db.update_contact_status(contact["id"], "contacted")
    except Exception as e:
        db.insert_message(thread_id, "out", body, llm_generated=False,
                          status="failed", error=str(e))
    return RedirectResponse(url=f"/inbox/{thread_id}", status_code=303)


@router.get("/inbox/contacts/{contact_id}/edit", response_class=HTMLResponse)
async def contact_edit_form(request: Request, contact_id: int):
    contact = db.get_contact(contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="contatto non trovato")
    # Decifra il social_json in lista comprensibile dal template
    import json as _json
    socials_by_platform = {"instagram": "", "tiktok": "", "facebook": ""}
    sj = contact.get("social_json")
    if sj:
        try:
            arr = _json.loads(sj) if isinstance(sj, str) else sj
            if isinstance(arr, list):
                for s in arr:
                    if not isinstance(s, dict):
                        continue
                    plat = (s.get("platform") or "").lower()
                    if plat in socials_by_platform:
                        socials_by_platform[plat] = s.get("url") or ""
        except (_json.JSONDecodeError, TypeError):
            pass
    return templates.TemplateResponse(
        request,
        "inbox_contact_edit.html",
        {
            "contact": contact,
            "socials_by_platform": socials_by_platform,
        },
    )


@router.post("/inbox/contacts/{contact_id}/edit")
async def contact_edit_submit(
    contact_id: int,
    display_name: str = Form(""),
    email: str = Form(""),
    telegram_username: str = Form(""),
    whatsapp: str = Form(""),
    sitoweb: str = Form(""),
    instagram_url: str = Form(""),
    tiktok_url: str = Form(""),
    facebook_url: str = Form(""),
    source_url: str = Form(""),
    source_domain: str = Form(""),
    notes: str = Form(""),
    status: str = Form(""),
    whatsapp_consent: str = Form(""),
):
    """Aggiorna i campi modificabili di un contatto."""
    existing = db.get_contact(contact_id)
    if not existing:
        raise HTTPException(status_code=404, detail="contatto non trovato")

    import json as _json
    # Costruisci nuovo social_json se almeno uno dei campi è popolato
    socials: list[dict[str, str]] = []
    for platform, url in (
        ("instagram", instagram_url),
        ("tiktok", tiktok_url),
        ("facebook", facebook_url),
    ):
        url = (url or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            socials.append({"platform": platform, "url": url})

    # Normalizza source_domain
    sd = (source_domain or "").strip().lower() or None
    src_url = (source_url or "").strip()
    if not sd and src_url:
        from urllib.parse import urlparse
        try:
            sd = (urlparse(src_url).hostname or "").lower() or None
        except Exception:
            sd = None

    # Valida whatsapp_consent (None se vuoto = mantiene esistente)
    wc = (whatsapp_consent or "").strip().lower()
    if wc and wc not in ("cold", "opt_in", "optedout"):
        wc = None  # ignora valori invalidi

    # Costruisci fields da aggiornare (None = non tocco il campo, "" = azzero)
    fields: dict[str, object] = {
        "display_name": (display_name or "").strip() or None,
        "email": (email or "").strip() or None,
        "telegram_username": (telegram_username or "").strip().lstrip("@") or None,
        "whatsapp": (whatsapp or "").strip() or None,
        "sitoweb": (sitoweb or "").strip() or None,
        "social_json": _json.dumps(socials, ensure_ascii=False) if socials else None,
        "source_url": src_url or None,
        "source_domain": sd,
    }
    if status:
        fields["status"] = status.strip()
    if wc:
        fields["whatsapp_consent"] = wc

    # Notes: aggiornato solo se l'utente lo scrive nel form
    if notes:
        fields["notes"] = notes.strip()

    db.update_contact(contact_id, fields)
    return RedirectResponse(
        url=f"/inbox/contacts?flash=Contatto+%23{contact_id}+aggiornato",
        status_code=303,
    )


@router.post("/inbox/contacts/{contact_id}/optout")
async def contact_optout(contact_id: int):
    db.update_contact_status(contact_id, "optedout", notes="Opt-out manuale")
    return RedirectResponse(url="/inbox/contacts", status_code=303)


@router.post("/inbox/contacts/{contact_id}/reset")
async def contact_reset(contact_id: int):
    db.update_contact_status(contact_id, "qualified", notes="Reset manuale (re-contattabile)")
    return RedirectResponse(url="/inbox/contacts", status_code=303)


@router.post("/inbox/contacts/{contact_id}/delete")
async def contact_delete(contact_id: int):
    db.delete_contact(contact_id)
    return RedirectResponse(
        url=f"/inbox/contacts?flash=Contatto+%23{contact_id}+cancellato",
        status_code=303,
    )


@router.post("/inbox/contacts/delete-bulk")
async def contacts_delete_bulk(
    request: Request,
    redirect_to: str = Form("/inbox/contacts"),
):
    form = await request.form()
    raw_ids = form.getlist("contact_ids") if hasattr(form, "getlist") else form.get("contact_ids")
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids] if raw_ids else []
    ids: list[int] = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    n = db.delete_contacts_bulk(ids) if ids else 0
    target = redirect_to if redirect_to.startswith("/") else "/inbox/contacts"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        url=f"{target}{sep}flash={n}+contatti+cancellati",
        status_code=303,
    )
