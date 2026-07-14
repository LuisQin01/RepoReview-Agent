import json
from types import SimpleNamespace

from src.cli import run_review_agent
from src.llm_client import LLMRetryableError
from src import review_service
from src.review_service import record_step


def _make_trace_args(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return True
""",
        encoding="utf-8",
    )
    return SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=False,
        llm_provider="mock",
        trace=True,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )


def test_record_step_uses_its_own_start_time_and_allows_zero_duration(monkeypatch):
    state = SimpleNamespace(trace_steps=[])
    timestamps = iter([10.0, 10.125])
    monkeypatch.setattr(review_service, "perf_counter", lambda: next(timestamps))

    record_step(state, "zero", started_at_perf=10.0)
    record_step(state, "later", started_at_perf=10.0)

    assert [step["duration_ms"] for step in state.trace_steps] == [0, 125]


def test_saved_trace_records_duration_for_every_step_including_final_save(tmp_path):
    _, trace_steps = run_review_agent(_make_trace_args(tmp_path))
    trace_path = next((tmp_path / "traces").glob("*.json"))
    saved_steps = json.loads(trace_path.read_text(encoding="utf-8"))["steps"]

    assert [step["step"] for step in saved_steps] == [
        "receive_task",
        "parse_diff",
        "collect_context",
        "run_static_checks",
        "run_llm_review",
        "validate_output",
        "render_report",
        "save_trace",
    ]
    assert saved_steps == trace_steps
    assert all(isinstance(step["duration_ms"], int) for step in saved_steps)
    assert all(step["duration_ms"] >= 0 for step in saved_steps)
    assert saved_steps[-1]["detail"]["enabled"] is True


def test_saved_trace_redacts_retry_errors_and_keeps_retry_metadata(tmp_path, monkeypatch):
    secret = "super-secret-token"

    def failing_call_model(_prompt):
        raise LLMRetryableError(f"Authorization: Bearer {secret}; api_key={secret}")

    failing_call_model.last_retry_info = {
        "attempts": 3,
        "retries": 2,
        "retry_errors": [
            f"Authorization: Bearer {secret}; api_key={secret}",
            "x" * 400,
            f"token={secret}",
            (
                f'provider response {{"api_key": "{secret}", '
                f'"token": "{secret}", "password": "{secret}"}}'
            ),
        ],
        "exhausted": True,
    }
    monkeypatch.setattr(
        review_service, "get_call_model", lambda *_args, **_kwargs: failing_call_model
    )
    args = _make_trace_args(tmp_path)
    args.llm = True

    _, trace_steps = run_review_agent(args)
    trace_path = next((tmp_path / "traces").glob("*.json"))
    trace_text = trace_path.read_text(encoding="utf-8")
    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")

    assert secret not in trace_text
    assert llm_step["detail"]["attempts"] == 3
    assert llm_step["detail"]["retries"] == 2
    assert llm_step["detail"]["exhausted"] is True
    assert all(len(error) <= 303 for error in llm_step["detail"]["retry_errors"])
    assert "[REDACTED]" in llm_step["detail"]["retry_errors"][0]
