'''
1. 读取 evals/cases 下的每个 case 目录
2. 每个 case 读取 input.diff 和 expected.json
3. 构造 args，调用已有的 run_review_agent(args)
4. 解析 review 输出里的 findings
5. 提取实际命中的 category
6. 和 expected_categories 对比
7. 汇总输出指标
'''

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from time import perf_counter

from .cli import run_review_agent

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

def run_one_case(
        case_dir: Path,
        repo_root: Path,
        use_llm: bool = False,
        llm_provider: str = "mock",
        ):
    expected = load_expected(case_dir)

    args=SimpleNamespace(
        diff=str(case_dir / "input.diff"),
        repo=str(repo_root),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=use_llm,
        llm_provider=llm_provider,
        trace=False,
        trace_dir="traces",
        max_extra_context_files=3,
    )

    started=perf_counter()

    try:
        output, trace_steps=run_review_agent(args)
        duration_ms=int((perf_counter() - started) * 1000)

        data=json.loads(output)
        findings=data.get("findings", [])
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
        passed = expected_categories.issubset(actual_categories)
    else:
        passed=len(findings) == 0

    false_positive=(not should_find) and (len(findings) > 0)

    return {
        "case_id": expected.get("case_id", case_dir.name),
        "passed": passed,
        "json_valid": json_valid,
        "expected_categories": sorted(expected_categories),
        "actual_categories": sorted(actual_categories),
        "findings_count": len(findings),
        "false_positive": false_positive,
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

    metrics = {
        "cases": case_count,
        "category_hit_rate": passed_count / case_count if case_count else 0,
        "false_positive_count": false_positive_count,
        "json_valid_rate": json_valid_count / case_count if case_count else 0,
        "average_findings": total_findings / case_count if case_count else 0,
        "average_duration_ms": total_duration / case_count if case_count else 0,
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

    print()
    print(json.dumps(metrics["results"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
