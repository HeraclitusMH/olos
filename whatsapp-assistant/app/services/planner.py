"""LLM planner that maps WhatsApp messages to assistant tool calls."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from app.config import get_settings
from app.services.llm_tools import SYSTEM_PROMPT_TEMPLATE, TOOLS
from app.utils.exceptions import OpenAIError
from app.utils.retry import async_retry

logger = logging.getLogger(__name__)

# How long to wait on a 429 retry if the server doesn't send Retry-After.
_DEFAULT_429_BACKOFF_SECONDS = 2.0
# Hard cap so we never sleep longer than the request timeout itself.
_MAX_429_BACKOFF_SECONDS = 10.0


_RETRYABLE_OPENAI_EXCS: tuple[type[BaseException], ...] = (
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


class Planner:
    def __init__(self) -> None:
        self.client = AsyncOpenAI()

    async def plan(
        self,
        user_message: str,
        conversation_history: list[dict[str, str]],
        user_timezone: str,
    ) -> list[dict[str, Any]]:
        timezone = _get_timezone(user_timezone)
        current_datetime = datetime.now(timezone).isoformat()
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    current_datetime=current_datetime,
                    user_timezone=timezone.key,
                ),
            },
            *conversation_history[-10:],
            {"role": "user", "content": user_message},
        ]

        started = time.monotonic()
        try:
            response = await self._call_openai(messages)
        except RateLimitError as exc:
            retry_after = _retry_after_seconds(exc)
            logger.warning(
                "OpenAI 429 — sleeping %.2fs and retrying once",
                retry_after,
                extra={"event": "openai.rate_limited", "retry_after": retry_after},
            )
            await asyncio.sleep(retry_after)
            try:
                response = await self._call_openai(messages)
            except RateLimitError as second:
                raise OpenAIError(
                    log_message=f"OpenAI rate-limited twice: {second}",
                ) from second
            except _RETRYABLE_OPENAI_EXCS as second_transient:
                raise OpenAIError(
                    log_message=(
                        f"OpenAI transient error after 429: {second_transient}"
                    ),
                ) from second_transient
        except _RETRYABLE_OPENAI_EXCS as exc:
            raise OpenAIError(
                log_message=f"OpenAI transient failure: {exc}",
            ) from exc
        except (APIError, Exception) as exc:
            raise OpenAIError(log_message=f"OpenAI request failed: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            tool_calls = _extract_tool_calls(response)
        except (json.JSONDecodeError, ValueError, IndexError, AttributeError) as exc:
            logger.warning(
                "OpenAI planner returned an invalid response: %s",
                exc,
                extra={"event": "openai.invalid_response"},
            )
            return _fallback_reply("I couldn't understand that. Please rephrase.")

        logger.info(
            "OpenAI planner call completed",
            extra={
                "event": "openai.call",
                "duration_ms": duration_ms,
                "tool_count": len(tool_calls),
                "tools": [call.get("name") for call in tool_calls],
            },
        )
        return tool_calls

    @async_retry(
        max_retries=2,
        delay=1.0,
        backoff=2.0,
        exceptions=_RETRYABLE_OPENAI_EXCS,
    )
    async def _call_openai(self, messages: list[dict[str, str]]) -> Any:
        return await self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            tool_choice="required",
            timeout=20.0,
        )


def _retry_after_seconds(exc: RateLimitError) -> float:
    """Read ``Retry-After`` off a RateLimitError, falling back to a default."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return _DEFAULT_429_BACKOFF_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_429_BACKOFF_SECONDS
    return max(0.0, min(value, _MAX_429_BACKOFF_SECONDS))


def _get_timezone(user_timezone: str) -> ZoneInfo:
    fallback_timezone = get_settings().default_timezone
    try:
        return ZoneInfo(user_timezone)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Unknown user timezone=%s, falling back to %s",
            user_timezone,
            fallback_timezone,
        )
        return ZoneInfo(fallback_timezone)


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    choices = _get_value(response, "choices")
    if not choices:
        raise ValueError("OpenAI response had no choices")

    message = _get_value(choices[0], "message")
    raw_tool_calls = _get_value(message, "tool_calls") or []
    if not raw_tool_calls:
        raise ValueError("OpenAI response had no tool calls")

    tool_calls: list[dict[str, Any]] = []
    for raw_tool_call in raw_tool_calls:
        function = _get_value(raw_tool_call, "function")
        name = _get_value(function, "name")
        raw_arguments = _get_value(function, "arguments") or "{}"
        if not name:
            raise ValueError("OpenAI tool call missing function name")
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("OpenAI tool arguments must be a JSON object")
        tool_calls.append({"name": name, "arguments": arguments})

    return tool_calls


def _get_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key)


def _fallback_reply(message: str) -> list[dict[str, Any]]:
    return [{"name": "reply", "arguments": {"message": message}}]
