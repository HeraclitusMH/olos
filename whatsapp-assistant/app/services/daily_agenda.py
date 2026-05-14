"""Daily agenda scheduler service.

Sends each user a WhatsApp message every day at their configured local
time (default ``08:00 Asia/Makassar``) listing the events on their
calendar for that local day.

Idempotency
-----------
Each successful send writes a ``DailyAgendaSend(user_id, agenda_date)``
row (unique constraint). The scheduler can run every minute and on
restart, but a user only receives one agenda per local date.

Concurrency
-----------
``run_tick`` wraps the user iteration in a Postgres advisory lock
(``pg_try_advisory_lock``) so multiple replicas do not duplicate work.
If the lock cannot be acquired the tick is a no-op for that minute —
the DB unique constraint is still the source of truth for "did we
send it?".
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import DailyAgendaSend, GoogleAccount, User
from app.services.google_auth import GoogleAuthError, GoogleAuthService
from app.services.google_calendar import (
    GoogleCalendarError,
    GoogleCalendarService,
)
from app.services.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.utils.exceptions import TokenExpiredError, WhatsAppSendError
from app.utils.timezone import format_event_time, parse_iso_datetime

logger = logging.getLogger(__name__)


# Postgres advisory-lock key. Arbitrary 64-bit int unique to this scheduler.
_ADVISORY_LOCK_KEY = 0x6461696C795F6167  # "daily_ag" — fits in int64.

_PREFS_KEY = "daily_agenda"

AGENDA_CUSTOM_TEXT_MAX_LENGTH = 500

# Meta WhatsApp error code for "outside the 24-hour customer service window".
_OUTSIDE_24H_ERROR_CODE = "131026"

# Five contiguous ranges of half-hour slots covering the 24-hour day. Used by
# the WhatsApp settings flow to pick a delivery time in two list-message steps
# (WhatsApp lists max out at 10 rows, and we have 48 half-hour slots).
TIME_RANGES: list[tuple[str, str, str]] = [
    ("00:00", "04:30", "Night / Early morning"),
    ("05:00", "09:30", "Morning"),
    ("10:00", "14:30", "Late morning / Afternoon"),
    ("15:00", "19:30", "Afternoon / Evening"),
    ("20:00", "23:30", "Night"),
]

_TIME_SLOT_STARTS: list[int] = [0, 10, 20, 30, 40]
_TIME_SLOT_COUNTS: list[int] = [10, 10, 10, 10, 8]


def generate_time_slots(range_index: int) -> list[dict[str, str]]:
    """Half-hour slots for ``range_index`` (0-4) as list-message row payloads."""
    if range_index < 0 or range_index >= len(_TIME_SLOT_STARTS):
        raise ValueError(f"range_index out of range: {range_index}")
    base = _TIME_SLOT_STARTS[range_index]
    count = _TIME_SLOT_COUNTS[range_index]
    slots: list[dict[str, str]] = []
    for offset in range(count):
        total_minutes = (base + offset) * 30
        hour, minute = divmod(total_minutes, 60)
        time_str = f"{hour:02d}:{minute:02d}"
        slots.append({"id": f"agenda_time:{time_str}", "title": time_str})
    return slots


@dataclass(frozen=True)
class AgendaPrefs:
    enabled: bool
    timezone: str
    time_local: str  # "HH:MM"
    custom_footer_text: str | None = None


def _default_prefs() -> AgendaPrefs:
    settings = get_settings()
    return AgendaPrefs(
        enabled=True,
        timezone=settings.daily_agenda_default_timezone,
        time_local=settings.daily_agenda_default_time_local,
        custom_footer_text=None,
    )


def get_user_prefs(user: User) -> AgendaPrefs:
    """Resolve ``user.preferences_json["daily_agenda"]`` against defaults."""
    defaults = _default_prefs()
    prefs = (user.preferences_json or {}).get(_PREFS_KEY) or {}
    if not isinstance(prefs, dict):
        return defaults

    enabled = prefs.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True

    tz_raw = prefs.get("timezone")
    timezone = tz_raw if isinstance(tz_raw, str) and tz_raw else defaults.timezone

    time_raw = prefs.get("time_local")
    time_local = (
        time_raw if isinstance(time_raw, str) and time_raw else defaults.time_local
    )

    footer_raw = prefs.get("custom_footer_text")
    custom_footer_text = (
        footer_raw.strip()
        if isinstance(footer_raw, str) and footer_raw.strip()
        else None
    )

    return AgendaPrefs(
        enabled=enabled,
        timezone=timezone,
        time_local=time_local,
        custom_footer_text=custom_footer_text,
    )


def _resolve_zone(tz_name: str) -> tuple[ZoneInfo, str]:
    """Return a usable ZoneInfo, falling back to the default on bad input."""
    try:
        return ZoneInfo(tz_name), tz_name
    except (ZoneInfoNotFoundError, ValueError):
        fallback = get_settings().daily_agenda_default_timezone
        logger.warning(
            "Invalid daily-agenda timezone=%r, falling back to %s",
            tz_name,
            fallback,
            extra={"event": "daily_agenda.invalid_timezone"},
        )
        return ZoneInfo(fallback), fallback


def _parse_time_local(value: str) -> time:
    try:
        hh, mm = value.split(":", 1)
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        fallback = get_settings().daily_agenda_default_time_local
        logger.warning(
            "Invalid daily-agenda time_local=%r, falling back to %s",
            value,
            fallback,
            extra={"event": "daily_agenda.invalid_time_local"},
        )
        hh, mm = fallback.split(":", 1)
        return time(int(hh), int(mm))


def is_due(
    prefs: AgendaPrefs,
    now_utc: datetime,
    *,
    window_minutes: int | None = None,
) -> tuple[bool, date]:
    """Return ``(due, local_date)`` for ``prefs`` at ``now_utc``.

    ``local_date`` is always returned (the user's local date *now*),
    even when not due, so the caller can use it for idempotency checks.
    """
    if window_minutes is None:
        window_minutes = get_settings().daily_agenda_send_window_minutes

    zone, _ = _resolve_zone(prefs.timezone)
    now_local = now_utc.astimezone(zone)
    local_today = now_local.date()
    scheduled_time = _parse_time_local(prefs.time_local)
    scheduled_local = datetime.combine(local_today, scheduled_time, tzinfo=zone)
    window_end = scheduled_local + timedelta(minutes=window_minutes)
    due = scheduled_local <= now_local < window_end
    return due, local_today


def _format_event_line(event: dict[str, Any], timezone: str) -> str:
    summary = str(event.get("summary") or "(no title)")
    start_obj = event.get("start") or {}
    end_obj = event.get("end") or {}
    if start_obj.get("date") and not start_obj.get("dateTime"):
        return f"• (All day) — {summary}"
    start = start_obj.get("dateTime")
    end = end_obj.get("dateTime") or end_obj.get("date")
    if not start or not end:
        return f"• {summary}"
    try:
        # format_event_time gives "Tue 13 May, 09:30-10:15" — we only want
        # the time range here, since the header already names the day.
        range_label = format_event_time(start, end, timezone)
        time_part = range_label.split(",", 1)[-1].strip()
        # format_event_time uses ``-``; the spec asks for an en-dash.
        time_part = time_part.replace("-", "–", 1)
        return f"• {time_part} — {summary}"
    except Exception:  # noqa: BLE001 - defensive against malformed payloads
        return f"• {summary}"


def format_agenda_message(
    events: list[dict[str, Any]],
    local_date: date,
    timezone: str,
    custom_footer_text: str | None = None,
) -> str:
    custom = (custom_footer_text or "").strip()
    if not events:
        base = "Good morning! You have no events today."
        if custom:
            return f"{base}\n\n---\n\n{custom}"
        return base

    # Built explicitly to avoid ``%-d`` (non-portable on Windows).
    header_day = (
        f"{local_date.strftime('%a')}, {local_date.day} "
        f"{local_date.strftime('%b')}"
    )
    lines = [f"Good morning! Here are your events for {header_day} (your time):"]
    for event in events:
        lines.append(_format_event_line(event, timezone))
    body = "\n".join(lines)
    if custom:
        return f"{body}\n\n---\n\n{custom}"
    return body


def _is_outside_24h_window(exc: WhatsAppAPIError) -> bool:
    body = getattr(exc, "body", "") or ""
    return _OUTSIDE_24H_ERROR_CODE in body


def _flatten_for_template(message: str) -> str:
    """Collapse newlines/tabs so WhatsApp template body params are accepted.

    Template body parameters reject newline/tab characters and runs of 4+
    consecutive spaces (Meta error 132018). Free-form text messages have no
    such restriction, so this is only applied when we fall back to a template.
    """
    flattened = message.replace("\t", " ")
    flattened = flattened.replace("\r\n", "\n").replace("\r", "\n")
    flattened = flattened.replace("\n", " | ")
    while "    " in flattened:
        flattened = flattened.replace("    ", "   ")
    return flattened


async def _send_agenda_message(
    *,
    whatsapp: WhatsAppClient,
    to: str,
    body: str,
    template_name: str,
    template_language: str,
) -> None:
    """Try send_text_message first; on 24h-window error fall back to template.

    The agenda body is multi-line. ``send_text_message`` handles newlines
    fine, but ``send_template_message`` does not — its body parameter must be
    single-line — so the template fallback flattens the message first.
    """
    try:
        await whatsapp.send_text_message(to=to, text=body)
        return
    except WhatsAppAPIError as exc:
        if not _is_outside_24h_window(exc):
            raise
        logger.info(
            "Daily agenda text send rejected as outside 24h window — using template",
            extra={"event": "daily_agenda.fallback_template"},
        )

    await whatsapp.send_template_message(
        to=to,
        template_name=template_name,
        body_text=_flatten_for_template(body),
        language_code=template_language,
    )


# Type alias for clarity in dependency injection.
SessionFactory = Callable[[], AsyncSession]


class DailyAgendaService:
    """Run a periodic tick that sends today's agenda to each due user."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        whatsapp_client_factory: Callable[[], WhatsAppClient] | None = None,
        google_auth_factory: Callable[[AsyncSession], GoogleAuthService] | None = None,
        calendar_service_factory: Callable[
            [str, str], GoogleCalendarService
        ]
        | None = None,
    ) -> None:
        self._session_factory: Callable[[], Any] = (
            session_factory or AsyncSessionLocal
        )
        self._whatsapp_client_factory = whatsapp_client_factory or WhatsAppClient
        self._google_auth_factory = google_auth_factory or (
            lambda session: GoogleAuthService(session)
        )
        self._calendar_service_factory = calendar_service_factory or (
            lambda token, calendar_id: GoogleCalendarService(
                access_token=token, calendar_id=calendar_id
            )
        )

    async def run_tick(self, *, now_utc: datetime | None = None) -> int:
        """Process one scheduler tick. Returns the number of agendas sent."""
        if now_utc is None:
            now_utc = datetime.now(UTC)

        async with self._session_factory() as session:
            got_lock = await self._try_acquire_lock(session)
            if not got_lock:
                logger.debug(
                    "Daily-agenda tick skipped: advisory lock not acquired",
                    extra={"event": "daily_agenda.tick_skipped"},
                )
                return 0
            try:
                users = await self._load_enabled_users(session)
            finally:
                # Release the lock before doing per-user network work, so a
                # long tick on one replica doesn't block the next minute on
                # another replica. The DB unique constraint guarantees no
                # duplicate send anyway.
                await self._release_lock(session)

        sent_count = 0
        for user in users:
            try:
                if await self._process_user(user=user, now_utc=now_utc):
                    sent_count += 1
            except Exception:  # noqa: BLE001 - one user must not break the rest
                logger.exception(
                    "Daily agenda failed for user=%s",
                    user.id,
                    extra={"event": "daily_agenda.user_failed"},
                )
        return sent_count

    async def _try_acquire_lock(self, session: AsyncSession) -> bool:
        from sqlalchemy import text

        try:
            result = await session.execute(
                text("SELECT pg_try_advisory_lock(:k)").bindparams(k=_ADVISORY_LOCK_KEY)
            )
            return bool(result.scalar())
        except Exception:  # noqa: BLE001
            # Advisory locks aren't critical — fall back to idempotency only.
            logger.debug(
                "Advisory lock acquisition failed; relying on DB uniqueness",
                extra={"event": "daily_agenda.lock_unavailable"},
            )
            return True

    async def _release_lock(self, session: AsyncSession) -> None:
        from sqlalchemy import text

        try:
            await session.execute(
                text("SELECT pg_advisory_unlock(:k)").bindparams(k=_ADVISORY_LOCK_KEY)
            )
        except Exception:  # noqa: BLE001
            pass

    async def _load_enabled_users(self, session: AsyncSession) -> list[User]:
        result = await session.execute(select(User))
        users = list(result.scalars().all())
        # Filter in Python — the JSONB predicate is awkward to write portably
        # and the user table is small for this kind of service.
        return [u for u in users if get_user_prefs(u).enabled]

    async def _process_user(self, *, user: User, now_utc: datetime) -> bool:
        prefs = get_user_prefs(user)
        if not prefs.enabled:
            return False

        due, local_today = is_due(prefs, now_utc)
        if not due:
            return False

        async with self._session_factory() as session:
            if await self._already_sent(session, user.id, local_today):
                return False

            token, account = await self._resolve_token(session, user)
            if token is None or account is None:
                logger.info(
                    "Daily agenda: skipping user=%s (no active Google account)",
                    user.id,
                    extra={"event": "daily_agenda.no_account"},
                )
                return False

            events = await self._fetch_events_for_local_day(
                token=token,
                account=account,
                local_date=local_today,
                timezone_name=prefs.timezone,
            )
            if events is None:
                return False

            message = format_agenda_message(
                events,
                local_today,
                prefs.timezone,
                custom_footer_text=prefs.custom_footer_text,
            )
            settings = get_settings()
            try:
                await _send_agenda_message(
                    whatsapp=self._whatsapp_client_factory(),
                    to=user.wa_id,
                    body=message,
                    template_name=settings.whatsapp_template_name,
                    template_language=settings.whatsapp_template_language,
                )
            except WhatsAppSendError:
                logger.exception(
                    "Daily agenda: WhatsApp send failed for user=%s",
                    user.id,
                    extra={"event": "daily_agenda.whatsapp_failed"},
                )
                return False

            try:
                await self._record_send(
                    session,
                    user_id=user.id,
                    local_date=local_today,
                    timezone_name=prefs.timezone,
                    time_local=prefs.time_local,
                )
            except IntegrityError:
                # Another worker recorded the send in the same minute. The
                # message already went out from there — log and move on.
                await session.rollback()
                logger.info(
                    "Daily agenda: duplicate suppressed for user=%s on %s",
                    user.id,
                    local_today,
                    extra={"event": "daily_agenda.duplicate"},
                )
                return False
            return True

    async def _already_sent(
        self, session: AsyncSession, user_id: uuid.UUID, local_date: date
    ) -> bool:
        result = await session.execute(
            select(DailyAgendaSend.id).where(
                DailyAgendaSend.user_id == user_id,
                DailyAgendaSend.agenda_date == local_date,
            )
        )
        return result.scalar_one_or_none() is not None

    async def _resolve_token(
        self, session: AsyncSession, user: User
    ) -> tuple[str | None, GoogleAccount | None]:
        auth = self._google_auth_factory(session)
        try:
            token = await auth.get_valid_token(user.id)
        except TokenExpiredError:
            logger.info(
                "Daily agenda: token revoked for user=%s — skipping",
                user.id,
                extra={"event": "daily_agenda.token_revoked"},
            )
            return None, None
        except GoogleAuthError:
            logger.warning(
                "Daily agenda: transient Google auth failure for user=%s",
                user.id,
                extra={"event": "daily_agenda.auth_transient"},
            )
            return None, None
        if token is None:
            return None, None
        account_row = await session.execute(
            select(GoogleAccount).where(GoogleAccount.user_id == user.id)
        )
        account = account_row.scalars().first()
        if account is None or account.status != "active":
            return None, None
        return token, account

    async def _fetch_events_for_local_day(
        self,
        *,
        token: str,
        account: GoogleAccount,
        local_date: date,
        timezone_name: str,
    ) -> list[dict[str, Any]] | None:
        zone, _ = _resolve_zone(timezone_name)
        day_start_local = datetime.combine(local_date, time(0, 0), tzinfo=zone)
        day_end_local = day_start_local + timedelta(days=1)

        calendar = self._calendar_service_factory(token, account.calendar_id)
        try:
            return await calendar.list_events(
                time_min=day_start_local.isoformat(),
                time_max=day_end_local.isoformat(),
            )
        except GoogleCalendarError:
            logger.exception(
                "Daily agenda: calendar fetch failed for account=%s",
                account.id,
                extra={"event": "daily_agenda.calendar_failed"},
            )
            return None

    async def _record_send(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        local_date: date,
        timezone_name: str,
        time_local: str,
    ) -> None:
        session.add(
            DailyAgendaSend(
                user_id=user_id,
                agenda_date=local_date,
                timezone=timezone_name,
                scheduled_time_local=time_local,
            )
        )
        await session.commit()


