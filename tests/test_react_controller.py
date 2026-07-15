"""Deterministic end-to-end evidence for the minimal ReAct happy path."""

import json

import pytest

from src import react_controller
from src.llm_client import ScriptedMockProvider
from src.agent_state import ReviewState
from src.react_controller import ReActBudget, ReActController, ReActControllerError
from src.review_tools import FinishReview, ToolDispatcher, ToolResult
from src.schemas import ChangedFile, ContextBudget, DiffHunk


class SpyTool:
    """A read-only tool fake that exposes exactly how many times it was run."""

    name = "inspect_change"
    description = "Return one deterministic inspection result."
    parameters_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, events: list[str], *, payload_chars: int = 0) -> None:
        self.calls: list[dict[str, object]] = []
        self._events = events
        self._payload_chars = payload_chars

    def run(self, arguments):
        self._events.append("tool")
        self.calls.append(arguments)
        return ToolResult(
            success=True,
            model_summary="Inspected one changed file.",
            error_code=None,
            truncated=False,
            result_size=1,
            result_limit=1,
            data={
                "path": arguments["path"],
                "state": "needs-finish",
                **({"payload": "x" * self._payload_chars} if self._payload_chars else {}),
            },
            usage={"files": 1},
        )


class StaticResultTool:
    """Return a caller-selected result so guardrails see untrusted tool data."""

    name = "inspect_change"
    description = "Return one scripted tool result."
    parameters_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "token": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, result: ToolResult) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    def run(self, arguments):
        self.calls.append(arguments)
        return self._result


class RecordingScriptedProvider(ScriptedMockProvider):
    """Record provider turns in the same sequence observed by the tool fake."""

    def __init__(self, script, events: list[str]) -> None:
        super().__init__(script)
        self._events = events

    def complete(self, request):
        self._events.append("provider")
        return super().complete(request)


def _changed_files():
    return [
        ChangedFile(
            path="src/app.py",
            added_lines=[],
            deleted_lines=[],
            patch="@@ -10 +10 @@\n+value = True",
            hunks=[DiffHunk(start_line=10, end_line=10)],
        )
    ]


def _review_state() -> ReviewState:
    return ReviewState(
        diff_path="review.diff",
        repo_root=".",
        output_format="json",
        use_llm=True,
        context_budget=ContextBudget(),
    )


def _finish_finding():
    return {
        "file": "src/app.py",
        "line": 10,
        "severity": "high",
        "issue": "Finish-owned finding marker",
        "reason": "The scripted terminal call supplied this finding.",
        "suggested_fix": "Handle the checked result.",
        "confidence": 0.91,
        "evidence": "src/app.py:10",
    }


def test_scripted_provider_drives_tool_history_and_finish_without_extra_calls():
    """Prove the M7-11 loop, including immediate stop after terminal finish."""
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "call_id": "tool-call-1",
                        "name": "inspect_change",
                        "arguments": {"path": "src/app.py"},
                    },
                    {
                        "call_id": "tool-call-2",
                        "name": "inspect_change",
                        "arguments": {"path": "src/worker.py"},
                    },
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "finish-call-2",
                        "name": "finish_review",
                        "arguments": {"findings": [_finish_finding()]},
                    },
                    {
                        "call_id": "must-not-run",
                        "name": "inspect_change",
                        "arguments": {"path": "src/app.py"},
                    },
                ]
            },
        ],
        events,
    )
    tool = SpyTool(events)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    controller = ReActController(provider, dispatcher, FinishReview(_changed_files()))

    result = controller.run({"review_id": "review-1", "history": []})

    assert result.finished is True
    assert [(issue.message, issue.line_no) for issue in result.findings] == [
        ("Finish-owned finding marker", 10)
    ]
    assert tool.calls == [{"path": "src/app.py"}, {"path": "src/worker.py"}]
    assert events == ["provider", "tool", "tool", "provider"]
    assert provider.consumed_count == 2
    assert len(provider.requests) == 2
    assert provider.requests[1]["history"] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "call_id": "tool-call-1",
                    "name": "inspect_change",
                    "arguments": {"argument_keys": ["path"], "path": "src/app.py"},
                },
                {
                    "call_id": "tool-call-2",
                    "name": "inspect_change",
                    "arguments": {"argument_keys": ["path"], "path": "src/worker.py"},
                },
            ],
        },
        {
            "role": "tool",
            "call_id": "tool-call-1",
            "name": "inspect_change",
            "result": {
                "success": True,
                "model_summary": "Tool result is available.",
                "error_code": None,
                "truncated": False,
                "result_size": 1,
                "result_limit": 1,
                "data": {"path": "src/app.py", "state": "needs-finish"},
                "usage": {"files": 1},
            },
        },
        {
            "role": "tool",
            "call_id": "tool-call-2",
            "name": "inspect_change",
            "result": {
                "success": True,
                "model_summary": "Tool result is available.",
                "error_code": None,
                "truncated": False,
                "result_size": 1,
                "result_limit": 1,
                "data": {"path": "src/worker.py", "state": "needs-finish"},
                "usage": {"files": 1},
            },
        },
    ]


