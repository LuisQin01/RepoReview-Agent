"""Internal model-response types for the future ReAct controller.

These types deliberately contain no provider SDK objects.  Provider adapters and
the scripted mock can therefore exchange the same JSON-serializable values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Mapping, TypeAlias


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


class ModelProtocolError(ValueError):
    """Raised when an untrusted provider payload cannot form an internal response."""


def _json_object(value: object, *, field_name: str) -> dict[str, JSONValue]:
    """Return a JSON-object copy or reject non-object and non-JSON values."""
    if not isinstance(value, Mapping):
        raise ModelProtocolError(f"{field_name}_must_be_an_object")
    try:
        copied = json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ModelProtocolError(f"{field_name}_must_be_json_serializable") from exc
    if not isinstance(copied, dict):  # Defensive: JSON decoding must preserve an object.
        raise ModelProtocolError(f"{field_name}_must_be_an_object")
    return copied


@dataclass(frozen=True)
class ToolCall:
    """One provider-independent tool request with already-parsed arguments."""

    call_id: str
    name: str
    arguments: dict[str, JSONValue]

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ModelProtocolError("tool_call_id_must_be_a_non_empty_string")
        if not isinstance(self.name, str) or not self.name:
            raise ModelProtocolError("tool_call_name_must_be_a_non_empty_string")
        # Do not turn malformed arguments into {}; that would fabricate a valid no-arg call.
        object.__setattr__(self, "arguments", _json_object(self.arguments, field_name="tool_call_arguments"))

    @classmethod
    def from_raw_arguments(
        cls,
        *,
        call_id: str,
        name: str,
        arguments: str | Mapping[str, JSONValue],
    ) -> "ToolCall":
        """Build a call from provider JSON text or a parsed JSON object."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError("tool_call_arguments_invalid_json") from exc
        return cls(call_id=call_id, name=name, arguments=_json_object(arguments, field_name="tool_call_arguments"))

    @classmethod
    def from_dict(cls, value: Mapping[str, JSONValue]) -> "ToolCall":
        """Parse the normalized provider representation used by scripted tests."""
        if not isinstance(value, Mapping):
            raise ModelProtocolError("tool_call_must_be_an_object")
        try:
            call_id = value["call_id"]
            name = value["name"]
            arguments = value["arguments"]
        except KeyError as exc:
            raise ModelProtocolError(f"tool_call_missing_{exc.args[0]}") from exc
        if not isinstance(arguments, (str, Mapping)):
            raise ModelProtocolError("tool_call_arguments_must_be_an_object")
        return cls.from_raw_arguments(call_id=call_id, name=name, arguments=arguments)


@dataclass(frozen=True)
class ModelResponse:
    """A provider-independent response retaining text and tool calls together."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.text is not None and not isinstance(self.text, str):
            raise ModelProtocolError("model_response_text_must_be_a_string_or_null")
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise ModelProtocolError("model_response_finish_reason_must_be_a_string_or_null")
        calls = tuple(self.tool_calls)
        if not all(isinstance(call, ToolCall) for call in calls):
            raise ModelProtocolError("model_response_tool_calls_must_contain_tool_calls")
        object.__setattr__(self, "tool_calls", calls)
        object.__setattr__(self, "usage", _json_object(self.usage, field_name="model_response_usage"))

    @classmethod
    def from_dict(cls, value: Mapping[str, JSONValue]) -> "ModelResponse":
        """Parse a JSON-object response while retaining mixed text and tool calls."""
        if not isinstance(value, Mapping):
            raise ModelProtocolError("model_response_must_be_an_object")
        text = value.get("text")
        finish_reason = value.get("finish_reason")
        usage = value.get("usage", {})
        raw_calls = value.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ModelProtocolError("model_response_tool_calls_must_be_a_list")
        if not all(isinstance(call, Mapping) for call in raw_calls):
            raise ModelProtocolError("model_response_tool_calls_must_contain_objects")
        return cls(
            text=text,
            tool_calls=tuple(ToolCall.from_dict(call) for call in raw_calls),
            finish_reason=finish_reason,
            usage=_json_object(usage, field_name="model_response_usage"),
        )

    @classmethod
    def from_json(cls, response_text: str) -> "ModelResponse":
        """Parse one JSON response object from an offline scripted provider."""
        if not isinstance(response_text, str):
            raise ModelProtocolError("model_response_json_must_be_a_string")
        try:
            value = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError("model_response_invalid_json") from exc
        if not isinstance(value, Mapping):
            raise ModelProtocolError("model_response_must_be_an_object")
        return cls.from_dict(value)
