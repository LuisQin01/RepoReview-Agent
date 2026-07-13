import json

from src.validation import validate_issue_locations, validate_llm_response
from src.llm_reviewer import (
    _TRUNCATION_MARKER,
    build_llm_prompt,
    parse_llm_response,
    review_with_llm,
)
from src.reporter import render_json_report, render_markdown_report
from src.schemas import ChangedFile, DiffHunk, DiffLine, FileContext, ReviewIssue


def test_validate_issue_locations_keeps_changed_hunk_boundaries_and_repository_rules():
    changed_files = [
        ChangedFile(
            path="src/app.py",
            added_lines=[],
            deleted_lines=[],
            patch="",
            hunks=[DiffHunk(10, 12), DiffHunk(20, 20)],
        )
    ]
    issues = [
        ReviewIssue("src/app.py", 10, "warning", "llm", "start", "fix", source="llm"),
        ReviewIssue("src/app.py", 12, "warning", "llm", "end", "fix", source="llm"),
        ReviewIssue("src/app.py", 20, "warning", "llm", "second", "fix", source="llm"),
        ReviewIssue("(repository)", 0, "warning", "test_gap", "gap", "fix", source="rule"),
    ]

    validated = validate_issue_locations(issues, changed_files)

    assert validated == issues


def test_validate_issue_locations_drops_out_of_scope_or_unlocatable_findings():
    changed_files = [
        ChangedFile(
            path="src/app.py",
            added_lines=[],
            deleted_lines=[],
            patch="",
            hunks=[DiffHunk(10, 12)],
        )
    ]
    valid_issue = ReviewIssue("src/app.py", 11, "warning", "llm", "valid", "fix", source="llm")
    invalid_issues = [
        ReviewIssue("src/other.py", 11, "warning", "llm", "wrong file", "fix", source="llm"),
        ReviewIssue("src/app.py", 9, "warning", "llm", "before", "fix", source="llm"),
        ReviewIssue("src/app.py", 13, "warning", "llm", "after", "fix", source="llm"),
        ReviewIssue(["src/app.py"], 11, "warning", "llm", "bad path", "fix", source="llm"),
        ReviewIssue("src/app.py", True, "warning", "llm", "bad line", "fix", source="llm"),
    ]

    validated = validate_issue_locations([valid_issue, *invalid_issues], changed_files)

    assert validated == [valid_issue]


def test_bad_json_returns_error_not_exception():
    result = validate_llm_response("this is not json")

    assert result.valid is False
    assert result.findings == []
    assert "llm_json_parse_error" in result.errors


def test_missing_fields_are_repaired():
    response_text = """
    {
      "findings": [
        {
          "severity": "strange",
          "issue": "Something is wrong"
        }
      ]
    }
    """

    result = validate_llm_response(response_text)

    assert result.valid is True
    assert result.repaired is True

    finding = result.findings[0]
    assert finding["severity"] == "medium"
    assert finding["file"] == "(unknown)"
    assert finding["line"] == 0
    assert finding["confidence"] == 0.5
    assert finding["reason"] == ""
    assert finding["suggested_fix"] == ""
    assert finding["evidence"] == ""


def test_metadata_fields_are_preserved():
    response_text = """
    {
      "findings": [
        {
          "severity": "high",
          "file": "src/app.py",
          "line": 12,
          "issue": "Unhandled error",
          "reason": "The operation can fail.",
          "suggested_fix": "Handle the error.",
          "confidence": 0.82,
          "evidence": "src/app.py:12"
        }
      ]
    }
    """

    result = validate_llm_response(response_text)

    assert result.valid is True
    assert result.repaired is False
    assert result.findings[0]["issue"] == "Unhandled error"
    assert result.findings[0]["reason"] == "The operation can fail."
    assert result.findings[0]["suggested_fix"] == "Handle the error."
    assert result.findings[0]["confidence"] == 0.82
    assert result.findings[0]["evidence"] == "src/app.py:12"
    assert "source" not in result.findings[0]


def test_llm_prompt_declares_source_system_managed():
    prompt = build_llm_prompt([], [], [])

    assert '"source": "llm"' not in prompt
    assert "source 字段由系统根据调用路径赋值；不要在 JSON 中输出 source。" in prompt


def test_llm_source_is_assigned_by_the_parser():
    issues, validation = parse_llm_response(
        """{
          "findings": [{
            "severity": "low",
            "file": "src/app.py",
            "line": 1,
            "issue": "Example",
            "reason": "Example reason",
            "suggested_fix": "Example fix",
            "confidence": 0.6,
            "evidence": "src/app.py:1",
            "source": "rule"
          }]
        }"""
    )

    assert validation.valid is True
    assert validation.repaired is True
    assert "llm_finding_0_ignored_source" in validation.errors
    assert "source" not in validation.findings[0]
    assert issues[0].source == "llm"


