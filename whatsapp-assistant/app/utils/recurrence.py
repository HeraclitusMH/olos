"""Build Google Calendar RRULE strings from structured recurrence input.

The planner emits a small structured ``recurrence`` object (frequency / interval
/ count / until / by_day); we translate it into a single RFC 5545 RRULE string
that the Calendar API accepts in the event ``recurrence`` field. Keeping the
translation here (rather than asking the LLM for a raw RRULE) lets us validate
the parts and produce a friendly error instead of a Google 400.
"""

from __future__ import annotations

from datetime import timezone as _dt_timezone

from app.utils.timezone import (
    InvalidDateTimeError,
    _zoneinfo,
    parse_iso_datetime,
)

_VALID_FREQ = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
_VALID_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_VALID_DAY_SET = set(_VALID_DAYS)

# Guardrail so a runaway COUNT can't create thousands of instances.
_MAX_COUNT = 730

_FREQ_NOUN = {
    "DAILY": "day",
    "WEEKLY": "week",
    "MONTHLY": "month",
    "YEARLY": "year",
}
_DAY_LABEL = {
    "MO": "Mon",
    "TU": "Tue",
    "WE": "Wed",
    "TH": "Thu",
    "FR": "Fri",
    "SA": "Sat",
    "SU": "Sun",
}


class RecurrenceError(ValueError):
    """Raised when recurrence parameters from the planner are invalid."""


def build_recurrence_rule(recurrence: dict, *, timezone: str) -> str:
    """Return a single ``RRULE:...`` string for Google Calendar.

    Raises :class:`RecurrenceError` (a ``ValueError``) on malformed input so the
    caller can return a friendly WhatsApp reply instead of hitting the API.
    """
    if not isinstance(recurrence, dict):
        raise RecurrenceError("recurrence must be an object")

    freq = str(recurrence.get("frequency") or "").strip().upper()
    if freq not in _VALID_FREQ:
        raise RecurrenceError(
            f"Unsupported recurrence frequency: {recurrence.get('frequency')!r}"
        )
    parts = [f"FREQ={freq}"]

    interval = recurrence.get("interval")
    if interval is not None:
        interval_int = _coerce_positive_int(interval, "interval")
        if interval_int != 1:
            parts.append(f"INTERVAL={interval_int}")

    days = _normalize_by_day(recurrence.get("by_day"))
    if days:
        parts.append("BYDAY=" + ",".join(days))

    count = recurrence.get("count")
    until = recurrence.get("until")
    # RFC 5545 forbids COUNT and UNTIL together; an explicit count wins.
    if count is not None:
        count_int = _coerce_positive_int(count, "count")
        if count_int > _MAX_COUNT:
            raise RecurrenceError(
                f"recurrence count too large (max {_MAX_COUNT})"
            )
        parts.append(f"COUNT={count_int}")
    elif until:
        parts.append(f"UNTIL={_format_until(until, timezone)}")

    return "RRULE:" + ";".join(parts)


def describe_recurrence(recurrence: dict) -> str:
    """Short human phrase for confirmation messages, e.g. ``daily ×3``.

    Best-effort and never raises — used only for cosmetic confirmation text.
    """
    if not isinstance(recurrence, dict):
        return "repeats"

    freq = str(recurrence.get("frequency") or "").strip().upper()
    noun = _FREQ_NOUN.get(freq)
    if noun is None:
        return "repeats"

    interval = recurrence.get("interval")
    try:
        interval_int = int(interval) if interval is not None else 1
    except (TypeError, ValueError):
        interval_int = 1

    if interval_int <= 1:
        base = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}[
            noun
        ]
    else:
        base = f"every {interval_int} {noun}s"

    days = _normalize_by_day(recurrence.get("by_day"), strict=False)
    if days:
        base += " on " + ", ".join(_DAY_LABEL[d] for d in days)

    count = recurrence.get("count")
    try:
        count_int = int(count) if count is not None else None
    except (TypeError, ValueError):
        count_int = None
    if count_int and count_int > 0:
        base += f" ×{count_int}"

    return base


def _coerce_positive_int(value: object, field: str) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RecurrenceError(f"recurrence {field} must be an integer") from exc
    if result < 1:
        raise RecurrenceError(f"recurrence {field} must be >= 1")
    return result


def _normalize_by_day(by_day: object, *, strict: bool = True) -> list[str]:
    if not by_day:
        return []
    if isinstance(by_day, str):
        by_day = [by_day]
    if not isinstance(by_day, (list, tuple)):
        if strict:
            raise RecurrenceError("recurrence by_day must be a list of weekdays")
        return []
    days: list[str] = []
    for raw in by_day:
        day = str(raw).strip().upper()[:2]
        if day not in _VALID_DAY_SET:
            if strict:
                raise RecurrenceError(f"Invalid weekday in by_day: {raw!r}")
            continue
        if day not in days:
            days.append(day)
    # Keep canonical week order for stable output.
    return [d for d in _VALID_DAYS if d in days]


def _format_until(until: object, timezone: str) -> str:
    """Convert an ISO date/datetime to an RRULE UTC ``UNTIL`` value."""
    try:
        dt = parse_iso_datetime(str(until))
    except InvalidDateTimeError as exc:
        raise RecurrenceError(f"Invalid recurrence until date: {until!r}") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zoneinfo(timezone))
    # A date-only "until" parses to local midnight; treat it as inclusive of the
    # whole day so "until Friday" still includes Friday's occurrence.
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        dt = dt.replace(hour=23, minute=59, second=59)

    return dt.astimezone(_dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