def test_controller_rejects_non_terminal_response_without_inventing_a_result():
    provider = ScriptedMockProvider([{"text": "no tool call"}])
    controller = ReActController(provider, ToolDispatcher(), FinishReview(_changed_files()))

    with pytest.raises(ReActControllerError, match="unavailable"):
        controller.run({"history": []})

    assert provider.consumed_count == 1
    assert len(provider.requests) == 1


def test_controller_rejects_reuse_after_finish_without_provider_or_tool_calls():
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "call_id": "finish-call-1",
                        "name": "finish_review",
                        "arguments": {"findings": []},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "must-not-run-after-finish",
                        "name": "inspect_change",
                        "arguments": {"path": "src/app.py"},
                    },
                    {
                        "call_id": "repeated-finish",
                        "name": "finish_review",
                        "arguments": {"findings": []},
                    },
                ]
            },
        ],
        events,
    )
    tool = SpyTool(events)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    controller = ReActController(provider, dispatcher, FinishReview(_changed_files()))

    result = controller.run({"review_id": "review-1", "history": []})

    assert result.finished is True
    assert result.findings == ()
    with pytest.raises(ReActControllerError, match="already_finished"):
        controller.run({"review_id": "review-1", "history": []})

    assert provider.consumed_count == 1
    assert len(provider.requests) == 1
    assert tool.calls == []
    assert events == ["provider"]


def test_max_steps_stops_an_infinite_tool_script_and_records_degraded_trace():
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {"tool_calls": [{"call_id": f"tool-{number}", "name": "inspect_change", "arguments": {"path": "src/app.py"}}]}
            for number in range(3)
        ],
        events,
    )
    tool = SpyTool(events)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    state = _review_state()
    controller = ReActController(
        provider,
        dispatcher,
        FinishReview(_changed_files()),
        budget=ReActBudget(max_steps=2, max_llm_calls=8, max_total_tokens=100, max_tool_result_bytes=512, max_total_tool_result_bytes=2_000),
        state=state,
    )

    result = controller.run({"history": []})

    assert result.finished is False
    assert result.status == "max_steps_exhausted"
    assert provider.consumed_count == 2
    assert len(provider.requests) == 2
    assert len(tool.calls) == 2
    assert state.react_termination_reason == "max_steps_exhausted"
    assert state.react_degraded is True
    assert state.trace_steps[-1]["detail"]["reason"] == "max_steps_exhausted"


def test_llm_call_budget_blocks_the_next_provider_request():
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {"tool_calls": [{"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py"}}]},
            {"tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}]},
        ],
        events,
    )
    tool = SpyTool(events)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    controller = ReActController(
        provider,
        dispatcher,
        FinishReview(_changed_files()),
        budget=ReActBudget(max_steps=8, max_llm_calls=1, max_total_tokens=100, max_tool_result_bytes=512, max_total_tool_result_bytes=2_000),
    )

    result = controller.run({"history": []})

    assert result.finished is False
    assert result.status == "max_llm_calls_exhausted"
    assert provider.consumed_count == 1
    assert len(provider.requests) == 1
    assert len(tool.calls) == 1


def test_token_budget_blocks_the_next_provider_request_after_accounting_usage():
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {
                "usage": {"total_tokens": 3},
                "tool_calls": [{"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py"}}],
            },
            {"tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}]},
        ],
        events,
    )
    tool = SpyTool(events)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    state = _review_state()
    controller = ReActController(
        provider,
        dispatcher,
        FinishReview(_changed_files()),
        budget=ReActBudget(max_steps=8, max_llm_calls=8, max_total_tokens=3, max_tool_result_bytes=512, max_total_tool_result_bytes=2_000),
        state=state,
    )

    result = controller.run({"history": []})

    assert result.finished is False
    assert result.status == "max_tokens_exhausted"
    assert provider.consumed_count == 1
    assert len(tool.calls) == 1
    assert state.react_total_tokens == 3


