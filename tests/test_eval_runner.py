import json

import pytest

from src import eval_runner


def make_case(tmp_path, *, expected_categories, should_find):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "expected.json").write_text(
        json.dumps(
            {
                "case_id": "case",
                "expected_categories": expected_categories,
                "should_find": should_find,
            }
        ),
        encoding="utf-8",
    )
    return case_dir


def test_extract_categories_prefers_category_and_falls_back_to_reason():
    findings = [
        {"category": "secret", "reason": "ignored"},
        {"category": "", "reason": "test_gap"},
        {"reason": ""},
        {},
    ]

    assert eval_runner.extract_categories(findings) == {"secret", "test_gap"}


@pytest.mark.parametrize(
    ("categories", "expected_passed", "expected_false_positive"),
    [
        (["secret"], True, False),
        (["secret", "debug"], False, True),
    ],
)
def test_run_one_case_requires_exact_categories_for_positive_cases(
    tmp_path, monkeypatch, categories, expected_passed, expected_false_positive
):
    case_dir = make_case(tmp_path, expected_categories=["secret"], should_find=True)
    output = json.dumps({"findings": [{"category": category} for category in categories]})
    monkeypatch.setattr(eval_runner, "run_review_agent", lambda args: (output, []))

    result = eval_runner.run_one_case(case_dir, tmp_path)

    assert result["passed"] is expected_passed
    assert result["false_positive"] is expected_false_positive
    assert result["actual_categories"] == sorted(categories)


def test_run_one_case_marks_findings_in_no_find_case_as_false_positive(
    tmp_path, monkeypatch
):
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)
    monkeypatch.setattr(
        eval_runner,
        "run_review_agent",
        lambda args: (json.dumps({"findings": [{"issue": "unexpected"}]}), []),
    )

    result = eval_runner.run_one_case(case_dir, tmp_path)

    assert result["passed"] is False
    assert result["false_positive"] is True
    assert result["actual_categories"] == []


def test_run_one_case_marks_runner_failure_as_not_passed(tmp_path, monkeypatch):
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)

    def fail_review(args):
        raise RuntimeError("review failed")

    monkeypatch.setattr(eval_runner, "run_review_agent", fail_review)

    result = eval_runner.run_one_case(case_dir, tmp_path)

    assert result["json_valid"] is False
    assert result["passed"] is False
    assert result["false_positive"] is False
    assert result["error"] == "review failed"


def test_run_eval_aggregates_case_metrics(tmp_path, monkeypatch):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for name in ("passing", "extra", "failed"):
        (cases_dir / name).mkdir()

    results_by_case = {
        "passing": {
            "passed": True,
            "json_valid": True,
            "false_positive": False,
            "findings_count": 2,
            "duration_ms": 10,
        },
        "extra": {
            "passed": False,
            "json_valid": True,
            "false_positive": True,
            "findings_count": 3,
            "duration_ms": 20,
        },
        "failed": {
            "passed": False,
            "json_valid": False,
            "false_positive": False,
            "findings_count": 0,
            "duration_ms": 30,
        },
    }

    def fake_run_one_case(case_dir, **kwargs):
        return {"case_id": case_dir.name, **results_by_case[case_dir.name]}

    monkeypatch.setattr(eval_runner, "run_one_case", fake_run_one_case)

    metrics = eval_runner.run_eval(cases_dir, tmp_path)

    assert metrics["cases"] == 3
    assert metrics["category_hit_rate"] == pytest.approx(1 / 3)
    assert metrics["false_positive_count"] == 1
    assert metrics["json_valid_rate"] == pytest.approx(2 / 3)
    assert metrics["average_findings"] == pytest.approx(5 / 3)
    assert metrics["average_duration_ms"] == pytest.approx(20)
