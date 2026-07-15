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
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import eval_runner
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


def test_main_rejects_llm_enabled_fixed_baseline(tmp_path, monkeypatch):
    """固定基线不得和 LLM 启用的运行结果混用。"""
    output_path = tmp_path / "baseline.json"
    args = SimpleNamespace(
        cases="evals/cases",
        repo=".",
        llm=True,
        llm_provider="mock",
        baseline_output=str(output_path),
        commit="abc123",
        worktree_state="clean",
    )

    calls = []

    def unexpected_run_eval(**kwargs):
        calls.append(kwargs)
        raise AssertionError("invalid fixed baseline must not start Eval")

    monkeypatch.setattr(eval_runner, "parse_args", lambda: args)
    monkeypatch.setattr(eval_runner, "run_eval", unexpected_run_eval)

    with pytest.raises(ValueError, match="fixed_baseline_requires_llm_disabled"):
        eval_runner.main()

    assert not output_path.exists()
    assert calls == []


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
    )
    metrics = {
        "cases": 0,
        "category_hit_rate": 0.0,
        "false_positive_count": 0,
        "json_valid_rate": 0.0,
        "average_findings": 0.0,
        "average_duration_ms": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "false_positive_rate": 0.0,
        "false_negative_count": 0,
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
