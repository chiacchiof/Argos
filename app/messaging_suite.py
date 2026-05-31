"""Account-aware launcher data for the messaging / social tool suite.

`build_suite(current_user)` returns an ordered list of channel descriptors consumed by the
shared partial (templates/partials/messaging_suite.html). Each channel is a service the
tenant can open in one click; if the tenant has configured account(s) for it, the tile
shows as "connected". Pure read, tenant-scoped via the user's tenant_id (passed
explicitly so the result is correct even off the request path, e.g. tests/warmup).
"""
from __future__ import annotations

from . import db
from .auth import CurrentUser


def _safe(fn, **kwargs) -> list:
    """Call a db.list_* helper, degrading to [] so the launcher never breaks a dashboard."""
    try:
        return fn(**kwargs) or []
    except Exception:
        return []


def build_suite(current_user: CurrentUser) -> list[dict]:
    # int for tenant users/architects; None for super_admin => helpers return all tenants.
    tid = current_user.tenant_id

    social = _safe(db.list_social_accounts, tenant_id=tid)
    telegram_bots = _safe(db.list_telegram_bots, tenant_id=tid)
    email_accounts = _safe(db.list_email_accounts, tenant_id=tid)
    wa_api = _safe(db.list_whatsapp_api_config, tenant_id=tid)

    by_platform: dict[str, int] = {}
    for r in social:
        plat = (r.get("platform") or "").strip().lower()
        by_platform[plat] = by_platform.get(plat, 0) + 1

    wa_n = by_platform.get("whatsapp_browser", 0) + len(wa_api)
    fb_n = by_platform.get("facebook", 0)
    ig_n = by_platform.get("instagram", 0)
    x_n = by_platform.get("x", 0) + by_platform.get("twitter", 0)
    tt_n = by_platform.get("tiktok", 0)
    tg_n = len(telegram_bots)
    em_n = len(email_accounts)

    return [
        {
            "key": "whatsapp", "label": "WhatsApp", "logo": "whatsapp.svg",
            "launch_url": "https://web.whatsapp.com/", "launch_label": "Apri WhatsApp Web",
            "connected": wa_n > 0, "n_accounts": wa_n,
            "manage_url": "/accounts/messaging?tab=browser",
        },
        {
            "key": "telegram", "label": "Telegram", "logo": "telegram.svg",
            "launch_url": "https://web.telegram.org/a/", "launch_label": "Apri Telegram Web",
            "connected": tg_n > 0, "n_accounts": tg_n,
            "manage_url": "/accounts/messaging?tab=telegram",
        },
        {
            "key": "messenger", "label": "Messenger", "logo": "messenger.svg",
            "launch_url": "https://www.messenger.com/", "launch_label": "Apri Messenger",
            "connected": fb_n > 0, "n_accounts": fb_n,
            "manage_url": "/social/accounts",
        },
        {
            "key": "facebook", "label": "Facebook", "logo": "facebook.svg",
            "launch_url": "https://www.facebook.com/", "launch_label": "Apri Facebook",
            "connected": fb_n > 0, "n_accounts": fb_n,
            "manage_url": "/social/accounts",
        },
        {
            "key": "instagram", "label": "Instagram", "logo": "instagram.svg",
            "launch_url": "https://www.instagram.com/direct/inbox/", "launch_label": "Apri Instagram",
            "connected": ig_n > 0, "n_accounts": ig_n,
            "manage_url": "/social/accounts",
        },
        {
            "key": "x", "label": "X", "logo": "x.svg",
            "launch_url": "https://x.com/messages/", "launch_label": "Apri X (Twitter)",
            "connected": x_n > 0, "n_accounts": x_n,
            "manage_url": "/social/accounts" if x_n else None,
            "launch_only": x_n == 0,
        },
        {
            "key": "tiktok", "label": "TikTok", "logo": "tiktok.svg",
            "launch_url": "https://www.tiktok.com/messages", "launch_label": "Apri TikTok",
            "connected": tt_n > 0, "n_accounts": tt_n,
            "manage_url": "/social/accounts" if tt_n else None,
            "launch_only": tt_n == 0,
        },
        {
            "key": "linkedin", "label": "LinkedIn", "logo": "linkedin.svg",
            "launch_url": "https://www.linkedin.com/messaging/", "launch_label": "Apri LinkedIn",
            "connected": False, "n_accounts": 0,
            "manage_url": None, "launch_only": True,
        },
        {
            "key": "email", "label": "Email", "logo": "gmail.svg",
            "launch_url": "https://mail.google.com/", "launch_label": "Apri Gmail",
            "connected": em_n > 0, "n_accounts": em_n,
            "manage_url": "/accounts/email",
        },
        {
            "key": "outlook", "label": "Outlook", "logo": "outlook.svg",
            "launch_url": "https://outlook.live.com/mail/", "launch_label": "Apri Outlook",
            "connected": False, "n_accounts": 0,
            "manage_url": None, "launch_only": True,
        },
    ]
