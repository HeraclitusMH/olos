"""Send WhatsApp interactive disambiguation prompts and decode replies."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.event_resolver import ResolvedEvent
from app.services.whatsapp import WhatsAppClient
from app.utils.timezone import (
    InvalidDateTimeError,
    format_event_time,
    parse_iso_datetime,
)

logger = logging.getLogger(__name__)


# WhatsApp interactive limits, per the Cloud API docs.
BUTTON_TITLE_MAX = 20
BUTTON_LIMIT = 3
LIST_ROW_TITLE_MAX = 24
LIST_ROW_DESCRIPTION_MAX = 72

OPTION_ID_PREFIX = "disambig"


class DisambiguationService:
    """Send button/list prompts when an event reference matches >1 event."""

    def __init__(self, whatsapp_client: WhatsAppClient | None = None) -> None:
        self._client = whatsapp_client or WhatsAppClient()

    async def send_event_choices(
        self,
        wa_id: str,
        events: list[ResolvedEvent],
        action: str,
        timezone_name: str,
        disambiguation_id: str | None = None,
    ) -> str:
        """Send interactive buttons (<=3 events) or a list (>=4).

        Returns the disambiguation id used in the option ids.
        """
        if not events:
            raise ValueError("send_event_choices requires at least one event")

        disambig_id = disambiguation_id or uuid.uuid4().hex
        body_text = f"Which event would you like to {action}?"

        if len(events) <= BUTTON_LIMIT:
            buttons = [
                {
                    "type": "reply",
                    "reply": {
                        "id": build_option_id(disambig_id, idx),
                        "title": _truncate(
                            _button_title(event, timezone_name), BUTTON_TITLE_MAX
                        ),
                    },
                }
                for idx, event in enumerate(events)
            ]
            await self._client.send_interactive_buttons(
                to=wa_id, body_text=body_text, buttons=buttons
            )
        else:
            rows = [
                {
                    "id": build_option_id(disambig_id, idx),
                    "title": _truncate(event.title, LIST_ROW_TITLE_MAX),
                    "description": _truncate(
                        _event_when(event, timezone_name), LIST_ROW_DESCRIPTION_MAX
                    ),
                }
                for idx, event in enumerate(events)
            ]
            await self._client.send_interactive_list(
                to=wa_id,
                body_text=body_text,
                button_text="Pick event",
                sections=[{"title": "Events", "rows": rows}],
            )
        return disambig_id


def build_option_id(disambiguation_id: str, index: int) -> str:
    return f"{OPTION_ID_PREFIX}:{disambiguation_id}:{index}"


def parse_option_id(option_id: str | None) -> tuple[str, int] | None:
    """Decode a button/list reply id into ``(disambig_id, option_index)``.

    Returns ``None`` for ids the assistant did not produce.
    """
    if not isinstance(option_id, str):
        return None
    parts = option_id.split(":")
    if len(parts) != 3 or parts[0] != OPTION_ID_PREFIX:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def _button_title(event: ResolvedEvent, timezone_name: str) -> str:
    when = _short_when(event, timezone_name)
    if when:
        return f"{event.title} - {when}"
    return event.title


def _event_when(event: ResolvedEvent, timezone_name: str) -> str:
    try:
        return format_event_time(event.start, event.end, timezone_name)
    except InvalidDateTimeError:
        return ""


def _short_when(event: ResolvedEvent, timezone_name: str) -> str:
    """Compact local-time label, e.g. ``13 May 16:00``."""
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ""
    try:
        parsed = parse_iso_datetime(event.start)
    except InvalidDateTimeError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    local: datetime = parsed.astimezone(zone)
    return f"{local.day} {local.strftime('%b')} {local.strftime('%H:%M')}"


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return value[: max_len - 3] + "..."
