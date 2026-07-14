import json
import sys
from types import SimpleNamespace

import pytest

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
    assert data["findings"]
    assert all(finding["source"] == "rule" for finding in data["findings"])
    assert any(step["step"] == "parse_diff" for step in trace_steps)
    assert any(step["step"] == "run_static_checks" for step in trace_steps)


def test_cli_mock_llm_metadata_reaches_json_and_markdown(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    app_file = repo / "app.py"
    app_file.write_text(
        "".join(f"existing_{line_no} = {line_no}\n" for line_no in range(1, 10))
        + "def run():\n    return 1\n",
        encoding="utf-8",
    )

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -9,0 +10,2 @@
+def run():
+    return 1
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
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    json_output, _ = run_review_agent(args)
    json_findings = json.loads(json_output)["findings"]
    llm_finding = next(finding for finding in json_findings if finding["source"] == "llm")

    assert llm_finding["reason"] == "新增代码可能执行失败，但没有看到错误处理逻辑"
    assert llm_finding["confidence"] == 0.76
    assert llm_finding["evidence"] == "app.py:10"
    assert llm_finding["source"] == "llm"

    args.format = "markdown"
    markdown_output, _ = run_review_agent(args)

    findings_section = markdown_output.split("## Findings\n\n", 1)[1].split(
        "\n\n## JSON Output", 1
    )[0]
    table_lines = [line for line in findings_section.splitlines() if line.startswith("|")]
    separator_cells = [cell.strip() for cell in table_lines[1].strip("|").split("|")]
    data_rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in table_lines[2:]
    ]
    cells = next(row for row in data_rows if row[3] == "llm")

    assert table_lines[0] == "| Severity | File | Line | Category | Issue | Reason | Suggestion | Confidence | Evidence | Source |"
    assert table_lines[1] == "| --- | --- | ---: | --- | --- | --- | --- | ---: | --- | --- |"
    assert len(separator_cells) == 10
    assert len(cells) == 10
    assert cells[5] == "新增代码可能执行失败，但没有看到错误处理逻辑"
    assert cells[7] == "0.76"
    assert cells[8] == "app.py:10"
    assert cells[9] == "llm"


def make_llm_fixture_args(tmp_path, fixture):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return 1
""",
        encoding="utf-8",
    )
    return SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,
        llm_provider="mock",
        mock_fixture=fixture,
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )


@pytest.mark.parametrize(
    ("fixture", "expected_valid", "expected_errors"),
    [
        ("bad_json", False, ["llm_json_parse_error"]),
        ("empty", True, []),
    ],
)
def test_cli_mock_llm_fixtures_reach_validation(
    tmp_path, fixture, expected_valid, expected_errors
):
    output, trace_steps = run_review_agent(make_llm_fixture_args(tmp_path, fixture))

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    assert llm_step["detail"]["valid"] is expected_valid
    assert llm_step["detail"]["errors"] == expected_errors
    assert llm_step["detail"]["findings"] == 0
    assert not any(finding["source"] == "llm" for finding in findings)


def test_cli_mock_llm_timeout_is_recorded_without_an_llm_finding(tmp_path):
    output, trace_steps = run_review_agent(make_llm_fixture_args(tmp_path, "timeout"))

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    assert llm_step["detail"] == {
        "called": True,
        "provider": "mock",
        "findings": 0,
        "error": "mock_timeout",
        "attempts": 3,
        "retries": 2,
        "retry_errors": ["mock_timeout", "mock_timeout", "mock_timeout"],
        "exhausted": True,
    }
    assert not any(finding["source"] == "llm" for finding in findings)


def test_cli_records_exhausted_openai_http_503_without_llm_finding(
    tmp_path, monkeypatch
):
    request_arguments = []

    class ProviderUnavailable(Exception):
        status_code = 503

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            raise ProviderUnavailable("service unavailable")

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    args = make_llm_fixture_args(tmp_path, "normal")
    args.llm_provider = "openai"

    output, trace_steps = run_review_agent(args)

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    assert len(request_arguments) == 3
    assert llm_step["detail"]["findings"] == 0
    assert llm_step["detail"]["error"] == "openai_call_failed:service unavailable"
    assert not any(finding["source"] == "llm" for finding in findings)


def test_cli_retries_mock_timeout_then_publishes_recovered_llm_finding(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "".join(f"existing_{line_no} = {line_no}\n" for line_no in range(1, 10))
        + "def run():\n    return 1\n",
        encoding="utf-8",
    )
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -9,0 +10,2 @@
+def run():
+    return 1
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
        mock_fixture="timeout_then_success",
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    output, trace_steps = run_review_agent(args)

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    assert llm_step["detail"]["valid"] is True
    assert llm_step["detail"]["attempts"] == 3
    assert llm_step["detail"]["retries"] == 2
    assert llm_step["detail"]["retry_errors"] == ["mock_timeout", "mock_timeout"]
    assert llm_step["detail"]["exhausted"] is False
    assert any(finding["source"] == "llm" for finding in findings)


def test_cli_does_not_publish_llm_findings_outside_changed_hunks(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("new_value = 1\n", encoding="utf-8")
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,1 @@
+new_value = 1
""",
        encoding="utf-8",
    )
    response = {
        "findings": [
            {
                "severity": "low",
                "file": "app.py",
                "line": 1,
                "issue": "valid",
                "reason": "inside hunk",
                "suggested_fix": "fix it",
                "confidence": 0.9,
                "evidence": "app.py:1",
            },
            {
                "severity": "low",
                "file": "app.py",
                "line": 2,
                "issue": "wrong line",
                "reason": "outside hunk",
                "suggested_fix": "fix it",
                "confidence": 0.9,
                "evidence": "app.py:2",
            },
            {
                "severity": "low",
                "file": "other.py",
                "line": 1,
                "issue": "wrong file",
                "reason": "outside scope",
                "suggested_fix": "fix it",
                "confidence": 0.9,
                "evidence": "other.py:1",
            },
        ]
    }
    monkeypatch.setattr(
        "src.cli.get_call_model",
        lambda *_args, **_kwargs: lambda _prompt: json.dumps(response),
    )
    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,
        llm_provider="mock",
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    output, trace_steps = run_review_agent(args)

    findings = json.loads(output)["findings"]
    assert [finding["issue"] for finding in findings if finding["source"] == "llm"] == ["valid"]
    validate_step = next(step for step in trace_steps if step["step"] == "validate_output")
    assert validate_step["detail"]["findings"] == len(findings)


def test_cli_trace_records_context_provenance(tmp_path):
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
    args = SimpleNamespace(
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

    _, trace_steps = run_review_agent(args)

    collect_step = next(step for step in trace_steps if step["step"] == "collect_context")
    selected_context = collect_step["detail"]["selected_contexts"][0]
    assert selected_context["source"] == "changed_file"
    assert selected_context["selection_reason"] == "file is changed in the pull request"

    trace_paths = list((tmp_path / "traces").glob("*.json"))
    assert len(trace_paths) == 1
    saved_context = json.loads(trace_paths[0].read_text(encoding="utf-8"))["context_files"][0]
    assert saved_context["source"] == "changed_file"
    assert saved_context["selection_reason"] == "file is changed in the pull request"
