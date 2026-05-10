from __future__ import annotations

import uuid
from typing import Any

from app.services.event_resolver import EventResolver, ResolvedEvent, is_vague_title


def _event(
    *,
    event_id: str,
    summary: str,
    start: str = "2026-05-13T16:00:00+02:00",
    end: str = "2026-05-13T17:00:00+02:00",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


def _list_events_returning(events: list[dict[str, Any]]):
    captured: list[dict[str, Any]] = []

    async def fn(time_min: str, time_max: str, query: str | None):
        captured.append({"time_min": time_min, "time_max": time_max, "query": query})
        return events

    return fn, captured


async def test_resolve_returns_single_event_for_one_match() -> None:
    fn, _ = _list_events_returning(
        [_event(event_id="evt-1", summary="Guitar practice")]
    )
    resolver = EventResolver(fn, "primary")

    result = await resolver.resolve(
        "guitar", None, uuid.uuid4(), db=None  # type: ignore[arg-type]
    )

    assert isinstance(result, ResolvedEvent)
    assert result.event_id == "evt-1"
    assert result.title == "Guitar practice"
    assert result.calendar_id == "primary"


async def test_resolve_returns_list_for_multiple_matches() -> None:
    fn, _ = _list_events_returning(
        [
            _event(event_id="evt-1", summary="Team meeting"),
            _event(event_id="evt-2", summary="Project meeting"),
        ]
    )
    resolver = EventResolver(fn, "primary")

    result = await resolver.resolve(
        "meeting", None, uuid.uuid4(), db=None  # type: ignore[arg-type]
    )

    assert isinstance(result, list)
    assert {ev.event_id for ev in result} == {"evt-1", "evt-2"}


async def test_resolve_filters_non_matching_titles() -> None:
    fn, _ = _list_events_returning(
        [
            _event(event_id="evt-1", summary="Dentist"),
            _event(event_id="evt-2", summary="Lunch with Bob"),
        ]
    )
    resolver = EventResolver(fn, "primary")

    result = await resolver.resolve(
        "dentist", None, uuid.uuid4(), db=None  # type: ignore[arg-type]
    )

    assert isinstance(result, ResolvedEvent)
    assert result.event_id == "evt-1"


async def test_resolve_returns_none_when_no_matches() -> None:
    fn, _ = _list_events_returning([_event(event_id="evt-1", summary="Lunch")])
    resolver = EventResolver(fn, "primary")

    result = await resolver.resolve(
        "dentist", None, uuid.uuid4(), db=None  # type: ignore[arg-type]
    )

    assert result is None


async def test_resolve_uses_search_date_window() -> None:
    fn, captured = _list_events_returning([])
    resolver = EventResolver(fn, "primary")

    await resolver.resolve(
        "x",
        "2026-05-13T16:00:00+00:00",
        uuid.uuid4(),
        db=None,  # type: ignore[arg-type]
    )

    assert len(captured) == 1
    assert "2026-05-06" in captured[0]["time_min"]
    assert "2026-05-20" in captured[0]["time_max"]
    assert captured[0]["query"] == "x"


async def test_resolve_from_context_returns_recent_event_id() -> None:
    class _Msg:
        execution_result_json = {
            "results": [
                {
                    "tool": "calendar_create",
                    "success": True,
                    "data": {"google_event_id": "evt-recent"},
                }
            ]
        }

    event_id = await EventResolver.resolve_from_context([_Msg()], "it")
    assert event_id == "evt-recent"


async def test_resolve_from_context_skips_non_vague_title() -> None:
    class _Msg:
        execution_result_json = {
            "results": [{"data": {"google_event_id": "evt-recent"}}]
        }

    assert (
        await EventResolver.resolve_from_context([_Msg()], "guitar practice") is None
    )


async def test_resolve_from_context_returns_none_without_recent_event() -> None:
    class _Msg:
        execution_result_json = {"results": []}

    assert await EventResolver.resolve_from_context([_Msg()], "it") is None


def test_is_vague_title_detects_pronouns() -> None:
    for title in ("it", "That", " the event ", "", None):
        assert is_vague_title(title) is True
    assert is_vague_title("dentist") is False
