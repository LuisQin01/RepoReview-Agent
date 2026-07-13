import json

from src.validation import validate_llm_response
from src.llm_reviewer import build_llm_prompt, parse_llm_response
from src.reporter import render_json_report, render_markdown_report


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
    assert json_report["findings"][0]["issue"] == ""
    assert json_report["findings"][0]["reason"] == ""
    assert json_report["findings"][0]["suggested_fix"] == ""
    assert json_report["findings"][0]["evidence"] == ""
    assert "| info | src/app.py | 1 | llm |  |  |  | 0.5 |  | llm |" in markdown_report
    assert "None" not in markdown_report