def test_non_finite_confidence_is_repaired_before_json_rendering():
    for confidence in ("NaN", "Infinity", "-Infinity"):
        issues, validation = parse_llm_response(
            f"""{{
              "findings": [{{
                "severity": "low",
                "file": "src/app.py",
                "line": 1,
                "issue": "Example",
                "reason": "Example reason",
                "suggested_fix": "Example fix",
                "confidence": "{confidence}",
                "evidence": "src/app.py:1"
              }}]
            }}"""
        )

        report = render_json_report(issues)

        assert validation.repaired is True
        assert "llm_finding_0_non_finite_confidence" in validation.errors
        assert issues[0].confidence == 0.5
        assert "NaN" not in report
        assert json.loads(report)["findings"][0]["confidence"] == 0.5


def test_invalid_text_metadata_is_repaired_in_json_and_markdown_reports():
    issues, validation = parse_llm_response(
        """{
          "findings": [{
            "severity": "low",
            "file": "src/app.py",
            "line": 1,
            "issue": null,
            "reason": {"detail": "Example reason"},
            "suggested_fix": ["Example fix"],
            "confidence": 0.5,
            "evidence": null
          }]
        }"""
    )

    json_report = json.loads(render_json_report(issues))
    markdown_report = render_markdown_report(issues, [], [])

    assert validation.valid is True
    assert validation.repaired is True
    assert {
        "llm_finding_0_invalid_issue_type",
        "llm_finding_0_invalid_reason_type",
        "llm_finding_0_invalid_suggested_fix_type",
        "llm_finding_0_invalid_evidence_type",
    }.issubset(validation.errors)
    assert json_report["findings"][0]["category"] == "llm"
    assert json_report["findings"][0]["issue"] == ""
    assert json_report["findings"][0]["reason"] == ""
    assert json_report["findings"][0]["suggested_fix"] == ""
    assert json_report["findings"][0]["evidence"] == ""
    assert "| info | src/app.py | 1 | llm |  |  |  | 0.5 |  | llm |" in markdown_report
    assert "None" not in markdown_report


def test_llm_prompt_budget_includes_large_patch_without_contexts():
    changed_files = [
        ChangedFile(
            path="src/large.py",
            patch="+" + "x" * 5000,
            added_lines=[],
            deleted_lines=[],
        )
    ]

    prompt = build_llm_prompt(
        changed_files,
        contexts=[],
        rule_issues=[],
        max_prompt_chars=4000,
    )

    assert len(prompt) <= 4000
    assert '"patch"' in prompt
    assert prompt.endswith(_TRUNCATION_MARKER)


def test_llm_prompt_budget_covers_serialized_diff_lines_contexts_and_findings():
    changed_files = [
        ChangedFile(
            path="src/app.py",
            patch="patch-" + "p" * 1200,
            added_lines=[DiffLine("src/app.py", 4, "added-" + "a" * 600)],
            deleted_lines=[DiffLine("src/app.py", 3, "deleted-" + "d" * 600)],
        )
    ]
    contexts = [
        FileContext(
            path="src/app.py",
            exists=True,
            content="context-" + "c" * 1200,
            truncated=False,
            chars_read=1208,
        )
    ]
    findings = [
        ReviewIssue(
            file_path="src/app.py",
            line_no=4,
            severity="warning",
            category="rule",
            message="finding-" + "f" * 600,
            suggestion="fix",
        )
    ]

    prompt = build_llm_prompt(
        changed_files,
        contexts,
        findings,
        max_prompt_chars=2200,
    )

    assert len(prompt) <= 2200
    assert '"changed_files"' in prompt
    assert prompt.endswith(_TRUNCATION_MARKER)


def test_llm_prompt_keeps_complete_normal_input_when_within_budget():
    changed_files = [
        ChangedFile(
            path="src/app.py",
            patch="+safe_change()",
            added_lines=[DiffLine("src/app.py", 1, "safe_change()")],
            deleted_lines=[],
        )
    ]
    contexts = [
        FileContext("src/app.py", True, "def safe_change():\n    return True\n", False, 35)
    ]

    prompt = build_llm_prompt(
        changed_files,
        contexts,
        rule_issues=[],
        max_prompt_chars=10000,
    )

    assert len(prompt) <= 10000
    assert _TRUNCATION_MARKER not in prompt
    assert "+safe_change()" in prompt
    assert "def safe_change" in prompt


def test_review_with_llm_sends_the_budgeted_prompt_to_the_model():
    captured_prompts = []
    changed_files = [
        ChangedFile("src/app.py", [], [], "+" + "x" * 5000)
    ]

    issues, validation = review_with_llm(
        changed_files,
        contexts=[],
        rule_issues=[],
        call_model=lambda prompt: captured_prompts.append(prompt) or '{"findings": []}',
        max_prompt_chars=4000,
    )

    assert issues == []
    assert validation.valid is True
    assert len(captured_prompts) == 1
    assert len(captured_prompts[0]) <= 4000
    assert captured_prompts[0].endswith(_TRUNCATION_MARKER)
