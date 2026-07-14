from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Callable, Optional

from .agent_state import ReviewState
from .diff_parser import parse_diff
from .file_context import collect_file_contexts
from .git_provider import GitProvider, PullRequestRef, SummaryCommentResult
from .llm_client import LLMClientError, get_call_model
from .llm_reviewer import review_with_llm
from .reporter import (
    render_json_report,
    render_markdown_report,
    render_summary_comment,
)
from .reviewers import review_changed_files
from .schemas import ContextBudget
from .trace import (
    redact_sensitive_structure,
    sanitize_trace_text,
    save_trace,
)
from .validation import validate_issue_locations


@dataclass(frozen=True)
class ReviewRequest:
    """Structured input shared by CLI, API, and workflow adapters."""

    # Exactly one diff source must be provided.  CLI adapters use ``diff_path``;
    # HTTP adapters can pass untrusted request content through ``diff_text``
    # without creating a temporary file on the server.
    diff_path: Optional[str] = None
    repo_root: str = "."
    diff_text: Optional[str] = None
    output_format: str = "markdown"
    use_llm: bool = False
    context_budget: ContextBudget = field(default_factory=ContextBudget)
    llm_provider: str = "mock"
    mock_fixture: str = "normal"
    trace_enabled: bool = False
    trace_dir: str = "traces"
    publish_summary_comment: bool = False
    pull_request: Optional[PullRequestRef] = None


@dataclass
class ReviewResult:
    """Structured result of one review run, including all intermediate state."""

    state: ReviewState
    summary_comment: Optional[SummaryCommentResult] = None

    @property
    def output(self) -> str:
        return self.state.output

    @property
    def trace_steps(self) -> list[dict]:
        return self.state.trace_steps


def record_step(state, step, detail=None, started_at_perf=None):
    if started_at_perf is None:
        started_at_perf = perf_counter()
    duration_ms = int((perf_counter() - started_at_perf) * 1000)
    state.trace_steps.append(
        {
            "step": step,
            "duration_ms": duration_ms,
            "detail": redact_sensitive_structure(detail or {}),
        }
    )


def _retry_detail(call_model):
    retry_info = getattr(call_model, "last_retry_info", {})
    return {
        "attempts": retry_info.get("attempts", 0),
        "retries": retry_info.get("retries", 0),
        "retry_errors": [
            sanitize_trace_text(error)
            for error in retry_info.get("retry_errors", [])
        ],
        "exhausted": retry_info.get("exhausted", False),
    }


def validate_issues(issues, changed_files):
    if not isinstance(issues, list):
        raise ValueError("Issues should be a list")
    return validate_issue_locations(issues, changed_files)


