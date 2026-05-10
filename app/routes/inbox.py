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


@router.get("/inbox/contacts", response_class=HTMLResponse)
async def inbox_contacts(request: Request, status: str | None = None):
    contacts = db.list_contacts(status=status, limit=500)
    return templates.TemplateResponse(
        request,
        "inbox_contacts.html",
        {"contacts": contacts, "filter_status": status or ""},
    )


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
