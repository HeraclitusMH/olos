from app.services.planner import _extract_tool_calls


def test_extract_tool_calls_from_openai_response_dict() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "reply",
                                "arguments": '{"message":"Hello"}',
                            }
                        }
                    ]
                }
            }
        ]
    }

    assert _extract_tool_calls(response) == [
        {"name": "reply", "arguments": {"message": "Hello"}}
    ]
