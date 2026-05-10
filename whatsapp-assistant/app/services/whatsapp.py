"""Async WhatsApp Cloud API client (Meta Graph API)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class WhatsAppAPIError(Exception):
    """Raised when the WhatsApp Cloud API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"WhatsApp API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


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

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._messages_url, headers=self._headers, json=payload
            )
        if response.status_code >= 400:
            logger.error(
                "WhatsApp API call failed: status=%s body=%s",
                response.status_code,
                response.text,
            )
            raise WhatsAppAPIError(response.status_code, response.text)
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