async def scheduler_loop(
    service: DailyAgendaService | None = None,
    *,
    stop_event: asyncio.Event | None = None,
    tick_seconds: int | None = None,
) -> None:
    """Run ticks forever (until ``stop_event`` is set).

    Designed to be started as an ``asyncio.create_task`` from the FastAPI
    lifespan. Sleeps in interruptible chunks so shutdown is prompt.
    """
    service = service or DailyAgendaService()
    stop_event = stop_event or asyncio.Event()
    settings = get_settings()
    interval = tick_seconds or settings.daily_agenda_tick_seconds

    logger.info(
        "Daily-agenda scheduler started (tick=%ss)",
        interval,
        extra={"event": "daily_agenda.scheduler_started"},
    )
    try:
        while not stop_event.is_set():
            try:
                await service.run_tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception(
                    "Daily-agenda tick crashed",
                    extra={"event": "daily_agenda.tick_crashed"},
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info(
            "Daily-agenda scheduler stopped",
            extra={"event": "daily_agenda.scheduler_stopped"},
        )


# Helper for tests / admin code to update prefs in the JSONB-safe way.
def set_user_prefs(user: User, prefs: AgendaPrefs) -> None:
    """Reassign ``user.preferences_json`` with the given daily-agenda prefs."""
    current = dict(user.preferences_json or {})
    payload: dict[str, Any] = {
        "enabled": prefs.enabled,
        "timezone": prefs.timezone,
        "time_local": prefs.time_local,
    }
    if prefs.custom_footer_text:
        payload["custom_footer_text"] = prefs.custom_footer_text
    current[_PREFS_KEY] = payload
    user.preferences_json = current


__all__ = [
    "AgendaPrefs",
    "DailyAgendaService",
    "format_agenda_message",
    "get_user_prefs",
    "is_due",
    "scheduler_loop",
    "set_user_prefs",
]
