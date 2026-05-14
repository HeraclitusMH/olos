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
            "description": "Store a personal fact or preference for later recall. Do NOT use for navigation commands or feature-access phrases like 'agenda settings', 'agenda setting', 'help', 'settings', or similar — those are action requests, not facts to remember.",
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
            "name": "reminder_create",
            "description": (
                "Set a reminder for the user. The assistant will send a "
                "WhatsApp message at the specified time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_text": {
                        "type": "string",
                        "description": "What to remind the user about.",
                    },
                    "remind_at": {
                        "type": "string",
                        "description": (
                            "ISO 8601 datetime (with timezone) when the "
                            "reminder should fire. Compute from current "
                            "time + relative offset, or from absolute "
                            "time given."
                        ),
                    },
                },
                "required": ["reminder_text", "remind_at"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_query",
            "description": (
                "List the user's upcoming (unsent) reminders. Use when the "
                "user asks what reminders they have, or to look up an "
                "existing reminder before modifying or cancelling it. "
                "Returns up to 10 unsent reminders ordered by scheduled time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_text": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive substring filter on "
                            "reminder text."
                        ),
                    },
                    "time_min": {
                        "type": "string",
                        "description": (
                            "Optional ISO 8601 datetime (with timezone). "
                            "Only return reminders scheduled at or after this time."
                        ),
                    },
                    "time_max": {
                        "type": "string",
                        "description": (
                            "Optional ISO 8601 datetime (with timezone). "
                            "Only return reminders scheduled at or before this time."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_update",
            "description": (
                "Find an existing unsent reminder by text and change its "
                "scheduled time and/or body. Use this — NEVER reminder_create "
                "— when the user says 'change', 'move', 'update' an existing "
                "reminder. At least one of new_reminder_text or new_remind_at "
                "must be provided."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_text": {
                        "type": "string",
                        "description": (
                            "Keywords used to find the reminder to update. "
                            "Matched case-insensitively against reminder text."
                        ),
                    },
                    "new_reminder_text": {
                        "type": "string",
                        "description": "Optional replacement reminder body.",
                    },
                    "new_remind_at": {
                        "type": "string",
                        "description": (
                            "Optional replacement scheduled time as an ISO 8601 "
                            "datetime with timezone offset. Computed from current "
                            "time + user request, in the user's timezone."
                        ),
                    },
                },
                "required": ["search_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_cancel",
            "description": (
                "Find and cancel (delete) an existing unsent reminder. Use "
                "when the user says 'cancel', 'delete', 'remove', 'nevermind' "
                "about a reminder. NEVER use reminder_create for cancellations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_text": {
                        "type": "string",
                        "description": (
                            "Keywords used to find the reminder to cancel. "
                            "Matched case-insensitively against reminder text."
                        ),
                    },
                },
                "required": ["search_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timezone",
            "description": (
                "Permanently update the user's timezone when they tell you "
                "where they currently are or that they have moved. Use only "
                "when the user expresses a persistent location, not for "
                "transient mentions ('I'm in a meeting', 'at the gym'). "
                "After this, all future calendar and reminder times are "
                "interpreted in the new timezone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": (
                            "IANA timezone name such as 'Asia/Makassar' "
                            "(Bali), 'Europe/Rome', 'America/New_York'. "
                            "Pick the closest canonical zone for the city or "
                            "region the user named."
                        ),
                    },
                    "location_label": {
                        "type": "string",
                        "description": (
                            "Short human-readable location the user mentioned, "
                            "e.g. 'Bali' or 'New York'. Used only in the "
                            "confirmation message."
                        ),
                    },
                },
                "required": ["timezone"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "daily_agenda_settings",
            "description": (
                "Open the settings menu for the automated daily agenda — the "
                "WhatsApp message the assistant sends every morning with the "
                "user's calendar events. Use this (NOT calendar_query) when "
                "the user wants to configure or customize the automatic daily "
                "summary: change the delivery time, add/edit/remove the "
                "custom footer text, or view current settings. Trigger "
                "phrases: 'agenda settings', 'agenda setting', 'daily agenda "
                "settings', 'change agenda time', 'modify my daily agenda', "
                "'customize daily agenda', 'edit morning routine text', "
                "'agenda options', 'agenda preferences'. Takes no arguments."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
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
8. Use reply tool for greetings, thank-yous, simple conversational responses.
9. For reminders: when the user asks to be reminded at a specific time or after a delay, use the reminder_create tool. Compute the exact remind_at datetime from the current time above and the user's request, in the user's timezone. For relative times like "in 5 minutes", add exactly that duration to the current datetime. For "tomorrow at 9am", compute the next occurrence of 09:00 in the user's timezone. Always return remind_at as an ISO 8601 datetime with timezone offset.
10. Reminder updates and cancellations:
    - Use reminder_update when the user wants to change the time or text of an existing reminder ("change", "move", "update", "reschedule", "make it earlier/later"). NEVER call reminder_create for these — that would create a duplicate.
    - Use reminder_cancel when the user wants to delete or stop a reminder ("cancel", "delete", "remove", "scratch that", "nevermind about the reminder").
    - Use reminder_query when the user asks what reminders they have, or when you need to look up an existing reminder before modifying it.
    - Identify which reminder to update/cancel via search_text — short keywords from the original reminder (e.g. "invite Kale", "dentist"). If the user just refers to "the reminder" without specifics and only one is plausible, pass a broad search_text (e.g. "reminder") and the system will disambiguate.
11. 12am / 12pm clarification: if the user says "12am" or "12pm" when scheduling or rescheduling a reminder or calendar event, call ask_clarification first: "Just to confirm — do you mean midnight (00:00, start of day) or noon (12:00, middle of day)?" Do not assume — these are commonly confused.
12. For timezone changes: when the user says where they are or that they have moved ("I'm in Bali", "I'm now in Tokyo", "moved back to Rome"), call set_timezone with the matching IANA zone (e.g. Bali -> Asia/Makassar, NYC -> America/New_York, Rome -> Europe/Rome). If the location is too ambiguous to map to one zone (e.g. just "the US"), use ask_clarification instead. Do not call set_timezone for transient phrases like "in a meeting" or "at the gym".
13. OVERRIDE RULE — daily agenda settings: if the user's message contains 'agenda settings', 'agenda setting', 'daily agenda settings', 'change agenda time', 'customize daily agenda', or 'agenda options', you MUST call daily_agenda_settings with no arguments. This takes priority over memory_store, calendar_query, and every other tool. Do NOT interpret these phrases as facts to remember or as calendar queries. They are commands to open the settings menu for the automated morning WhatsApp summary."""

