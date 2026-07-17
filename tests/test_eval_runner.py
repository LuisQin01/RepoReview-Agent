"""评估器（src/eval_runner.py）单元测试。

本文件覆盖 RepoReview Agent 的评估流水线，验证其在不同输入条件下能否正确
判别单条用例的通过/失败状态、统计 TP/FP/FN 计数，并最终聚合出
precision / recall / f1 / hit_rate / false_positive_rate 等指标。

测试策略：
    - 使用 make_case 辅助函数在临时目录中生成 case 目录与 expected.json，
      以最小成本构造可重复的评估用例，无需依赖真实仓库；
    - 通过 install_review_service 用 monkeypatch 将真实的 ReviewService 替换为
      FakeReviewService，返回预设的 findings，从而隔离 LLM 与静态检查的副作用，
      使测试专注于评估逻辑本身；
    - 对 run_eval 聚合层，使用 monkeypatch 替换 run_one_case，直接注入预期
      单用例结果，验证聚合算法的正确性，而非端到端流程。

在整体测试体系中的位置：
    本文件位于「评估器」测试层，介于 review_service（编排服务）测试与
    trace（轨迹记录）测试之间，确保产出的指标可用于回归基线与实习面试演示。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import eval_runner, review_service
from src.diff_parser import parse_diff
from src.file_context import collect_file_contexts
from src.llm_client import LLMClientError
from src.reviewers import review_changed_files
from src.review_service import ReviewRequest
from src.schemas import ContextBudget, ReviewIssue


def make_case(tmp_path, *, expected_categories, should_find):
    """在临时目录下构造一个评估用例目录及其 expected.json 元数据文件。

    用途：以最小成本生成 eval_runner 读取所需的 case 目录结构，避免依赖真实
    仓库或外部数据。每个 case 目录包含一个 expected.json，其中声明该用例
    期望命中的类别集合（expected_categories）以及是否属于「应发现」用例
    （should_find：True 表示正向用例，False 表示负向/clean 用例）。

    参数：
        tmp_path: pytest 提供的临时目录 fixture；
        expected_categories: 期望评估器命中的类别列表（如 ["secret"]）；
        should_find: 该用例是否期望产出 findings（正向用例为 True）。

    返回：
        构造好的 case 目录路径对象，可直接传给 run_one_case。
    """
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


def install_review_service(monkeypatch, findings):
    """通过 monkeypatch 将 eval_runner 模块内的 ReviewService 替换为伪造实现。

    用途：隔离真实审查服务（避免触发 LLM 调用、静态检查等副作用），让评估器
    测试只聚焦于评估逻辑。FakeReviewService.review 校验传入的请求类型，记录
    所有请求以便测试断言，并返回包含预设 findings 的结构化 state。

    参数：
        monkeypatch: pytest 内置 fixture，用于在测试结束后自动还原替换；
        findings: 预设的 findings 列表，作为 FakeReviewService 的返回值。

    返回：
        requests 列表引用，调用方可断言传入 ReviewService 的请求序列。
    """
    requests = []

    class FakeReviewService:
        def review(self, request):
            # 校验请求类型符合契约，便于早期发现接口误用
            assert isinstance(request, ReviewRequest)
            requests.append(request)
            # 用 SimpleNamespace 构造最小可用的 state，仅包含评估器关心的 issues
            return SimpleNamespace(state=SimpleNamespace(issues=findings))

    monkeypatch.setattr(eval_runner, "ReviewService", FakeReviewService)
    return requests


def test_cross_file_case_ground_truth_requires_context_and_survives_rename(
    tmp_path, monkeypatch
):
    """The cross-file case must rely on its evidence, never on its directory name."""
    case_dir = (
        Path(__file__).resolve().parent.parent
        / "evals"
        / "cases"
        / "uncaught_card_decline"
    )
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    repository_root = case_dir / "repository_context"

    changed_files = parse_diff((case_dir / "input.diff").read_text(encoding="utf-8"))
    expected_finding = expected["expected_findings"][0]
    assert (
        expected_finding["file"],
        expected_finding["line"],
        expected_finding["category"],
    ) == ("api/checkout.py", 2, "exception_handling")
    assert any(
        line.file_path == expected_finding["file"]
        and line.line_no == expected_finding["line"]
        for changed_file in changed_files
        for line in changed_file.added_lines
    )
    assert review_changed_files(changed_files) == []

    contexts = collect_file_contexts(
        repository_root,
        changed_files,
        ContextBudget(max_prompt_chars=4000, max_extra_context_files=3),
    )
    # The contract is intentionally non-Python: fixed collection cannot infer it.
    assert "docs/payment-contract.md" not in {context.path for context in contexts}
    contract = (repository_root / "docs" / "payment-contract.md").read_text(
        encoding="utf-8"
    )
    assert "CardDeclined" in contract and "HTTP\n422" in contract

    renamed_case = tmp_path / "renamed-without-answer"
    shutil.copytree(case_dir, renamed_case)
    caller_repository_root = tmp_path / "caller-repository"
    caller_repository_root.mkdir()

    class EvidenceOnlyReviewService:
        def review(self, request):
            assert Path(request.repo_root) == (
                renamed_case / "repository_context"
            ).resolve()
            diff_text = Path(request.diff_path).read_text(encoding="utf-8")
            context_text = (
                Path(request.repo_root) / "docs" / "payment-contract.md"
            ).read_text(encoding="utf-8")
            assert "processor.charge_card(amount)" in diff_text
            assert "CardDeclined" in context_text
            return SimpleNamespace(
                state=SimpleNamespace(
                    issues=[
                        ReviewIssue(
                            "api/checkout.py",
                            2,
                            "warning",
                            "exception_handling",
                            "Unhandled CardDeclined",
                            "Translate CardDeclined to HTTP 422.",
                        )
                    ]
                )
            )

    monkeypatch.setattr(eval_runner, "ReviewService", EvidenceOnlyReviewService)
    result = eval_runner.run_one_case(renamed_case, caller_repository_root)

    assert result["case_id"] == "uncaught_card_decline"
    assert result["passed"] is True


def test_run_one_case_keeps_caller_repo_root_for_legacy_cases(tmp_path, monkeypatch):
    """Cases without repository_context must preserve the existing runner contract."""
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)
    caller_repository_root = tmp_path / "caller-repository"
    caller_repository_root.mkdir()

    class FakeReviewService:
        def review(self, request):
            assert Path(request.repo_root) == caller_repository_root
            return SimpleNamespace(state=SimpleNamespace(issues=[]))

    monkeypatch.setattr(eval_runner, "ReviewService", FakeReviewService)

    result = eval_runner.run_one_case(case_dir, caller_repository_root)

    assert result["passed"] is True


def test_run_one_case_rejects_case_context_path_escape(tmp_path, monkeypatch):
    """A case manifest cannot redirect repository reads outside its own fixture."""
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)
    (case_dir / "expected.json").write_text(
        json.dumps(
            {
                "case_id": "case",
                "expected_categories": [],
                "should_find": False,
                "repository_context": {"root": "../outside", "required_paths": []},
            }
        ),
        encoding="utf-8",
    )
    called = []

    class UnexpectedReviewService:
        def review(self, request):
            called.append(request)
            raise AssertionError("invalid case context must not reach ReviewService")

    monkeypatch.setattr(eval_runner, "ReviewService", UnexpectedReviewService)

    with pytest.raises(ValueError, match="forbidden"):
        eval_runner.run_one_case(case_dir, tmp_path)

    assert called == []


def test_extract_categories_prefers_category_and_falls_back_to_reason():
    """验证 extract_categories 的类别提取优先级与回退逻辑。

    测试目的：
        确认从 findings 中提取类别时，优先使用 category 字段；当 category 为
        空字符串或缺失时，回退到 reason 字段；两者均缺失的条目被忽略。

    测试场景：
        构造四类典型 finding：1) category 非空（应取 category）；2) category 为空
        但 reason 非空（应回退取 reason）；3) 仅 reason 且为空（应被忽略）；
        4) 空字典（应被忽略）。

    预期输出：
        提取结果为 {"secret", "test_gap"}，即仅前两类 finding 贡献了类别。
    """
    findings = [
        {"category": "secret", "reason": "ignored"},  # category 优先，reason 被忽略
        {"category": "", "reason": "test_gap"},  # category 为空，回退到 reason
        {"reason": ""},  # reason 也为空，不贡献类别
        {},  # 完全空对象，不贡献类别
    ]

    # 核心不变量：仅 category 非空或回退到非空 reason 的条目贡献类别
    assert eval_runner.extract_categories(findings) == {"secret", "test_gap"}


@pytest.mark.parametrize(
    ("categories", "expected_passed", "expected_false_positive", "expected_counts"),
    [
        # 参数化用例 1：实际类别恰好等于期望集合 -> 精确匹配，通过且无 FP
        (["secret"], True, False, (1, 0, 0)),
        # 参数化用例 2：实际类别多于期望（多出 debug）-> 多出的判为 FP，整体不通过
        (["secret", "debug"], False, True, (1, 1, 0)),
    ],
)
def test_run_one_case_requires_exact_categories_for_positive_cases(
    tmp_path, monkeypatch, categories, expected_passed, expected_false_positive, expected_counts
):
    """验证正向用例（should_find=True）要求实际类别集合与期望精确匹配。

    测试目的：
        对正向用例，run_one_case 必须以「类别集合精确匹配」作为通过判据：实际产出
        的类别集合必须恰好等于 expected_categories。多出的类别会被记为假阳性（FP），
        导致用例不通过。

    测试场景（参数化）：
        - 用例 A：期望 ["secret"]，实际仅产出 ["secret"] -> 精确匹配，passed=True；
        - 用例 B：期望 ["secret"]，实际产出 ["secret", "debug"] -> 多出 debug，
          被判为 FP，passed=False，false_positive=True。

    特殊逻辑：
        参数化设计使两条边界条件共用同一测试体，分别覆盖「精确通过」与
        「多出类别判 FP」两种结果，确保通过判据的一致性。

    预期输出：
        passed / false_positive / actual_categories / (tp,fp,fn) 均与参数化期望一致。
    """
    # 期望类别为 ["secret"] 的正向用例
    case_dir = make_case(tmp_path, expected_categories=["secret"], should_find=True)
    install_review_service(
        monkeypatch,
        # 根据参数化 categories 构造对应数量的 ReviewIssue
        [ReviewIssue("app.py", 1, "warning", category, "issue", "fix") for category in categories],
    )

    result = eval_runner.run_one_case(case_dir, tmp_path)

    # 不变量：通过状态与参数化期望一致
    assert result["passed"] is expected_passed
    # 不变量：假阳性标记与参数化期望一致
    assert result["false_positive"] is expected_false_positive
    # 不变量：实际类别集合经排序后应等于输入 categories 的排序
    assert result["actual_categories"] == sorted(categories)
    # 不变量：TP/FP/FN 计数与参数化期望一致
    assert (result["tp"], result["fp"], result["fn"]) == expected_counts


def test_run_one_case_marks_findings_in_no_find_case_as_false_positive(
    tmp_path, monkeypatch
):
    """验证负向用例（should_find=False）在产出任何 finding 时即判为假阳性。

    测试目的：
        对负向/clean 用例（期望不发现任何问题），只要审查器产出了 finding，
        就应整体标记为假阳性（false_positive=True）且不通过。

    测试场景：
        构造一个 expected_categories=[] 且 should_find=False 的 clean 用例，
        并通过 FakeReviewService 注入一个非预期 finding（{"issue": "unexpected"}）。

    特殊逻辑：
        findings 以原始 dict 形式注入（而非 ReviewIssue），验证评估器对 dict
        形式 finding 的兼容处理。

    预期输出：
        passed=False、false_positive=True、is_negative_case=True，
        TP=0/FP=1/FN=0，actual_categories 为空列表。
    """
    # 负向用例：期望不发现任何问题
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)
    # 注入一个意外的 finding，模拟审查器误报
    install_review_service(monkeypatch, [{"issue": "unexpected"}])

    result = eval_runner.run_one_case(case_dir, tmp_path)

    # 不变量：clean 用例出现 finding 必然不通过
    assert result["passed"] is False
    # 不变量：clean 用例出现 finding 必然标记为假阳性
    assert result["false_positive"] is True
    # 不变量：clean 用例无期望类别，实际类别集合应为空
    assert result["actual_categories"] == []
    # 不变量：标记为负向用例，供后续聚合指标区分分母
    assert result["is_negative_case"] is True
    # 不变量：0 个真阳性、1 个假阳性、0 个假阴性
    assert (result["tp"], result["fp"], result["fn"]) == (0, 1, 0)


def test_run_one_case_marks_runner_failure_as_not_passed(tmp_path, monkeypatch):
    """验证审查服务抛异常时 run_one_case 的降级处理。

    测试目的：
        当 ReviewService.review 抛出异常时，run_one_case 不应崩溃，而应捕获异常
        并将该用例标记为 json_valid=False、passed=False，同时记录错误信息。

    测试场景：
        注入 FailingReviewService，其 review 方法抛出 RuntimeError("review failed")，
        对一个正向用例（期望 ["secret"]）执行评估。

    特殊逻辑：
        直接用 monkeypatch 替换为内联定义的失败服务，无需走 install_review_service
        辅助函数，因为此处需要的是抛异常行为而非返回预设 findings。

    预期输出：
        json_valid=False、passed=False、false_positive=False（异常不视为误报）、
        error="review failed"，TP/FP/FN=(0,0,1)（因未命中期望类别，记 1 个假阴性）。
    """
    case_dir = make_case(tmp_path, expected_categories=["secret"], should_find=True)

    class FailingReviewService:
        def review(self, request):
            # 模拟审查服务运行时失败
            raise RuntimeError("review failed")

    monkeypatch.setattr(eval_runner, "ReviewService", FailingReviewService)

    result = eval_runner.run_one_case(case_dir, tmp_path)

    # 不变量：异常导致输出无效
    assert result["json_valid"] is False
    # 不变量：异常用例必然不通过
    assert result["passed"] is False
    # 不变量：异常不属于误报，false_positive 保持 False
    assert result["false_positive"] is False
    # 不变量：错误信息被原样保留，便于排查
    assert result["error"] == "review failed"
    # 不变量：未产出任何 finding，期望类别未命中 -> 1 个假阴性
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 1)


@pytest.mark.parametrize(
    ("provider_result", "expected_error"),
    [
        ("provider_error", "llm_provider_unavailable"),
        ("invalid_json", "llm_output_invalid:llm_json_parse_error"),
    ],
)
def test_run_one_case_marks_fatal_fixed_llm_failures_as_not_passed(
    tmp_path, monkeypatch, provider_result, expected_error
):
    """Provider and parse failures must fail a case through the real pipeline."""
    case_dir = (
        Path(__file__).resolve().parent.parent
        / "evals"
        / "cases"
        / "clean_change_no_issue"
    )
    provider_requests = []

    def fake_get_call_model(provider, *, mock_fixture=None):
        provider_requests.append((provider, mock_fixture))
        if provider_result == "provider_error":
            def call_model(_prompt):
                raise LLMClientError("provider down secret=must-not-leak")

            return call_model
        return lambda _prompt: "not json"

    monkeypatch.setattr(review_service, "get_call_model", fake_get_call_model)

    result = eval_runner.run_one_case(
        case_dir,
        tmp_path,
        use_llm=True,
        llm_provider="mock",
        review_mode="fixed",
    )

    assert [provider for provider, _fixture in provider_requests] == ["mock"]
    assert result["passed"] is False
    assert result["json_valid"] is False
    assert result["findings_count"] == 0
    assert result["false_positive"] is False
    assert result["error"] == expected_error
    assert "must-not-leak" not in result["error"]
    assert result["llm_calls"] == 1


def test_fixed_llm_repair_warning_is_not_a_fatal_failure():
    """Usable repaired output remains valid even when state.errors is non-empty."""
    state = SimpleNamespace(
        errors=["llm_finding_0_missing_confidence"],
        trace_steps=[{
            "step": "run_llm_review",
            "detail": {
                "called": True,
                "valid": True,
                "repaired": True,
                "errors": ["llm_finding_0_missing_confidence"],
            },
        }],
    )

    assert eval_runner._fixed_llm_failure_error(state) == ""


def test_run_one_case_passes_context_budget_to_review_agent(tmp_path, monkeypatch):
    """验证 context_budget 能从 run_one_case 透传至 ReviewService.review。

    测试目的：
        确保调用方可通过 context_budget 参数控制审查服务的上下文预算（如 prompt
        字符上限、额外上下文文件数上限），且该对象以引用方式原样传入 review 请求。

    测试场景：
        构造一个 clean 用例与一个 ContextBudget（max_prompt_chars=17,
        max_extra_context_files=0），注入 FakeReviewService，在其 review 内部
        断言 request.context_budget 即为传入的同一对象且字段值一致。

    特殊逻辑：
        使用自定义 FakeReviewService（而非 install_review_service），因为需要在
        review 内部对 context_budget 做断言，验证「同一对象引用」这一不变量。

    预期输出：
        review 内断言全部通过，且用例结果 passed=True、TP/FP/FN=(0,0,0)。
    """
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)
    # 构造一个具有辨识度的预算对象，便于断言字段值
    budget = ContextBudget(max_prompt_chars=17, max_extra_context_files=0)

    class FakeReviewService:
        def review(self, request):
            # 不变量：context_budget 必须是同一对象引用（透传，非拷贝）
            assert request.context_budget is budget
            # 不变量：字段值原样保留
            assert request.context_budget.max_prompt_chars == 17
            assert request.context_budget.max_extra_context_files == 0
            return SimpleNamespace(state=SimpleNamespace(issues=[]))

    monkeypatch.setattr(eval_runner, "ReviewService", FakeReviewService)

    result = eval_runner.run_one_case(case_dir, tmp_path, context_budget=budget)

    # 不变量：clean 用例无 finding，正常通过
    assert result["passed"] is True
    # 不变量：无任何正/假阳性、假阴性
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 0)


@pytest.mark.parametrize(
    ("findings", "error_fragment"),
    [
        # 参数化用例 1：findings 为 None -> 类型错误，issues 必须是列表
        (None, "issues must be a list"),
        # 参数化用例 2：findings 列表中含 None -> 单个元素必须是 finding 对象
        ([None], "issues[0] must be a finding object"),
    ],
)
def test_run_one_case_degrades_on_malformed_reviewer_output(
    tmp_path, monkeypatch, findings, error_fragment
):
    """验证审查器输出格式错误时 run_one_case 的降级处理。

    测试目的：
        当 ReviewService 返回的 issues 格式不合法（如 None 或列表内含非 finding
        对象）时，run_one_case 应安全降级：标记 json_valid=False、passed=False，
        findings_count 归零，并在 error 中包含可定位问题的错误片段。

    测试场景（参数化）：
        - 用例 A：findings=None，模拟审查器未返回列表；
        - 用例 B：findings=[None]，模拟列表内单个元素非法。
        两者均为正向用例（期望 ["secret"]）。

    特殊逻辑：
        参数化覆盖两种典型畸形输入，分别对应「整体类型错」与「元素类型错」，
        验证错误消息的精准定位能力。

    预期输出：
        json_valid=False、passed=False、findings_count=0、TP/FP/FN=(0,0,1)，
        且 error 字符串包含参数化的 error_fragment。
    """
    case_dir = make_case(tmp_path, expected_categories=["secret"], should_find=True)
    install_review_service(monkeypatch, findings)

    result = eval_runner.run_one_case(case_dir, tmp_path)

    # 不变量：畸形输出导致 JSON 无效
    assert result["json_valid"] is False
    # 不变量：畸形输出导致用例不通过
    assert result["passed"] is False
    # 不变量：畸形输出不计入有效 findings 数量
    assert result["findings_count"] == 0
    # 不变量：未命中期望类别 -> 1 个假阴性
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 1)
    # 不变量：错误信息包含可定位问题的片段
    assert error_fragment in result["error"]


def test_run_eval_aggregates_case_metrics(tmp_path, monkeypatch):
    """验证 run_eval 能正确聚合多用例的评估指标。

    测试目的：
        确认 run_eval 在遍历所有 case 目录后，能基于各单用例结果正确计算
        precision / recall / f1 / category_hit_rate / json_valid_rate /
        false_positive_rate / average_findings / average_duration_ms 等聚合指标。

    测试场景：
        构造三个子目录（passing / extra / failed），分别对应三种典型用例：
        - passing：完全通过，2 TP；
        - extra：有 1 TP 但多出 2 FP，不通过；
        - failed：JSON 无效且为负向用例，1 FP。
        通过 monkeypatch 替换 run_one_case 为 fake_run_one_case，按目录名返回
        预设结果，隔离单用例逻辑。

    特殊逻辑：
        使用 fake_run_one_case 直接注入结果，使测试聚焦于聚合算法的正确性，
        而非 run_one_case 的内部实现。三个用例的 TP/FP/FN 与 duration 经过
        手工核算，作为聚合指标的期望基线。

    预期输出：
        各聚合指标均与手工核算值一致（见下方断言）。
    """
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    # 三个子目录对应三个用例，run_eval 会遍历这些目录
    for name in ("passing", "extra", "failed"):
        (cases_dir / name).mkdir()

    # 预设每个用例的单用例结果，覆盖通过/多报/失败三种典型场景
    results_by_case = {
        "passing": {  # 完全通过：2 个真阳性，无误报
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
        "extra": {  # 部分正确但多报：1 TP + 2 FP + 1 FN
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
        "failed": {  # 失败用例：JSON 无效，负向用例，1 FP
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
        # 按目录名返回预设结果，附加 case_id 字段
        return {"case_id": case_dir.name, **results_by_case[case_dir.name]}

    monkeypatch.setattr(eval_runner, "run_one_case", fake_run_one_case)

    metrics = eval_runner.run_eval(cases_dir, tmp_path)

    # 不变量：用例总数为 3
    assert metrics["cases"] == 3
    # 不变量：仅 passing 用例类别命中 -> 命中率 1/3
    assert metrics["category_hit_rate"] == pytest.approx(1 / 3)
    # 不变量：extra 与 failed 均为假阳性 -> 假阳性计数 2
    assert metrics["false_positive_count"] == 2
    # 不变量：passing 与 extra 的 JSON 有效 -> 有效率 2/3
    assert metrics["json_valid_rate"] == pytest.approx(2 / 3)
    # 不变量：(2+3+1)/3 = 2，平均 findings 数为 2
    assert metrics["average_findings"] == pytest.approx(2)
    # 不变量：(10+20+30)/3 = 20，平均耗时 20ms
    assert metrics["average_duration_ms"] == pytest.approx(20)
    # 不变量：TP=3, FP=3 -> precision=0.5
    assert metrics["precision"] == pytest.approx(0.5)
    # 不变量：TP=3, FN=1 -> recall=0.75
    assert metrics["recall"] == pytest.approx(0.75)
    # 不变量：f1 = 2*P*R/(P+R) = 2*0.5*0.75/(0.5+0.75) = 0.6
    assert metrics["f1"] == pytest.approx(0.6)
    # 不变量：唯一负向用例 failed 出现 FP -> 假阳性率 1.0
    assert metrics["false_positive_rate"] == 1.0
    # 不变量：仅 extra 用例有 1 个假阴性
    assert metrics["false_negative_count"] == 1


def test_run_eval_false_positive_rate_uses_clean_cases_as_denominator(tmp_path, monkeypatch):
    """验证 false_positive_rate 以 clean（负向）用例数作为分母。

    测试目的：
        确认 false_positive_rate 的计算口径为「假阳性用例数 / 负向用例总数」，
        而非「假阳性用例数 / 全部用例数」。这是评估误报率的关键业务约定。

    测试场景：
        构造 10 个 clean 用例（clean-0 ~ clean-9，均为 is_negative_case=True），
        其中仅 clean-0 出现假阳性。若分母为全部用例，结果应为 0.1；若分母为
        负向用例（同样为 10），结果也是 0.1。本测试通过 precision=0、recall=0
        进一步验证无 TP 时的指标退化行为。

    特殊逻辑：
        所有用例均为负向用例，确保分母语义明确；fake_run_one_case 仅对 clean-0
        置 is_false_positive=True，其余为干净通过。

    预期输出：
        false_positive_rate=0.1（1/10）、precision=0.0（无 TP）、recall=0.0（无 TP）。
    """
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    # 构造 10 个 clean 用例，分母明确为 10
    for index in range(10):
        (cases_dir / f"clean-{index}").mkdir()

    def fake_run_one_case(case_dir, **kwargs):
        # 仅 clean-0 出现假阳性，其余干净通过
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

    # 不变量：1 个假阳性 / 10 个负向用例 = 0.1
    assert metrics["false_positive_rate"] == pytest.approx(0.1)
    # 不变量：无 TP -> precision 为 0
    assert metrics["precision"] == 0.0
    # 不变量：无 TP -> recall 为 0
    assert metrics["recall"] == 0.0


def test_fixed_baseline_record_preserves_configuration_metrics_and_observability():
    """固定基线记录必须如实保存模式、配置、指标与不可用 token 语义。"""
    metrics = {
        "precision": 0.5,
        "recall": 1.0,
        "f1": 2 / 3,
        "false_positive_rate": 0.25,
        "results": [{"case_id": "case-a", "duration_ms": 12}],
    }

    record = eval_runner.build_fixed_baseline_record(
        metrics,
        cases_dir="evals/cases",
        repo_root=".",
        llm_provider="mock",
        commit="abc123",
        worktree_state="dirty",
        worktree_diff_sha256="a" * 64,
        baseline_output="evals/baselines/m7-0-fixed.json",
    )

    assert record["schema_version"] == "m7_fixed_baseline.v1"
    assert record["review_mode"] == "fixed"
    assert record["commit"] == "abc123"
    assert record["worktree_state"] == "dirty"
    assert record["worktree_diff_sha256"] == "a" * 64
    assert record["configuration"] == {
        "cases": "evals/cases",
        "repo": ".",
        "use_llm": False,
        "llm_provider": "mock",
        "context_budget": {
            "max_prompt_chars": 4000,
            "max_extra_context_files": 3,
        },
    }
    assert record["prerequisite_capabilities"]["M2"]["status"] == "confirmed"
    assert record["prerequisite_capabilities"]["M3"]["status"] == "confirmed"
    assert record["prerequisite_capabilities"]["M4"]["status"] == "confirmed"
    assert set(record["environment"]) == {"python_version", "platform"}
    assert record["reproduction"] == {
        "command": (
            "py -m src.eval_runner --cases evals/cases --repo . "
            "--baseline-output evals/baselines/m7-0-fixed.json "
            "--commit abc123 --worktree-state dirty"
        )
    }
    assert record["observability"]["llm_call_count"] == 0
    assert record["observability"]["token_usage"] == {
        "available": False,
        "reason": "fixed_pipeline_does_not_expose_token_usage",
    }
    assert record["metrics"] == metrics


def test_write_baseline_record_creates_json_artifact(tmp_path):
    """基线产物必须可由脚本重新读取，且父目录可由写入函数创建。"""
    output_path = tmp_path / "nested" / "baseline.json"
    record = {"schema_version": "m7_fixed_baseline.v1", "metrics": {"cases": 1}}

    eval_runner.write_baseline_record(output_path, record)

    assert json.loads(output_path.read_text(encoding="utf-8")) == record


def test_write_baseline_record_does_not_report_a_write_failure_as_success(
    tmp_path, monkeypatch
):
    """写入失败必须向调用方传播，不能生成伪造的成功基线。"""
    output_path = tmp_path / "baseline.json"

    def raise_write_error(self, *args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(type(output_path), "write_text", raise_write_error)

    with pytest.raises(OSError, match="disk unavailable"):
        eval_runner.write_baseline_record(output_path, {"metrics": {}})


def test_main_rejects_non_mock_single_baseline_before_side_effects(
    tmp_path, monkeypatch
):
    """A single baseline cannot capture source, run Eval, or reach a real provider."""
    output_path = tmp_path / "baseline.json"
    output_path.write_text("existing baseline\n", encoding="utf-8")
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=True,
        llm_provider="openai",
        baseline_output=str(output_path),
        commit="unknown",
        worktree_state="unknown",
        compare_modes=False,
        comparison_output=None,
    )
    calls = []

    def unexpected_call(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not run")

        return fail

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        eval_runner, "capture_source_revision", unexpected_call("source_capture")
    )
    monkeypatch.setattr(eval_runner, "run_eval", unexpected_call("run_eval"))
    monkeypatch.setattr(
        review_service, "get_call_model", unexpected_call("provider_initialization")
    )

    with pytest.raises(ValueError, match="single_baseline_requires_mock_provider"):
        eval_runner.main()

    assert calls == []
    assert output_path.read_text(encoding="utf-8") == "existing baseline\n"


def test_main_writes_complete_single_llm_baseline_from_real_eval(
    tmp_path, monkeypatch
):
    """The mock single baseline traverses Eval, aggregation, build, and JSON IO."""
    cases_dir = Path(__file__).resolve().parent.parent / "evals" / "cases"
    output_path = tmp_path / "baseline.json"
    args = SimpleNamespace(
        cases=str(cases_dir),
        repo=str(tmp_path),
        llm=True,
        llm_provider="mock",
        baseline_output=str(output_path),
        commit="abc123",
        worktree_state="clean",
        compare_modes=False,
        comparison_output=None,
    )
    providers = []
    production_get_call_model = review_service.get_call_model

    def offline_get_call_model(provider, *, mock_fixture=None):
        providers.append(provider)
        assert provider == "mock"
        return production_get_call_model(provider, mock_fixture=mock_fixture)

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(review_service, "get_call_model", offline_get_call_model)
    monkeypatch.setattr(
        eval_runner,
        "capture_source_revision",
        lambda repo_root, **kwargs: eval_runner.SourceRevision(
            commit="abc123", worktree_state="clean", worktree_diff_sha256=None
        ),
    )

    eval_runner.main()

    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert record["schema_version"] == "m8_single_baseline.v1"
    assert record["review_mode"] == "fixed"
    assert record["commit"] == "abc123"
    assert record["worktree_state"] == "clean"
    assert record["worktree_diff_sha256"] is None
    assert record["configuration"] == {
        "cases": str(cases_dir),
        "repo": str(tmp_path),
        "use_llm": True,
        "llm_provider": "mock",
        "context_budget": {
            "max_prompt_chars": ContextBudget().max_prompt_chars,
            "max_extra_context_files": ContextBudget().max_extra_context_files,
        },
    }
    assert type(record["environment"]["python_version"]) is str
    assert type(record["environment"]["platform"]) is str
    assert "--llm --llm-provider mock" in record["reproduction"]["command"]

    metrics = record["metrics"]
    integer_metrics = {
        "cases",
        "false_positive_count",
        "negative_case_count",
        "false_positive_negative_case_count",
        "false_negative_count",
        "total_tokens",
        "total_llm_calls",
        "unknown_tool_count",
        "budget_exhausted_count",
    }
    float_metrics = {
        "category_hit_rate",
        "json_valid_rate",
        "average_findings",
        "average_duration_ms",
        "p95_duration_ms",
        "precision",
        "recall",
        "f1",
        "false_positive_rate",
        "estimated_cost_usd",
    }
    for field in integer_metrics:
        assert type(metrics[field]) is int
    for field in float_metrics:
        assert type(metrics[field]) is float
    assert type(metrics["results"]) is list
    assert metrics["cases"] == len(metrics["results"]) == len(providers) == 6

    bool_result_fields = {
        "passed",
        "json_valid",
        "false_positive",
        "is_negative_case",
        "token_usage_available",
        "react_degraded",
        "budget_exhausted",
    }
    integer_result_fields = {
        "findings_count",
        "tp",
        "fp",
        "fn",
        "duration_ms",
        "llm_calls",
        "total_tokens",
        "react_steps",
        "react_llm_calls",
        "react_total_tokens",
        "react_tool_results_truncated",
        "unknown_tool_count",
    }
    string_result_fields = {"case_id", "error", "review_mode", "react_termination_reason"}
    for result in metrics["results"]:
        for field in bool_result_fields:
            assert type(result[field]) is bool
        for field in integer_result_fields:
            assert type(result[field]) is int
        for field in string_result_fields:
            assert type(result[field]) is str
        for field in ("expected_categories", "actual_categories"):
            assert type(result[field]) is list
            assert all(type(value) is str for value in result[field])

    results = metrics["results"]
    negative_results = [result for result in results if result["is_negative_case"]]
    assert len(negative_results) == metrics["negative_case_count"] == 1
    assert negative_results[0]["case_id"] == "clean_change_no_issue"
    assert metrics["false_positive_negative_case_count"] == sum(
        result["false_positive"] for result in negative_results
    )
    assert metrics["json_valid_rate"] == pytest.approx(
        sum(result["json_valid"] for result in results) / len(results)
    )
    assert metrics["average_findings"] == pytest.approx(
        sum(result["findings_count"] for result in results) / len(results)
    )
    assert metrics["average_duration_ms"] == pytest.approx(
        sum(result["duration_ms"] for result in results) / len(results)
    )
    assert metrics["p95_duration_ms"] == eval_runner._percentile(
        [result["duration_ms"] for result in results], 95
    )
    assert metrics["total_tokens"] == sum(result["total_tokens"] for result in results)
    assert metrics["total_llm_calls"] == sum(result["llm_calls"] for result in results)
    assert record["observability"]["llm_call_count"] == metrics["total_llm_calls"]


def test_main_writes_zero_case_single_baseline_with_stable_numeric_types(
    tmp_path, monkeypatch
):
    """An empty case set has a valid JSON shape and explicit zero semantics."""
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    output_path = tmp_path / "baseline.json"
    args = SimpleNamespace(
        cases=str(cases_dir),
        repo=str(tmp_path),
        llm=True,
        llm_provider="mock",
        baseline_output=str(output_path),
        commit="abc123",
        worktree_state="clean",
        compare_modes=False,
        comparison_output=None,
    )

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        review_service,
        "get_call_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty Eval must not initialize a provider")
        ),
    )
    monkeypatch.setattr(
        eval_runner,
        "capture_source_revision",
        lambda repo_root, **kwargs: eval_runner.SourceRevision(
            commit="abc123", worktree_state="clean", worktree_diff_sha256=None
        ),
    )

    eval_runner.main()

    metrics = json.loads(output_path.read_text(encoding="utf-8"))["metrics"]
    assert metrics["results"] == []
    for field in (
        "cases",
        "false_positive_count",
        "negative_case_count",
        "false_positive_negative_case_count",
        "false_negative_count",
        "total_tokens",
        "total_llm_calls",
        "unknown_tool_count",
        "budget_exhausted_count",
    ):
        assert type(metrics[field]) is int
        assert metrics[field] == 0
    for field in (
        "category_hit_rate",
        "json_valid_rate",
        "average_findings",
        "average_duration_ms",
        "p95_duration_ms",
        "precision",
        "recall",
        "f1",
        "false_positive_rate",
        "estimated_cost_usd",
    ):
        assert type(metrics[field]) is float
        assert metrics[field] == 0.0


def test_run_eval_counts_fixed_llm_call_from_real_review_trace(tmp_path):
    """The production eval path records the one fixed LLM call without fake tokens."""
    cases_dir = Path(__file__).resolve().parent.parent / "evals" / "cases"

    metrics = eval_runner.run_eval(
        cases_dir,
        tmp_path,
        use_llm=True,
        llm_provider="mock",
        review_mode="fixed",
    )

    assert metrics["cases"] == 6
    assert metrics["total_llm_calls"] == 6
    assert metrics["total_tokens"] == 0
    assert all(result["llm_calls"] == 1 for result in metrics["results"])
    negative = [result for result in metrics["results"] if result["is_negative_case"]]
    assert len(negative) == 1
    assert negative[0]["case_id"] == "clean_change_no_issue"


def test_main_does_not_write_single_baseline_when_eval_fails(tmp_path, monkeypatch):
    """An Eval exception must propagate before any baseline artifact is written."""
    output_path = tmp_path / "baseline.json"
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=True,
        llm_provider="mock",
        baseline_output=str(output_path),
        commit="abc123",
        worktree_state="clean",
        compare_modes=False,
        comparison_output=None,
    )
    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        eval_runner,
        "capture_source_revision",
        lambda repo_root, **kwargs: eval_runner.SourceRevision(
            commit="abc123", worktree_state="clean", worktree_diff_sha256=None
        ),
    )
    monkeypatch.setattr(
        eval_runner,
        "run_eval",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("eval failed")),
    )

    with pytest.raises(RuntimeError, match="eval failed"):
        eval_runner.main()

    assert not output_path.exists()


def test_main_writes_a_readable_fixed_baseline_with_reproduction_command(
    tmp_path, monkeypatch
):
    """CLI 产物必须可读，且保留本次 fixed Eval 的复制命令。"""
    output_path = tmp_path / "baseline.json"
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=False,
        llm_provider="mock",
        baseline_output=str(output_path),
        commit="abc123",
        worktree_state="clean",
        compare_modes=False,
        comparison_output=None,
    )
    metrics = {
        "cases": 1,
        "category_hit_rate": 1.0,
        "false_positive_count": 0,
        "json_valid_rate": 1.0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "false_positive_rate": 0.0,
        "false_negative_count": 0,
        "average_findings": 1.0,
        "average_duration_ms": 12.0,
        "p95_duration_ms": 12.0,
        "total_tokens": 0,
        "total_llm_calls": 0,
        "estimated_cost_usd": 0.0,
        "unknown_tool_count": 0,
        "budget_exhausted_count": 0,
        "results": [],
    }

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(eval_runner, "run_eval", lambda **kwargs: metrics)
    monkeypatch.setattr(
        eval_runner,
        "capture_source_revision",
        lambda repo_root, **kwargs: eval_runner.SourceRevision(
            commit="abc123",
            worktree_state="clean",
            worktree_diff_sha256=None,
        ),
    )

    eval_runner.main()

    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert record["metrics"] == metrics
    assert record["reproduction"]["command"] == (
        f"py -m src.eval_runner --cases evals/cases --repo . "
        f"--baseline-output {output_path} --commit abc123 --worktree-state clean"
    )


def test_capture_source_revision_fingerprints_a_dirty_tracked_diff(monkeypatch, tmp_path):
    """Dirty snapshots must bind the recorded commit to a deterministic diff hash."""
    outputs = iter((b"abc123\n", b" M src/eval_runner.py\n", b"diff --git a/x b/x\n"))

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=next(outputs))

    monkeypatch.setattr(eval_runner.subprocess, "run", fake_run)

    revision = eval_runner.capture_source_revision(tmp_path)

    assert revision.commit == "abc123"
    assert revision.worktree_state == "dirty"
    assert revision.worktree_diff_sha256 == (
        "1a059963bbf3198857755a48c741d351e21515186ce951464b89a0de0797c081"
    )


def test_main_rejects_untracked_files_before_starting_fixed_baseline_eval(
    tmp_path, monkeypatch
):
    """Untracked input cannot be represented by the tracked-diff fingerprint."""
    output_path = tmp_path / "baseline.json"
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=False,
        llm_provider="mock",
        baseline_output=str(output_path),
        commit="unknown",
        worktree_state="unknown",
        compare_modes=False,
        comparison_output=None,
    )
    git_outputs = iter((b"abc123\n", b"?? evals/cases/new-case/expected.json\n"))
    calls = []

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=next(git_outputs))

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(eval_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(eval_runner, "run_eval", lambda **kwargs: calls.append(kwargs))

    with pytest.raises(ValueError, match="unsupported"):
        eval_runner.main()

    assert calls == []
    assert not output_path.exists()


def test_main_can_record_the_same_untracked_baseline_output_twice_in_a_git_repo(
    tmp_path, monkeypatch
):
    """A generated baseline is output, not untracked source input, on rerun."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )

    git("init")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Eval Test")
    (repo_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "initial")

    output_path = repo_root / "evals" / "baselines" / "fixed.json"
    commit = git("rev-parse", "HEAD").stdout.decode("ascii").strip()
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=False,
        llm_provider="mock",
        baseline_output="evals/baselines/fixed.json",
        commit=commit,
        worktree_state="clean",
        compare_modes=False,
        comparison_output=None,
    )
    metrics = {
        "cases": 0,
        "category_hit_rate": 0.0,
        "false_positive_count": 0,
        "json_valid_rate": 0.0,
        "average_findings": 0.0,
        "average_duration_ms": 0.0,
        "p95_duration_ms": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "false_positive_rate": 0.0,
        "false_negative_count": 0,
        "total_tokens": 0,
        "total_llm_calls": 0,
        "estimated_cost_usd": 0.0,
        "unknown_tool_count": 0,
        "budget_exhausted_count": 0,
        "results": [],
    }

    (repo_root / "src").mkdir()
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(eval_runner, "__file__", str(repo_root / "src" / "eval_runner.py"))
    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(eval_runner, "run_eval", lambda **kwargs: metrics)

    eval_runner.main()
    assert output_path.exists()
    assert git("status", "--porcelain").stdout == b"?? evals/\n"

    eval_runner.main()
    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert record["metrics"] == metrics
    assert record["reproduction"]["command"] == (
        "py -m src.eval_runner --cases evals/cases --repo . "
        "--baseline-output evals/baselines/fixed.json "
        f"--commit {commit} --worktree-state clean"
    )

    (repo_root / "untracked-input.txt").write_text("input\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        eval_runner.main()


def test_main_rejects_spoofed_baseline_commit_before_eval(tmp_path, monkeypatch):
    """Caller-supplied commit metadata must not misidentify the measured source."""
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=False,
        llm_provider="mock",
        baseline_output=str(tmp_path / "baseline.json"),
        commit="spoofed",
        worktree_state="clean",
        compare_modes=False,
        comparison_output=None,
    )
    calls = []

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        eval_runner,
        "capture_source_revision",
        lambda repo_root, **kwargs: eval_runner.SourceRevision("actual", "clean", None),
    )
    monkeypatch.setattr(eval_runner, "run_eval", lambda **kwargs: calls.append(kwargs))

    with pytest.raises(ValueError, match="fixed_baseline_commit_mismatch"):
        eval_runner.main()

    assert calls == []


