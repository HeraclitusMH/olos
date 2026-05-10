"""Async Google Calendar API client (REST, no google-api-python-client)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GoogleCalendarError(Exception):
    """Generic Google Calendar API failure."""

    def __init__(self, status_code: int | None, body: str) -> None:
        super().__init__(f"Google Calendar API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class GoogleCalendarUnauthorized(GoogleCalendarError):
    """Raised on a 401 from Google Calendar — caller should refresh + retry."""


class GoogleCalendarRateLimited(GoogleCalendarError):
    """Raised on a 429 from Google Calendar."""


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
        raise GoogleCalendarError(response.status_code, body)

    async def create_event(
        self,
        title: str,
        start: str,
        end: str,
        description: str | None = None,
        timezone: str = "Europe/Madrid",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start, "timeZone": timezone},
            "end": {"dateTime": end, "timeZone": timezone},
        }
        if description:
            body["description"] = description

        async with self._http_client_factory() as client:
            response = await client.post(
                self._events_url(), headers=self._headers, json=body
            )
        self._raise_for_status(response)
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

        async with self._http_client_factory() as client:
            response = await client.get(
                self._events_url(), headers=self._headers, params=params
            )
        self._raise_for_status(response)
        return list(response.json().get("items") or [])

    async def get_event(self, event_id: str) -> dict[str, Any]:
        async with self._http_client_factory() as client:
            response = await client.get(
                self._events_url(event_id), headers=self._headers
            )
        self._raise_for_status(response)
        return response.json()

    async def update_event(
        self, event_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._http_client_factory() as client:
            response = await client.patch(
                self._events_url(event_id),
                headers=self._headers,
                json=updates,
            )
        self._raise_for_status(response)
        return response.json()

    async def delete_event(self, event_id: str) -> None:
        async with self._http_client_factory() as client:
            response = await client.delete(
                self._events_url(event_id), headers=self._headers
            )
        if response.status_code == 204:
            return
        self._raise_for_status(response)
