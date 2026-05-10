"""Timezone-aware datetime helpers for user-facing formatting."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings

logger = logging.getLogger(__name__)


class InvalidDateTimeError(ValueError):
    """Raised when an ISO 8601 string from the LLM cannot be parsed."""


def _zoneinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        fallback = get_settings().default_timezone
        logger.warning(
            "Unknown timezone=%s, falling back to %s", timezone, fallback
        )
        return ZoneInfo(fallback)


def get_current_datetime(timezone: str) -> datetime:
    """Return the current datetime in ``timezone``."""
    return datetime.now(_zoneinfo(timezone))


def parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime string, accepting trailing ``Z`` for UTC."""
    if not isinstance(value, str) or not value:
        raise InvalidDateTimeError(f"Invalid datetime: {value!r}")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise InvalidDateTimeError(f"Invalid datetime: {value!r}") from exc


def _to_zone(value: str, timezone: str) -> datetime:
    parsed = parse_iso_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zoneinfo(timezone))
    return parsed.astimezone(_zoneinfo(timezone))


def format_event_time(start_iso: str, end_iso: str, timezone: str) -> str:
    """Format a single event's time range, e.g. ``Tue 13 May, 16:00-17:00``."""
    start = _to_zone(start_iso, timezone)
    end = _to_zone(end_iso, timezone)
    day = f"{start.strftime('%a')} {start.day} {start.strftime('%b')}"
    return f"{day}, {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


def format_date_range(start_iso: str, end_iso: str, timezone: str) -> str:
    """Format a date range for headings, e.g. ``Tuesday 13 May`` or ``13-15 May``."""
    start = _to_zone(start_iso, timezone)
    end = _to_zone(end_iso, timezone)

    if start.date() == end.date():
        return f"{start.strftime('%A')} {start.day} {start.strftime('%B')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}-{end.day} {start.strftime('%B')}"
    if start.year == end.year:
        return (
            f"{start.day} {start.strftime('%b')} - "
            f"{end.day} {end.strftime('%b')}"
        )
    return (
        f"{start.day} {start.strftime('%b')} {start.year} - "
        f"{end.day} {end.strftime('%b')} {end.year}"
    )
