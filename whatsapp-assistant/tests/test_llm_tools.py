from app.services.llm_tools import SYSTEM_PROMPT_TEMPLATE, TOOLS


def test_defines_all_planner_tools() -> None:
    names = {tool["function"]["name"] for tool in TOOLS}

    assert names == {
        "calendar_create",
        "calendar_query",
        "calendar_update",
        "calendar_cancel",
        "memory_store",
        "memory_retrieve",
        "memory_update",
        "memory_forget",
        "reminder_create",
        "reminder_query",
        "reminder_update",
        "reminder_cancel",
        "set_timezone",
        "daily_agenda_settings",
        "ask_clarification",
        "reply",
    }


def test_every_tool_has_json_schema_descriptions() -> None:
    for tool in TOOLS:
        function = tool["function"]
        parameters = function["parameters"]

        assert tool["type"] == "function"
        assert function["description"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        for schema in parameters["properties"].values():
            assert schema["description"]


def test_system_prompt_requires_tool_calls_and_runtime_context() -> None:
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        current_datetime="2026-05-10T09:00:00+02:00",
        user_timezone="Europe/Madrid",
    )

    assert "ALWAYS use a tool call" in prompt
    assert "2026-05-10T09:00:00+02:00" in prompt
    assert "Europe/Madrid" in prompt
