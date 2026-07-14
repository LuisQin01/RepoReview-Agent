'''
1. 读取 evals/cases 下的每个 case 目录
2. 每个 case 读取 input.diff 和 expected.json
3. 构造 ReviewRequest，调用 ReviewService
4. 从结构化 ReviewResult 中读取 findings
5. 提取实际命中的 category
6. 和 expected_categories 对比
7. 汇总输出指标

本模块是 RepoReview Agent 的评估器（Evaluator），用于量化审查流水线在
固定测试集上的表现。它通过对比“实际命中类别”与“期望类别”计算
precision/recall/f1/false_positive_rate 等指标，支撑模型/提示词迭代的效果回归。
设计上与 CLI/API 共用 ReviewService，保证评估与生产走同一套流水线。
'''

from __future__ import annotations

import argparse
from pathlib import Path
from time import perf_counter

import json

from .review_service import ReviewRequest, ReviewService
from .schemas import ContextBudget

def load_expected(case_dir: Path):
    """读取单个 case 目录下的期望结果 ``expected.json``。

    Args:
        case_dir: 单个 case 的目录路径。

    Returns:
        解析后的期望结果 dict，通常含 ``expected_categories``、``should_find`` 等字段。
    """
    expected_path = case_dir / "expected.json"
    return json.loads(expected_path.read_text(encoding="utf-8"))

def extract_categories(findings):
    """从 findings 列表中提取类别（category）集合。

    兼容两种 finding 表示：
    - dict 形式：优先取 ``category``，回退取 ``reason``；
    - 对象形式：优先取 ``category`` 属性，回退取 ``reason`` 属性。

    Args:
        findings: finding 列表，元素可为 dict 或对象。

    Returns:
        命中的类别字符串集合（``set``）。
    """
    categories = set()
    for finding in findings:
        if isinstance(finding, dict):
            # dict 形式：优先 category，回退 reason
            category = finding.get("category") or finding.get("reason")
        else:
            # 对象形式：优先 category 属性，回退 reason 属性
            category = getattr(finding, "category", None) or getattr(
                finding, "reason", None
            )
        if category:
            categories.add(category)
    return categories


def _category_counts(actual_categories, expected_categories):
    """Return category-level true-positive, false-positive, and false-negative counts.

    在类别集合层面计算 TP/FP/FN：
    - TP = 实际与期望的交集大小；
    - FP = 实际中有但期望中没有的类别数；
    - FN = 期望中有但实际中没有的类别数。

    Args:
        actual_categories: 实际命中的类别集合。
        expected_categories: 期望命中的类别集合。

    Returns:
        ``(tp, fp, fn)`` 三元组。
    """
    true_positive_count = len(actual_categories & expected_categories)
    false_positive_count = len(actual_categories - expected_categories)
    false_negative_count = len(expected_categories - actual_categories)
    return true_positive_count, false_positive_count, false_negative_count


def _safe_ratio(numerator, denominator):
    """安全计算比值，分母为 0 时返回 0.0，避免 ZeroDivisionError。

    Args:
        numerator: 分子。
        denominator: 分母。

    Returns:
        ``numerator / denominator``，分母为 0 时返回 ``0.0``。
    """
    return numerator / denominator if denominator else 0.0


