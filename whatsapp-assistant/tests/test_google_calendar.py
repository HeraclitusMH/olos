from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.google_calendar import (
    GoogleCalendarError,
    GoogleCalendarRateLimited,
    GoogleCalendarService,
    GoogleCalendarUnauthorized,
)


def _make_response(
    status_code: int = 200, body: dict | None = None, text: str = ""
) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body or {}
    response.text = text or (str(body) if body else "")
    return response


def _http_factory(
    *,
    post: MagicMock | None = None,
    get: MagicMock | None = None,
    patch: MagicMock | None = None,
    delete: MagicMock | None = None,
) -> tuple[Any, MagicMock]:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.post = AsyncMock(return_value=post) if post is not None else AsyncMock()
    client.get = AsyncMock(return_value=get) if get is not None else AsyncMock()
    client.patch = AsyncMock(return_value=patch) if patch is not None else AsyncMock()
    client.delete = (
        AsyncMock(return_value=delete) if delete is not None else AsyncMock()
    )
    return (lambda: client), client


async def test_create_event_posts_expected_body() -> None:
    response = _make_response(
        status_code=200,
        body={"id": "evt-1", "htmlLink": "https://cal/evt-1"},
    )
    factory, client = _http_factory(post=response)
    service = GoogleCalendarService(
        access_token="ya29.token", http_client_factory=factory
    )

    result = await service.create_event(
        title="Guitar",
        start="2026-05-13T16:00:00+02:00",
        end="2026-05-13T17:00:00+02:00",
        description="practice",
        timezone="Europe/Madrid",
    )

    assert result["id"] == "evt-1"
    assert client.post.await_count == 1
    args, kwargs = client.post.call_args
    assert args[0].endswith("/calendars/primary/events")
    assert kwargs["json"]["summary"] == "Guitar"
    assert kwargs["json"]["start"] == {
        "dateTime": "2026-05-13T16:00:00+02:00",
        "timeZone": "Europe/Madrid",
    }
    assert kwargs["json"]["description"] == "practice"
    assert kwargs["headers"]["Authorization"] == "Bearer ya29.token"


async def test_create_event_omits_description_when_none() -> None:
    response = _make_response(status_code=200, body={"id": "evt-2"})
    factory, client = _http_factory(post=response)
    service = GoogleCalendarService(
        access_token="ya29.token", http_client_factory=factory
    )

    await service.create_event(
        title="Guitar",
        start="2026-05-13T16:00:00+02:00",
        end="2026-05-13T17:00:00+02:00",
    )
    _, kwargs = client.post.call_args
    assert "description" not in kwargs["json"]


async def test_list_events_passes_query_and_params() -> None:
    response = _make_response(status_code=200, body={"items": [{"id": "a"}]})
    factory, client = _http_factory(get=response)
    service = GoogleCalendarService(
        access_token="ya29.token",
        calendar_id="primary",
        http_client_factory=factory,
    )

    items = await service.list_events(
        time_min="2026-05-13T00:00:00+02:00",
        time_max="2026-05-14T00:00:00+02:00",
        query="dentist",
        max_results=5,
    )
    assert items == [{"id": "a"}]
    _, kwargs = client.get.call_args
    params = kwargs["params"]
    assert params["timeMin"] == "2026-05-13T00:00:00+02:00"
    assert params["timeMax"] == "2026-05-14T00:00:00+02:00"
    assert params["q"] == "dentist"
    assert params["singleEvents"] == "true"
    assert params["orderBy"] == "startTime"
    assert params["maxResults"] == 5


async def test_list_events_returns_empty_list_when_no_items() -> None:
    response = _make_response(status_code=200, body={})
    factory, _ = _http_factory(get=response)
    service = GoogleCalendarService(
        access_token="ya29.token", http_client_factory=factory
    )
    assert await service.list_events("a", "b") == []


async def test_unauthorized_raises_specific_exception() -> None:
    response = _make_response(status_code=401, text="unauthorized")
    factory, _ = _http_factory(get=response)
    service = GoogleCalendarService(
        access_token="bad", http_client_factory=factory
    )
    with pytest.raises(GoogleCalendarUnauthorized):
        await service.list_events("a", "b")


async def test_rate_limit_raises_specific_exception() -> None:
    response = _make_response(status_code=429, text="rate limited")
    factory, _ = _http_factory(get=response)
    service = GoogleCalendarService(
        access_token="ya29", http_client_factory=factory
    )
    with pytest.raises(GoogleCalendarRateLimited):
        await service.list_events("a", "b")


async def test_other_4xx_raises_generic_calendar_error() -> None:
    response = _make_response(status_code=500, text="server error")
    factory, _ = _http_factory(post=response)
    service = GoogleCalendarService(
        access_token="ya29", http_client_factory=factory
    )
    with pytest.raises(GoogleCalendarError):
        await service.create_event(
            title="x",
            start="2026-05-13T16:00:00+02:00",
            end="2026-05-13T17:00:00+02:00",
        )


async def test_update_event_patches_event_id() -> None:
    response = _make_response(status_code=200, body={"id": "evt-1"})
    factory, client = _http_factory(patch=response)
    service = GoogleCalendarService(
        access_token="ya29", http_client_factory=factory
    )

    await service.update_event("evt-1", {"summary": "new title"})
    args, kwargs = client.patch.call_args
    assert args[0].endswith("/calendars/primary/events/evt-1")
    assert kwargs["json"] == {"summary": "new title"}


async def test_delete_event_returns_none_on_204() -> None:
    response = _make_response(status_code=204)
    factory, client = _http_factory(delete=response)
    service = GoogleCalendarService(
        access_token="ya29", http_client_factory=factory
    )

    assert await service.delete_event("evt-1") is None
    args, _ = client.delete.call_args
    assert args[0].endswith("/calendars/primary/events/evt-1")