def test_cli_reports_unsupported_without_traceback_or_absolute_paths(tmp_path):
    """The expected unsupported CLI failure must be safe for scripts to parse."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )

    git("init")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Eval Test")
    (repo_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "initial")
    (repo_root / "untracked-input.txt").write_text("input\n", encoding="utf-8")
    (repo_root / "src").mkdir()

    workspace_root = Path(__file__).resolve().parent.parent
    script = "\n".join(
        (
            "import sys",
            "from src import eval_runner",
            "module_file, baseline_output = sys.argv[1:]",
            "eval_runner.__file__ = module_file",
            "sys.argv = ['eval_runner.py', '--baseline-output', baseline_output]",
            "eval_runner.cli_main()",
        )
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(repo_root / "src" / "eval_runner.py"),
            str(repo_root / "evals" / "baselines" / "fixed.json"),
        ],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "unsupported\n"
    assert "Traceback" not in completed.stderr
    assert str(repo_root) not in completed.stderr
    assert str(workspace_root) not in completed.stderr


def test_output_exclusion_paths_returns_parent_dir_for_in_repo_output(monkeypatch, tmp_path):
    """The helper must exclude the output file plus its in-repo parent directory.

    Why: regenerating ``evals/comparisons/m7-18.json`` while a previously
    generated sibling such as ``evals/comparisons/m7-18-prev.json`` is still
    untracked must not be blocked by that sibling.  Returning the parent
    directory (``evals/comparisons/``) as a prefix exclusion lets the snapshot
    logic mask every untracked sibling under that directory.
    """
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(
        eval_runner, "__file__", str(repo_root / "src" / "eval_runner.py")
    )

    excluded = eval_runner._output_exclusion_paths("evals/comparisons/m7-18.json")

    assert excluded[0] == Path("evals/comparisons/m7-18.json")
    assert excluded[1] == "evals/comparisons/"


def test_output_exclusion_paths_falls_back_to_file_only_at_repo_root(monkeypatch, tmp_path):
    """An output at the repository root must not exclude every untracked file."""
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(
        eval_runner, "__file__", str(repo_root / "src" / "eval_runner.py")
    )

    excluded = eval_runner._output_exclusion_paths("baseline.json")

    assert excluded == (Path("baseline.json"),)


def test_capture_source_revision_excludes_untracked_sibling_outputs_under_same_dir(
    monkeypatch, tmp_path
):
    """An existing untracked sibling output must not block regenerating a new one.

    Trigger: ``excluded_untracked_paths`` includes both the exact output file
    and its parent directory prefix.  An untracked sibling under that prefix
    must be filtered out so the snapshot can proceed and fingerprint the
    tracked diff.
    """
    monkeypatch.chdir(tmp_path)
    outputs = iter(
        (
            b"abc123\n",
            b"?? evals/comparisons/m7-18-prev.json\x00 M src/eval_runner.py\n",
            b"diff --git a/x b/x\n",
        )
    )

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=next(outputs))

    monkeypatch.setattr(eval_runner.subprocess, "run", fake_run)

    revision = eval_runner.capture_source_revision(
        tmp_path,
        excluded_untracked_paths=(
            "evals/comparisons/m7-18.json",
            "evals/comparisons/",
        ),
    )

    assert revision.commit == "abc123"
    assert revision.worktree_state == "dirty"
    assert revision.worktree_diff_sha256 == (
        "1a059963bbf3198857755a48c741d351e21515186ce951464b89a0de0797c081"
    )


def test_capture_source_revision_still_rejects_untracked_source_outside_output_dir(
    monkeypatch, tmp_path
):
    """Untracked source outside the output directory must still raise unsupported.

    Why: the directory-prefix relaxation is scoped to the output's parent
    directory only.  An untracked case file under ``evals/cases/`` is genuine
    source input and must continue to trigger ``unsupported`` so the snapshot
    cannot misidentify the measured source.
    """
    monkeypatch.chdir(tmp_path)
    outputs = iter(
        (
            b"abc123\n",
            b"?? evals/cases/new-case/expected.json\n",
        )
    )

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=next(outputs))

    monkeypatch.setattr(eval_runner.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="unsupported"):
        eval_runner.capture_source_revision(
            tmp_path,
            excluded_untracked_paths=(
                "evals/comparisons/m7-18.json",
                "evals/comparisons/",
            ),
        )


def test_main_compare_modes_regenerates_output_with_existing_untracked_sibling(
    tmp_path, monkeypatch
):
    """--compare-modes must succeed when a sibling output is already untracked.

    Reproduces the P0-1 scenario: ``evals/comparisons/m7-18.json`` is recorded
    against a stale commit, and a previously generated sibling output sits
    untracked in the same directory.  Regenerating the same path must not
    raise ``unsupported`` and must overwrite the recorded file.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )

    git("init")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Eval Test")
    (repo_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "initial")

    output_dir = repo_root / "evals" / "comparisons"
    output_dir.mkdir(parents=True)
    # A previously generated sibling output, untracked, would block regenerating
    # any new sibling under the same directory before the P0-1 fix.
    (output_dir / "m7-18-prev.json").write_text(
        json.dumps({"stale": True}), encoding="utf-8"
    )
    output_path = output_dir / "m7-18.json"

    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=False,
        llm_provider="mock",
        baseline_output=None,
        commit="unknown",
        worktree_state="unknown",
        compare_modes=True,
        comparison_output="evals/comparisons/m7-18.json",
    )
    comparison = {
        "fixed": {
            "precision": 0.5, "recall": 1.0, "f1": 0.667,
            "p95_duration_ms": 10, "total_tokens": 0, "total_llm_calls": 0,
            "estimated_cost_usd": 0.0, "unknown_tool_count": 0,
            "budget_exhausted_count": 0, "results": [],
        },
        "react": {
            "precision": 0.5, "recall": 1.0, "f1": 0.667,
            "p95_duration_ms": 20, "total_tokens": 1000, "total_llm_calls": 3,
            "estimated_cost_usd": 0.0005, "unknown_tool_count": 0,
            "budget_exhausted_count": 0, "results": [],
        },
        "comparison": {
            "per_case_diff": [],
            "aggregate_diff": {
                "precision_delta": 0.0, "recall_delta": 0.0, "f1_delta": 0.0,
                "p95_latency_delta_ms": 10, "token_delta": 1000,
                "llm_call_delta": 3, "cost_delta_usd": 0.0005,
            },
        },
    }

    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(
        eval_runner, "__file__", str(repo_root / "src" / "eval_runner.py")
    )
    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        eval_runner, "run_mode_comparison", lambda **kwargs: comparison
    )

    eval_runner.main()

    assert output_path.exists()
    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert record["schema_version"] == "m7_comparison.v1"
    # The pre-existing sibling must remain untouched.
    assert json.loads((output_dir / "m7-18-prev.json").read_text()) == {"stale": True}


