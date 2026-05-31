"""Deep-link builders for opening external messaging/social tools pre-pointed at a contact.

Pure functions: given a lead/asset (or contact) row's contact fields, build https links
that open WhatsApp / Telegram / a social profile / an email composer in a new browser tab.
No network, no DB. The WhatsApp/social URL conventions mirror the outreach runners
(runner_outreach_whatsapp._normalize_e164, import_csv._social_url_for) so the links a user
clicks manually match what the automation would target.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote


# ---- low-level builders ---------------------------------------------------

def normalize_e164_digits(raw: str | None, default_cc: str = "39") -> str | None:
    """Return E.164 digits WITHOUT the leading '+' (wa.me wants bare digits), or None.

    Mirrors runner_outreach_whatsapp._normalize_e164: strips non-digits, drops a 00
    international prefix, and prepends the default country code for local numbers
    (<=10 digits). default_cc is Italy ('39') to match the current user base.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) <= 10:  # local number without country code
        digits = default_cc + digits
    return digits


def whatsapp_link(phone: str | None, text: str | None = None, default_cc: str = "39") -> str | None:
    digits = normalize_e164_digits(phone, default_cc=default_cc)
    if not digits:
        return None
    url = f"https://wa.me/{digits}"
    if text:
        url += "?text=" + quote(text)
    return url


def telegram_link(username: str | None) -> str | None:
    u = (username or "").strip().lstrip("@")
    if not u or " " in u or "/" in u:
        return None
    return f"https://t.me/{u}"


def email_link(addr: str | None, subject: str | None = None, body: str | None = None) -> str | None:
    a = (addr or "").strip()
    if not a or "@" not in a:
        return None
    params = []
    if subject:
        params.append("subject=" + quote(subject))
    if body:
        params.append("body=" + quote(body))
    url = "mailto:" + a
    if params:
        url += "?" + "&".join(params)
    return url


def social_profile_url(platform: str, handle: str | None = None, url: str | None = None) -> str | None:
    """Prefer a stored absolute profile URL; otherwise reconstruct from platform+handle."""
    if url and url.strip().lower().startswith("http"):
        return url.strip()
    # Lazy import: import_csv may pull heavier deps at module load.
    from .import_csv import _social_url_for
    return _social_url_for(platform, handle or "")


# ---- social_json parsing --------------------------------------------------

def _parse_social_json(raw) -> dict:
    """Return {platform: {'handle': str|'', 'url': str|None}} tolerating the stored shapes.

    social_json is canonically a JSON string holding a LIST of {platform, url, handle};
    some rows store a plain {platform: handle} dict or a single item dict.
    """
    if not raw:
        return {}
    data = raw
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return {}
    if isinstance(data, dict):
        if "platform" in data:
            data = [data]
        else:
            return {
                str(k).strip().lower(): {"handle": str(v).strip().lstrip("@"), "url": None}
                for k, v in data.items() if v
            }
    out: dict = {}
    if isinstance(data, list):
        for it in data:
            if not isinstance(it, dict):
                continue
            plat = (it.get("platform") or "").strip().lower()
            if not plat:
                continue
            out[plat] = {
                "handle": (it.get("handle") or "").strip().lstrip("@"),
                "url": (it.get("url") or "").strip() or None,
            }
    return out


# ---- consolidated per-contact builder -------------------------------------

_SOCIAL_LABELS = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "tiktok": "TikTok",
    "linkedin": "LinkedIn",
    "x": "X",
    "twitter": "X",
}
_SOCIAL_LOGO = {
    "facebook": "facebook.svg",
    "instagram": "instagram.svg",
    "linkedin": "linkedin.svg",
    "tiktok": "tiktok.svg",
    "x": "x.svg",
    "twitter": "x.svg",
}


def build_contact_deeplinks(row: dict, *, message: str | None = None) -> list[dict]:
    """Ordered list of {key, label, url, logo} deep-links for a lead/asset/contact row.

    `row` carries whatsapp / telegram_username / email / social_json. `logo` is a filename
    under static/brand/logos/ (None when no brand logo is available). Empty list if the row
    has no reachable channel.
    """
    out: list[dict] = []
    socials = _parse_social_json(row.get("social_json"))

    wa = whatsapp_link(row.get("whatsapp"), text=message)
    if wa:
        out.append({"key": "whatsapp", "label": "WhatsApp", "url": wa, "logo": "whatsapp.svg"})

    tg = telegram_link(row.get("telegram_username"))
    if not tg and "telegram" in socials:
        s = socials["telegram"]
        tg = telegram_link(s.get("handle")) or s.get("url")
    if tg:
        out.append({"key": "telegram", "label": "Telegram", "url": tg, "logo": "telegram.svg"})

    for plat in ("instagram", "facebook", "linkedin", "tiktok", "x", "twitter"):
        if plat in socials:
            s = socials[plat]
            u = social_profile_url(plat, s.get("handle"), s.get("url"))
            if u:
                out.append({
                    "key": plat,
                    "label": _SOCIAL_LABELS.get(plat, plat.title()),
                    "url": u,
                    "logo": _SOCIAL_LOGO.get(plat),
                })

    em = email_link(row.get("email"))
    if em:
        out.append({"key": "email", "label": "Email", "url": em, "logo": "gmail.svg"})

    return out