def test_oversized_tool_result_is_truncated_without_cutting_off_a_normal_finish():
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {"tool_calls": [{"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py"}}]},
            {"tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}]},
        ],
        events,
    )
    tool = SpyTool(events, payload_chars=2_000)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    state = _review_state()
    controller = ReActController(
        provider,
        dispatcher,
        FinishReview(_changed_files()),
        budget=ReActBudget(max_steps=8, max_llm_calls=8, max_total_tokens=100, max_tool_result_bytes=512, max_total_tool_result_bytes=2_000),
        state=state,
    )

    result = controller.run({"history": []})

    assert result.finished is True
    assert provider.consumed_count == 2
    assert state.react_tool_results_truncated == 1
    bounded_result = provider.requests[1]["history"][1]["result"]
    assert bounded_result["truncated"] is True
    assert bounded_result["data"] is None
    assert len(json.dumps(bounded_result, separators=(",", ":")).encode("utf-8")) <= 512


def test_total_tool_result_budget_stops_later_tool_and_provider_calls():
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {
                "tool_calls": [
                    {"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py"}},
                    {"call_id": "tool-2", "name": "inspect_change", "arguments": {"path": "src/other.py"}},
                ]
            },
            {"tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}]},
        ],
        events,
    )
    tool = SpyTool(events)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    controller = ReActController(
        provider,
        dispatcher,
        FinishReview(_changed_files()),
        budget=ReActBudget(max_steps=8, max_llm_calls=8, max_total_tokens=100, max_tool_result_bytes=512, max_total_tool_result_bytes=1),
    )

    result = controller.run({"history": []})

    assert result.finished is False
    assert result.status == "max_tool_result_bytes_exhausted"
    assert provider.consumed_count == 1
    assert len(provider.requests) == 1
    assert len(tool.calls) == 1


def test_malformed_token_usage_terminates_without_dispatching_a_tool():
    events: list[str] = []
    provider = RecordingScriptedProvider(
        [
            {
                "usage": {"total_tokens": "three"},
                "tool_calls": [{"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py"}}],
            }
        ],
        events,
    )
    tool = SpyTool(events)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    controller = ReActController(provider, dispatcher, FinishReview(_changed_files()))

    result = controller.run({"history": []})

    assert result.finished is False
    assert result.status == "unavailable"
    assert provider.consumed_count == 1
    assert tool.calls == []


def test_shared_guardrail_rejects_sensitive_paths_and_redacts_history_and_trace():
    """One projection protects both sinks, including the prior assistant call."""
    secret = "sk-proj-" + "A" * 20
    host_path = r"C:\\Users\\reviewer\\.env"
    provider = ScriptedMockProvider(
        [
            {
                "tool_calls": [
                    {"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": ".env"}}
                ]
            },
            {"tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}]},
        ]
    )
    tool = StaticResultTool(
        ToolResult(
            success=True,
            model_summary=f"Read {host_path} api_key={secret}",
            error_code=None,
            truncated=False,
            result_size=1,
            result_limit=1,
            data={"path": ".env", "content": f"API_KEY={secret}"},
            usage={"files": 1},
        )
    )
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    state = _review_state()

    result = ReActController(provider, dispatcher, FinishReview(_changed_files()), state=state).run({"history": []})

    assert result.finished is True
    recorded = json.dumps({"history": provider.requests[1]["history"], "trace": state.trace_steps})
    assert ".env" not in recorded
    assert host_path not in recorded
    assert secret not in recorded
    guarded = provider.requests[1]["history"][1]["result"]
    assert guarded["error_code"] == "forbidden"
    assert guarded["data"] is None
    assert state.trace_steps[0]["detail"]["result"]["error_code"] == "forbidden"


