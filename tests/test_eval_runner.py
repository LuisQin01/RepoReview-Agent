import json

import pytest

from src import eval_runner
from src.schemas import ContextBudget


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
    ("categories", "expected_passed", "expected_false_positive", "expected_counts"),
    [
        (["secret"], True, False, (1, 0, 0)),
        (["secret", "debug"], False, True, (1, 1, 0)),
    ],
)
def test_run_one_case_requires_exact_categories_for_positive_cases(
    tmp_path, monkeypatch, categories, expected_passed, expected_false_positive, expected_counts
):
    case_dir = make_case(tmp_path, expected_categories=["secret"], should_find=True)
    output = json.dumps({"findings": [{"category": category} for category in categories]})
    monkeypatch.setattr(eval_runner, "run_review_agent", lambda args: (output, []))

    result = eval_runner.run_one_case(case_dir, tmp_path)

    assert result["passed"] is expected_passed
    assert result["false_positive"] is expected_false_positive
    assert result["actual_categories"] == sorted(categories)
    assert (result["tp"], result["fp"], result["fn"]) == expected_counts


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
    assert result["is_negative_case"] is True
    assert (result["tp"], result["fp"], result["fn"]) == (0, 1, 0)


def test_run_one_case_marks_runner_failure_as_not_passed(tmp_path, monkeypatch):
    case_dir = make_case(tmp_path, expected_categories=["secret"], should_find=True)

    def fail_review(args):
        raise RuntimeError("review failed")

    monkeypatch.setattr(eval_runner, "run_review_agent", fail_review)

    result = eval_runner.run_one_case(case_dir, tmp_path)

    assert result["json_valid"] is False
    assert result["passed"] is False
    assert result["false_positive"] is False
    assert result["error"] == "review failed"
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 1)


def test_run_one_case_passes_context_budget_to_review_agent(tmp_path, monkeypatch):
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)
    budget = ContextBudget(max_prompt_chars=17, max_extra_context_files=0)

    def fake_review(args):
        assert args.context_budget is budget
        assert args.max_prompt_chars == 17
        assert args.max_extra_context_files == 0
        return json.dumps({"findings": []}), []

    monkeypatch.setattr(eval_runner, "run_review_agent", fake_review)

    result = eval_runner.run_one_case(case_dir, tmp_path, context_budget=budget)

    assert result["passed"] is True
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 0)


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    [
        (json.dumps({}), "missing required 'findings' field"),
        (json.dumps(["not", "an", "object"]), "top-level must be an object"),
        (json.dumps({"findings": "not-a-list"}), "'findings' must be a list"),
        (json.dumps({"findings": [None]}), "'findings[0]' must be an object"),
    ],
)
def test_run_one_case_degrades_on_malformed_reviewer_output(
    tmp_path, monkeypatch, payload, error_fragment
):
    case_dir = make_case(tmp_path, expected_categories=["secret"], should_find=True)
    monkeypatch.setattr(eval_runner, "run_review_agent", lambda args: (payload, []))

    result = eval_runner.run_one_case(case_dir, tmp_path)

    assert result["json_valid"] is False
    assert result["passed"] is False
    assert result["findings_count"] == 0
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 1)
    assert error_fragment in result["error"]


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
            "is_negative_case": False,
            "findings_count": 2,
            "tp": 2,
            "fp": 0,
            "fn": 0,
            "duration_ms": 10,
        },
        "extra": {
            "passed": False,
            "json_valid": True,
            "false_positive": True,
            "is_negative_case": False,
            "findings_count": 3,
            "tp": 1,
            "fp": 2,
            "fn": 1,
            "duration_ms": 20,
        },
        "failed": {
            "passed": False,
            "json_valid": False,
            "false_positive": True,
            "is_negative_case": True,
            "findings_count": 1,
            "tp": 0,
            "fp": 1,
            "fn": 0,
            "duration_ms": 30,
        },
    }

    def fake_run_one_case(case_dir, **kwargs):
        return {"case_id": case_dir.name, **results_by_case[case_dir.name]}

    monkeypatch.setattr(eval_runner, "run_one_case", fake_run_one_case)

    metrics = eval_runner.run_eval(cases_dir, tmp_path)

    assert metrics["cases"] == 3
    assert metrics["category_hit_rate"] == pytest.approx(1 / 3)
    assert metrics["false_positive_count"] == 2
    assert metrics["json_valid_rate"] == pytest.approx(2 / 3)
    assert metrics["average_findings"] == pytest.approx(2)
    assert metrics["average_duration_ms"] == pytest.approx(20)
    assert metrics["precision"] == pytest.approx(0.5)
    assert metrics["recall"] == pytest.approx(0.75)
    assert metrics["f1"] == pytest.approx(0.6)
    assert metrics["false_positive_rate"] == 1.0
    assert metrics["false_negative_count"] == 1


def test_run_eval_false_positive_rate_uses_clean_cases_as_denominator(tmp_path, monkeypatch):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for index in range(10):
        (cases_dir / f"clean-{index}").mkdir()

    def fake_run_one_case(case_dir, **kwargs):
        is_false_positive = case_dir.name == "clean-0"
        return {
            "case_id": case_dir.name,
            "passed": not is_false_positive,
            "json_valid": True,
            "false_positive": is_false_positive,
            "is_negative_case": True,
            "findings_count": int(is_false_positive),
            "tp": 0,
            "fp": int(is_false_positive),
            "fn": 0,
            "duration_ms": 1,
        }

    monkeypatch.setattr(eval_runner, "run_one_case", fake_run_one_case)

    metrics = eval_runner.run_eval(cases_dir, tmp_path)

    assert metrics["false_positive_rate"] == pytest.approx(0.1)
    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0
