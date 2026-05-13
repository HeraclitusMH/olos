"""Tests for the WhatsApp-driven daily agenda settings flow."""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

import httpx
import pytest

from app.models import User
from app.services.daily_agenda import (
    AGENDA_CUSTOM_TEXT_MAX_LENGTH,
    AgendaPrefs,
    TIME_RANGES,
    format_agenda_message,
    generate_time_slots,
    get_user_prefs,
)
from app.services.message_processor import (
    _process_agenda_settings_reply,
    _process_interactive_reply,
    _try_consume_agenda_custom_text,
)
from app.services.pending_action import (
    get_active_pending_action,
    set_pending_action,
)
from app.services.tool_executor import ToolExecutor
from app.services.whatsapp import WhatsAppAPIError, WhatsAppClient


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _DummyUser:
    def __init__(self, *, prefs: dict[str, Any] | None = None) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"
        self.preferences_json: dict[str, Any] = prefs or {}


class _NoOpSession:
    """Fake session — agenda-settings paths only need flush()."""

    def __init__(self) -> None:
        self.flushed = 0

    def add(self, _obj: Any) -> None:  # pragma: no cover
        pass

    async def flush(self) -> None:
        self.flushed += 1

    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover
        raise AssertionError("session not used directly")


class _FakeWhatsApp:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.button_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def send_interactive_buttons(
        self, to: str, body_text: str, buttons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("simulated send failure")
        self.button_calls.append(
            {"to": to, "body_text": body_text, "buttons": buttons}
        )
        return {"ok": True}

    async def send_list_message(
        self,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict[str, Any]],
        header: str | None = None,
        footer: str | None = None,
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("simulated send failure")
        self.list_calls.append(
            {
                "to": to,
                "body": body,
                "button_text": button_text,
                "sections": sections,
                "header": header,
                "footer": footer,
            }
        )
        return {"ok": True}


def _executor(whatsapp: _FakeWhatsApp) -> ToolExecutor:
    return ToolExecutor(whatsapp_client_factory=lambda: whatsapp)


def _set_pending(user: _DummyUser, action_type: str) -> None:
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="agenda_settings",
        action_type=action_type,
        arguments={},
        options=[],
    )


# ---------------------------------------------------------------------------
# 1. send_list_message payload + validation
# ---------------------------------------------------------------------------


def _client_with_transport(transport: httpx.MockTransport) -> WhatsAppClient:
    class _StubClient(WhatsAppClient):
        async def _post(self, payload):  # type: ignore[override]
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.post(
                    self._messages_url, headers=self._headers, json=payload
                )
            if response.status_code >= 400:
                raise WhatsAppAPIError(response.status_code, response.text)
            return response.json()

    return _StubClient(access_token="tok", phone_number_id="PHONE_ID")


