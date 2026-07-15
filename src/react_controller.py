"""The minimal provider -> tool -> history -> finish review loop.

This controller is deliberately an explicit opt-in.  It does not change the
legacy text-review path or add provider retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from time import perf_counter
from typing import Mapping, Protocol, runtime_checkable

from .agent_state import ReviewState
from .file_context import _is_sensitive_file_path
from .model_protocol import JSONValue, ModelResponse
from .review_tools import (
    FinishResult,
    FinishReview,
    ToolDispatcher,
    _normalize_file_context_path,
)
from .trace import redact_sensitive_structure


class ReActControllerError(ValueError):
    """A stable controller-boundary failure with a public status code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReActBudget:
    """Hard limits for one explicit ReAct review lifecycle.

    Tool-result limits are UTF-8 JSON bytes at the controller boundary: this
    is the only common unit after different tools have produced their own
    domain-specific ``ToolResult.result_size`` values.
    """

    max_steps: int = 8
    max_llm_calls: int = 8
    max_total_tokens: int = 16_000
    max_tool_result_bytes: int = 8_000
    max_total_tool_result_bytes: int = 32_000

    _MIN_TOOL_RESULT_BYTES = 256

    def __post_init__(self) -> None:
        for field_name, value in (
            ("max_steps", self.max_steps),
            ("max_llm_calls", self.max_llm_calls),
            ("max_total_tokens", self.max_total_tokens),
            ("max_tool_result_bytes", self.max_tool_result_bytes),
            ("max_total_tool_result_bytes", self.max_total_tool_result_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name}_must_be_a_positive_integer")
        if self.max_tool_result_bytes < self._MIN_TOOL_RESULT_BYTES:
            raise ValueError("max_tool_result_bytes_must_allow_a_truncation_marker")


@dataclass(frozen=True)
class _GuardedToolResult:
    """One shared, JSON-safe projection for history and structured trace."""

    history_result: dict[str, JSONValue]
    trace_summary: dict[str, JSONValue]
    result_bytes: int
    truncated: bool


@runtime_checkable
class ModelProvider(Protocol):
    """Provider-independent boundary used by the minimal ReAct controller."""

    def complete(self, request: Mapping[str, JSONValue]) -> ModelResponse:
        """Return one normalized response for one JSON-safe request."""


class ReActController:
    """Drive one review until a validated ``finish_review`` call terminates it.

    Requests contain a JSON-serializable ``history`` list.  Every ordinary tool
    result is associated with the originating ``call_id`` before the next model
    request, so a provider cannot rely on list position to interpret results.
    """

    def __init__(
        self,
        provider: ModelProvider,
        dispatcher: ToolDispatcher,
        finish_review: FinishReview,
        *,
        budget: ReActBudget | None = None,
        state: ReviewState | None = None,
    ) -> None:
        if not isinstance(provider, ModelProvider):
            raise TypeError("provider_must_implement_model_provider_protocol")
        self._provider = provider
        self._dispatcher = dispatcher
        self._finish_review = finish_review
        self._budget = budget or ReActBudget()
        self._state = state
        self._terminated = False
        self._counters: dict[str, int] = {}
        self._trace_sequence = 0

    def run(self, initial_request: Mapping[str, JSONValue]) -> FinishResult:
        """Run until finish or a recorded budget degradation terminates it.

        A controller instance owns one review lifecycle.  Reusing it after a
        terminal finish raises ``already_finished`` before request validation,
        provider calls, or tool dispatch.

        ``initial_request`` must be a JSON object whose optional ``history`` is
        a JSON list.  Provider errors propagate unchanged; this loop owns no
        retry policy.  A response without tool calls cannot prove a
        completed review and therefore raises ``unavailable`` instead of
        returning an invented empty result.
        """
        if self._finish_review.is_finished:
            # Termination is irreversible: no later invocation may re-enter the loop.
            raise ReActControllerError("already_finished")
        if self._terminated:
            raise ReActControllerError("already_terminated")

        request, history = self._initial_request(initial_request)
        self._reset_state()

        while True:
            termination = self._pre_call_termination()
            if termination is not None:
                return termination

            self._increment("react_steps")
            self._increment("react_llm_calls")
            response = self._provider.complete(request)
            token_count = self._response_token_count(response)
            if token_count is None:
                return self._terminate("unavailable")
            self._increment("react_total_tokens", token_count)
            if not response.tool_calls:
                raise ReActControllerError("unavailable")

            history.append(self._assistant_event(response))
            for call in response.tool_calls:
                if call.name == self._finish_review.name:
                    result = self._finish_review.finish(call.arguments)
                    if result.finished:
                        # Finish is terminal: later calls in this response must never run.
                        self._record_termination(
                            "finish",
                            success=True,
                            tool_name=call.name,
                            arguments=call.arguments,
                            result_summary={
                                "status": result.status,
                                "finding_count": result.finding_count,
                                "received_count": result.received_count,
                                "rejected_count": result.rejected_count,
                                "truncated": result.truncated,
                            },
                            model_usage=response.usage,
                        )
                        return result
                    history.append(self._finish_event(call.call_id, result.status))
                    continue

                started_at = perf_counter()
                tool_result = self._dispatcher.dispatch(call.name, call.arguments)
                guarded_result = self._guard_tool_result(
                    call.call_id, call.name, tool_result.to_model_dict()
                )
                if guarded_result.truncated:
                    self._increment("react_tool_results_truncated")
                self._record_tool_result(
                    call_id=call.call_id,
                    name=call.name,
                    arguments=call.arguments,
                    guarded_result=guarded_result,
                    duration_ms=self._duration_ms(started_at),
                    model_usage=response.usage,
                )
                if self._tool_bytes() + guarded_result.result_bytes > self._budget.max_total_tool_result_bytes:
                    # Do not let a same-turn second tool bypass the cumulative history cap.
                    return self._terminate("max_tool_result_bytes_exhausted")
                self._increment("react_tool_result_bytes", guarded_result.result_bytes)
                history.append(
                    self._tool_event(call.call_id, call.name, guarded_result.history_result)
                )

            request["history"] = self._json_copy(history)

    def _pre_call_termination(self) -> FinishResult | None:
        """Stop before another provider request once a finite budget is spent."""
        if self._value("react_steps") >= self._budget.max_steps:
            return self._terminate("max_steps_exhausted")
        if self._value("react_llm_calls") >= self._budget.max_llm_calls:
            return self._terminate("max_llm_calls_exhausted")
        if self._value("react_total_tokens") >= self._budget.max_total_tokens:
            return self._terminate("max_tokens_exhausted")
        if self._tool_bytes() >= self._budget.max_total_tool_result_bytes:
            return self._terminate("max_tool_result_bytes_exhausted")
        return None

    def _terminate(self, reason: str) -> FinishResult:
        """Return a non-finish result so exhaustion can never look successful."""
        self._terminated = True
        if self._state is not None:
            self._state.react_termination_reason = reason
            self._state.react_degraded = True
        self._record_termination(
            reason,
            success=False,
            result_summary={
                "steps": self._value("react_steps"),
                "llm_calls": self._value("react_llm_calls"),
                "total_tokens": self._value("react_total_tokens"),
                "tool_result_bytes": self._value("react_tool_result_bytes"),
                "tool_results_truncated": self._value("react_tool_results_truncated"),
            },
        )
        return FinishResult(
            finished=False,
            status=reason,
            findings=(),
            finding_count=0,
            finding_limit=0,
            received_count=0,
            rejected_count=0,
            truncated=True,
        )

    def _reset_state(self) -> None:
        self._counters = {
            "react_steps": 0,
            "react_llm_calls": 0,
            "react_total_tokens": 0,
            "react_tool_result_bytes": 0,
            "react_tool_results_truncated": 0,
        }
        self._trace_sequence = 0
        if self._state is not None:
            self._state.react_steps = 0
            self._state.react_llm_calls = 0
            self._state.react_total_tokens = 0
            self._state.react_tool_result_bytes = 0
            self._state.react_tool_results_truncated = 0
            self._state.react_termination_reason = ""
            self._state.react_degraded = False

    def _value(self, name: str) -> int:
        return self._counters.get(name, 0)

    def _increment(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._value(name) + amount
        if self._state is not None:
            setattr(self._state, name, self._counters[name])

    def _tool_bytes(self) -> int:
        return self._value("react_tool_result_bytes")

    def _tool_event(
        self,
        call_id: str,
        name: str,
        result: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        """Attach the one already-guarded result to its model-history event."""
        return {
            "role": "tool",
            "call_id": call_id,
            "name": name,
            "result": result,
        }

    def _guard_tool_result(
        self,
        call_id: str,
        name: str,
        result: dict[str, JSONValue],
    ) -> _GuardedToolResult:
        """Apply the single M3-derived boundary before history or trace sees data."""
        safe_result = self._json_copy(redact_sensitive_structure(result))
        if not isinstance(safe_result, dict):  # ToolResult already promises an object.
            raise ReActControllerError("internal_error")

        try:
            safe_result = self._sanitize_result_paths(safe_result)
        except ValueError:
            # A rejected path must not survive as a diagnostic in either sink.
            safe_result = self._forbidden_result()
        if safe_result.get("success") is False:
            # Tool failures are status-bearing, never a carrier for host diagnostics.
            safe_result = self._failure_result(safe_result)
        else:
            safe_result = self._success_result(safe_result)

        result_bytes = self._json_size(safe_result)
        was_truncated = bool(safe_result["truncated"])
        if result_bytes > self._budget.max_tool_result_bytes:
            # Dropping data instead of slicing preserves JSON validity and makes the
            # missing portion explicit, so incomplete source cannot look conclusive.
            safe_result = self._truncated_result(safe_result, result_bytes)
            result_bytes = self._json_size(safe_result)
            was_truncated = True

        return _GuardedToolResult(
            history_result=safe_result,
            trace_summary={
                "call_id": call_id,
                "tool_name": name,
                "success": safe_result["success"],
                "error_code": safe_result["error_code"],
                "truncated": safe_result["truncated"],
                "result_size": safe_result["result_size"],
                "result_limit": safe_result["result_limit"],
                "summary": safe_result["model_summary"],
            },
            result_bytes=result_bytes,
            truncated=was_truncated,
        )

    @staticmethod
    def _forbidden_result() -> dict[str, JSONValue]:
        """Return the non-leaking result used for sensitive or invalid paths."""
        return {
            "success": False,
            "model_summary": "The requested tool result is not available.",
            "error_code": "forbidden",
            "truncated": False,
            "result_size": 0,
            "result_limit": 0,
            "data": None,
            "usage": {},
        }

    @staticmethod
    def _failure_result(result: dict[str, JSONValue]) -> dict[str, JSONValue]:
        """Keep a stable failure code while discarding untrusted diagnostics."""
        error_code = result.get("error_code")
        if not isinstance(error_code, str):
            error_code = "internal_error"
        return {
            "success": False,
            "model_summary": "The tool did not complete successfully.",
            "error_code": error_code,
            "truncated": bool(result.get("truncated")),
            "result_size": ReActController._non_negative_int(result.get("result_size")) or 0,
            "result_limit": ReActController._non_negative_int(result.get("result_limit")) or 0,
            "data": None,
            "usage": ReActController._numeric_usage(result.get("usage")),
        }

    @staticmethod
    def _success_result(result: dict[str, JSONValue]) -> dict[str, JSONValue]:
        """Keep only structured data; summaries never echo untrusted source text."""
        return {
            "success": True,
            "model_summary": (
                "Tool result is available, but the returned information is incomplete."
                if bool(result.get("truncated"))
                else "Tool result is available."
            ),
            "error_code": None,
            "truncated": bool(result.get("truncated")),
            "result_size": ReActController._non_negative_int(result.get("result_size")) or 0,
            "result_limit": ReActController._non_negative_int(result.get("result_limit")) or 0,
            "data": result.get("data"),
            "usage": ReActController._numeric_usage(result.get("usage")),
        }

    def _truncated_result(
        self,
        result: dict[str, JSONValue],
        actual_size: int,
    ) -> dict[str, JSONValue]:
        return {
            "success": result["success"],
            "model_summary": "Tool result was truncated by the ReAct result budget.",
            "error_code": result["error_code"],
            "truncated": True,
            "result_size": actual_size,
            "result_limit": self._budget.max_tool_result_bytes,
            "data": None,
            "usage": {},
        }

    @staticmethod
    def _numeric_usage(value: JSONValue | object) -> dict[str, JSONValue]:
        """Usage is audit metadata, so retain only non-negative numeric counters."""
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if isinstance(key, str)
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
            and item >= 0
        }

    @staticmethod
    def _sanitize_result_paths(value: JSONValue) -> JSONValue:
        """Normalize path fields or reject them before they reach either sink."""
        if isinstance(value, dict):
            sanitized: dict[str, JSONValue] = {}
            for key, item in value.items():
                if key in {"path", "old_path", "file_path"} and item is not None:
                    if not isinstance(item, str):
                        raise ValueError("tool_result_path_must_be_a_string")
                    normalized_path = _normalize_file_context_path(item)
                    if _is_sensitive_file_path(normalized_path):
                        raise ValueError("tool_result_path_is_sensitive")
                    sanitized[key] = normalized_path
                else:
                    sanitized[key] = ReActController._sanitize_result_paths(item)
            return sanitized
        elif isinstance(value, list):
            return [ReActController._sanitize_result_paths(item) for item in value]
        return value

    def _record_tool_result(
        self,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, JSONValue],
        guarded_result: _GuardedToolResult,
        duration_ms: int,
        model_usage: dict[str, JSONValue],
    ) -> None:
        self._record_trace(
            "react_tool_result",
            {
                "call_id": call_id,
                "tool_name": name,
                "arguments": self._arguments_summary(arguments),
                "result": guarded_result.trace_summary,
                "success": guarded_result.history_result["success"],
                "duration_ms": duration_ms,
                "usage": self._numeric_usage(guarded_result.history_result["usage"]),
                "model_usage": self._numeric_usage(model_usage),
            },
        )

    def _record_termination(
        self,
        reason: str,
        *,
        success: bool,
        tool_name: str | None = None,
        arguments: dict[str, JSONValue] | None = None,
        result_summary: dict[str, JSONValue] | None = None,
        model_usage: dict[str, JSONValue] | None = None,
    ) -> None:
        self._record_trace(
            "react_termination",
            {
                "reason": reason,
                "success": success,
                "tool_name": tool_name,
                "arguments": self._arguments_summary(arguments or {}),
                "result": result_summary or {},
                "usage": self._numeric_usage(model_usage or {}),
            },
        )

    def _record_trace(self, step: str, detail: dict[str, JSONValue]) -> None:
        if self._state is None:
            return
        self._trace_sequence += 1
        self._state.trace_steps.append(
            {
                "step": step,
                "step_index": self._trace_sequence,
                "duration_ms": int(detail.pop("duration_ms", 0)),
                "detail": detail,
            }
        )

    @staticmethod
    def _arguments_summary(arguments: dict[str, JSONValue]) -> dict[str, JSONValue]:
        """Record argument shape only: raw model arguments can contain credentials."""
        summary: dict[str, JSONValue] = {"argument_keys": sorted(arguments)}
        path = arguments.get("path")
        if isinstance(path, str):
            try:
                normalized_path = _normalize_file_context_path(path)
            except ValueError:
                summary["path"] = "[REDACTED]"
            else:
                summary["path"] = (
                    "[REDACTED]" if _is_sensitive_file_path(normalized_path) else normalized_path
                )
        return summary

    @staticmethod
    def _duration_ms(started_at: float) -> int:
        return max(0, int((perf_counter() - started_at) * 1000))

    @staticmethod
    def _json_size(value: JSONValue) -> int:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _response_token_count(response: ModelResponse) -> int | None:
        """Read a normalized token total without trusting malformed provider usage."""
        usage = response.usage
        total = usage.get("total_tokens")
        if total is not None:
            return ReActController._non_negative_int(total)

        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
        input_count = ReActController._non_negative_int(input_tokens)
        output_count = ReActController._non_negative_int(output_tokens)
        if input_count is None or output_count is None:
            return None
        return input_count + output_count

    @staticmethod
    def _non_negative_int(value: JSONValue) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _initial_request(
        initial_request: Mapping[str, JSONValue],
    ) -> tuple[dict[str, JSONValue], list[JSONValue]]:
        """Copy an untrusted boundary request and normalize its optional history."""
        if not isinstance(initial_request, Mapping):
            raise ReActControllerError("invalid_arguments")
        request = ReActController._json_copy(dict(initial_request))
        if not isinstance(request, dict):  # Defensive: JSON copies of mappings stay objects.
            raise ReActControllerError("invalid_arguments")
        history = request.get("history", [])
        if not isinstance(history, list):
            raise ReActControllerError("invalid_arguments")
        request["history"] = history
        return request, history

    @staticmethod
    def _json_copy(value: JSONValue) -> JSONValue:
        """Reject non-JSON boundary values before they reach a provider."""
        try:
            return json.loads(json.dumps(value, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ReActControllerError("invalid_arguments") from exc

    @staticmethod
    def _assistant_event(response: ModelResponse) -> dict[str, JSONValue]:
        """Preserve the provider response that produced the following tool results."""
        return {
            "role": "assistant",
            "content": response.text,
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": ReActController._arguments_summary(call.arguments),
                }
                for call in response.tool_calls
            ],
        }

    @staticmethod
    def _finish_event(call_id: str, status: str) -> dict[str, JSONValue]:
        """Tell the provider why an invalid finish request did not terminate."""
        return {
            "role": "tool",
            "call_id": call_id,
            "name": "finish_review",
            "result": {"success": False, "error_code": status},
        }
