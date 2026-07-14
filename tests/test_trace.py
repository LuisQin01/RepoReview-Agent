import json
from types import SimpleNamespace

from src import cli
from src.cli import run_review_agent


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
    monkeypatch.setattr(cli, "perf_counter", lambda: next(timestamps))

    cli.record_step(state, "zero", started_at_perf=10.0)
    cli.record_step(state, "later", started_at_perf=10.0)

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
