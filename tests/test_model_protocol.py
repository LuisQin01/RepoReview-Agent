"""Deterministic construction tests for the internal ReAct model protocol."""

import pytest

from src.model_protocol import ModelProtocolError, ModelResponse, ToolCall


def test_tool_call_parses_json_arguments_and_preserves_call_id():
    call = ToolCall.from_raw_arguments(
        call_id="call-17",
        name="get_changed_hunks",
        arguments='{"path": "src/app.py"}',
    )

    assert call.call_id == "call-17"
    assert call.name == "get_changed_hunks"
    assert call.arguments == {"path": "src/app.py"}


@pytest.mark.parametrize("arguments", ["{bad json", "[]", "null"])
def test_tool_call_rejects_bad_or_non_object_arguments(arguments):
    with pytest.raises(ModelProtocolError):
        ToolCall.from_raw_arguments(call_id="call-1", name="read_file_context", arguments=arguments)


def test_model_response_supports_text_single_multiple_and_finish_calls():
    response = ModelResponse.from_dict(
        {
            "text": "I need two checks.",
            "tool_calls": [
                {"call_id": "call-1", "name": "unknown_tool", "arguments": {}},
                {
                    "call_id": "call-2",
                    "name": "finish_review",
                    "arguments": {"findings": []},
                },
            ],
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 12},
        }
    )

    assert response.text == "I need two checks."
    assert [call.call_id for call in response.tool_calls] == ["call-1", "call-2"]
    assert response.tool_calls[1].name == "finish_review"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"input_tokens": 12}


def test_model_response_supports_one_tool_call():
    response = ModelResponse.from_dict(
        {
            "tool_calls": [
                {"call_id": "call-only", "name": "get_changed_hunks", "arguments": {"path": "a.py"}}
            ],
            "finish_reason": "tool_calls",
        }
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].call_id == "call-only"


def test_model_response_supports_plain_text_stop():
    response = ModelResponse.from_json('{"text": "done", "finish_reason": "stop"}')

    assert response.text == "done"
    assert response.tool_calls == ()
    assert response.finish_reason == "stop"


def test_model_response_rejects_invalid_tool_call_construction():
    with pytest.raises(ModelProtocolError, match="tool_call_missing_call_id"):
        ModelResponse.from_dict({"tool_calls": [{"name": "tool", "arguments": {}}]})
