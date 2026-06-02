"""Async Google Calendar API client (REST, no google-api-python-client)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.utils.exceptions import GoogleCalendarError as _BaseCalendarError
from app.utils.retry import async_retry

logger = logging.getLogger(__name__)


class GoogleCalendarError(_BaseCalendarError):
    """Generic Google Calendar API failure (any non-2xx response)."""

    def __init__(self, status_code: int | None, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            log_message=f"Google Calendar API error {status_code}: {body}",
        )

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"Google Calendar API error {self.status_code}: {self.body}"


class GoogleCalendarUnauthorized(GoogleCalendarError):
    """Raised on a 401 from Google Calendar — caller should refresh + retry."""


class GoogleCalendarRateLimited(GoogleCalendarError):
    """Raised on a 429 from Google Calendar."""


class GoogleCalendarServerError(GoogleCalendarError):
    """Raised on a 5xx — retry inside the service before bubbling up."""


# httpx transport-level errors we retry automatically.
_TRANSPORT_EXCS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


class GoogleCalendarService:
    BASE_URL = "https://www.googleapis.com/calendar/v3"

    def __init__(
        self,
        access_token: str,
        calendar_id: str = "primary",
        http_client_factory=None,
        timeout: float = 10.0,
    ) -> None:
        self._access_token = access_token
        self._calendar_id = calendar_id
        self._timeout = timeout
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=self._timeout)
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _events_url(self, event_id: str | None = None) -> str:
        base = f"{self.BASE_URL}/calendars/{self._calendar_id}/events"
        return f"{base}/{event_id}" if event_id else base

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        body = response.text
        if response.status_code == 401:
            raise GoogleCalendarUnauthorized(response.status_code, body)
        if response.status_code == 429:
            raise GoogleCalendarRateLimited(response.status_code, body)
        if response.status_code == 403 and _is_rate_limit_exceeded(response):
            # Google's app-level quota burst signal — distinct from 429.
            raise GoogleCalendarRateLimited(response.status_code, body)
        if response.status_code >= 500:
            raise GoogleCalendarServerError(response.status_code, body)
        raise GoogleCalendarError(response.status_code, body)

    @async_retry(
        max_retries=1,
        delay=0.2,
        backoff=2.0,
        exceptions=_TRANSPORT_EXCS + (GoogleCalendarServerError,),
    )
    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        async with self._http_client_factory() as client:
            method_lower = method.lower()
            callable_ = getattr(client, method_lower)
            response = await callable_(url, headers=self._headers, **kwargs)
        self._raise_for_status(response)
        return response

    async def _request_handling_rate_limit(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Like ``_request`` but waits 1s and retries once on 429."""
        try:
            return await self._request(method, url, **kwargs)
        except GoogleCalendarRateLimited:
            logger.warning(
                "Google Calendar rate-limited — sleeping 1s and retrying once",
                extra={"event": "calendar.rate_limited"},
            )
            await asyncio.sleep(1.0)
            return await self._request(method, url, **kwargs)

    async def create_event(
        self,
        title: str,
        start: str,
        end: str,
        description: str | None = None,
        timezone: str = "Europe/Madrid",
        recurrence: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start, "timeZone": timezone},
            "end": {"dateTime": end, "timeZone": timezone},
        }
        if description:
            body["description"] = description
        if recurrence:
            # List of RFC 5545 RRULE/RDATE strings, e.g. ["RRULE:FREQ=DAILY;COUNT=3"].
            body["recurrence"] = recurrence

        response = await self._request_handling_rate_limit(
            "POST", self._events_url(), json=body
        )
        return response.json()

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        query: str | None = None,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if query:
            params["q"] = query

        response = await self._request_handling_rate_limit(
            "GET", self._events_url(), params=params
        )
        return list(response.json().get("items") or [])

    async def get_event(self, event_id: str) -> dict[str, Any]:
        response = await self._request_handling_rate_limit(
            "GET", self._events_url(event_id)
        )
        return response.json()

    async def update_event(
        self, event_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self._request_handling_rate_limit(
            "PATCH", self._events_url(event_id), json=updates
        )
        return response.json()

    async def delete_event(self, event_id: str) -> None:
        # ``_request`` already raises on >=400; a 204 (success-no-content) just
        # falls through here without a return body.
        await self._request_handling_rate_limit(
            "DELETE", self._events_url(event_id)
        )


def _is_rate_limit_exceeded(response: httpx.Response) -> bool:
    try:
        data = response.json()
    except ValueError:
        return False
    errors = (data.get("error") or {}).get("errors") or []
    if not errors:
        return False
    reason = errors[0].get("reason") if isinstance(errors[0], dict) else None
    return reason == "rateLimitExceeded"