async def test_send_list_message_builds_correct_payload() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    transport = httpx.MockTransport(handler)
    client = _client_with_transport(transport)

    sections = [
        {
            "title": "Times",
            "rows": [
                {"id": "agenda_time:07:00", "title": "07:00"},
                {"id": "agenda_time:07:30", "title": "07:30"},
            ],
        }
    ]
    await client.send_list_message(
        to="34600",
        body="Pick a time",
        button_text="Select time",
        sections=sections,
        header="Header",
        footer="Footer",
    )

    interactive = captured["body"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["body"] == {"text": "Pick a time"}
    assert interactive["action"]["button"] == "Select time"
    assert interactive["header"] == {"type": "text", "text": "Header"}
    assert interactive["footer"] == {"text": "Footer"}
    rows = interactive["action"]["sections"][0]["rows"]
    assert rows[0]["id"] == "agenda_time:07:00"


async def test_send_list_message_rejects_too_many_rows() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    rows = [{"id": f"r{i}", "title": f"t{i}"} for i in range(11)]
    with pytest.raises(ValueError):
        await client.send_list_message(
            to="34600",
            body="x",
            button_text="ok",
            sections=[{"title": "x", "rows": rows}],
        )


async def test_send_list_message_rejects_overlong_button_text() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(ValueError):
        await client.send_list_message(
            to="34600",
            body="x",
            button_text="x" * 21,
            sections=[{"title": "x", "rows": [{"id": "r", "title": "t"}]}],
        )


async def test_send_list_message_rejects_overlong_row_title() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(ValueError):
        await client.send_list_message(
            to="34600",
            body="x",
            button_text="ok",
            sections=[{"title": "x", "rows": [{"id": "r", "title": "t" * 25}]}],
        )


async def test_send_list_message_omits_header_when_not_provided() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    transport = httpx.MockTransport(handler)
    client = _client_with_transport(transport)

    await client.send_list_message(
        to="34600",
        body="x",
        button_text="ok",
        sections=[{"title": "T", "rows": [{"id": "r", "title": "t"}]}],
    )

    interactive = captured["body"]["interactive"]
    assert "header" not in interactive
    assert "footer" not in interactive


# ---------------------------------------------------------------------------
# 2. Tool executor: daily_agenda_settings handler
# ---------------------------------------------------------------------------


async def test_daily_agenda_settings_sends_menu_and_sets_pending() -> None:
    user = _DummyUser()
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    result = await executor.execute(
        "daily_agenda_settings",
        {},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == ""
    assert (result.data or {}).get("sent_directly") is True
    assert len(whatsapp.button_calls) == 1
    ids = {
        btn["reply"]["id"]
        for btn in whatsapp.button_calls[0]["buttons"]
    }
    assert ids == {
        "agenda_settings:change_time",
        "agenda_settings:edit_text",
        "agenda_settings:remove_text",
    }
    pending = get_active_pending_action(user)  # type: ignore[arg-type]
    assert pending is not None
    assert pending["type"] == "agenda_settings"


async def test_daily_agenda_settings_menu_shows_current_state() -> None:
    user = _DummyUser(
        prefs={
            "daily_agenda": {
                "enabled": True,
                "timezone": "Europe/Madrid",
                "time_local": "07:30",
                "custom_footer_text": "Morning routine",
            }
        }
    )
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    await executor.execute(
        "daily_agenda_settings",
        {},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    body = whatsapp.button_calls[0]["body_text"]
    assert "07:30" in body
    assert "✓ Set" in body


async def test_daily_agenda_settings_handles_send_failure() -> None:
    user = _DummyUser()
    whatsapp = _FakeWhatsApp(fail=True)
    executor = _executor(whatsapp)

    result = await executor.execute(
        "daily_agenda_settings",
        {},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "couldn't open" in result.message.lower()


# ---------------------------------------------------------------------------
# 3. Time range selection sends time slot list
# ---------------------------------------------------------------------------


async def test_change_time_button_sends_time_range_list() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_settings")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_settings:change_time",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    assert len(whatsapp.list_calls) == 1
    rows = whatsapp.list_calls[0]["sections"][0]["rows"]
    assert len(rows) == 5
    assert rows[0]["id"] == "agenda_range:0"
    assert log.sent_directly is True
    # pending should now be agenda_time_range
    pending = get_active_pending_action(user)  # type: ignore[arg-type]
    assert pending["type"] == "agenda_time_range"


async def test_range_selection_sends_specific_time_slots() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_time_range")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_range:1",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    rows = whatsapp.list_calls[0]["sections"][0]["rows"]
    # Range 1 is 05:00 – 09:30 (10 slots)
    assert len(rows) == 10
    assert rows[0]["id"] == "agenda_time:05:00"
    assert rows[-1]["id"] == "agenda_time:09:30"
    assert log.sent_directly is True
    pending = get_active_pending_action(user)  # type: ignore[arg-type]
    assert pending["type"] == "agenda_time_select"


async def test_range_4_has_eight_slots() -> None:
    """The last range (20:00–23:30) has 8 half-hour slots, not 10."""
    slots = generate_time_slots(4)
    assert len(slots) == 8
    assert slots[0]["id"] == "agenda_time:20:00"
    assert slots[-1]["id"] == "agenda_time:23:30"


# ---------------------------------------------------------------------------
# 4. Time selection updates preferences
# ---------------------------------------------------------------------------


async def test_time_selection_updates_preferences_and_confirms() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_time_select")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_time:07:30",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    daily = user.preferences_json["daily_agenda"]
    assert daily["time_local"] == "07:30"
    assert daily["enabled"] is True
    assert "07:30" in log.combined_reply
    # Pending action cleared.
    assert "pending_action" not in user.preferences_json


async def test_time_selection_rejects_invalid_minute() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_time_select")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_time:07:15",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    assert "not valid" in log.combined_reply.lower()
    assert "daily_agenda" not in user.preferences_json


# ---------------------------------------------------------------------------
# 5. Edit text flow
# ---------------------------------------------------------------------------


async def test_edit_text_button_sets_pending_action_and_prompts() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_settings")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_settings:edit_text",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    pending = get_active_pending_action(user)  # type: ignore[arg-type]
    assert pending["type"] == "agenda_custom_text"
    assert "custom text" in log.combined_reply.lower()
    assert str(AGENDA_CUSTOM_TEXT_MAX_LENGTH) in log.combined_reply


async def test_edit_text_includes_existing_text_in_prompt() -> None:
    user = _DummyUser(
        prefs={
            "daily_agenda": {
                "enabled": True,
                "timezone": "Europe/Madrid",
                "time_local": "08:00",
                "custom_footer_text": "Old morning routine",
            }
        }
    )
    _set_pending(user, "agenda_settings")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_settings:edit_text",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    assert "Old morning routine" in log.combined_reply


# ---------------------------------------------------------------------------
# 6. Custom text capture
# ---------------------------------------------------------------------------


async def test_custom_text_save_updates_preferences() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_custom_text")

    log = await _try_consume_agenda_custom_text(
        user,  # type: ignore[arg-type]
        _NoOpSession(),  # type: ignore[arg-type]
        "🌅 Morning Routine:\n• Meditate\n• Journal",
    )

    assert log is not None
    assert "saved" in log.combined_reply.lower()
    daily = user.preferences_json["daily_agenda"]
    assert daily["custom_footer_text"].startswith("🌅 Morning Routine")
    assert "pending_action" not in user.preferences_json


async def test_custom_text_too_long_keeps_pending() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_custom_text")

    log = await _try_consume_agenda_custom_text(
        user,  # type: ignore[arg-type]
        _NoOpSession(),  # type: ignore[arg-type]
        "x" * (AGENDA_CUSTOM_TEXT_MAX_LENGTH + 1),
    )

    assert log is not None
    assert "too long" in log.combined_reply.lower()
    # Pending action is still alive so the user can try again.
    pending = get_active_pending_action(user)  # type: ignore[arg-type]
    assert pending is not None
    assert pending["type"] == "agenda_custom_text"
    # The pref was not written.
    assert "daily_agenda" not in user.preferences_json


async def test_custom_text_empty_keeps_pending() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_custom_text")

    log = await _try_consume_agenda_custom_text(
        user,  # type: ignore[arg-type]
        _NoOpSession(),  # type: ignore[arg-type]
        "   ",
    )

    assert log is not None
    assert "empty" in log.combined_reply.lower()
    pending = get_active_pending_action(user)  # type: ignore[arg-type]
    assert pending is not None and pending["type"] == "agenda_custom_text"


async def test_custom_text_cancel_clears_pending() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_custom_text")

    log = await _try_consume_agenda_custom_text(
        user,  # type: ignore[arg-type]
        _NoOpSession(),  # type: ignore[arg-type]
        "cancel",
    )

    assert log is not None
    assert "cancelled" in log.combined_reply.lower()
    assert "pending_action" not in user.preferences_json
    assert "daily_agenda" not in user.preferences_json


async def test_no_pending_action_does_not_consume_text() -> None:
    user = _DummyUser()

    log = await _try_consume_agenda_custom_text(
        user,  # type: ignore[arg-type]
        _NoOpSession(),  # type: ignore[arg-type]
        "hello",
    )

    # No pending means we don't consume — planner should handle it.
    assert log is None


# ---------------------------------------------------------------------------
# 7. Remove text
# ---------------------------------------------------------------------------


async def test_remove_text_clears_custom_footer_text() -> None:
    user = _DummyUser(
        prefs={
            "daily_agenda": {
                "enabled": True,
                "timezone": "Europe/Madrid",
                "time_local": "08:00",
                "custom_footer_text": "Morning routine",
            }
        }
    )
    _set_pending(user, "agenda_settings")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_settings:remove_text",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    daily = user.preferences_json["daily_agenda"]
    assert "custom_footer_text" not in daily
    # But other fields preserved.
    assert daily["time_local"] == "08:00"
    assert "removed" in log.combined_reply.lower()
    assert "pending_action" not in user.preferences_json


async def test_remove_text_when_none_set_still_responds_gracefully() -> None:
    user = _DummyUser()
    _set_pending(user, "agenda_settings")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_agenda_settings_reply(
        interactive_id="agenda_settings:remove_text",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        tool_executor=executor,
    )

    assert "nothing to remove" in log.combined_reply.lower()


# ---------------------------------------------------------------------------
# 8. Expired / mismatched pending
# ---------------------------------------------------------------------------


async def test_agenda_reply_with_no_pending_returns_expired() -> None:
    user = _DummyUser()
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_interactive_reply(
        interactive_id="agenda_settings:change_time",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert "expired" in log.combined_reply.lower()
    assert whatsapp.list_calls == []


async def test_agenda_reply_with_expired_pending_returns_expired() -> None:
    user = _DummyUser(
        prefs={
            "pending_action": {
                "id": "agenda_settings",
                "type": "agenda_settings",
                "arguments": {},
                "options": [],
                "expires_at": "2000-01-01T00:00:00+00:00",
            }
        }
    )
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_interactive_reply(
        interactive_id="agenda_settings:change_time",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert "expired" in log.combined_reply.lower()
    assert whatsapp.list_calls == []


# ---------------------------------------------------------------------------
# 9. Daily agenda message formatting with custom footer
# ---------------------------------------------------------------------------


def test_format_agenda_appends_custom_footer_when_set() -> None:
    events = [
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-05-11T09:30:00+08:00"},
            "end": {"dateTime": "2026-05-11T10:15:00+08:00"},
        }
    ]
    msg = format_agenda_message(
        events,
        date(2026, 5, 11),
        "Asia/Makassar",
        custom_footer_text="🌅 Morning Routine:\n• Meditate",
    )
    assert "Standup" in msg
    assert "\n\n---\n\n" in msg
    assert msg.endswith("🌅 Morning Routine:\n• Meditate")


def test_format_agenda_no_custom_footer_has_no_separator() -> None:
    events = [
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-05-11T09:30:00+08:00"},
            "end": {"dateTime": "2026-05-11T10:15:00+08:00"},
        }
    ]
    msg = format_agenda_message(
        events, date(2026, 5, 11), "Asia/Makassar"
    )
    assert "---" not in msg


def test_format_agenda_no_events_with_custom_footer() -> None:
    msg = format_agenda_message(
        [],
        date(2026, 5, 11),
        "Asia/Makassar",
        custom_footer_text="Just my morning routine.",
    )
    assert "no events today" in msg.lower()
    assert msg.endswith("Just my morning routine.")
    assert "\n\n---\n\n" in msg


# ---------------------------------------------------------------------------
# 10. get_user_prefs reads custom_footer_text
# ---------------------------------------------------------------------------


def test_get_user_prefs_reads_custom_footer_text() -> None:
    user = User(
        wa_id="34600111222",
        timezone="Europe/Madrid",
        locale="en",
        preferences_json={
            "daily_agenda": {
                "enabled": True,
                "timezone": "Europe/Madrid",
                "time_local": "07:30",
                "custom_footer_text": "Stay hydrated",
            }
        },
    )
    user.id = uuid.uuid4()
    prefs = get_user_prefs(user)
    assert prefs.custom_footer_text == "Stay hydrated"


def test_get_user_prefs_treats_blank_custom_footer_as_none() -> None:
    user = User(
        wa_id="34600111222",
        timezone="Europe/Madrid",
        locale="en",
        preferences_json={
            "daily_agenda": {
                "enabled": True,
                "timezone": "Europe/Madrid",
                "time_local": "08:00",
                "custom_footer_text": "   ",
            }
        },
    )
    user.id = uuid.uuid4()
    prefs = get_user_prefs(user)
    assert prefs.custom_footer_text is None


# ---------------------------------------------------------------------------
# 11. End-to-end interactive routing
# ---------------------------------------------------------------------------


async def test_interactive_router_dispatches_agenda_ids() -> None:
    """_process_interactive_reply must route agenda_* before disambig parsing."""
    user = _DummyUser()
    _set_pending(user, "agenda_settings")
    whatsapp = _FakeWhatsApp()
    executor = _executor(whatsapp)

    log = await _process_interactive_reply(
        interactive_id="agenda_settings:change_time",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert len(whatsapp.list_calls) == 1
    assert log.sent_directly is True


# ---------------------------------------------------------------------------
# 12. Sanity check on TIME_RANGES constant
# ---------------------------------------------------------------------------


def test_time_ranges_cover_all_48_slots() -> None:
    total = sum(len(generate_time_slots(i)) for i in range(len(TIME_RANGES)))
    assert total == 48
