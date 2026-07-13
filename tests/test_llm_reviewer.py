import json
from types import SimpleNamespace

from src.cli import run_review_agent
from src.llm_reviewer import build_llm_prompt, review_with_llm
from src.schemas import ChangedFile, DiffLine


def _sensitive_changed_file():
    return ChangedFile(
        path=".env",
        added_lines=[DiffLine(".env", 1, "+OPENAI_API_KEY=review-secret")],
        deleted_lines=[],
        patch="@@ -0,0 +1,1 @@\n+OPENAI_API_KEY=review-secret\n",
    )


def _normal_changed_file():
    return ChangedFile(
        path="app.py",
        added_lines=[DiffLine("app.py", 1, "+def run():")],
        deleted_lines=[],
        patch="@@ -0,0 +1,1 @@\n+def run():\n",
    )


def test_build_llm_prompt_redacts_sensitive_diff():
    prompt = build_llm_prompt(
        changed_files=[_sensitive_changed_file()],
        contexts=[],
        rule_issues=[],
    )

    assert "review-secret" not in prompt
    assert "REDACTED" in prompt
    assert ".env" in prompt


def test_build_llm_prompt_keeps_normal_diff():
    prompt = build_llm_prompt(
        changed_files=[_normal_changed_file()],
        contexts=[],
        rule_issues=[],
    )

    assert "def run():" in prompt
    assert "review-secret" not in prompt
    assert "REDACTED" not in prompt


def test_review_with_llm_does_not_leak_secret_to_call_model():
    captured = {}

    def fake_call_model(prompt):
        captured["prompt"] = prompt
        return '{"findings": []}'

    review_with_llm(
        changed_files=[_sensitive_changed_file(), _normal_changed_file()],
        contexts=[],
        rule_issues=[],
        call_model=fake_call_model,
    )

    assert "review-secret" not in captured["prompt"]
    assert ".env" in captured["prompt"]
    assert "app.py" in captured["prompt"]
    assert "REDACTED" in captured["prompt"]


def test_trace_does_not_contain_secret_when_llm_enabled(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("OPENAI_API_KEY=review-secret\n", encoding="utf-8")

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -0,0 +1,1 @@
+OPENAI_API_KEY=review-secret
""",
        encoding="utf-8",
    )

    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,
        llm_provider="mock",
        mock_fixture="normal",
        trace=True,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    _, trace_steps = run_review_agent(args)

    blob = json.dumps(trace_steps, ensure_ascii=False)
    assert "review-secret" not in blob

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    assert "review-secret" not in json.dumps(llm_step["detail"], ensure_ascii=False)

    saved = list((tmp_path / "traces").glob("*.json"))[0].read_text(encoding="utf-8")
    assert "review-secret" not in saved