# ---------------------------------------------------------------------------
# M7-18: Fixed vs ReAct comparison eval tests
# ---------------------------------------------------------------------------


def test_percentile_nearest_rank_basic():
    """Nearest-rank percentile returns the expected value for known input."""
    values = [10, 20, 30, 40, 50, 60]
    assert eval_runner._percentile(values, 95) == 60.0
    assert eval_runner._percentile(values, 50) == 30.0
    assert eval_runner._percentile(values, 100) == 60.0


def test_percentile_empty_returns_zero():
    """An empty duration list must not raise."""
    assert eval_runner._percentile([], 95) == 0.0


def test_estimate_cost_zero_for_zero_tokens():
    """No tokens means no estimated cost."""
    assert eval_runner._estimate_cost(0) == 0.0


def test_estimate_cost_positive_for_positive_tokens():
    """A positive token count yields a positive, finite cost."""
    cost = eval_runner._estimate_cost(1_000_000)
    assert cost > 0
    # 60% input at $0.15/1M + 40% output at $0.60/1M = 0.09 + 0.24 = 0.33
    assert abs(cost - 0.33) < 0.001


def test_extract_react_observability_reads_state_counters():
    """React counters on state are surfaced; missing fields default safely."""
    state = SimpleNamespace(
        react_steps=3,
        react_llm_calls=3,
        react_total_tokens=1200,
        react_degraded=False,
        react_termination_reason="finish",
        react_tool_results_truncated=0,
        trace_steps=[
            {
                "step": "react_tool_result",
                "detail": {"tool_name": "get_changed_hunks", "result": {"error_code": None}},
            },
            {
                "step": "react_tool_result",
                "detail": {"tool_name": "nonexistent_tool", "result": {"error_code": "not_found"}},
            },
        ],
    )
    obs = eval_runner._extract_react_observability(state)
    assert obs["react_steps"] == 3
    assert obs["react_llm_calls"] == 3
    assert obs["react_total_tokens"] == 1200
    assert obs["react_degraded"] is False
    assert obs["react_termination_reason"] == "finish"
    assert obs["unknown_tool_count"] == 1
    assert obs["budget_exhausted"] is False


