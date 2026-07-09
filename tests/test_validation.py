from src.validation import validate_llm_response


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
    assert finding["suggested_fix"] == ""