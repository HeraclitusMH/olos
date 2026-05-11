"""Async WhatsApp Cloud API client (Meta Graph API)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.utils.exceptions import WhatsAppSendError
from app.utils.retry import async_retry

logger = logging.getLogger(__name__)


class WhatsAppAPIError(WhatsAppSendError):
    """Raised when the WhatsApp Cloud API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(log_message=f"WhatsApp API error {status_code}: {body}")

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"WhatsApp API error {self.status_code}: {self.body}"


# Transport-level exceptions that should trigger a retry.
_TRANSPORT_EXCS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


class _WhatsAppServerError(WhatsAppAPIError):
    """Internal marker for 5xx responses so the retry decorator only retries those."""


class WhatsAppClient:
    BASE_URL = "https://graph.facebook.com/v19.0"

    def __init__(
        self,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        settings = get_settings()
        self._access_token = access_token or settings.whatsapp_access_token
        self._phone_number_id = phone_number_id or settings.whatsapp_phone_number_id
        self._timeout = timeout

    @property
    def _messages_url(self) -> str:
        return f"{self.BASE_URL}/{self._phone_number_id}/messages"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    @async_retry(
        max_retries=1,
        delay=0.2,
        backoff=2.0,
        exceptions=_TRANSPORT_EXCS + (_WhatsAppServerError,),
    )
    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._messages_url, headers=self._headers, json=payload
            )
        if response.status_code >= 400:
            if response.status_code == 429:
                # Meta WA does not benefit from retry — log and surface.
                logger.warning(
                    "WhatsApp API rate-limited (429)",
                    extra={
                        "event": "whatsapp.rate_limited",
                        "status_code": 429,
                    },
                )
                raise WhatsAppAPIError(response.status_code, response.text)
            if response.status_code >= 500:
                raise _WhatsAppServerError(response.status_code, response.text)
            logger.error(
                "WhatsApp API call failed: status=%s body=%s",
                response.status_code,
                response.text,
                extra={
                    "event": "whatsapp.send_failed",
                    "status_code": response.status_code,
                },
            )
            raise WhatsAppAPIError(response.status_code, response.text)
        logger.info(
            "WhatsApp send succeeded",
            extra={"event": "whatsapp.send", "status_code": response.status_code},
        )
        return response.json()

    async def send_text_message(self, to: str, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        return await self._post(payload)

    async def send_interactive_buttons(
        self, to: str, body_text: str, buttons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Send an interactive button message.

        ``buttons`` items must already be in WhatsApp button format::

            [{"type": "reply", "reply": {"id": "btn_1", "title": "Option 1"}}]
        """
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": buttons},
            },
        }
        return await self._post(payload)

    async def send_interactive_list(
        self,
        to: str,
        body_text: str,
        button_text: str,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Send an interactive list message."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body_text},
                "action": {"button": button_text, "sections": sections},
            },
        }
        return await self._post(payload)
