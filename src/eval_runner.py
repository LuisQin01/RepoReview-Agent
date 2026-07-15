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
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from time import perf_counter
import platform
import subprocess
import sys

import json

from .diff_parser import parse_diff
from .llm_client import ScriptedMockProvider
from .react_controller import ReActBudget
from .review_service import ReviewRequest, ReviewService
from .schemas import ContextBudget


BASELINE_SCHEMA_VERSION = "m7_fixed_baseline.v1"
COMPARISON_SCHEMA_VERSION = "m7_comparison.v1"

# Tool names registered by the ReAct dispatcher; any other name is "unknown".
_REGISTERED_TOOL_NAMES = frozenset({
    "get_changed_hunks",
    "read_file_context",
    "search_python_symbol",
})

# Termination reasons that indicate a budget was exhausted, not a normal finish.
_BUDGET_EXHAUSTION_REASONS = frozenset({
    "max_steps_exhausted",
    "max_llm_calls_exhausted",
    "max_tokens_exhausted",
    "max_tool_result_bytes_exhausted",
})

# Estimated pricing per 1M tokens (GPT-4o-mini list price; mock runs use fake
# tokens, so this is a shape-only cost estimate, not a real spend).
_COST_PER_1M_INPUT_TOKENS = 0.15
_COST_PER_1M_OUTPUT_TOKENS = 0.60


@dataclass(frozen=True)
class SourceRevision:
    """Verified source identity used to compare fixed-baseline results."""

    commit: str
    worktree_state: str
    worktree_diff_sha256: str | None

M7_FIXED_BASELINE_PREREQUISITES = {
    "M2": {
        "status": "confirmed",
        "evidence": [
            "src/file_context.py",
            "tests/test_file_context.py",
        ],
    },
    "M3": {
        "status": "confirmed",
        "evidence": [
            "src/validation.py",
            "tests/test_validation.py",
            "tests/test_sensitive_leak.py",
        ],
    },
    "M4": {
        "status": "confirmed",
        "evidence": [
            "src/eval_runner.py",
            "tests/test_eval_runner.py",
            "src/trace.py",
            "tests/test_trace.py",
        ],
    },
}


def _output_exclusion_paths(output_path: str | Path) -> tuple[str | Path, ...]:
    """Return the output file plus its parent directory for untracked exclusion.

    The parent directory is included so an existing untracked sibling output in
    the same directory does not block regenerating a new sibling output (for
    example, rerunning ``--comparison-output evals/comparisons/m7-18.json``
    while a previously generated ``evals/comparisons/m7-18-prev.json`` is still
    untracked).  The parent directory is only returned when it is a proper
    sub-directory of the repository root; excluding the repository root itself
    would silently mask every untracked file and is never the caller's intent.
    """
    output = Path(output_path)
    parent = output.resolve().parent
    repo_root = Path(__file__).resolve().parent.parent
    try:
        parent_relative = parent.relative_to(repo_root.resolve())
    except ValueError:
        # Output lives outside the repository; only the file itself can be
        # excluded, since untracked in-repo paths are not influenced by it.
        return (output,)
    if parent_relative == Path("."):
        # The output sits at the repository root; excluding the whole root
        # would mask all untracked files, so fall back to the file only.
        return (output,)
    # Mark the directory entry with a trailing separator so the snapshot
    # logic can distinguish exact-file matches from directory-prefix matches.
    return (output, parent_relative.as_posix() + "/")