def test_guardrail_redacts_api_key_and_truncates_large_source_before_history_and_trace():
    """Redaction happens before the byte budget; trace contains a summary, never data."""
    secret = "sk-proj-" + "B" * 20
    provider = ScriptedMockProvider(
        [
            {"tool_calls": [{"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py"}}]},
            {"tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}]},
        ]
    )
    tool = StaticResultTool(
        ToolResult(
            success=True,
            model_summary="source payload",
            error_code=None,
            truncated=False,
            result_size=2_000,
            result_limit=2_000,
            data={"path": "src/app.py", "content": f"api_key={secret}\n" + "x" * 2_000},
            usage={"characters": 2_000},
        )
    )
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    state = _review_state()

    result = ReActController(
        provider,
        dispatcher,
        FinishReview(_changed_files()),
        budget=ReActBudget(max_tool_result_bytes=512),
        state=state,
    ).run({"history": []})

    assert result.finished is True
    bounded = provider.requests[1]["history"][1]["result"]
    assert bounded["truncated"] is True
    assert bounded["data"] is None
    assert bounded["result_size"] > bounded["result_limit"]
    trace_text = json.dumps(state.trace_steps)
    assert secret not in json.dumps(provider.requests[1]["history"])
    assert secret not in trace_text
    assert "x" * 100 not in trace_text
    assert state.trace_steps[0]["detail"]["result"]["truncated"] is True


def test_structured_trace_records_tool_failure_duration_usage_and_finish(monkeypatch):
    """Trace is JSON-safe audit data without raw arguments, source, or diagnostics."""
    provider = ScriptedMockProvider(
        [
            {
                "usage": {"total_tokens": 3},
                "tool_calls": [
                    {"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py", "token": "secret"}}
                ],
            },
            {
                "usage": {"total_tokens": 2},
                "tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}],
            },
        ]
    )
    tool = StaticResultTool(
        ToolResult(
            success=False,
            model_summary=r"failed at C:\\host\\repo\\app.py token=secret",
            error_code="unavailable",
            truncated=False,
            result_size=0,
            result_limit=0,
            data={"error": r"C:\\host\\repo\\app.py"},
            usage={"files": 1},
        )
    )
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    state = _review_state()
    ticks = iter([10.0, 10.125])
    monkeypatch.setattr(react_controller, "perf_counter", lambda: next(ticks))

    result = ReActController(provider, dispatcher, FinishReview(_changed_files()), state=state).run({"history": []})

    assert result.finished is True
    assert json.loads(json.dumps(state.trace_steps)) == state.trace_steps
    tool_step, termination = state.trace_steps
    assert tool_step["step"] == "react_tool_result"
    assert tool_step["step_index"] == 1
    assert tool_step["duration_ms"] == 125
    assert tool_step["detail"]["success"] is False
    assert tool_step["detail"]["usage"] == {"files": 1}
    assert tool_step["detail"]["model_usage"] == {"total_tokens": 3}
    assert tool_step["detail"]["arguments"] == {"argument_keys": ["path", "token"], "path": "src/app.py"}
    assert termination["step"] == "react_termination"
    assert termination["step_index"] == 2
    assert termination["detail"]["reason"] == "finish"
    assert termination["detail"]["usage"] == {"total_tokens": 2}
    assert r"C:\\host" not in json.dumps(state.trace_steps)
    assert "token=secret" not in json.dumps(state.trace_steps)


def test_trace_disabled_keeps_react_requests_results_and_call_counts_unchanged():
    """The trace opt-in changes observability only, never the controller decision path."""
    script = [
        {"tool_calls": [{"call_id": "tool-1", "name": "inspect_change", "arguments": {"path": "src/app.py"}}]},
        {"tool_calls": [{"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}]},
    ]
    def run_with_state(state):
        provider = ScriptedMockProvider(script)
        dispatcher = ToolDispatcher()
        tool = SpyTool([])
        dispatcher.register(tool)
        result = ReActController(provider, dispatcher, FinishReview(_changed_files()), state=state).run({"history": []})
        return result, provider, tool

    without_trace, provider_without_trace, tool_without_trace = run_with_state(None)
    disabled_state = _review_state()
    disabled_state.trace_enabled = False
    disabled, provider_disabled, tool_disabled = run_with_state(disabled_state)

    assert disabled == without_trace
    assert provider_disabled.requests == provider_without_trace.requests
    assert provider_disabled.consumed_count == provider_without_trace.consumed_count == 2
    assert tool_disabled.calls == tool_without_trace.calls == [{"path": "src/app.py"}]