def test_extract_react_observability_marks_budget_exhausted():
    """Termination reasons in the exhaustion set are flagged."""
    state = SimpleNamespace(
        react_steps=8,
        react_llm_calls=8,
        react_total_tokens=16000,
        react_degraded=True,
        react_termination_reason="max_steps_exhausted",
        react_tool_results_truncated=0,
        trace_steps=[],
    )
    obs = eval_runner._extract_react_observability(state)
    assert obs["budget_exhausted"] is True
    assert obs["react_degraded"] is True


def test_run_one_case_passes_review_mode_to_request(tmp_path, monkeypatch):
    """run_one_case must forward review_mode to ReviewRequest."""
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)
    captured = []

    class CapturingReviewService:
        def review(self, request):
            captured.append(request)
            return SimpleNamespace(
                state=SimpleNamespace(
                    issues=[],
                    react_steps=0,
                    react_llm_calls=0,
                    react_total_tokens=0,
                    react_degraded=False,
                    react_termination_reason="",
                    react_tool_results_truncated=0,
                    trace_steps=[],
                )
            )

    monkeypatch.setattr(eval_runner, "ReviewService", CapturingReviewService)

    eval_runner.run_one_case(case_dir, tmp_path, review_mode="react", use_llm=True)

    assert captured[0].review_mode == "react"