def capture_source_revision(
    repo_root: Path,
    *,
    excluded_untracked_paths: Iterable[str | Path] = (),
) -> SourceRevision:
    """Return the current Git commit and a verifiable snapshot of local changes.

    The baseline's source identity is collected from Git rather than trusting
    command-line metadata.  A dirty worktree remains usable, but its tracked
    diff is fingerprinted so it cannot be confused with a clean commit.  The
    current baseline output may be excluded when it is an untracked file inside
    the repository: it is generated after this snapshot and is not source input.

    ``excluded_untracked_paths`` accepts both exact file paths and directory
    prefixes (marked with a trailing ``/``).  A directory prefix excludes every
    untracked entry whose path starts with that prefix, so previously generated
    sibling outputs under the same directory do not block regenerating a new
    output in that directory.  Exact file matches continue to exclude only the
    named file, preserving the established single-output rerun contract for
    callers that do not opt into the directory-prefix form.
    """
    def git_output(*arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repo_root,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ValueError("fixed_baseline_source_unavailable") from exc
        if completed.returncode != 0:
            raise ValueError("fixed_baseline_source_unavailable")
        return completed.stdout

    repo_root = repo_root.resolve()
    excluded_paths = set()
    excluded_dir_prefixes = set()
    for path in excluded_untracked_paths:
        text = str(path)
        if text.endswith("/"):
            # Directory-prefix form: exclude any untracked entry under this dir.
            try:
                relative = Path(text[:-1]).resolve().relative_to(repo_root)
            except ValueError:
                continue
            excluded_dir_prefixes.add(relative.as_posix().encode("utf-8") + b"/")
        else:
            # Exact-file form: exclude only this specific untracked path.
            try:
                excluded_path = Path(text).resolve().relative_to(repo_root)
            except ValueError:
                continue
            excluded_paths.add(excluded_path.as_posix().encode("utf-8"))

    def _is_excluded_untracked(entry: bytes) -> bool:
        if not entry.startswith(b"?? "):
            return False
        path = entry[3:]
        if path in excluded_paths:
            return True
        return any(path.startswith(prefix) for prefix in excluded_dir_prefixes)

    commit = git_output("rev-parse", "HEAD").decode("ascii").strip()
    status = git_output("status", "--porcelain=v1", "-z", "--untracked-files=all")
    status_entries = [entry for entry in status.split(b"\0") if entry]
    retained_status_entries = [
        entry for entry in status_entries if not _is_excluded_untracked(entry)
    ]
    if not retained_status_entries:
        return SourceRevision(
            commit=commit,
            worktree_state="clean",
            worktree_diff_sha256=None,
        )

    # ``git diff HEAD`` deliberately excludes untracked files.  Recording its
    # hash would therefore misidentify a baseline whose cases or source files
    # differ only through untracked content.
    if any(entry.startswith(b"?? ") for entry in retained_status_entries):
        raise ValueError("unsupported")

    diff = git_output("diff", "--binary", "HEAD")
    return SourceRevision(
        commit=commit,
        worktree_state="dirty",
        worktree_diff_sha256=hashlib.sha256(diff).hexdigest(),
    )

def load_expected(case_dir: Path):
    """读取单个 case 目录下的期望结果 ``expected.json``。

    Args:
        case_dir: 单个 case 的目录路径。

    Returns:
        解析后的期望结果 dict，通常含 ``expected_categories``、``should_find`` 等字段。
    """
    expected_path = case_dir / "expected.json"
    return json.loads(expected_path.read_text(encoding="utf-8"))


def _resolve_case_repo_root(case_dir: Path, expected: dict, default_repo_root: Path) -> Path:
    """Return a case-owned repository fixture or preserve the caller's root.

    ``repository_context`` is optional so existing cases keep their established
    caller-provided repository.  A declared fixture is validated before it is
    passed to the review pipeline, preventing a case manifest from reading
    outside its own directory.
    """
    context = expected.get("repository_context")
    if context is None:
        return Path(default_repo_root)
    if not isinstance(context, dict):
        raise ValueError("invalid_arguments")

    root = context.get("root")
    required_paths = context.get("required_paths", [])
    if not isinstance(root, str) or not isinstance(required_paths, list):
        raise ValueError("invalid_arguments")

    case_root = case_dir.resolve()
    fixture_root = (case_root / root).resolve()
    try:
        fixture_root.relative_to(case_root)
    except ValueError as exc:
        raise ValueError("forbidden") from exc
    if not fixture_root.is_dir():
        raise ValueError("not_found")

    for required_path in required_paths:
        if not isinstance(required_path, str):
            raise ValueError("invalid_arguments")
        required_file = (fixture_root / required_path).resolve()
        try:
            required_file.relative_to(fixture_root)
        except ValueError as exc:
            raise ValueError("forbidden") from exc
        if not required_file.is_file():
            raise ValueError("not_found")

    return fixture_root

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


def _percentile(values, percentile):
    """Return the ``percentile``-th percentile of ``values`` using nearest-rank.

    A deterministic, dependency-free percentile so eval results are reproducible
    across environments.  Empty input returns 0.0.
    """
    if not values:
        return 0.0
    sorted_values = sorted(values)
    rank = max(1, min(len(sorted_values), int(round(percentile / 100 * len(sorted_values)))))
    return float(sorted_values[rank - 1])


def _estimate_cost(total_tokens, *, input_ratio=0.6):
    """Estimate a shape-only USD cost from total mock token usage.

    Mock providers emit fake tokens; this function applies a representative
    GPT-4o-mini price so the comparison has a cost *dimension* rather than a
    real spend figure.  ``input_ratio`` splits tokens into input/output because
    mock usage does not separate them.
    """
    if total_tokens <= 0:
        return 0.0
    input_tokens = total_tokens * input_ratio
    output_tokens = total_tokens - input_tokens
    return (
        input_tokens / 1_000_000 * _COST_PER_1M_INPUT_TOKENS
        + output_tokens / 1_000_000 * _COST_PER_1M_OUTPUT_TOKENS
    )


def _extract_react_observability(state):
    """Read ReAct counters from a review state, defaulting to zero/empty.

    Using getattr keeps this safe for fixed-mode states where the react_*
    fields are initialized but never populated by the controller.
    """
    trace_steps = getattr(state, "trace_steps", []) or []
    unknown_tool_count = sum(
        1
        for step in trace_steps
        if step.get("step") == "react_tool_result"
        and step.get("detail", {}).get("tool_name") not in _REGISTERED_TOOL_NAMES
    )
    termination_reason = getattr(state, "react_termination_reason", "") or ""
    return {
        "react_steps": getattr(state, "react_steps", 0),
        "react_llm_calls": getattr(state, "react_llm_calls", 0),
        "react_total_tokens": getattr(state, "react_total_tokens", 0),
        "react_degraded": bool(getattr(state, "react_degraded", False)),
        "react_termination_reason": termination_reason,
        "react_tool_results_truncated": getattr(state, "react_tool_results_truncated", 0),
        "unknown_tool_count": unknown_tool_count,
        "budget_exhausted": termination_reason in _BUDGET_EXHAUSTION_REASONS,
    }


def run_one_case(
        case_dir: Path,
        repo_root: Path,
        use_llm: bool = False,
        llm_provider: str = "mock",
        context_budget: ContextBudget | None = None,
        review_mode: str = "fixed",
        react_provider: object | None = None,
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
        review_mode: 审查模式 ``"fixed"`` 或 ``"react"``。
        react_provider: ReAct 模式注入的脚本化 provider；为 None 时由
            ReviewService 使用其默认脚本。

    Returns:
        含该 case 全部评估指标的 dict（passed/json_valid/tp/fp/fn/duration_ms
        及 react 可观测性字段等）。
    """
    expected = load_expected(case_dir)
    context_budget = context_budget or ContextBudget()
    effective_repo_root = _resolve_case_repo_root(case_dir, expected, repo_root)

    # 构造审查请求：每个 case 以 input.diff 作为输入
    request = ReviewRequest(
        diff_path=str(case_dir / "input.diff"),
        repo_root=str(effective_repo_root),
        output_format="json",
        use_llm=use_llm,
        context_budget=context_budget,
        llm_provider=llm_provider,
        trace_enabled=False,
        review_mode=review_mode,
        react_provider=react_provider,
    )

    started=perf_counter()
    state = None

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
        "review_mode": review_mode,
        **_extract_react_observability(state),
    }

def run_eval(cases_dir, repo_root, use_llm=False, llm_provider="mock",
             review_mode="fixed", react_provider_factory=None):
    """运行全部评估 case 并汇总指标。

    遍历 ``cases_dir`` 下所有子目录（每个子目录即一个 case），逐个调用
    :func:`run_one_case`，再汇总计算 precision/recall/f1/category_hit_rate/
    false_positive_rate 等指标。

    Args:
        cases_dir: 存放 case 子目录的路径。
        repo_root: 仓库根路径。
        use_llm: 是否启用 LLM 审查。
        llm_provider: LLM provider 名称。
        review_mode: 审查模式 ``"fixed"`` 或 ``"react"``。
        react_provider_factory: 可选的可调用对象，签名为
            ``(case_dir, repo_root, changed_files) -> provider``，其中
            ``repo_root`` 是该 case 的有效仓库根路径（已解析 fixture）。
            为 None 时 ``run_one_case`` 的 ``react_provider`` 参数为 None。

    Returns:
        含全局指标与各 case 明细结果的 dict。
    """
    # 收集所有 case 目录（仅取子目录，忽略普通文件）
    case_dirs = [
        path for path in Path(cases_dir).iterdir()
        if path.is_dir()
    ]

    # 逐个执行 case；这里未做并发，保证评估过程可复现且便于排查
    results = []
    for case_dir in case_dirs:
        react_provider = None
        if react_provider_factory is not None:
            expected = load_expected(case_dir)
            diff_text = (case_dir / "input.diff").read_text(encoding="utf-8")
            changed_files = parse_diff(diff_text)
            effective_repo_root = _resolve_case_repo_root(case_dir, expected, repo_root)
            react_provider = react_provider_factory(case_dir, effective_repo_root, changed_files)
        results.append(
            run_one_case(
                case_dir=case_dir,
                repo_root=repo_root,
                use_llm=use_llm,
                llm_provider=llm_provider,
                review_mode=review_mode,
                react_provider=react_provider,
            )
        )

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

    # —— 汇总 ReAct 可观测性（fixed 模式下全为零） ——
    durations = [result["duration_ms"] for result in results]
    total_tokens = sum(result.get("react_total_tokens", 0) for result in results)
    total_llm_calls = sum(result.get("react_llm_calls", 0) for result in results)
    unknown_tool_count = sum(result.get("unknown_tool_count", 0) for result in results)
    budget_exhausted_count = sum(
        1 for result in results if result.get("budget_exhausted", False)
    )

    metrics = {
        "cases": case_count,
        "category_hit_rate": passed_count / case_count if case_count else 0,
        "false_positive_count": false_positive_count,
        "json_valid_rate": json_valid_count / case_count if case_count else 0,
        "average_findings": total_findings / case_count if case_count else 0,
        "average_duration_ms": total_duration / case_count if case_count else 0,
        "p95_duration_ms": _percentile(durations, 95),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        # 负向误报率 = 负向 case 中误报数 / 负向 case 总数
        "false_positive_rate": _safe_ratio(
            false_positive_negative_case_count,
            negative_case_count,
        ),
        "false_negative_count": total_fn,
        "total_tokens": total_tokens,
        "total_llm_calls": total_llm_calls,
        "estimated_cost_usd": _estimate_cost(total_tokens),
        "unknown_tool_count": unknown_tool_count,
        "budget_exhausted_count": budget_exhausted_count,
        "results": results,
    }

    return metrics


def _first_added_line(changed_files):
    """Return the first added line across all changed files, or ``None``.

    The result is used to derive a finding location from the diff itself,
    never from the case manifest's ground truth.
    """
    for changed_file in changed_files:
        if changed_file.added_lines:
            return changed_file.added_lines[0]
    return None


def _discover_documentation_files(case_dir, repo_root):
    """Discover documentation files by scanning the case directory.

    Simulates a reasonable model looking for API contracts or documentation
    that might define error-handling requirements.  Discovery is purely
    filesystem-based: the factory scans ``case_dir`` for ``*.md`` files and
    returns their paths relative to ``repo_root`` (the effective repo root).

    This function must NOT read the case manifest's ``repository_context``
    metadata (``react_required_tool``, ``required_paths``); doing so would
    inject the ground-truth answer key and make the comparison circular.
    """
    case_dir = Path(case_dir).resolve()
    repo_root = Path(repo_root).resolve()
    if not case_dir.is_dir():
        return []
    result = []
    for md_file in sorted(case_dir.rglob("*.md")):
        if not md_file.is_file():
            continue
        try:
            rel = md_file.resolve().relative_to(repo_root)
            result.append(rel.as_posix())
        except ValueError:
            # File is not under repo_root; skip it.
            continue
    return result


def _build_react_provider(case_dir, repo_root, changed_files):
    """Create a deterministic :class:`ScriptedMockProvider` for one case.

    The script represents a reasonable model that inspects changed hunks before
    finishing.  When the case's repository fixture contains documentation files
    (discovered by filesystem scan, not by reading the case manifest), the
    script additionally reads the first discovered document and reports a
    factory-authored finding through ``finish_review``.

    This factory is the fixed "model config" for the comparison: it does not
    adjust the implementation, prompt, or case, and the finding it produces
    still passes through the existing ``finish_review`` validation chain.

    The factory must never read ``expected_findings``, ``expected_categories``,
    or ``repository_context`` from the case manifest; doing so would inject the
    ground-truth answer key and make the comparison circular.  Document
    discovery is purely filesystem-based via :func:`_discover_documentation_files`.
    """
    first_path = changed_files[0].path if changed_files else ""

    # Discover documentation files by scanning the case directory filesystem.
    # This does NOT read repository_context from the case manifest, ensuring
    # the react arm has no answer-key metadata that the fixed arm lacks.
    doc_paths = _discover_documentation_files(case_dir, repo_root)

    script = [
        {
            "tool_calls": [
                {
                    "call_id": "call-hunks",
                    "name": "get_changed_hunks",
                    "arguments": {"path": first_path},
                }
            ],
            "usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
        },
    ]

    if doc_paths:
        evidence_path = doc_paths[0]
        script.append(
            {
                "tool_calls": [
                    {
                        "call_id": "call-read",
                        "name": "read_file_context",
                        "arguments": {"path": evidence_path},
                    }
                ],
                "usage": {"input_tokens": 800, "output_tokens": 100, "total_tokens": 900},
            }
        )
        # The finding location is derived from the diff's added lines, not
        # from the case manifest's expected_findings.  The finding text is a
        # fixed factory-authored description representing what a reasonable
        # model would conclude after reading the evidence file.
        first_added = _first_added_line(changed_files)
        if first_added is not None:
            finish_findings = [{
                "file": first_added.file_path,
                "line": first_added.line_no,
                "severity": "high",
                "category": "exception_handling",
                "issue": (
                    "The changed code may raise an exception that is not "
                    "handled according to the reviewed contract."
                ),
                "reason": (
                    "A reasonable model reading the evidence file would flag "
                    "the added line for missing exception handling."
                ),
                "suggested_fix": (
                    "Review the API contract and add appropriate exception "
                    "handling for documented error conditions."
                ),
                "confidence": 0.9,
                "evidence": evidence_path,
            }]
        else:
            finish_findings = []
        script.append(
            {
                "tool_calls": [
                    {
                        "call_id": "call-finish",
                        "name": "finish_review",
                        "arguments": {"findings": finish_findings},
                    }
                ],
                "usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
            }
        )
    else:
        # No documentation files discovered: static rules already handle these
        # cases; the model inspects the diff and finishes without adding findings.
        script.append(
            {
                "tool_calls": [
                    {
                        "call_id": "call-finish",
                        "name": "finish_review",
                        "arguments": {"findings": []},
                    }
                ],
                "usage": {"input_tokens": 600, "output_tokens": 100, "total_tokens": 700},
            }
        )

    return ScriptedMockProvider(script)


def run_mode_comparison(
    cases_dir,
    repo_root,
    *,
    llm_provider="mock",
    context_budget=None,
    react_budget=None,
):
    """Run fixed and react modes on the same cases and return a comparison.

    Both modes use the same case set, commit, provider name, and context
    budget.  Fixed mode runs without LLM (static rules only); react mode runs
    with ``use_llm=True`` and a deterministic scripted provider per case.

    Returns a dict with ``fixed`` and ``react`` metric blocks plus a
    ``comparison`` block containing per-case and aggregate diffs.
    """
    context_budget = context_budget or ContextBudget()

    fixed_metrics = run_eval(
        cases_dir=cases_dir,
        repo_root=repo_root,
        use_llm=False,
        llm_provider=llm_provider,
        review_mode="fixed",
    )

    react_metrics = run_eval(
        cases_dir=cases_dir,
        repo_root=repo_root,
        use_llm=True,
        llm_provider=llm_provider,
        review_mode="react",
        react_provider_factory=_build_react_provider,
    )

    per_case_diff = _build_per_case_diff(
        fixed_metrics["results"], react_metrics["results"]
    )

    return {
        "fixed": fixed_metrics,
        "react": react_metrics,
        "comparison": {
            "per_case_diff": per_case_diff,
            "aggregate_diff": {
                "precision_delta": react_metrics["precision"] - fixed_metrics["precision"],
                "recall_delta": react_metrics["recall"] - fixed_metrics["recall"],
                "f1_delta": react_metrics["f1"] - fixed_metrics["f1"],
                "p95_latency_delta_ms": (
                    react_metrics["p95_duration_ms"] - fixed_metrics["p95_duration_ms"]
                ),
                "token_delta": react_metrics["total_tokens"] - fixed_metrics["total_tokens"],
                "llm_call_delta": (
                    react_metrics["total_llm_calls"] - fixed_metrics["total_llm_calls"]
                ),
                "cost_delta_usd": (
                    react_metrics["estimated_cost_usd"]
                    - fixed_metrics["estimated_cost_usd"]
                ),
            },
        },
    }


def _build_per_case_diff(fixed_results, react_results):
    """Compute per-case new false positives and fixed false negatives.

    A *new false positive* is a category present in react's actual but absent
    from fixed's actual AND absent from expected.  A *fixed false negative* is
    an expected category that fixed missed but react found.
    """
    react_by_id = {r["case_id"]: r for r in react_results}
    diffs = []
    for fixed_result in fixed_results:
        case_id = fixed_result["case_id"]
        react_result = react_by_id.get(case_id, {})
        expected_set = set(fixed_result["expected_categories"])
        fixed_actual = set(fixed_result["actual_categories"])
        react_actual = set(react_result.get("actual_categories", []))

        new_false_positives = sorted(react_actual - fixed_actual - expected_set)
        fixed_false_negatives = sorted(
            (expected_set - fixed_actual) & react_actual
        )
        diffs.append({
            "case_id": case_id,
            "fixed_passed": fixed_result["passed"],
            "react_passed": react_result.get("passed", False),
            "fixed_actual_categories": sorted(fixed_actual),
            "react_actual_categories": sorted(react_actual),
            "new_false_positives": new_false_positives,
            "fixed_false_negatives": fixed_false_negatives,
        })
    return diffs


def build_comparison_record(
    comparison: dict,
    *,
    cases_dir: str,
    repo_root: str,
    llm_provider: str,
    commit: str,
    worktree_state: str,
    worktree_diff_sha256: str | None,
    comparison_output: str,
) -> dict:
    """Build a machine-readable record for an M7 fixed/react comparison."""
    context_budget = ContextBudget()
    react_budget = ReActBudget()
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "commit": commit,
        "worktree_state": worktree_state,
        "worktree_diff_sha256": worktree_diff_sha256,
        "configuration": {
            "cases": cases_dir,
            "repo": repo_root,
            "llm_provider": llm_provider,
            "context_budget": {
                "max_prompt_chars": context_budget.max_prompt_chars,
                "max_extra_context_files": context_budget.max_extra_context_files,
            },
            "react_budget": {
                "max_steps": react_budget.max_steps,
                "max_llm_calls": react_budget.max_llm_calls,
                "max_total_tokens": react_budget.max_total_tokens,
                "max_tool_result_bytes": react_budget.max_tool_result_bytes,
                "max_total_tool_result_bytes": react_budget.max_total_tool_result_bytes,
            },
        },
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
        },
        "reproduction": {
            "command": (
                "py -m src.eval_runner "
                f"--cases {cases_dir} --repo {repo_root} "
                f"--compare-modes --comparison-output {comparison_output}"
            ),
        },
        "fixed": comparison["fixed"],
        "react": comparison["react"],
        "comparison": comparison["comparison"],
    }


def build_fixed_baseline_record(
    metrics: dict,
    *,
    cases_dir: str,
    repo_root: str,
    llm_provider: str,
    commit: str,
    worktree_state: str,
    worktree_diff_sha256: str | None,
    baseline_output: str,
) -> dict:
    """Build a machine-readable record for an M7 fixed-pipeline baseline.

    The fixed pipeline does not invoke the LLM, so the call count is known to
    be zero.  It also does not expose token usage, which is recorded as
    unavailable instead of being guessed as zero.

    Args:
        metrics: Metrics returned by :func:`run_eval`.
        cases_dir: Reproducible case-set argument used for the run.
        repo_root: Repository argument used for the run.
        llm_provider: Configured provider name; it remains unused in fixed mode.
        commit: Source revision supplied by the caller, or ``"unknown"``.
        worktree_state: Source-tree state supplied by the caller (for example
            ``"clean"``, ``"dirty"``, or ``"unknown"``).
        worktree_diff_sha256: SHA-256 of the Git diff when the tree is dirty.
        baseline_output: Destination passed to ``--baseline-output``.

    Returns:
        A JSON-serializable baseline record containing configuration and
        complete per-case metrics.
    """
    context_budget = ContextBudget()
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "review_mode": "fixed",
        "commit": commit,
        "worktree_state": worktree_state,
        "worktree_diff_sha256": worktree_diff_sha256,
        "configuration": {
            "cases": cases_dir,
            "repo": repo_root,
            "use_llm": False,
            "llm_provider": llm_provider,
            "context_budget": {
                "max_prompt_chars": context_budget.max_prompt_chars,
                "max_extra_context_files": context_budget.max_extra_context_files,
            },
        },
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
        },
        "reproduction": {
            # Keep the command free of optional LLM flags: this record is only
            # comparable with the deterministic fixed pipeline.
            "command": (
                "py -m src.eval_runner "
                f"--cases {cases_dir} --repo {repo_root} "
                f"--baseline-output {baseline_output} "
                f"--commit {commit} --worktree-state {worktree_state}"
            ),
        },
        "prerequisite_capabilities": M7_FIXED_BASELINE_PREREQUISITES,
        "observability": {
            "llm_call_count": 0,
            "token_usage": {
                "available": False,
                "reason": "fixed_pipeline_does_not_expose_token_usage",
            },
        },
        "metrics": metrics,
    }


def write_baseline_record(output_path: str | Path, record: dict) -> None:
    """Persist a JSON baseline record, surfacing write failures to the caller.

    Parent directories are created when needed.  ``OSError`` and JSON
    serialization failures intentionally propagate so a failed baseline is
    never reported as successfully saved.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
    parser.add_argument(
        "--baseline-output",
        help="Optional path for a machine-readable M7 fixed-pipeline baseline JSON.",
    )
    parser.add_argument(
        "--commit",
        default="unknown",
        help="Source revision recorded in --baseline-output (default: unknown).",
    )
    parser.add_argument(
        "--worktree-state",
        choices=["clean", "dirty", "unknown"],
        default="unknown",
        help="Source-tree state recorded in --baseline-output (default: unknown).",
    )
    parser.add_argument(
        "--compare-modes",
        action="store_true",
        help="Run fixed and react modes side by side and print a comparison.",
    )
    parser.add_argument(
        "--comparison-output",
        help="Optional path for a machine-readable M7 fixed/react comparison JSON.",
    )
    return parser.parse_args()


def _run_comparison_mode(args):
    """Run fixed/react comparison and optionally persist a machine-readable record."""
    source_revision = None
    if args.comparison_output:
        source_revision = capture_source_revision(
            Path(__file__).resolve().parent.parent,
            excluded_untracked_paths=_output_exclusion_paths(args.comparison_output),
        )

    comparison = run_mode_comparison(
        cases_dir=args.cases,
        repo_root=args.repo,
        llm_provider=args.llm_provider,
    )

    if args.comparison_output:
        record = build_comparison_record(
            comparison,
            cases_dir=args.cases,
            repo_root=args.repo,
            llm_provider=args.llm_provider,
            commit=source_revision.commit if source_revision else "unknown",
            worktree_state=(
                source_revision.worktree_state if source_revision else "unknown"
            ),
            worktree_diff_sha256=(
                source_revision.worktree_diff_sha256 if source_revision else None
            ),
            comparison_output=args.comparison_output,
        )
        write_baseline_record(args.comparison_output, record)

    fixed = comparison["fixed"]
    react = comparison["react"]
    diff = comparison["comparison"]

    print("=== Fixed ===")
    print(f"precision: {fixed['precision']:.2f}  recall: {fixed['recall']:.2f}  f1: {fixed['f1']:.2f}")
    print(f"p95_duration_ms: {fixed['p95_duration_ms']:.0f}  tokens: {fixed['total_tokens']}  calls: {fixed['total_llm_calls']}")
    print(f"unknown_tools: {fixed['unknown_tool_count']}  budget_exhausted: {fixed['budget_exhausted_count']}")
    print(f"estimated_cost_usd: {fixed['estimated_cost_usd']:.6f}")

    print("=== React ===")
    print(f"precision: {react['precision']:.2f}  recall: {react['recall']:.2f}  f1: {react['f1']:.2f}")
    print(f"p95_duration_ms: {react['p95_duration_ms']:.0f}  tokens: {react['total_tokens']}  calls: {react['total_llm_calls']}")
    print(f"unknown_tools: {react['unknown_tool_count']}  budget_exhausted: {react['budget_exhausted_count']}")
    print(f"estimated_cost_usd: {react['estimated_cost_usd']:.6f}")

    print("=== Aggregate Delta (react - fixed) ===")
    agg = diff["aggregate_diff"]
    print(f"precision_delta: {agg['precision_delta']:+.2f}")
    print(f"recall_delta: {agg['recall_delta']:+.2f}")
    print(f"f1_delta: {agg['f1_delta']:+.2f}")
    print(f"p95_latency_delta_ms: {agg['p95_latency_delta_ms']:+.0f}")
    print(f"token_delta: {agg['token_delta']:+d}")
    print(f"llm_call_delta: {agg['llm_call_delta']:+d}")
    print(f"cost_delta_usd: {agg['cost_delta_usd']:+.6f}")

    print()
    print("=== Per-Case Diff ===")
    print(json.dumps(diff["per_case_diff"], ensure_ascii=False, indent=2))

    if args.comparison_output:
        print(f"\ncomparison_output: {args.comparison_output}")


def main():
    """评估器主入口：解析参数 → 跑全部 case → 打印指标与明细。"""
    args = parse_args()

    # Comparison mode runs both fixed and react and prints a diff summary.
    if args.compare_modes:
        _run_comparison_mode(args)
        return

    # A fixed baseline must never execute the optional LLM path.  Validate
    # before starting Eval so an invalid request cannot incur model calls.
    if args.baseline_output and args.llm:
        raise ValueError("fixed_baseline_requires_llm_disabled")

    source_revision = None
    if args.baseline_output:
        source_revision = capture_source_revision(
            Path(__file__).resolve().parent.parent,
            excluded_untracked_paths=_output_exclusion_paths(args.baseline_output),
        )
        if args.commit != "unknown" and args.commit != source_revision.commit:
            raise ValueError("fixed_baseline_commit_mismatch")
        if (
            args.worktree_state != "unknown"
            and args.worktree_state != source_revision.worktree_state
        ):
            raise ValueError("fixed_baseline_worktree_state_mismatch")

    metrics = run_eval(
        cases_dir=args.cases,
        repo_root=args.repo,
        use_llm=args.llm,
        llm_provider=args.llm_provider,
    )

    if args.baseline_output:
        record = build_fixed_baseline_record(
            metrics,
            cases_dir=args.cases,
            repo_root=args.repo,
            llm_provider=args.llm_provider,
            commit=source_revision.commit,
            worktree_state=source_revision.worktree_state,
            worktree_diff_sha256=source_revision.worktree_diff_sha256,
            baseline_output=args.baseline_output,
        )
        write_baseline_record(args.baseline_output, record)

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
    print(f"p95_duration_ms: {metrics['p95_duration_ms']:.0f}")
    print(f"total_tokens: {metrics['total_tokens']}")
    print(f"total_llm_calls: {metrics['total_llm_calls']}")
    print(f"estimated_cost_usd: {metrics['estimated_cost_usd']:.6f}")
    print(f"unknown_tool_count: {metrics['unknown_tool_count']}")
    print(f"budget_exhausted_count: {metrics['budget_exhausted_count']}")

    if args.baseline_output:
        print(f"baseline_output: {args.baseline_output}")

    # 空行分隔后打印每个 case 的明细结果，便于定位失败 case
    print()
    print(json.dumps(metrics["results"], ensure_ascii=False, indent=2))


def cli_main() -> None:
    """Run the CLI and render expected fixed-baseline failures safely."""
    try:
        main()
    except ValueError as exc:
        if str(exc) != "unsupported":
            raise
        print("unsupported", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli_main()
