"""Reminder service and background scheduler.

Persists one-shot reminders and fires them as WhatsApp messages around
their target time. The scheduler tick runs every
``REMINDER_TICK_SECONDS`` (default 15s) so reminders go out within ~15s
of their ``remind_at``.

Send strategy
-------------
Most reminders fire within minutes/hours of the user's request, well
inside the 24-hour WhatsApp customer-service window, so we try
``send_text_message`` first. If Meta returns error code ``131026``
(outside the 24h window) we fall back to a pre-approved template.

Resilience
----------
Per-reminder errors must never break the batch. After
``REMINDER_MAX_ATTEMPTS`` failed sends we mark the reminder sent (with a
warning) to avoid an infinite retry loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import Reminder, User
from app.services.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.utils.exceptions import AssistantError, WhatsAppSendError

logger = logging.getLogger(__name__)


# Meta WhatsApp error code for "outside the 24-hour customer service window".
_OUTSIDE_24H_ERROR_CODE = "131026"

# Grace period: accept ``remind_at`` up to this far in the past to absorb
# planner / processing delay.
_PAST_GRACE = timedelta(seconds=30)

# Hard upper bound on how far out a reminder can be scheduled.
_MAX_FUTURE = timedelta(days=365)


class ReminderError(AssistantError):
    """Raised when a reminder cannot be created (invalid arguments)."""

    default_user_message = "I couldn't set that reminder."


# Module-level stop event so app.main lifespan can flip it on shutdown.
reminder_stop_event: asyncio.Event = asyncio.Event()


class ReminderService:
    """CRUD operations for the ``reminders`` table."""

    async def create_reminder(
        self,
        *,
        user_id: uuid.UUID,
        reminder_text: str,
        remind_at: datetime,
        db: AsyncSession,
    ) -> Reminder:
        text = (reminder_text or "").strip()
        if not text:
            raise ReminderError(log_message="reminder_text was empty")
        if remind_at.tzinfo is None:
            raise ReminderError(
                log_message="remind_at must be timezone-aware",
                user_message="Reminder time needs a timezone.",
            )

        now_utc = datetime.now(UTC)
        if remind_at < now_utc - _PAST_GRACE:
            raise ReminderError(
                log_message=f"remind_at {remind_at.isoformat()} is in the past",
                user_message="That time is in the past — when should I remind you?",
            )
        if remind_at > now_utc + _MAX_FUTURE:
            raise ReminderError(
                log_message=f"remind_at {remind_at.isoformat()} is over a year out",
                user_message="That's too far out — I can only remind up to a year ahead.",
            )

        reminder = Reminder(
            user_id=user_id,
            reminder_text=text,
            remind_at=remind_at,
        )
        db.add(reminder)
        await db.flush()
        return reminder

    async def get_due_reminders(
        self, now_utc: datetime, db: AsyncSession, *, limit: int = 50
    ) -> list[Reminder]:
        result = await db.execute(
            select(Reminder)
            .where(Reminder.sent.is_(False), Reminder.remind_at <= now_utc)
            .order_by(Reminder.remind_at.asc())
            .limit(limit)
            .options(selectinload(Reminder.user))
        )
        return list(result.scalars().all())

    async def search_unsent(
        self,
        user_id: uuid.UUID,
        db: AsyncSession,
        *,
        search_text: str | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        limit: int = 10,
    ) -> list[Reminder]:
        """Return unsent reminders for a user, newest-due first.

        ``search_text`` is matched against ``reminder_text`` with
        case-insensitive ``ILIKE %text%`` semantics. ``time_min`` /
        ``time_max`` bound ``remind_at`` inclusively.
        """
        stmt = select(Reminder).where(
            Reminder.user_id == user_id, Reminder.sent.is_(False)
        )
        if isinstance(search_text, str) and search_text.strip():
            stmt = stmt.where(
                Reminder.reminder_text.ilike(f"%{search_text.strip()}%")
            )
        if time_min is not None:
            stmt = stmt.where(Reminder.remind_at >= time_min)
        if time_max is not None:
            stmt = stmt.where(Reminder.remind_at <= time_max)
        stmt = stmt.order_by(Reminder.remind_at.asc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(
        self,
        reminder_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
        *,
        include_sent: bool = False,
    ) -> Reminder | None:
        stmt = select(Reminder).where(
            Reminder.id == reminder_id, Reminder.user_id == user_id
        )
        if not include_sent:
            stmt = stmt.where(Reminder.sent.is_(False))
        result = await db.execute(stmt)
        return result.scalars().first()

    async def update_reminder(
        self,
        reminder: Reminder,
        *,
        new_text: str | None,
        new_remind_at: datetime | None,
        db: AsyncSession,
    ) -> Reminder:
        """Update text and/or scheduled time of an unsent reminder.

        Same validation as ``create_reminder`` applies to
        ``new_remind_at`` (naive rejected, past beyond grace rejected,
        more than a year out rejected).
        """
        if new_text is None and new_remind_at is None:
            raise ReminderError(log_message="update called with no changes")

        if new_text is not None:
            cleaned = new_text.strip()
            if not cleaned:
                raise ReminderError(log_message="new_text was empty")
            reminder.reminder_text = cleaned

        if new_remind_at is not None:
            if new_remind_at.tzinfo is None:
                raise ReminderError(
                    log_message="new_remind_at must be timezone-aware",
                    user_message="Reminder time needs a timezone.",
                )
            now_utc = datetime.now(UTC)
            if new_remind_at < now_utc - _PAST_GRACE:
                raise ReminderError(
                    log_message=(
                        f"new_remind_at {new_remind_at.isoformat()} is in the past"
                    ),
                    user_message=(
                        "That time is in the past — when should I remind you?"
                    ),
                )
            if new_remind_at > now_utc + _MAX_FUTURE:
                raise ReminderError(
                    log_message=(
                        f"new_remind_at {new_remind_at.isoformat()} is over a year out"
                    ),
                    user_message=(
                        "That's too far out — I can only remind up to a year ahead."
                    ),
                )
            reminder.remind_at = new_remind_at

        await db.flush()
        return reminder

    async def delete_reminder(
        self, reminder: Reminder, db: AsyncSession
    ) -> None:
        """Hard-delete a reminder row. Reminders are ephemeral — no audit."""
        await db.delete(reminder)
        await db.flush()

    async def mark_sent(self, reminder: Reminder, db: AsyncSession) -> None:
        reminder.sent = True
        reminder.sent_at = datetime.now(UTC)
        await db.flush()

    async def increment_failed_attempts(
        self, reminder: Reminder, db: AsyncSession
    ) -> int:
        reminder.failed_attempts = (reminder.failed_attempts or 0) + 1
        await db.flush()
        return reminder.failed_attempts


def _format_reminder_body(reminder_text: str) -> str:
    return f"Reminder: {reminder_text}"


def _is_outside_24h_window(exc: WhatsAppAPIError) -> bool:
    body = getattr(exc, "body", "") or ""
    return _OUTSIDE_24H_ERROR_CODE in body


async def _send_reminder_message(
    *,
    whatsapp: WhatsAppClient,
    to: str,
    body: str,
    template_name: str,
    template_language: str,
) -> None:
    """Try send_text_message; on 24h-window error fall back to template."""
    try:
        await whatsapp.send_text_message(to=to, text=body)
        return
    except WhatsAppAPIError as exc:
        if not _is_outside_24h_window(exc):
            raise
        logger.info(
            "Reminder text send rejected as outside 24h window — using template",
            extra={"event": "reminder.fallback_template"},
        )

    await whatsapp.send_template_message(
        to=to,
        template_name=template_name,
        body_text=body,
        language_code=template_language,
    )


class ReminderScheduler:
    """Periodic tick that sends due reminders."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        whatsapp_client_factory: Callable[[], WhatsAppClient] | None = None,
        reminder_service_factory: Callable[[], ReminderService] | None = None,
    ) -> None:
        self._session_factory: Callable[[], Any] = (
            session_factory or AsyncSessionLocal
        )
        self._whatsapp_client_factory = whatsapp_client_factory or WhatsAppClient
        self._reminder_service_factory = (
            reminder_service_factory or ReminderService
        )

    async def run_tick(self, *, now_utc: datetime | None = None) -> int:
        if now_utc is None:
            now_utc = datetime.now(UTC)

        sent_count = 0
        async with self._session_factory() as session:
            service = self._reminder_service_factory()
            try:
                due = await service.get_due_reminders(now_utc, session)
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception(
                    "Reminder tick: failed to load due reminders",
                    extra={"event": "reminder.load_failed"},
                )
                return 0

            for reminder in due:
                try:
                    if await self._process_reminder(
                        reminder=reminder, session=session, service=service
                    ):
                        sent_count += 1
                except Exception:  # noqa: BLE001 - per-reminder isolation
                    logger.exception(
                        "Reminder send failed for reminder=%s",
                        reminder.id,
                        extra={"event": "reminder.user_failed"},
                    )

            try:
                await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Reminder tick: commit failed",
                    extra={"event": "reminder.commit_failed"},
                )
                await session.rollback()

        if sent_count:
            logger.info(
                "Reminder tick sent %d reminder(s)",
                sent_count,
                extra={"event": "reminder.tick_sent", "count": sent_count},
            )
        return sent_count

    async def _process_reminder(
        self,
        *,
        reminder: Reminder,
        session: AsyncSession,
        service: ReminderService,
    ) -> bool:
        settings = get_settings()
        user: User | None = reminder.user
        if user is None or not user.wa_id:
            # Defensive: orphan / phone-less rows should not jam the queue.
            logger.warning(
                "Reminder %s has no user/wa_id — marking sent to skip",
                reminder.id,
                extra={"event": "reminder.no_user"},
            )
            await service.mark_sent(reminder, session)
            return False

        body = _format_reminder_body(reminder.reminder_text)
        whatsapp = self._whatsapp_client_factory()
        try:
            await _send_reminder_message(
                whatsapp=whatsapp,
                to=user.wa_id,
                body=body,
                template_name=settings.whatsapp_reminder_template_name,
                template_language=settings.whatsapp_template_language,
            )
        except (WhatsAppSendError, Exception) as exc:  # noqa: BLE001
            attempts = await service.increment_failed_attempts(reminder, session)
            if attempts >= settings.reminder_max_attempts:
                logger.warning(
                    "Reminder %s gave up after %d attempts: %s",
                    reminder.id,
                    attempts,
                    exc,
                    extra={
                        "event": "reminder.max_attempts",
                        "reminder_id": str(reminder.id),
                        "attempts": attempts,
                    },
                )
                await service.mark_sent(reminder, session)
            else:
                logger.warning(
                    "Reminder %s send failed (attempt %d): %s",
                    reminder.id,
                    attempts,
                    exc,
                    extra={
                        "event": "reminder.send_failed",
                        "reminder_id": str(reminder.id),
                        "attempts": attempts,
                    },
                )
            return False

        await service.mark_sent(reminder, session)
        return True


async def reminder_scheduler_loop(
    scheduler: ReminderScheduler | None = None,
    *,
    stop_event: asyncio.Event | None = None,
    tick_seconds: int | None = None,
) -> None:
    """Run scheduler ticks until ``stop_event`` is set.

    Designed to be started as an ``asyncio.create_task`` from the
    FastAPI lifespan, mirroring ``daily_agenda.scheduler_loop``.
    """
    scheduler = scheduler or ReminderScheduler()
    stop_event = stop_event or reminder_stop_event
    settings = get_settings()
    interval = tick_seconds or settings.reminder_tick_seconds

    logger.info(
        "Reminder scheduler started (tick=%ss)",
        interval,
        extra={"event": "reminder.scheduler_started"},
    )
    try:
        while not stop_event.is_set():
            try:
                await scheduler.run_tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception(
                    "Reminder tick crashed",
                    extra={"event": "reminder.tick_crashed"},
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info(
            "Reminder scheduler stopped",
            extra={"event": "reminder.scheduler_stopped"},
        )


__all__ = [
    "ReminderError",
    "ReminderScheduler",
    "ReminderService",
    "reminder_scheduler_loop",
    "reminder_stop_event",
]