def test_run_one_case_includes_react_observability_in_result(tmp_path, monkeypatch):
    """React counters must appear in the per-case result dict."""
    case_dir = make_case(tmp_path, expected_categories=[], should_find=False)

    class ReactStateReviewService:
        def review(self, request):
            return SimpleNamespace(
                state=SimpleNamespace(
                    issues=[],
                    react_steps=2,
                    react_llm_calls=2,
                    react_total_tokens=500,
                    react_degraded=False,
                    react_termination_reason="finish",
                    react_tool_results_truncated=0,
                    trace_steps=[],
                )
            )

    monkeypatch.setattr(eval_runner, "ReviewService", ReactStateReviewService)

    result = eval_runner.run_one_case(case_dir, tmp_path, review_mode="react", use_llm=True)

    assert result["react_steps"] == 2
    assert result["react_llm_calls"] == 2
    assert result["react_total_tokens"] == 500
    assert result["budget_exhausted"] is False
    assert result["unknown_tool_count"] == 0
    assert result["review_mode"] == "react"


def test_build_react_provider_cross_file_case_calls_read_and_finish(tmp_path):
    """Cases with discovered documentation must read evidence and finish.

    The factory discovers docs by scanning the filesystem, not by reading the
    case manifest.  The finding location is derived from the diff's added lines.
    """
    # Create a documentation file in the repo root (discovered by filesystem scan).
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "payment-contract.md").write_text("# Contract\n", encoding="utf-8")

    changed_files = parse_diff(
        "diff --git a/api/checkout.py b/api/checkout.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def f():\n"
        "+    pass\n"
    )

    provider = eval_runner._build_react_provider(tmp_path, tmp_path, changed_files)

    # Three scripted responses: get_changed_hunks, read_file_context, finish_review.
    placeholder_history = [{"role": "tool", "call_id": "call-hunks", "result": {}}]

    resp1 = provider.complete({"history": []})
    assert resp1.tool_calls[0].name == "get_changed_hunks"

    resp2 = provider.complete({"history": placeholder_history})
    assert resp2.tool_calls[0].name == "read_file_context"
    assert resp2.tool_calls[0].arguments["path"] == "docs/payment-contract.md"

    resp3 = provider.complete({"history": placeholder_history})
    assert resp3.tool_calls[0].name == "finish_review"
    findings = resp3.tool_calls[0].arguments["findings"]
    assert len(findings) == 1
    # File and line are derived from the diff's first added line.
    assert findings[0]["file"] == "api/checkout.py"
    assert findings[0]["line"] == 2
    assert findings[0]["severity"] == "high"
    assert findings[0]["category"] == "exception_handling"
    assert findings[0]["evidence"] == "docs/payment-contract.md"


