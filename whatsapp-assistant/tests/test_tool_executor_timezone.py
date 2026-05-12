"""Tests for the set_timezone tool handler."""

from __future__ import annotations

import uuid
from typing import Any

from app.services.tool_executor import ToolExecutor


class _DummyUser:
    def __init__(self, timezone: str = "Europe/Madrid") -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = timezone
        self.preferences_json: dict[str, Any] = {}


class _CapturingSession:
    def __init__(self) -> None:
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1


async def test_set_timezone_updates_user_and_flushes() -> None:
    user = _DummyUser(timezone="Europe/Madrid")
    session = _CapturingSession()
    executor = ToolExecutor()

    result = await executor.execute(
        "set_timezone",
        {"timezone": "Asia/Makassar", "location_label": "Bali"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert user.timezone == "Asia/Makassar"
    assert session.flushes == 1
    assert "Bali" in result.message
    assert "Asia/Makassar" in result.message
    assert result.data == {
        "tool": "set_timezone",
        "timezone": "Asia/Makassar",
        "previous_timezone": "Europe/Madrid",
    }


async def test_set_timezone_without_label_falls_back_to_zone() -> None:
    user = _DummyUser(timezone="Europe/Madrid")
    session = _CapturingSession()
    executor = ToolExecutor()

    result = await executor.execute(
        "set_timezone",
        {"timezone": "America/New_York"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert user.timezone == "America/New_York"
    assert "America/New_York" in result.message


async def test_set_timezone_rejects_unknown_zone() -> None:
    user = _DummyUser(timezone="Europe/Madrid")
    session = _CapturingSession()
    executor = ToolExecutor()

    result = await executor.execute(
        "set_timezone",
        {"timezone": "Mars/Olympus_Mons"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert user.timezone == "Europe/Madrid"
    assert session.flushes == 0
    assert "Mars/Olympus_Mons" in result.message


async def test_set_timezone_rejects_missing_argument() -> None:
    user = _DummyUser(timezone="Europe/Madrid")
    session = _CapturingSession()
    executor = ToolExecutor()

    result = await executor.execute(
        "set_timezone",
        {},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert user.timezone == "Europe/Madrid"
    assert session.flushes == 0


async def test_set_timezone_noop_when_unchanged() -> None:
    user = _DummyUser(timezone="Asia/Makassar")
    session = _CapturingSession()
    executor = ToolExecutor()

    result = await executor.execute(
        "set_timezone",
        {"timezone": "Asia/Makassar"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert (result.data or {}).get("unchanged") is True
    assert session.flushes == 0
