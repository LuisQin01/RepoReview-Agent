import json
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from src.cli import run_review_agent
from src.git_provider import GitProviderInputError, SUMMARY_COMMENT_MARKER
from src.github_provider import GitHubAuthorizationError, GitHubPRProvider


class SummaryHttpResponse:
    def __init__(self, payload):
        self._body = payload.encode("utf-8")
        self.headers = {}

    def read(self):
        return self._body

    def close(self):
        pass


def make_summary_publish_args(tmp_path, *, publish=False, trace=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def run():\n    print('debug')\n", encoding="utf-8"
    )
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 def run():
+    print('debug')
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
        trace=trace,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
        publish_summary_comment=publish,
        pr_url="https://github.com/acme/reviewed-repo/pull/42",
    )


def install_summary_provider(monkeypatch, http_open, *, token="test-token", author="reporeview-bot"):
    monkeypatch.setattr(
        "src.cli.GitHubPRProvider",
        lambda: GitHubPRProvider(
            token=token,
            summary_comment_author_login=author,
            http_open=http_open,
        ),
    )


def install_summary_renderer(monkeypatch):
    body = SUMMARY_COMMENT_MARKER + "\n## rendered by CLI integration test"

    def render(issues, changed_files):
        assert issues
        assert changed_files
        return body

    monkeypatch.setattr("src.cli.render_summary_comment", render)
    return body


def test_cli_does_not_construct_summary_provider_without_opt_in(tmp_path, monkeypatch):
    args = make_summary_publish_args(tmp_path)
    monkeypatch.setattr(
        "src.cli.GitHubPRProvider",
        lambda: pytest.fail("summary provider must not be constructed"),
    )

    output, trace_steps = run_review_agent(args)

    assert json.loads(output)["findings"]
    assert not any(step["step"] == "publish_summary_comment" for step in trace_steps)


def test_cli_publishes_summary_comment_when_opted_in(tmp_path, monkeypatch):
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    requests = []
    expected_body = install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            assert request.full_url == comments_url + "?per_page=100&page=1"
            return SummaryHttpResponse("[]")
        assert request.method == "POST"
        assert request.full_url == comments_url
        assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
        return SummaryHttpResponse(json.dumps({"id": 73}))

    token = "test-token-must-not-leak"
    install_summary_provider(monkeypatch, http_open, token=token)
    _output, trace_steps = run_review_agent(
        make_summary_publish_args(tmp_path, publish=True, trace=True)
    )

    assert [request.method for request in requests] == ["GET", "POST"]
    assert next(step for step in trace_steps if step["step"] == "publish_summary_comment")[
        "detail"
    ] == {"action": "created", "comment_id": 73}
    trace_files = list((tmp_path / "traces").glob("*.json"))
    assert len(trace_files) == 1
    assert token not in trace_files[0].read_text(encoding="utf-8")


def test_cli_updates_its_existing_summary_comment_when_opted_in(tmp_path, monkeypatch):
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    update_url = "https://api.github.com/repos/acme/reviewed-repo/issues/comments/41"
    requests = []
    expected_body = install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            assert request.full_url == comments_url + "?per_page=100&page=1"
            return SummaryHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 41,
                            "body": SUMMARY_COMMENT_MARKER + "\nold",
                            "user": {"login": "reporeview-bot"},
                        }
                    ]
                )
            )
        assert request.method == "PATCH"
        assert request.full_url == update_url
        assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
        return SummaryHttpResponse(json.dumps({"id": 41}))

    install_summary_provider(monkeypatch, http_open)
    _output, trace_steps = run_review_agent(make_summary_publish_args(tmp_path, publish=True))

    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert next(step for step in trace_steps if step["step"] == "publish_summary_comment")[
        "detail"
    ] == {"action": "updated", "comment_id": 41}


def test_cli_creates_summary_instead_of_updating_external_marker(tmp_path, monkeypatch):
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    requests = []
    expected_body = install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return SummaryHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 41,
                            "body": SUMMARY_COMMENT_MARKER + "\nexternal",
                            "user": {"login": "collaborator"},
                        }
                    ]
                )
            )
        assert request.method == "POST"
        assert request.full_url == comments_url
        assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
        return SummaryHttpResponse(json.dumps({"id": 73}))

    install_summary_provider(monkeypatch, http_open)
    run_review_agent(make_summary_publish_args(tmp_path, publish=True))

    assert [request.method for request in requests] == ["GET", "POST"]
    assert all("/issues/comments/41" not in request.full_url for request in requests)