def test_build_react_provider_does_not_inject_ground_truth(tmp_path):
    """The factory must not contain content from expected_findings or repository_context.

    The factory signature no longer receives ``expected`` at all, structurally
    preventing ground-truth injection.  This test guards against a regression
    that re-introduces ``expected`` reading.
    """
    # Create a documentation file that the factory will discover via filesystem.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "contract.md").write_text("# Contract\n", encoding="utf-8")

    changed_files = parse_diff(
        "diff --git a/app.py b/app.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def f():\n"
        "+    pass\n"
    )

    # The factory receives (case_dir, repo_root, changed_files) — no expected.
    provider = eval_runner._build_react_provider(tmp_path, tmp_path, changed_files)

    # Drain all three scripted responses.
    placeholder_history = [{"role": "tool", "call_id": "call-hunks", "result": {}}]
    provider.complete({"history": []})
    provider.complete({"history": placeholder_history})
    resp3 = provider.complete({"history": placeholder_history})

    assert resp3.tool_calls[0].name == "finish_review"
    findings = resp3.tool_calls[0].arguments["findings"]
    assert len(findings) == 1

    # The finding's file and line must come from the diff, not from any manifest.
    assert findings[0]["file"] == "app.py"
    assert findings[0]["line"] == 2

    # The evidence path is discovered via filesystem scan, not from required_paths.
    assert findings[0]["evidence"] == "docs/contract.md"


