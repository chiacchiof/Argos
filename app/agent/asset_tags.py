"""Derivazione di tag dichiarativi dagli asset estratti.

Ogni `extraction_template` ha un mapping da campi del raw_json a tag (key->values).
I tag sono stringhe normalizzate, indicizzate in `asset_tags` per filtri rapidi.
Niente LLM qui: pure trasformazione deterministica.

Uso:
    from app.agent.asset_tags import derive_tags, derive_title

    tags = derive_tags("real_estate", raw_dict)
    title = derive_title("real_estate", raw_dict)
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUM_RE.search(value)
        if not m:
            return None
        try:
            return float(m.group(0).replace(",", "."))
        except ValueError:
            return None
    return None


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _price_band(amount: float | None, currency: str | None = None) -> str | None:
    """Bucketizza un prezzo in fasce coerenti per filtri UI.
    Usa EUR come riferimento; per altre valute il bucket e' indicativo.
    """
    if amount is None or amount <= 0:
        return None
    if amount < 50_000:
        band = "<50k"
    elif amount < 100_000:
        band = "50-100k"
    elif amount < 200_000:
        band = "100-200k"
    elif amount < 300_000:
        band = "200-300k"
    elif amount < 500_000:
        band = "300-500k"
    elif amount < 1_000_000:
        band = "500k-1M"
    else:
        band = ">1M"
    if currency and currency.upper() not in {"EUR", ""}:
        return f"{band} {currency.upper()}"
    return band


def _rent_band(amount: float | None) -> str | None:
    if amount is None or amount <= 0:
        return None
    if amount < 500:
        return "<500"
    if amount < 800:
        return "500-800"
    if amount < 1200:
        return "800-1200"
    if amount < 2000:
        return "1200-2000"
    return ">2000"


def _domain_from(value: Any) -> str | None:
    if not value:
        return None
    try:
        host = urlparse(str(value)).hostname or ""
        return host.lower() or None
    except Exception:
        return None


def derive_tags(asset_type: str, raw: dict[str, Any]) -> dict[str, list[str]]:
    """Ritorna mapping `{tag_key: [values]}` derivato da un raw_json di asset.

    asset_type non riconosciuto -> ritorna solo tag base (lang, source_domain).
    """
    tags: dict[str, list[str]] = {}

    def add(key: str, value: Any) -> None:
        v = _norm(value)
        if not v:
            return
        tags.setdefault(key, [])
        if v not in tags[key]:
            tags[key].append(v)

    # Tag generali sempre presenti
    add("lang", raw.get("lang"))
    add(
        "source_domain",
        raw.get("source_domain") or _domain_from(raw.get("source_url") or raw.get("url")),
    )

    if asset_type == "real_estate":
        add("tipo", raw.get("tipo"))
        add("categoria", raw.get("categoria"))
        add("citta", raw.get("citta"))
        add("classe_energetica", raw.get("classe_energetica"))
        locali = raw.get("locali")
        if isinstance(locali, (int, float)):
            add("locali", str(int(locali)))
        prezzo = _to_float(raw.get("prezzo_eur"))
        tipo = (raw.get("tipo") or "").lower()
        if "affitt" in tipo:
            band = _rent_band(prezzo)
        else:
            band = _price_band(prezzo, currency="EUR")
        if band:
            add("price_band", band)
        mq = _to_float(raw.get("metri_quadri"))
        if mq:
            if mq < 50:
                mqband = "<50"
            elif mq < 80:
                mqband = "50-80"
            elif mq < 120:
                mqband = "80-120"
            elif mq < 200:
                mqband = "120-200"
            else:
                mqband = ">200"
            add("mq_band", mqband)
        return tags

    if asset_type == "ecommerce_products":
        add("category", raw.get("category"))
        add("brand", raw.get("brand"))
        add("availability", raw.get("availability"))
        cur = raw.get("price_currency")
        amt = _to_float(raw.get("price_amount"))
        band = _price_band(amt, currency=cur)
        if band:
            add("price_band", band)
        rating = _to_float(raw.get("rating_avg"))
        if rating is not None:
            if rating >= 4.5:
                add("rating_band", "4.5+")
            elif rating >= 4:
                add("rating_band", "4+")
            elif rating >= 3:
                add("rating_band", "3+")
            else:
                add("rating_band", "<3")
        return tags

    if asset_type == "events":
        add("category", raw.get("category"))
        add("city", raw.get("city"))
        add("country", raw.get("country"))
        add("organizer", raw.get("organizer"))
        is_free = raw.get("is_free")
        if is_free is True:
            add("is_free", "yes")
        elif is_free is False:
            add("is_free", "no")
        is_sold_out = raw.get("is_sold_out")
        if is_sold_out is True:
            add("availability", "sold_out")
        return tags

    if asset_type == "news_articles":
        add("category", raw.get("category"))
        add("author", raw.get("author"))
        for t in raw.get("tags") or []:
            add("topic", t)
        wc = raw.get("word_count")
        if isinstance(wc, (int, float)):
            wc = int(wc)
            if wc < 300:
                add("length_band", "short")
            elif wc < 1000:
                add("length_band", "medium")
            else:
                add("length_band", "long")
        return tags

    if asset_type == "job_listings":
        add("company", raw.get("company"))
        add("location", raw.get("location"))
        add("remote_policy", raw.get("remote_policy"))
        add("employment_type", raw.get("employment_type"))
        add("experience_level", raw.get("experience_level"))
        amt_min = _to_float(raw.get("salary_min_eur"))
        amt_max = _to_float(raw.get("salary_max_eur"))
        ref = amt_max or amt_min
        if ref:
            if ref < 25_000:
                add("salary_band", "<25k")
            elif ref < 40_000:
                add("salary_band", "25-40k")
            elif ref < 60_000:
                add("salary_band", "40-60k")
            elif ref < 90_000:
                add("salary_band", "60-90k")
            else:
                add("salary_band", ">90k")
        return tags

    if asset_type == "profile_contacts":
        if raw.get("email"):
            add("contact", "email")
        if raw.get("whatsapp"):
            add("contact", "whatsapp")
        if raw.get("telegram"):
            add("contact", "telegram")
        for s in raw.get("social") or []:
            if isinstance(s, dict) and s.get("platform"):
                add("social", s["platform"])
        return tags

    return tags


def derive_title(asset_type: str, raw: dict[str, Any]) -> str | None:
    """Stringa breve (~120 char) per la lista UI."""
    candidates: list[str] = []
    if asset_type == "real_estate":
        bits: list[str] = []
        if raw.get("categoria"):
            bits.append(str(raw["categoria"]))
        if raw.get("citta"):
            bits.append(str(raw["citta"]))
        prezzo = _to_float(raw.get("prezzo_eur"))
        if prezzo:
            bits.append(f"€{int(prezzo):,}".replace(",", "."))
        candidates.append(" · ".join(bits))
    elif asset_type == "ecommerce_products":
        candidates.append(str(raw.get("name") or "").strip())
    elif asset_type == "events":
        candidates.append(str(raw.get("title") or "").strip())
    elif asset_type == "news_articles":
        candidates.append(str(raw.get("title") or "").strip())
    elif asset_type == "job_listings":
        bits = [str(raw.get("title") or ""), str(raw.get("company") or "")]
        candidates.append(" · ".join(b for b in bits if b))
    elif asset_type == "profile_contacts":
        bits = [
            str(raw.get("display_name") or raw.get("username") or "").strip(),
            str(raw.get("source_domain") or "").strip(),
        ]
        candidates.append(" · ".join(b for b in bits if b))
    candidates.append(str(raw.get("page_title") or "").strip())
    candidates.append(str(raw.get("url") or raw.get("source_url") or "").strip())
    for c in candidates:
        c = (c or "").strip()
        if c:
            return c[:200]
    return None