def test_cli_propagates_summary_permission_failure_without_token_in_trace(
    tmp_path, monkeypatch, capsys
):
    token = "test-token-must-not-leak"
    requests = []
    install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return SummaryHttpResponse("[]")
        assert request.method == "POST"
        raise HTTPError(request.full_url, 403, "Forbidden", {}, BytesIO())

    install_summary_provider(monkeypatch, http_open, token=token)
    args = make_summary_publish_args(tmp_path, publish=True, trace=True)

    with pytest.raises(GitHubAuthorizationError, match="github_access_denied:status=403") as exc_info:
        run_review_agent(args)

    captured = capsys.readouterr()
    assert [request.method for request in requests] == ["GET", "POST"]
    assert token not in str(exc_info.value)
    assert token not in captured.out
    assert token not in captured.err
    assert not list((tmp_path / "traces").glob("*.json"))


def test_cli_rejects_missing_summary_author_before_http_call(tmp_path, monkeypatch, capsys):
    token = "test-token-must-not-leak"
    requests = []
    monkeypatch.delenv("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN", raising=False)
    install_summary_renderer(monkeypatch)

    def http_open(*_args, **_kwargs):
        requests.append(True)
        pytest.fail("unexpected HTTP call")

    install_summary_provider(monkeypatch, http_open, token=token, author=None)
    args = make_summary_publish_args(tmp_path, publish=True, trace=True)

    with pytest.raises(
        GitProviderInputError, match="^missing_summary_comment_author_login$"
    ) as exc_info:
        run_review_agent(args)

    captured = capsys.readouterr()
    assert requests == []
    assert token not in str(exc_info.value)
    assert token not in captured.out
    assert token not in captured.err
    assert not list((tmp_path / "traces").glob("*.json"))


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


# ---------------------------------------------------------------------------
# P0 regression: secret values in finding messages must not reach the PR body
# ---------------------------------------------------------------------------

P0_SECRET_MARKER = "LEAKED_SECRET_VALUE_42"
P0_GITHUB_TOKEN_MARKER = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
P0_OPENAI_TOKEN_MARKER = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"