def test_build_react_provider_ignores_repository_context_metadata(tmp_path):
    """The factory must discover docs via filesystem, not via repository_context.

    This is the P0-1 regression guard: even if a case manifest declares
    ``repository_context.required_paths`` pointing to a non-existent file,
    the factory must discover the actual documentation by scanning the
    filesystem, not by reading the manifest.
    """
    # Create a fixture root with a real doc file.
    fixture_root = tmp_path / "repository_context"
    docs_dir = fixture_root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "payment-contract.md").write_text("# Contract\n", encoding="utf-8")

    # The factory receives the fixture root as repo_root; it does NOT receive
    # expected, so repository_context metadata is structurally inaccessible.
    changed_files = parse_diff(
        "diff --git a/api/checkout.py b/api/checkout.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def f():\n"
        "+    pass\n"
    )

    provider = eval_runner._build_react_provider(tmp_path, fixture_root, changed_files)

    placeholder_history = [{"role": "tool", "call_id": "call-hunks", "result": {}}]
    resp1 = provider.complete({"history": []})
    assert resp1.tool_calls[0].name == "get_changed_hunks"

    resp2 = provider.complete({"history": placeholder_history})
    assert resp2.tool_calls[0].name == "read_file_context"
    # The path is discovered via filesystem scan of fixture_root, not from
    # repository_context.required_paths (which the factory never sees).
    assert resp2.tool_calls[0].arguments["path"] == "docs/payment-contract.md"


