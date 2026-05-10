from __future__ import annotations

from typing import Any

from app.services.disambiguation import (
    BUTTON_TITLE_MAX,
    DisambiguationService,
    build_option_id,
    parse_option_id,
)
from app.services.event_resolver import ResolvedEvent


class _FakeWhatsAppClient:
    def __init__(self) -> None:
        self.button_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def send_interactive_buttons(
        self, to: str, body_text: str, buttons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.button_calls.append(
            {"to": to, "body_text": body_text, "buttons": buttons}
        )
        return {"messages": [{"id": "wamid.OUT"}]}

    async def send_interactive_list(
        self,
        to: str,
        body_text: str,
        button_text: str,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.list_calls.append(
            {
                "to": to,
                "body_text": body_text,
                "button_text": button_text,
                "sections": sections,
            }
        )
        return {"messages": [{"id": "wamid.OUT"}]}


def _events(n: int) -> list[ResolvedEvent]:
    return [
        ResolvedEvent(
            event_id=f"evt-{i}",
            title=f"Event {i}",
            start="2026-05-13T16:00:00+02:00",
            end="2026-05-13T17:00:00+02:00",
            calendar_id="primary",
        )
        for i in range(n)
    ]


async def test_send_two_events_uses_buttons() -> None:
    client = _FakeWhatsAppClient()
    service = DisambiguationService(client)  # type: ignore[arg-type]

    disambig_id = await service.send_event_choices(
        wa_id="34600", events=_events(2), action="cancel", timezone_name="Europe/Madrid"
    )

    assert disambig_id
    assert len(client.button_calls) == 1
    call = client.button_calls[0]
    assert call["to"] == "34600"
    assert "cancel" in call["body_text"]
    assert len(call["buttons"]) == 2
    for button in call["buttons"]:
        assert button["reply"]["id"].startswith(f"disambig:{disambig_id}:")
        assert len(button["reply"]["title"]) <= BUTTON_TITLE_MAX


async def test_send_three_events_uses_buttons() -> None:
    client = _FakeWhatsAppClient()
    service = DisambiguationService(client)  # type: ignore[arg-type]

    await service.send_event_choices(
        wa_id="34600", events=_events(3), action="update", timezone_name="Europe/Madrid"
    )

    assert len(client.button_calls) == 1
    assert len(client.button_calls[0]["buttons"]) == 3
    assert client.list_calls == []


async def test_send_four_events_uses_list() -> None:
    client = _FakeWhatsAppClient()
    service = DisambiguationService(client)  # type: ignore[arg-type]

    await service.send_event_choices(
        wa_id="34600", events=_events(4), action="cancel", timezone_name="Europe/Madrid"
    )

    assert client.button_calls == []
    assert len(client.list_calls) == 1
    section = client.list_calls[0]["sections"][0]
    assert len(section["rows"]) == 4


async def test_send_truncates_long_button_titles() -> None:
    client = _FakeWhatsAppClient()
    service = DisambiguationService(client)  # type: ignore[arg-type]

    long_event = ResolvedEvent(
        event_id="evt-long",
        title="A really really long event name that overflows",
        start="2026-05-13T16:00:00+02:00",
        end="2026-05-13T17:00:00+02:00",
        calendar_id="primary",
    )

    await service.send_event_choices(
        wa_id="34600",
        events=[long_event, long_event],
        action="cancel",
        timezone_name="Europe/Madrid",
    )

    for button in client.button_calls[0]["buttons"]:
        assert len(button["reply"]["title"]) <= BUTTON_TITLE_MAX


def test_parse_option_id_round_trip() -> None:
    option_id = build_option_id("abc123", 2)
    assert parse_option_id(option_id) == ("abc123", 2)


def test_parse_option_id_rejects_unknown_prefix() -> None:
    assert parse_option_id("opt:abc:0") is None
    assert parse_option_id("disambig:abc") is None
    assert parse_option_id(None) is None