class ReviewService:
    """Runs one deterministic review workflow without CLI-specific inputs."""

    def __init__(
        self,
        git_provider_factory: Optional[Callable[[], GitProvider]] = None,
    ):
        self._git_provider_factory = git_provider_factory

    def review(self, request: ReviewRequest) -> ReviewResult:
        if (request.diff_path is None) == (request.diff_text is None):
            raise ValueError("exactly_one_diff_source_required")
        if request.output_format not in {"json", "markdown"}:
            raise ValueError("unsupported_output_format")
        if request.publish_summary_comment and request.pull_request is None:
            raise ValueError("pull_request_required_for_summary_comment")
        if request.publish_summary_comment and self._git_provider_factory is None:
            raise ValueError("git_provider_required_for_summary_comment")

        state = ReviewState(
            diff_path=request.diff_path or "(inline diff)",
            repo_root=request.repo_root,
            output_format=request.output_format,
            use_llm=request.use_llm,
            context_budget=request.context_budget,
            llm_provider=request.llm_provider,
            trace_enabled=request.trace_enabled,
            trace_dir=request.trace_dir,
        )

        record_step(
            state,
            "receive_task",
            {
                "diff": state.diff_path,
                "repo": state.repo_root,
                "format": state.output_format,
                "llm": state.use_llm,
                "llm_provider": state.llm_provider,
            },
            started_at_perf=state.started_at_perf,
        )

        step_started_at_perf = perf_counter()
        state.diff_text = (
            request.diff_text
            if request.diff_text is not None
            else Path(state.diff_path).read_text(encoding="utf-8")
        )
        state.changed_files = parse_diff(state.diff_text)
        record_step(
            state,
            "parse_diff",
            {"changed_files": len(state.changed_files)},
            started_at_perf=step_started_at_perf,
        )

        step_started_at_perf = perf_counter()
        state.contexts = collect_file_contexts(
            repo_root=state.repo_root,
            changed_files=state.changed_files,
            context_budget=state.context_budget,
        )
        record_step(
            state,
            "collect_context",
            {
                "contexts": len(state.contexts),
                "selected_contexts": [
                    {
                        "path": context.path,
                        "source": context.source,
                        "selection_reason": context.selection_reason,
                        "exists": context.exists,
                        "truncated": context.truncated,
                        "chars_read": context.chars_read,
                        "error": context.error,
                    }
                    for context in state.contexts
                ],
            },
            started_at_perf=step_started_at_perf,
        )

        step_started_at_perf = perf_counter()
        state.rule_issues = review_changed_files(state.changed_files)
        state.issues = list(state.rule_issues)
        record_step(
            state,
            "run_static_checks",
            {"findings": len(state.issues)},
            started_at_perf=step_started_at_perf,
        )

        if state.use_llm:
            step_started_at_perf = perf_counter()
            call_model = None
            try:
                call_model = get_call_model(
                    state.llm_provider,
                    mock_fixture=request.mock_fixture,
                )
                state.llm_issues, validation = review_with_llm(
                    changed_files=state.changed_files,
                    contexts=state.contexts,
                    rule_issues=state.rule_issues,
                    call_model=call_model,
                    max_prompt_chars=state.context_budget.max_prompt_chars,
                )
                state.errors.extend(validation.errors)
                state.issues.extend(state.llm_issues)
                record_step(
                    state,
                    "run_llm_review",
                    {
                        "called": True,
                        "provider": state.llm_provider,
                        "findings": len(state.llm_issues),
                        "valid": validation.valid,
                        "repaired": validation.repaired,
                        "errors": validation.errors,
                        **_retry_detail(call_model),
                    },
                    started_at_perf=step_started_at_perf,
                )
            except LLMClientError as exc:
                state.errors.append(str(exc))
                record_step(
                    state,
                    "run_llm_review",
                    {
                        "called": True,
                        "provider": state.llm_provider,
                        "findings": 0,
                        "error": sanitize_trace_text(exc),
                        **_retry_detail(call_model),
                    },
                    started_at_perf=step_started_at_perf,
                )
        else:
            step_started_at_perf = perf_counter()
            record_step(
                state,
                "run_llm_review",
                {"called": False},
                started_at_perf=step_started_at_perf,
            )

        step_started_at_perf = perf_counter()
        state.issues = validate_issues(state.issues, state.changed_files)
        record_step(
            state,
            "validate_output",
            {"findings": len(state.issues)},
            started_at_perf=step_started_at_perf,
        )

        step_started_at_perf = perf_counter()
        if state.output_format == "json":
            state.output = render_json_report(state.issues)
        else:
            state.output = render_markdown_report(
                state.issues, state.changed_files, state.contexts
            )
        record_step(
            state,
            "render_report",
            {"format": state.output_format},
            started_at_perf=step_started_at_perf,
        )

        summary_comment = None
        if request.publish_summary_comment:
            step_started_at_perf = perf_counter()
            summary_body = render_summary_comment(state.issues, state.changed_files)
            summary_comment = self._git_provider_factory().publish_summary_comment(
                request.pull_request, summary_body
            )
            record_step(
                state,
                "publish_summary_comment",
                {
                    "action": summary_comment.action,
                    "comment_id": summary_comment.comment_id,
                },
                started_at_perf=step_started_at_perf,
            )

        if state.trace_enabled:
            save_started_at_perf = perf_counter()
            save_trace(
                state,
                state.trace_dir,
                final_step={
                    "step": "save_trace",
                    "detail": {"enabled": True, "trace_dir": state.trace_dir},
                    "started_at_perf": save_started_at_perf,
                },
            )
        else:
            step_started_at_perf = perf_counter()
            record_step(
                state,
                "save_trace",
                {"enabled": False},
                started_at_perf=step_started_at_perf,
            )

        return ReviewResult(state=state, summary_comment=summary_comment)
