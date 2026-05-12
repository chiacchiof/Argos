"""HTTP fetcher unificato con TLS fingerprint impersonation.

Wraps `curl_cffi.AsyncSession` per imitare il TLS handshake di un browser reale
(Chrome 120 di default). Bypassa Cloudflare e altri anti-bot che filtrano
client HTTP-plain (httpx, requests) tramite JA3 fingerprint.

Espone una API minima che sostituisce httpx.AsyncClient nei callsite dove serve
bypassare anti-bot — `site_recon`, `site_profiler`, fetch_page del site_explorer,
ecc. Tutto domain-agnostic.

Fallback: se `curl_cffi` non e' installato, ritorna a `httpx.AsyncClient`.
L'impersonate viene ignorato in quel caso.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


# Default impersonation profile. Aggiornare a "chrome124" o profili piu' recenti
# quando i siti targettano specificamente versioni vecchie.
DEFAULT_IMPERSONATE = "chrome120"

# Default UA: comunque inviato come header oltre al TLS fingerprint, per uniformita'.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    _HAS_CURL_CFFI = True
except ImportError:
    _CurlSession = None  # type: ignore
    _HAS_CURL_CFFI = False
    log.warning(
        "curl_cffi non disponibile, fallback su httpx (anti-bot bypass disattivato). "
        "Installa con: pip install curl_cffi"
    )


class FetchResponse:
    """Wrapper minimale comune per risposte da curl_cffi o httpx."""

    def __init__(self, status_code: int, text: str, url: str, headers: dict):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400


class HttpFetcher:
    """Context manager async che incapsula una sessione HTTP con TLS impersonation.

    Uso tipico:
        async with HttpFetcher() as fetcher:
            r = await fetcher.get(url)
            if r.ok:
                process(r.text)

    Se vuoi disabilitare l'impersonation per un dominio sicuro (overhead minimo,
    ~5ms in piu' per request), passa `impersonate=None`.
    """

    def __init__(
        self,
        *,
        impersonate: Optional[str] = DEFAULT_IMPERSONATE,
        user_agent: str = DEFAULT_UA,
        timeout: float = 15.0,
        follow_redirects: bool = True,
    ):
        self.impersonate = impersonate
        self.user_agent = user_agent
        self.timeout = timeout
        self.follow_redirects = follow_redirects
        self._session: Any = None
        self._is_curl = False

    async def __aenter__(self) -> "HttpFetcher":
        if _HAS_CURL_CFFI:
            self._session = _CurlSession()
            self._is_curl = True
        else:
            import httpx
            self._session = httpx.AsyncClient(
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                follow_redirects=self.follow_redirects,
            )
            self._is_curl = False
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is None:
            return
        try:
            if self._is_curl:
                await self._session.close()
            else:
                await self._session.aclose()
        except Exception as e:
            log.debug("session close failed: %s", e)
        self._session = None

    async def get(
        self,
        url: str,
        *,
        impersonate: Optional[str] = None,
        timeout: Optional[float] = None,
        headers: Optional[dict] = None,
    ) -> FetchResponse:
        """GET con TLS impersonation. Ritorna FetchResponse normalizzata."""
        if self._session is None:
            raise RuntimeError("HttpFetcher non inizializzato (usa async with)")
        eff_impersonate = impersonate if impersonate is not None else self.impersonate
        eff_timeout = timeout if timeout is not None else self.timeout
        eff_headers = {"User-Agent": self.user_agent}
        if headers:
            eff_headers.update(headers)
        if self._is_curl:
            kwargs: dict[str, Any] = {
                "headers": eff_headers,
                "timeout": eff_timeout,
                "allow_redirects": self.follow_redirects,
            }
            if eff_impersonate:
                kwargs["impersonate"] = eff_impersonate
            r = await self._session.get(url, **kwargs)
            return FetchResponse(
                status_code=r.status_code,
                text=r.text or "",
                url=str(r.url),
                headers=dict(r.headers or {}),
            )
        else:
            # httpx fallback
            r = await self._session.get(url, headers=eff_headers, timeout=eff_timeout)
            return FetchResponse(
                status_code=r.status_code,
                text=r.text or "",
                url=str(r.url),
                headers=dict(r.headers or {}),
            )


async def fetch_once(
    url: str,
    *,
    impersonate: Optional[str] = DEFAULT_IMPERSONATE,
    timeout: float = 15.0,
) -> FetchResponse:
    """One-shot GET. Crea/distrugge la sessione per una sola request.

    Conveniente per chiamate isolate (es. probe di un singolo URL). Se devi fare
    multiple chiamate, usa `HttpFetcher` come context manager (riusa connessione).
    """
    async with HttpFetcher(impersonate=impersonate, timeout=timeout) as f:
        return await f.get(url)


def has_anti_bot_bypass() -> bool:
    """True se curl_cffi e' disponibile (anti-bot bypass attivo)."""
    return _HAS_CURL_CFFI
