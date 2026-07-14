from src.git_provider import SUMMARY_COMMENT_MARKER
from src.reporter import render_summary_comment
from src.schemas import ChangedFile, ReviewIssue


def test_render_summary_comment_is_marked_compact_and_sorted():
    issues = [
        ReviewIssue(
            file_path="z.py",
            line_no=3,
            severity="warning",
            category="todo",
            message="later",
            suggestion="remove it",
        ),
        ReviewIssue(
            file_path="a.py",
            line_no=2,
            severity="error",
            category="secret",
            message="token|with\nnewline",
            suggestion="rotate it",
        ),
    ]
    changed_files = [
        ChangedFile("a.py", [], [], ""),
        ChangedFile("z.py", [], [], ""),
    ]

    summary = render_summary_comment(issues, changed_files)

    assert summary.startswith(SUMMARY_COMMENT_MARKER + "\n")
    assert "- Changed files: 2" in summary
    assert "- Findings: 2" in summary
    assert "## JSON Output" not in summary
    assert summary.index("| error | a.py | 2") < summary.index("| warning | z.py | 3")
    assert "token\\|with<br>newline" in summary


def test_render_summary_comment_marks_downgraded_finding_as_summary_only():
    issue = ReviewIssue(
        file_path="app.py",
        line_no=99,
        severity="warning",
        category="llm",
        message="line is outside the changed hunk",
        suggestion="fix it",
        source="llm",
        placement="summary",
    )

    summary = render_summary_comment([issue], [ChangedFile("app.py", [], [], "")])

    assert "| warning | app.py | summary only | summary | llm | line is outside the changed hunk |" in summary
    assert "| warning | app.py | 99" not in summary


def test_render_summary_comment_handles_no_findings():
    summary = render_summary_comment([], [])

    assert summary.startswith(SUMMARY_COMMENT_MARKER + "\n")
    assert "- Findings: 0" in summary
    assert "No findings." in summary


SECRET_MARKER = "LEAKED_SECRET_VALUE_42"
GITHUB_TOKEN_MARKER = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
OPENAI_TOKEN_MARKER = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"


def test_render_summary_comment_redacts_credential_values_in_finding_message():
    """P0: secret values in finding messages must not appear in the published summary."""
    issues = [
        ReviewIssue(
            file_path="configs/API_KEY={}.py".format(SECRET_MARKER),
            line_no=1,
            severity="error",
            category="token={}".format(SECRET_MARKER),
            message="Hardcoded credential: {}".format(GITHUB_TOKEN_MARKER),
            suggestion="Replace {} with an environment variable".format(
                OPENAI_TOKEN_MARKER
            ),
            reason=f"token={SECRET_MARKER} is exposed",
            evidence=f"app.py:1 API_KEY={SECRET_MARKER}",
        ),
    ]
    changed_files = [ChangedFile("app.py", [], [], "")]

    summary = render_summary_comment(issues, changed_files)

    assert SECRET_MARKER not in summary, (
        "P0 泄露: secret marker 不应出现在 summary comment 中"
    )
    assert "[REDACTED]" in summary
    assert "API_KEY=[REDACTED]" in summary
    assert GITHUB_TOKEN_MARKER not in summary
    assert OPENAI_TOKEN_MARKER not in summary


def test_render_json_report_redacts_credential_values():
    """P0: secret values must also be redacted in the local JSON report."""
    from src.reporter import render_json_report

    issues = [
        ReviewIssue(
            file_path="app.py",
            line_no=1,
            severity="error",
            category="secret",
            message="credential {}".format(GITHUB_TOKEN_MARKER),
            suggestion="replace {}".format(OPENAI_TOKEN_MARKER),
            reason=f"token={SECRET_MARKER}",
            evidence=f"app.py:1 password={SECRET_MARKER}",
        ),
    ]

    report = render_json_report(issues)

    assert SECRET_MARKER not in report
    assert "[REDACTED]" in report
    assert GITHUB_TOKEN_MARKER not in report
    assert OPENAI_TOKEN_MARKER not in report
