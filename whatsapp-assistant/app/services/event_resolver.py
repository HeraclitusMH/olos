"""Resolve a calendar event referenced by a user's message.

The resolver implements three strategies, in order:

* Match a vague reference ("it", "that") against the most recent event
  the assistant has discussed in conversation.
* Search Google Calendar for the title in a small window around the
  user-supplied date (or +/- 30 days from now if no date was given).
* Return ``None`` (no match), a ``ResolvedEvent`` (single match), or a
  list (caller must disambiguate).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401  (re-exported in signatures)

from app.utils.timezone import InvalidDateTimeError, parse_iso_datetime

logger = logging.getLogger(__name__)


# Vague references the planner may emit when the user says "move it",
# "cancel that", etc. These are the terms that trigger context-based
# resolution instead of a Google Calendar search.
_VAGUE_TITLES = {
    "",
    "it",
    "that",
    "this",
    "the event",
    "event",
    "the meeting",
    "meeting",
    "the appointment",
    "appointment",
}


@dataclass
class ResolvedEvent:
    event_id: str
    title: str
    start: str
    end: str
    calendar_id: str


# A callable that lists events on the user's calendar. We accept this
# rather than a full GoogleCalendarService so the resolver stays easy to
# test and so the executor can re-bind the access token transparently
# inside ``_call_with_refresh``.
ListEventsFn = Callable[[str, str, str | None], Awaitable[list[dict[str, Any]]]]


class EventResolver:
    """Locate a Google Calendar event from a (possibly vague) description."""

    def __init__(self, list_events: ListEventsFn, calendar_id: str) -> None:
        self._list_events = list_events
        self._calendar_id = calendar_id

    async def resolve(
        self,
        search_title: str | None,
        search_date: str | None,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> ResolvedEvent | list[ResolvedEvent] | None:
        time_min, time_max = _build_search_window(search_date)
        query = (search_title or "").strip() or None
        try:
            events = await self._list_events(time_min, time_max, query)
        except Exception:
            # Re-raise so the caller's refresh-and-retry / rate-limit
            # handling kicks in. This service does not own that policy.
            raise

        title_lower = (search_title or "").strip().lower()
        matches: list[ResolvedEvent] = []
        for event in events:
            summary = str(event.get("summary") or "")
            if title_lower and title_lower not in summary.lower():
                continue
            resolved = _to_resolved(event, self._calendar_id)
            if resolved is not None:
                matches.append(resolved)

        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return matches

    @staticmethod
    async def resolve_from_context(
        conversation_history: list[Any],
        search_title: str | None,
    ) -> str | None:
        """Return a Google event id derived from recent assistant output.

        ``conversation_history`` is expected to be an iterable of objects
        with an ``execution_result_json`` attribute (typically ORM
        ``Message`` rows ordered newest-first). Only invoked when the
        planner produced a vague ``search_title``.
        """
        if not is_vague_title(search_title):
            return None
        for message in conversation_history:
            results = _extract_results(message)
            for item in reversed(results):
                if not isinstance(item, dict):
                    continue
                data = item.get("data") or {}
                if not isinstance(data, dict):
                    continue
                event_id = data.get("google_event_id")
                if isinstance(event_id, str) and event_id:
                    return event_id
        return None


def is_vague_title(search_title: str | None) -> bool:
    if search_title is None:
        return True
    return search_title.strip().lower() in _VAGUE_TITLES


def _extract_results(message: Any) -> list[Any]:
    payload = getattr(message, "execution_result_json", None)
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    return results if isinstance(results, list) else []


def _to_resolved(event: dict[str, Any], calendar_id: str) -> ResolvedEvent | None:
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    summary = str(event.get("summary") or "(no title)")
    start_obj = event.get("start") or {}
    end_obj = event.get("end") or {}
    start = start_obj.get("dateTime") or start_obj.get("date")
    end = end_obj.get("dateTime") or end_obj.get("date")
    if not start or not end:
        return None
    return ResolvedEvent(
        event_id=event_id,
        title=summary,
        start=str(start),
        end=str(end),
        calendar_id=calendar_id,
    )


def _build_search_window(search_date: str | None) -> tuple[str, str]:
    """Return ``(timeMin, timeMax)`` ISO strings for a calendar search."""
    if search_date:
        try:
            anchor = parse_iso_datetime(search_date)
        except InvalidDateTimeError:
            anchor = datetime.now(timezone.utc)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        delta = timedelta(days=7)
    else:
        anchor = datetime.now(timezone.utc)
        delta = timedelta(days=30)
    return (
        (anchor - delta).isoformat(),
        (anchor + delta).isoformat(),
    )