def run_one_case(
        case_dir: Path,
        repo_root: Path,
        use_llm: bool = False,
        llm_provider: str = "mock",
        context_budget: ContextBudget | None = None,
        ):
    """执行单个评估 case 并返回其结果指标。

    单 case 执行流程：
    1. 读取 ``expected.json`` 期望结果；
    2. 构造 :class:`ReviewRequest`，调用 :class:`ReviewService.review`；
    3. 从 ``result.state.issues`` 取 findings，并校验其为 list 且每个元素
       是 dict 或具备 ``category`` 属性（校验失败走降级：``json_valid=False``、
       ``findings=[]``、记录 error）；
    4. 按期望判定 ``passed``：
       - 正向 case（``should_find=True``）：要求期望类别是实际类别的子集且无 FP；
       - 负向 case（``should_find=False``）：要求不产生任何 finding。

    Args:
        case_dir: 单个 case 目录路径。
        repo_root: 仓库根路径，用于读取额外上下文文件。
        use_llm: 是否启用 LLM 审查。
        llm_provider: LLM provider 名称。
        context_budget: 上下文预算；为 None 时使用默认 :class:`ContextBudget`。

    Returns:
        含该 case 全部评估指标的 dict（passed/json_valid/tp/fp/fn/duration_ms 等）。
    """
    expected = load_expected(case_dir)
    context_budget = context_budget or ContextBudget()

    # 构造审查请求：每个 case 以 input.diff 作为输入
    request = ReviewRequest(
        diff_path=str(case_dir / "input.diff"),
        repo_root=str(repo_root),
        output_format="json",
        use_llm=use_llm,
        context_budget=context_budget,
        llm_provider=llm_provider,
        trace_enabled=False,
    )

    started=perf_counter()

    try:
        result = ReviewService().review(request)
        duration_ms=int((perf_counter() - started) * 1000)

        # 从结构化结果中取 findings（state.issues）
        state = getattr(result, "state", None)
        findings = getattr(state, "issues", None)
        # 校验 findings 必须是 list
        if not isinstance(findings, list):
            raise ValueError(
                "review result issues must be a list, got "
                f"{type(findings).__name__}"
            )
        # 校验每个 finding 必须是 dict 或具备 category 属性的对象
        invalid_finding_index = next(
            (
                index
                for index, finding in enumerate(findings)
                if not isinstance(finding, dict) and not hasattr(finding, "category")
            ),
            None,
        )
        if invalid_finding_index is not None:
            invalid_finding = findings[invalid_finding_index]
            raise ValueError(
                "review result "
                f"issues[{invalid_finding_index}] must be a finding object, "
                f"got {type(invalid_finding).__name__}"
            )
        # 走到这说明结果合法
        json_valid=True
        error=""
    except Exception as exc:
        # 降级处理：任何异常都视为本次 case 失败，记录耗时与错误信息，findings 置空
        duration_ms=int((perf_counter() - started) * 1000)
        findings=[]
        json_valid=False
        error=str(exc)

    # 提取实际命中类别与期望类别
    actual_categories=extract_categories(findings)
    expected_categories=set(expected.get("expected_categories", []))
    should_find=expected.get("should_find", True)

    if should_find:
        # 正向 case：要求期望类别全部命中（子集关系）且无任何误报
        tp, fp, fn = _category_counts(actual_categories, expected_categories)
        false_positive = fp > 0
        passed = (
            json_valid
            and expected_categories.issubset(actual_categories)
            and not false_positive
        )
    else:
        # 负向 case：不应产生任何 finding，所有 finding 均视为误报
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
    """运行全部评估 case 并汇总指标。

    遍历 ``cases_dir`` 下所有子目录（每个子目录即一个 case），逐个调用
    :func:`run_one_case`，再汇总计算 precision/recall/f1/category_hit_rate/
    false_positive_rate 等指标。

    Args:
        cases_dir: 存放 case 子目录的路径。
        repo_root: 仓库根路径。
        use_llm: 是否启用 LLM 审查。
        llm_provider: LLM provider 名称。

    Returns:
        含全局指标与各 case 明细结果的 dict。
    """
    # 收集所有 case 目录（仅取子目录，忽略普通文件）
    case_dirs = [
        path for path in Path(cases_dir).iterdir()
        if path.is_dir()
    ]

    # 逐个执行 case；这里未做并发，保证评估过程可复现且便于排查
    results = [
        run_one_case(
            case_dir=case_dir,
            repo_root=repo_root,
            use_llm=use_llm,
            llm_provider=llm_provider,
        )
        for case_dir in case_dirs
    ]

    # —— 汇总各维度计数 ——
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
    # 负向 case 中产生误报的数量，用于计算负向误报率
    false_positive_negative_case_count = sum(
        1
        for result in results
        if result["is_negative_case"] and result["false_positive"]
    )
    # precision/recall/f1 均通过 _safe_ratio 安全计算，避免除零
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
        # 负向误报率 = 负向 case 中误报数 / 负向 case 总数
        "false_positive_rate": _safe_ratio(
            false_positive_negative_case_count,
            negative_case_count,
        ),
        "false_negative_count": total_fn,
        "results": results,
    }

    return metrics


def parse_args():
    """解析评估器 CLI 参数。

    Returns:
        解析后的 ``argparse.Namespace``。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="evals/cases")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--llm-provider", default="mock", choices=["mock", "openai"])
    return parser.parse_args()


def main():
    """评估器主入口：解析参数 → 跑全部 case → 打印指标与明细。"""
    args = parse_args()

    metrics = run_eval(
        cases_dir=args.cases,
        repo_root=args.repo,
        use_llm=args.llm,
        llm_provider=args.llm_provider,
    )

    # 打印全局汇总指标
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

    # 空行分隔后打印每个 case 的明细结果，便于定位失败 case
    print()
    print(json.dumps(metrics["results"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
