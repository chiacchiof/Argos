"""Proxy pool per outreach social: assegnazione sticky 1:1 account <-> proxy.

Regola chiave: **ogni account social deve usare SEMPRE LO STESSO IP** (sticky
session). Rotation IP per stesso account = red flag immediato per Instagram.

Provider supportati:
- IPRoyal (`https://iproyal.com/proxies/residential/`) — sticky 30-60 min
- Bright Data (`https://brightdata.com/proxy-types/residential-proxies`)
- Smartproxy (`https://smartproxy.com/proxies/residential-proxies`)

Tutti i provider espongono endpoint HTTP(S) tipo:
    http://<username>:<password>@<host>:<port>

Lo username encoda anche la sticky-session ID (varia per provider).

Config: lista provider in env `AGENTSCRAPER_PROXIES` (JSON), oppure tabella DB
`social_proxies`. Per ora supportiamo solo env-based (DB migration in roadmap).

Esempio config in `.env`:
    AGENTSCRAPER_PROXIES='[
      {"label":"ip1","url":"http://user1:pass@host:port","country":"IT"},
      {"label":"ip2","url":"http://user2:pass@host:port","country":"IT"}
    ]'
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class ProxyConfig:
    label: str
    url: str  # http(s)://user:pass@host:port
    country: str | None = None
    assigned_to_account_uuid: str | None = None  # sticky binding


def _load_pool_from_env() -> list[ProxyConfig]:
    # ARGOS_PROXIES (preferito) con fallback AGENTSCRAPER_PROXIES per back-compat.
    raw = (os.environ.get("ARGOS_PROXIES") or os.environ.get("AGENTSCRAPER_PROXIES") or "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("ARGOS_PROXIES / AGENTSCRAPER_PROXIES non e' JSON valido: %s", e)
        return []
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("url"):
            out.append(ProxyConfig(
                label=str(item.get("label") or item["url"][:30]),
                url=item["url"],
                country=item.get("country"),
            ))
    return out


_POOL: list[ProxyConfig] | None = None


def get_pool() -> list[ProxyConfig]:
    """Singleton del pool. Caricato da env."""
    global _POOL  # noqa: PLW0603
    if _POOL is None:
        _POOL = _load_pool_from_env()
        log.info("proxy pool: %d proxies loaded", len(_POOL))
    return _POOL


def assign_proxy_to_account(account_uuid: str) -> ProxyConfig | None:
    """Assegna un proxy ad un account (sticky). Se gia' assegnato, ritorna lo stesso.

    Per ora la binding e' in-memory (perso al restart). Se serve persistenza,
    estendere DB con tabella `social_proxy_bindings`.
    """
    pool = get_pool()
    if not pool:
        return None
    # Cerca binding esistente
    for p in pool:
        if p.assigned_to_account_uuid == account_uuid:
            return p
    # Trova proxy libero
    for p in pool:
        if p.assigned_to_account_uuid is None:
            p.assigned_to_account_uuid = account_uuid
            log.info("proxy %s assigned to account %s", p.label, account_uuid)
            return p
    # Tutti occupati: log warning, ritorna comunque uno (overflow)
    log.warning(
        "tutti i proxy sono gia' assegnati (%d account, %d proxy). "
        "Riuso del primo proxy in overflow.",
        sum(1 for p in pool if p.assigned_to_account_uuid),
        len(pool),
    )
    return pool[0]


def proxy_to_playwright_kwargs(proxy: ProxyConfig | None) -> dict:
    """Converte ProxyConfig in kwargs per Playwright `browser.new_context(proxy=...)`."""
    if proxy is None:
        return {}
    from urllib.parse import urlparse
    try:
        u = urlparse(proxy.url)
    except Exception:
        return {}
    server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
    out = {"server": server}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return {"proxy": out}
