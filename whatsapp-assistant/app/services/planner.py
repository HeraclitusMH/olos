"""LLM planner that maps WhatsApp messages to assistant tool calls."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai import APIError, APITimeoutError, AsyncOpenAI, OpenAIError, RateLimitError

from app.config import get_settings
from app.services.llm_tools import SYSTEM_PROMPT_TEMPLATE, TOOLS

logger = logging.getLogger(__name__)


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
            *conversation_history[-20:],
            {"role": "user", "content": user_message},
        ]

        try:
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=TOOLS,
                tool_choice="required",
                timeout=20.0,
            )
            return _extract_tool_calls(response)
        except APITimeoutError:
            logger.warning("OpenAI planner request timed out", exc_info=True)
            return _fallback_reply("Request timed out. Try again.")
        except RateLimitError:
            logger.warning("OpenAI planner request was rate limited", exc_info=True)
            return _fallback_reply("I'm temporarily rate limited. Try again shortly.")
        except (json.JSONDecodeError, ValueError, IndexError, AttributeError):
            logger.warning("OpenAI planner returned an invalid response", exc_info=True)
            return _fallback_reply("I couldn't understand that. Please rephrase.")
        except (APIError, OpenAIError):
            logger.exception("OpenAI planner request failed")
            return _fallback_reply("I couldn't process that right now. Try again shortly.")


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

