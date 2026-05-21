"""WhatsApp Cloud API — Motore B (Meta API ufficiale).

HTTP client async per Meta Cloud API v17.0+. Usato per:
- Inviare messaggi free-form a contatti che hanno scritto al business number
  negli ultimi 24h (24h-window di Meta).
- Inviare template message (approvati su Meta Business Manager) a qualsiasi
  contatto, indipendentemente dalla 24h-window.

NON adatto per cold outreach: Meta richiede opt-in. Quel caso d'uso è
gestito dal Motore A (browser automation).

Riusa `crypto_creds` per decifrare l'access_token salvato in
`whatsapp_api_config.encrypted_access_token`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .crypto_creds import decrypt


log = logging.getLogger(__name__)


META_GRAPH_VERSION = "v17.0"
META_GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_VERSION}"


@dataclass
class WhatsAppSendResult:
    """Risultato di una send via Cloud API."""
    ok: bool
    message_id: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)
    target: str | None = None


def can_send_freeform(last_inbound_iso: str | None) -> bool:
    """Verifica la 24h-window di Meta.

    Meta consente messaggi FREE-FORM solo se il contatto ha scritto al business
    negli ultimi 24h. Per messaggi outbound al di fuori della finestra, serve
    un TEMPLATE pre-approvato.
    """
    if not last_inbound_iso:
        return False
    try:
        last = datetime.fromisoformat(last_inbound_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - last < timedelta(hours=24)


def _normalize_phone_for_api(raw: str) -> str:
    """Meta Cloud API accetta `to` in formato E.164 SENZA il '+'.

    Es. '+393331234567' → '393331234567'.
    """
    import re
    return re.sub(r"\D", "", raw or "")


class WhatsAppAPI:
    """HTTP client async per Meta Cloud API. Una istanza = una config Meta
    (phone_number_id + WABA + access_token decifrato).
    """

    def __init__(self, config: dict[str, Any]):
        """`config` è una riga di `whatsapp_api_config` (dict) dal DB.

        Decifra l'access_token al volo. La chiave decifrata NON viene
        persistita né loggata: vive solo nell'istanza.
        """
        self.config_id = config.get("id")
        self.label = config.get("label", "")
        self.phone_number_id = config["phone_number_id"]
        self.business_account_id = config["business_account_id"]
        self.app_id = config.get("app_id")
        self.default_template_name = config.get("default_template_name")
        self.default_template_language = config.get("default_template_language", "it")
        self.daily_msg_cap = int(config.get("daily_msg_cap", 250))

        enc = config["encrypted_access_token"]
        # encrypted_access_token può essere bytes o memoryview da SQLite BLOB
        if isinstance(enc, memoryview):
            enc = bytes(enc)
        try:
            self._token = decrypt(enc)
        except Exception as e:
            raise RuntimeError(
                f"Impossibile decifrare access_token Meta per config "
                f"#{self.config_id} ({self.label}): {e}. "
                "Verifica ARGOS_SECRET in .env."
            ) from e

        self._base_url = f"{META_GRAPH_BASE}/{self.phone_number_id}"
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ----- API send -----

    async def send_text(self, to: str, body: str) -> WhatsAppSendResult:
        """Invia un messaggio TEXT free-form. Funziona SOLO dentro la 24h-window
        dopo che il destinatario ha scritto al business number. Fuori finestra,
        Meta risponde con error code 131056 (`Re-engagement message`).
        """
        digits = _normalize_phone_for_api(to)
        payload = {
            "messaging_product": "whatsapp",
            "to": digits,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        return await self._post_messages(payload, target=digits)

    async def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str,
        body_params: list[str] | None = None,
        header_params: list[dict[str, Any]] | None = None,
    ) -> WhatsAppSendResult:
        """Invia un TEMPLATE message pre-approvato su Meta.

        Args:
            to: numero E.164 (con o senza '+').
            template_name: nome del template come registrato su Meta Manager.
            language_code: es. 'it', 'en_US', 'es'.
            body_params: lista di valori per i placeholder {{1}}, {{2}}, ...
              del corpo del template. Vengono mappati nell'ordine.
            header_params: parametri tipizzati per l'header (text/image/document).
        """
        digits = _normalize_phone_for_api(to)
        components: list[dict[str, Any]] = []
        if header_params:
            components.append({"type": "header", "parameters": header_params})
        if body_params:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in body_params],
            })

        payload = {
            "messaging_product": "whatsapp",
            "to": digits,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components,
            },
        }
        return await self._post_messages(payload, target=digits)

    async def _post_messages(
        self, payload: dict[str, Any], target: str
    ) -> WhatsAppSendResult:
        """POST a /<phone_number_id>/messages con error handling Meta."""
        url = f"{self._base_url}/messages"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(url, json=payload, headers=self._headers)
        except Exception as e:
            return WhatsAppSendResult(
                ok=False,
                error_code=None,
                error_message=f"network: {type(e).__name__}: {e}",
                target=target,
            )

        try:
            data = r.json()
        except Exception:
            return WhatsAppSendResult(
                ok=False,
                error_code=r.status_code,
                error_message=f"non-json response (status {r.status_code}): {r.text[:200]}",
                target=target,
            )

        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            return WhatsAppSendResult(
                ok=False,
                error_code=err.get("code") or r.status_code,
                error_message=err.get("message") or str(data),
                raw_response=data,
                target=target,
            )

        msg_id = None
        msgs = data.get("messages") or []
        if msgs and isinstance(msgs, list):
            msg_id = msgs[0].get("id")

        return WhatsAppSendResult(
            ok=True,
            message_id=msg_id,
            raw_response=data,
            target=target,
        )

    # ----- API read / utility -----

    async def list_templates(self) -> list[dict[str, Any]]:
        """GET /<WABA_ID>/message_templates — lista i template approvati nel
        WhatsApp Business Account. Usato per popolare il dropdown UI.

        Ritorna i template con `status='APPROVED'` di solito (Meta restituisce
        anche PENDING e REJECTED; il filtro è demandato al chiamante).
        """
        url = f"{META_GRAPH_BASE}/{self.business_account_id}/message_templates"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url, headers=self._headers, params={"limit": 200})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning("[wa_api] list_templates failed: %s", e)
            return []
        return list(data.get("data") or [])

    async def verify_credentials(self) -> tuple[bool, str]:
        """Test rapido: GET /<phone_number_id>. Se 200 → credenziali valide.

        Ritorna (ok, message). Usato dal pulsante 'Test API' in Settings UI.
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(self._base_url, headers=self._headers)
        except Exception as e:
            return False, f"network: {type(e).__name__}: {e}"
        if r.status_code == 200:
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            display = data.get("display_phone_number") or self.phone_number_id
            return True, f"OK — numero {display}"
        try:
            err = r.json().get("error", {})
            return False, f"Meta error {err.get('code')}: {err.get('message') or r.text[:200]}"
        except Exception:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