def test_build_react_provider_non_cross_file_case_finishes_empty(tmp_path):
    """Cases without documentation files must finish with no findings."""
    # tmp_path has no *.md files — the factory should finish empty.
    changed_files = parse_diff(
        "diff --git a/app.py b/app.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def f():\n"
        "+    pass\n"
    )

    provider = eval_runner._build_react_provider(tmp_path, tmp_path, changed_files)

    placeholder_history = [{"role": "tool", "call_id": "call-hunks", "result": {}}]

    resp1 = provider.complete({"history": []})
    assert resp1.tool_calls[0].name == "get_changed_hunks"

    resp2 = provider.complete({"history": placeholder_history})
    assert resp2.tool_calls[0].name == "finish_review"
    assert resp2.tool_calls[0].arguments["findings"] == []


def test_build_per_case_diff_identifies_new_fp_and_fixed_fn():
    """Per-case diff must separate new false positives from fixed false negatives."""
    fixed_results = [
        {"case_id": "a", "passed": False, "expected_categories": ["x"],
         "actual_categories": []},
        {"case_id": "b", "passed": True, "expected_categories": ["y"],
         "actual_categories": ["y"]},
    ]
    react_results = [
        {"case_id": "a", "passed": False, "expected_categories": ["x"],
         "actual_categories": ["x", "llm"]},
        {"case_id": "b", "passed": True, "expected_categories": ["y"],
         "actual_categories": ["y"]},
    ]

    diffs = eval_runner._build_per_case_diff(fixed_results, react_results)

    assert diffs[0]["case_id"] == "a"
    assert diffs[0]["new_false_positives"] == ["llm"]
    assert diffs[0]["fixed_false_negatives"] == ["x"]
    assert diffs[1]["case_id"] == "b"
    assert diffs[1]["new_false_positives"] == []
    assert diffs[1]["fixed_false_negatives"] == []


def test_run_mode_comparison_returns_both_modes_and_diff(tmp_path, monkeypatch):
    """run_mode_comparison must run fixed and react and compute a diff."""
    cases_dir = tmp_path / "cases"
    (cases_dir / "c1").mkdir(parents=True)

    call_log = []

    def fake_run_eval(cases_dir, repo_root, **kwargs):
        mode = kwargs.get("review_mode", "fixed")
        call_log.append(mode)
        if mode == "fixed":
            return {
                "precision": 0.8, "recall": 0.6, "f1": 0.7,
                "p95_duration_ms": 50, "total_tokens": 0, "total_llm_calls": 0,
                "estimated_cost_usd": 0.0, "unknown_tool_count": 0,
                "budget_exhausted_count": 0,
                "results": [
                    {"case_id": "c1", "passed": False, "expected_categories": ["x"],
                     "actual_categories": []},
                ],
            }
        return {
            "precision": 0.7, "recall": 0.6, "f1": 0.65,
            "p95_duration_ms": 100, "total_tokens": 2000, "total_llm_calls": 6,
            "estimated_cost_usd": 0.001, "unknown_tool_count": 0,
            "budget_exhausted_count": 0,
            "results": [
                {"case_id": "c1", "passed": False, "expected_categories": ["x"],
                 "actual_categories": ["x", "llm"]},
            ],
        }

    monkeypatch.setattr(eval_runner, "run_eval", fake_run_eval)

    comparison = eval_runner.run_mode_comparison(cases_dir, tmp_path)

    assert "fixed" in comparison
    assert "react" in comparison
    assert "comparison" in comparison
    assert call_log == ["fixed", "react"]

    agg = comparison["comparison"]["aggregate_diff"]
    assert agg["precision_delta"] == pytest.approx(-0.1)
    assert agg["recall_delta"] == pytest.approx(0.0)
    assert agg["token_delta"] == 2000

    per_case = comparison["comparison"]["per_case_diff"]
    assert per_case[0]["new_false_positives"] == ["llm"]
    assert per_case[0]["fixed_false_negatives"] == ["x"]


def test_build_comparison_record_preserves_configuration_and_metrics():
    """The comparison record must be JSON-serializable and contain all blocks."""
    comparison = {
        "fixed": {"precision": 1.0, "results": []},
        "react": {"precision": 0.5, "results": []},
        "comparison": {
            "per_case_diff": [],
            "aggregate_diff": {"precision_delta": -0.5},
        },
    }

    record = eval_runner.build_comparison_record(
        comparison,
        cases_dir="evals/cases",
        repo_root=".",
        llm_provider="mock",
        commit="abc123",
        worktree_state="clean",
        worktree_diff_sha256=None,
        comparison_output="evals/comparisons/m7-18.json",
    )

    assert record["schema_version"] == "m7_comparison.v1"
    assert record["commit"] == "abc123"
    assert record["configuration"]["react_budget"]["max_steps"] > 0
    assert record["fixed"]["precision"] == 1.0
    assert record["react"]["precision"] == 0.5
    assert record["comparison"]["aggregate_diff"]["precision_delta"] == -0.5
    # Must be JSON-serializable.
    json.dumps(record)


def test_main_compare_modes_writes_output_and_prints_summary(tmp_path, monkeypatch):
    """CLI --compare-modes must persist a readable record and print metrics."""
    output_path = tmp_path / "comparison.json"
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=False,
        llm_provider="mock",
        baseline_output=None,
        commit="unknown",
        worktree_state="unknown",
        compare_modes=True,
        comparison_output=str(output_path),
    )
    comparison = {
        "fixed": {
            "precision": 0.8, "recall": 0.6, "f1": 0.7,
            "p95_duration_ms": 50, "total_tokens": 0, "total_llm_calls": 0,
            "estimated_cost_usd": 0.0, "unknown_tool_count": 0,
            "budget_exhausted_count": 0, "results": [],
        },
        "react": {
            "precision": 0.7, "recall": 0.6, "f1": 0.65,
            "p95_duration_ms": 100, "total_tokens": 2000, "total_llm_calls": 6,
            "estimated_cost_usd": 0.001, "unknown_tool_count": 0,
            "budget_exhausted_count": 0, "results": [],
        },
        "comparison": {
            "per_case_diff": [],
            "aggregate_diff": {
                "precision_delta": -0.1, "recall_delta": 0.0, "f1_delta": -0.05,
                "p95_latency_delta_ms": 50, "token_delta": 2000,
                "llm_call_delta": 6, "cost_delta_usd": 0.001,
            },
        },
    }

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(eval_runner, "run_mode_comparison", lambda **kwargs: comparison)
    monkeypatch.setattr(
        eval_runner,
        "capture_source_revision",
        lambda repo_root, **kwargs: eval_runner.SourceRevision("abc", "clean", None),
    )

    eval_runner.main()

    assert output_path.exists()
    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert record["schema_version"] == "m7_comparison.v1"
    assert record["fixed"]["precision"] == 0.8
    assert record["react"]["precision"] == 0.7


def test_run_eval_includes_observability_metrics(tmp_path, monkeypatch):
    """run_eval must include p95, tokens, cost, and budget metrics in the output."""
    cases_dir = tmp_path / "cases"
    (cases_dir / "c1").mkdir(parents=True)

    def fake_run_one_case(case_dir, **kwargs):
        return {
            "case_id": case_dir.name,
            "passed": True,
            "json_valid": True,
            "false_positive": False,
            "is_negative_case": False,
            "findings_count": 1,
            "tp": 1, "fp": 0, "fn": 0,
            "duration_ms": 42,
            "error": "",
            "review_mode": kwargs.get("review_mode", "fixed"),
            "react_steps": 1, "react_llm_calls": 1, "react_total_tokens": 100,
            "react_degraded": False, "react_termination_reason": "finish",
            "react_tool_results_truncated": 0,
            "unknown_tool_count": 0, "budget_exhausted": False,
        }

    monkeypatch.setattr(eval_runner, "run_one_case", fake_run_one_case)

    metrics = eval_runner.run_eval(cases_dir, tmp_path, review_mode="react")

    assert "p95_duration_ms" in metrics
    assert metrics["p95_duration_ms"] == 42.0
    assert metrics["total_tokens"] == 100
    assert metrics["total_llm_calls"] == 1
    assert metrics["estimated_cost_usd"] > 0
    assert metrics["unknown_tool_count"] == 0
    assert metrics["budget_exhausted_count"] == 0
