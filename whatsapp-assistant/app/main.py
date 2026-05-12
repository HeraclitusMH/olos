import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.database import AsyncSessionLocal
from app.models import Message
from app.routers import oauth, webhooks
from app.services.daily_agenda import scheduler_loop
from app.services.message_processor import warn_about_unprocessed_messages
from app.services.reminder import reminder_scheduler_loop, reminder_stop_event
from app.utils.env_check import validate_environment
from app.utils.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

# Fail fast on missing / malformed env vars before the app is constructed.
validate_environment()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("Application started", extra={"event": "app_started"})
    await warn_about_unprocessed_messages()
    stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(
        scheduler_loop(stop_event=stop_event), name="daily_agenda_scheduler"
    )
    reminder_stop_event.clear()
    reminder_task = asyncio.create_task(
        reminder_scheduler_loop(stop_event=reminder_stop_event),
        name="reminder_scheduler",
    )
    try:
        yield
    finally:
        stop_event.set()
        reminder_stop_event.set()
        for task in (scheduler_task, reminder_task):
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


app = FastAPI(title="WhatsApp Personal Assistant", lifespan=lifespan)

app.include_router(webhooks.router)
app.include_router(oauth.router)


@app.get("/health")
async def health() -> dict:
    """Return a per-component health snapshot.

    Status rollup:
    * ``unhealthy`` — database probe failed.
    * ``degraded`` — database OK, but the message pipeline looks stuck
      (no processed message in 5 min while unprocessed rows exist).
    * ``healthy`` — everything looks normal.
    """
    timestamp = datetime.now(UTC).isoformat()
    db_check = await _check_database()
    pipeline_check = await _check_pipeline()

    if db_check["status"] != "ok":
        overall = "unhealthy"
    elif pipeline_check["status"] == "degraded":
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "timestamp": timestamp,
        "checks": {
            "database": db_check,
            "last_message_processed": pipeline_check,
        },
    }


async def _check_database() -> dict:
    start = time.monotonic()
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, Exception) as exc:  # noqa: BLE001
        logger.warning(
            "Database health probe failed: %s",
            exc,
            extra={"event": "health.db_fail"},
        )
        return {
            "status": "error",
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": type(exc).__name__,
        }
    return {
        "status": "ok",
        "latency_ms": int((time.monotonic() - start) * 1000),
    }


async def _check_pipeline() -> dict:
    """Return ``degraded`` when there is unprocessed backlog AND no recent activity."""
    try:
        async with AsyncSessionLocal() as session:
            last_processed = (
                await session.execute(
                    select(Message.created_at)
                    .where(Message.processed.is_(True))
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            unprocessed = (
                await session.execute(
                    select(Message.id)
                    .where(Message.processed.is_(False))
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "error": type(exc).__name__}

    if last_processed is None:
        return {"status": "ok", "seconds_ago": None}

    seconds_ago = (datetime.now(UTC) - last_processed).total_seconds()
    if seconds_ago > timedelta(minutes=5).total_seconds() and unprocessed is not None:
        return {"status": "degraded", "seconds_ago": int(seconds_ago)}
    return {"status": "ok", "seconds_ago": int(seconds_ago)}