def test_cli_redacts_secret_values_in_published_summary_body(tmp_path, monkeypatch):
    """P0: a finding whose message quotes a credential value must be redacted
    before the summary body is POSTed to GitHub, written to local output, or
    saved to trace.
    """
    from src.schemas import ReviewIssue

    args = make_summary_publish_args(tmp_path, publish=True, trace=True)

    secret_path = "configs/API_KEY={}.py".format(P0_SECRET_MARKER)
    source_file = Path(args.repo) / secret_path
    source_file.parent.mkdir()
    source_file.write_text("value = 1\n", encoding="utf-8")
    Path(args.diff).write_text(
        """diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -0,0 +1,1 @@
+value = 1
""".format(path=secret_path),
        encoding="utf-8",
    )

    # Inject a finding whose fields all contain a secret or token value.
    def fake_review(changed_files):
        return [
            ReviewIssue(
                file_path=secret_path,
                line_no=1,
                severity="error",
                category="token={}".format(P0_SECRET_MARKER),
                message="Hardcoded credential: {}".format(P0_GITHUB_TOKEN_MARKER),
                suggestion="Replace {} with an environment variable".format(
                    P0_OPENAI_TOKEN_MARKER
                ),
                reason=f"token={P0_SECRET_MARKER} is exposed",
                evidence=f"app.py:1 API_KEY={P0_SECRET_MARKER}",
                source="rule",
            ),
        ]

    monkeypatch.setattr("src.cli.review_changed_files", fake_review)

    requests = []

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return SummaryHttpResponse("[]")
        assert request.method == "POST"
        return SummaryHttpResponse(json.dumps({"id": 73}))

    install_summary_provider(monkeypatch, http_open)
    output, trace_steps = run_review_agent(args)

    # Secret must not appear in any HTTP request body (POST or PATCH).
    for request in requests:
        if request.data:
            body = request.data.decode("utf-8")
            assert P0_SECRET_MARKER not in body, (
                "P0 泄露: secret marker 出现在 HTTP body 中"
            )

    # Secret must not appear in local output.
    assert P0_SECRET_MARKER not in output, (
        "P0 泄露: secret marker 出现在本地输出中"
    )

    assert P0_GITHUB_TOKEN_MARKER not in output
    assert P0_OPENAI_TOKEN_MARKER not in output

    # Secret must not appear in trace steps.
    assert P0_SECRET_MARKER not in json.dumps(trace_steps, ensure_ascii=False), (
        "P0 泄露: secret marker 出现在 trace_steps 中"
    )

    # Secret must not appear in saved trace files.
    trace_files = list((tmp_path / "traces").glob("*.json"))
    assert len(trace_files) == 1
    assert P0_SECRET_MARKER not in trace_files[0].read_text(encoding="utf-8"), (
        "P0 泄露: secret marker 出现在 trace 文件中"
    )

    trace_content = trace_files[0].read_text(encoding="utf-8")
    assert P0_GITHUB_TOKEN_MARKER not in trace_content
    assert P0_OPENAI_TOKEN_MARKER not in trace_content

    # Verify redaction actually happened (finding was not silently dropped).
    post_request = next(r for r in requests if r.method == "POST")
    post_body = json.loads(post_request.data.decode("utf-8"))["body"]
    assert P0_SECRET_MARKER not in post_body
    assert P0_GITHUB_TOKEN_MARKER not in post_body
    assert P0_OPENAI_TOKEN_MARKER not in post_body
    assert "[REDACTED]" in post_body
    assert "API_KEY=[REDACTED]" in post_body
    assert "error" in post_body  # severity row is present


# ---------------------------------------------------------------------------
# P1 lifecycle: re-running the agent updates the same comment, not duplicate
# ---------------------------------------------------------------------------

def test_cli_create_then_update_summary_uses_same_comment_id(tmp_path, monkeypatch):
    """P1: a second run must PATCH the comment created by the first run,
    proving the 're-run does not duplicate' exit condition.
    """
    args = make_summary_publish_args(tmp_path, publish=True)
    expected_body = install_summary_renderer(monkeypatch)

    get_count = [0]
    all_requests = []

    def http_open(request, timeout):
        all_requests.append(request)
        if request.method == "GET":
            get_count[0] += 1
            if get_count[0] == 1:
                # First run: no existing marked comment.
                return SummaryHttpResponse("[]")
            # Second run: the comment created by the first run now exists.
            return SummaryHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 73,
                            "body": SUMMARY_COMMENT_MARKER + "\n## RepoReview summary",
                            "user": {"login": "reporeview-bot"},
                        }
                    ]
                )
            )
        if request.method == "POST":
            assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
            return SummaryHttpResponse(json.dumps({"id": 73}))
        if request.method == "PATCH":
            assert request.full_url == (
                "https://api.github.com/repos/acme/reviewed-repo/issues/comments/73"
            )
            assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
            return SummaryHttpResponse(json.dumps({"id": 73}))
        raise AssertionError("unexpected method: {}".format(request.method))

    install_summary_provider(monkeypatch, http_open)

    # First run: creates comment with id 73.
    _output1, trace_steps1 = run_review_agent(args)
    publish1 = next(s for s in trace_steps1 if s["step"] == "publish_summary_comment")
    assert publish1["detail"] == {"action": "created", "comment_id": 73}

    # Second run: updates the same comment (PATCH, not POST).
    _output2, trace_steps2 = run_review_agent(args)
    publish2 = next(s for s in trace_steps2 if s["step"] == "publish_summary_comment")
    assert publish2["detail"] == {"action": "updated", "comment_id": 73}

    # Sequence: GET (1st run), POST (1st run), GET (2nd run), PATCH (2nd run).
    # No duplicate POST — the second run updates the same comment.
    assert [r.method for r in all_requests] == ["GET", "POST", "GET", "PATCH"]
    assert get_count[0] == 2
