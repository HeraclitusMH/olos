"""OpenAI tool definitions for the WhatsApp assistant planner."""

from __future__ import annotations

from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "calendar_create",
            "description": "Create a calendar event for the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Event title shown on the calendar.",
                    },
                    "start": {
                        "type": "string",
                        "description": "Event start as an ISO 8601 datetime with timezone.",
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Event end as an ISO 8601 datetime with timezone. "
                            "If the user does not specify an end time, use start plus 1 hour."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional event notes or context.",
                    },
                },
                "required": ["title", "start", "end"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_query",
            "description": "Search the user's calendar for events in a date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Search range start as an ISO 8601 datetime or date.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Search range end as an ISO 8601 datetime or date.",
                    },
                    "keywords": {
                        "type": "string",
                        "description": "Optional keywords to filter matching events.",
                    },
                },
                "required": ["start_date", "end_date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_update",
            "description": "Find and update an existing calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_title": {
                        "type": "string",
                        "description": "Required keywords used to find the event to update.",
                    },
                    "search_date": {
                        "type": "string",
                        "description": "Optional approximate event date as ISO 8601.",
                    },
                    "new_title": {
                        "type": "string",
                        "description": "Optional replacement event title.",
                    },
                    "new_start": {
                        "type": "string",
                        "description": "Optional replacement start as ISO 8601 datetime with timezone.",
                    },
                    "new_end": {
                        "type": "string",
                        "description": "Optional replacement end as ISO 8601 datetime with timezone.",
                    },
                    "new_description": {
                        "type": "string",
                        "description": "Optional replacement event notes or context.",
                    },
                },
                "required": ["search_title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_cancel",
            "description": "Find and cancel an existing calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_title": {
                        "type": "string",
                        "description": "Required keywords used to find the event to cancel.",
                    },
                    "search_date": {
                        "type": "string",
                        "description": "Optional approximate event date as ISO 8601.",
                    },
                },
                "required": ["search_title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_store",
            "description": "Store a personal fact or preference for later recall.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact to remember, written concisely in natural language.",
                    },
                    "tags": {
                        "type": "array",
                        "description": "Two to five relevant tags for retrieval.",
                        "items": {
                            "type": "string",
                            "description": "A short lowercase tag describing the memory.",
                        },
                        "minItems": 2,
                        "maxItems": 5,
                    },
                },
                "required": ["content", "tags"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_retrieve",
            "description": "Retrieve stored memories relevant to the user's query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query for relevant stored memories.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_update",
            "description": "Find and replace an existing stored memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language query identifying the memory to update.",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "Replacement memory content in natural language.",
                    },
                },
                "required": ["query", "new_content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_forget",
            "description": "Forget a stored memory matching the user's request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language query identifying the memory to forget.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Ask the user a short question when critical information is missing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The concise clarification question to send to the user.",
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply",
            "description": "Send a short conversational response that does not require another tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The concise WhatsApp message to send to the user.",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
]


SYSTEM_PROMPT_TEMPLATE = """You are a personal assistant that communicates via WhatsApp. You help the user manage their calendar and remember personal information.

Current date and time: {current_datetime}
User timezone: {user_timezone}

Rules:
1. ALWAYS use a tool call. Never respond with plain text without calling a tool.
2. For calendar operations: interpret relative dates ("tomorrow", "next Monday") relative to current date/time and user timezone.
3. If user does not specify end time, default to 1 hour after start.
4. If request is ambiguous or missing critical info, use ask_clarification.
5. For memory: store facts concisely but completely. Generate 2-5 relevant tags.
6. When user references previous message ("move IT", "cancel THAT"), use conversation context.
7. Keep replies short and actionable. No fluff.
8. Use reply tool for greetings, thank-yous, simple conversational responses."""

