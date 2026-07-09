import json
from types import SimpleNamespace

from src.cli import run_review_agent


def test_cli_smoke_runs_simple_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    app_file = repo / "app.py"
    app_file.write_text(
        """def login(user):
    print(user.password)
    return True
""",
        encoding="utf-8",
    )

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def login(user):
+    print(user.password)
     return True
""",
        encoding="utf-8",
    )

    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=False,
        llm_provider="mock",
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    output, trace_steps = run_review_agent(args)
    data = json.loads(output)

    assert "findings" in data
    assert any(step["step"] == "parse_diff" for step in trace_steps)
    assert any(step["step"] == "run_static_checks" for step in trace_steps)