'''
1. 读取 evals/cases 下的每个 case 目录
2. 每个 case 读取 input.diff 和 expected.json
3. 构造 args，调用已有的 run_review_agent(args)
4. 解析 review 输出里的 findings
5. 提取实际命中的 category
6. 和 expected_categories 对比
7. 汇总输出指标
'''

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from time import perf_counter

from .cli import run_review_agent
from .schemas import ContextBudget

def load_expected(case_dir: Path):
    expected_path = case_dir / "expected.json"
    return json.loads(expected_path.read_text(encoding="utf-8"))

def extract_categories(findings):
    categories = set()
    for finding in findings:
        category = finding.get("category") or finding.get("reason")
        if category:
            categories.add(category)
    return categories


def _category_counts(actual_categories, expected_categories):
    """Return category-level true-positive, false-positive, and false-negative counts."""
    true_positive_count = len(actual_categories & expected_categories)
    false_positive_count = len(actual_categories - expected_categories)
    false_negative_count = len(expected_categories - actual_categories)
    return true_positive_count, false_positive_count, false_negative_count


def _safe_ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def run_one_case(
        case_dir: Path,
        repo_root: Path,
        use_llm: bool = False,
        llm_provider: str = "mock",
        context_budget: ContextBudget | None = None,
        ):
    expected = load_expected(case_dir)
    context_budget = context_budget or ContextBudget()

    args=SimpleNamespace(
        diff=str(case_dir / "input.diff"),
        repo=str(repo_root),
        max_prompt_chars=context_budget.max_prompt_chars,
        format="json",
        output=None,
        llm=use_llm,
        llm_provider=llm_provider,
        trace=False,
        trace_dir="traces",
        max_extra_context_files=context_budget.max_extra_context_files,
        context_budget=context_budget,
    )

    started=perf_counter()

    try:
        output, trace_steps=run_review_agent(args)
        duration_ms=int((perf_counter() - started) * 1000)

        data=json.loads(output)
        if not isinstance(data, dict):
            raise ValueError(
                f"reviewer JSON top-level must be an object, got {type(data).__name__}"
            )
        if "findings" not in data:
            raise ValueError("reviewer JSON is missing required 'findings' field")
        findings=data["findings"]
        if not isinstance(findings, list):
            raise ValueError(
                f"reviewer JSON 'findings' must be a list, got {type(findings).__name__}"
            )
        invalid_finding_index = next(
            (index for index, finding in enumerate(findings) if not isinstance(finding, dict)),
            None,
        )
        if invalid_finding_index is not None:
            invalid_finding = findings[invalid_finding_index]
            raise ValueError(
                "reviewer JSON "
                f"'findings[{invalid_finding_index}]' must be an object, "
                f"got {type(invalid_finding).__name__}"
            )
        json_valid=True
        error=""
    except Exception as exc:
        duration_ms=int((perf_counter() - started) * 1000)
        findings=[]
        json_valid=False
        error=str(exc)

    actual_categories=extract_categories(findings)
    expected_categories=set(expected.get("expected_categories", []))
    should_find=expected.get("should_find", True)

    if should_find:
        tp, fp, fn = _category_counts(actual_categories, expected_categories)
        false_positive = fp > 0
        passed = (
            json_valid
            and expected_categories.issubset(actual_categories)
            and not false_positive
        )
    else:
        tp = 0
        fp = len(findings)
        fn = 0
        false_positive = len(findings) > 0
        passed = json_valid and not false_positive

    return {
        "case_id": expected.get("case_id", case_dir.name),
        "passed": passed,
        "json_valid": json_valid,
        "expected_categories": sorted(expected_categories),
        "actual_categories": sorted(actual_categories),
        "findings_count": len(findings),
        "false_positive": false_positive,
        "is_negative_case": not should_find,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "duration_ms": duration_ms,
        "error": error,
    }

def run_eval(cases_dir, repo_root, use_llm=False, llm_provider="mock"):
    case_dirs = [
        path for path in Path(cases_dir).iterdir()
        if path.is_dir()
    ]

    results = [
        run_one_case(
            case_dir=case_dir,
            repo_root=repo_root,
            use_llm=use_llm,
            llm_provider=llm_provider,
        )
        for case_dir in case_dirs
    ]

    case_count = len(results)
    passed_count = sum(1 for result in results if result["passed"])
    json_valid_count = sum(1 for result in results if result["json_valid"])
    false_positive_count = sum(1 for result in results if result["false_positive"])
    total_findings = sum(result["findings_count"] for result in results)
    total_duration = sum(result["duration_ms"] for result in results)
    total_tp = sum(result["tp"] for result in results)
    total_fp = sum(result["fp"] for result in results)
    total_fn = sum(result["fn"] for result in results)
    negative_case_count = sum(1 for result in results if result["is_negative_case"])
    false_positive_negative_case_count = sum(
        1
        for result in results
        if result["is_negative_case"] and result["false_positive"]
    )
    precision = _safe_ratio(total_tp, total_tp + total_fp)
    recall = _safe_ratio(total_tp, total_tp + total_fn)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)

    metrics = {
        "cases": case_count,
        "category_hit_rate": passed_count / case_count if case_count else 0,
        "false_positive_count": false_positive_count,
        "json_valid_rate": json_valid_count / case_count if case_count else 0,
        "average_findings": total_findings / case_count if case_count else 0,
        "average_duration_ms": total_duration / case_count if case_count else 0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": _safe_ratio(
            false_positive_negative_case_count,
            negative_case_count,
        ),
        "false_negative_count": total_fn,
        "results": results,
    }

    return metrics


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="evals/cases")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--llm-provider", default="mock", choices=["mock", "openai"])
    return parser.parse_args()


def main():
    args = parse_args()

    metrics = run_eval(
        cases_dir=args.cases,
        repo_root=args.repo,
        use_llm=args.llm,
        llm_provider=args.llm_provider,
    )

    print(f"cases: {metrics['cases']}")
    print(f"category_hit_rate: {metrics['category_hit_rate']:.2f}")
    print(f"false_positive_count: {metrics['false_positive_count']}")
    print(f"json_valid_rate: {metrics['json_valid_rate']:.2f}")
    print(f"average_findings: {metrics['average_findings']:.2f}")
    print(f"average_duration_ms: {metrics['average_duration_ms']:.0f}")
    print(f"precision: {metrics['precision']:.2f}")
    print(f"recall: {metrics['recall']:.2f}")
    print(f"f1: {metrics['f1']:.2f}")
    print(f"false_positive_rate: {metrics['false_positive_rate']:.2f}")
    print(f"false_negative_count: {metrics['false_negative_count']}")

    print()
    print(json.dumps(metrics["results"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
