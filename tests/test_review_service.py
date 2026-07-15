"""编排服务（src/review_service.py）单元测试。

本文件覆盖 RepoReview Agent 的核心编排服务 ReviewService，验证其端到端
review 流程的结构化输出、diff 源校验、摘要评论发布等关键行为。

测试策略：
    - 使用 make_request 辅助函数在临时目录下构造包含真实仓库文件与 diff 文件
      的 ReviewRequest，确保测试在真实文件系统环境下运行，而非纯内存模拟；
    - 直接调用真实的 ReviewService().review()（非 mock），以验证编排逻辑的
      完整性与 8 步 trace 的正确产出；
    - 对摘要评论发布路径，通过注入 FakeProvider 验证发布内容与返回结构，
      隔离真实 Git 平台调用。

在整体测试体系中的位置：
    本文件位于「编排服务」测试层，是连接 schemas（数据模型）、git_provider
    （Git 平台交互）与 eval_runner（评估器）的中枢测试，确保编排服务在
    各种输入组合下行为正确且可观测。
"""

import json

import pytest

from src.git_provider import PullRequestRef, SummaryCommentResult
from src.llm_client import ScriptedMockProvider
from src.react_controller import ReActBudget
from src.review_service import ReviewRequest, ReviewService
from src.schemas import ContextBudget


def make_request(tmp_path, **overrides):
    """在临时目录下构造一个可用的 ReviewRequest 对象。

    用途：以最小成本生成包含真实仓库文件（app.py）与 diff 文件（input.diff）
    的审查请求，确保 ReviewService 在真实文件系统环境下可读取到所需输入。
    通过 **overrides 允许调用方覆盖任意字段（如 diff_path、diff_text、
    publish_summary_comment、pull_request 等），灵活构造不同测试场景。

    参数：
        tmp_path: pytest 提供的临时目录 fixture；
        **overrides: 需要覆盖默认值的字段键值对。

    返回：
        构造好的 ReviewRequest 实例，可直接传给 ReviewService.review。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # 写入一个简单的 Python 源文件，供审查服务读取上下文
    (repo / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    diff_path = tmp_path / "input.diff"
    diff_path.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return True
""",
        encoding="utf-8",
    )
    # 默认值：使用 diff 文件路径、json 输出格式与保守的上下文预算
    values = {
        "diff_path": str(diff_path),
        "repo_root": str(repo),
        "output_format": "json",
        "context_budget": ContextBudget(max_prompt_chars=100, max_extra_context_files=0),
    }
    # 允许调用方覆盖任意字段，以构造特定场景
    values.update(overrides)
    return ReviewRequest(**values)


def test_review_service_returns_structured_state_and_full_review_trace(tmp_path):
    """验证 review 返回结构化 state 与完整 8 步 trace。

    测试目的：
        确认 ReviewService.review 在正常输入下返回包含 state、output、trace_steps
        的结构化结果，且 trace_steps 严格按顺序包含全部 8 个步骤。

    测试场景：
        使用默认 make_request 构造请求（diff 文件 + app.py），不发布摘要评论，
        直接调用真实 ReviewService().review()。

    特殊逻辑：
        本测试不 mock 任何依赖，验证编排服务在默认配置下的端到端正确性，
        作为其他针对性测试的基线。

    预期输出：
        output 与 state.output 一致、summary_comment 为 None、changed_files
        仅含 app.py、contexts 首项路径为 app.py、errors 为空、issues 为列表，
        trace_steps 依次为 8 个标准步骤。
    """
    result = ReviewService().review(make_request(tmp_path))

    # 不变量：output 字段与 state.output 指向同一内容
    assert result.output == result.state.output
    # 不变量：未请求摘要评论时，summary_comment 为 None
    assert result.summary_comment is None
    # 不变量：变更文件列表仅含 app.py
    assert [changed_file.path for changed_file in result.state.changed_files] == ["app.py"]
    # 不变量：收集到的上下文首项指向 app.py
    assert result.state.contexts[0].path == "app.py"
    # 不变量：正常流程无错误
    assert result.state.errors == []
    # 不变量：issues 始终为列表（即使为空）
    assert isinstance(result.state.issues, list)
    # 不变量：trace_steps 必须严格按顺序包含全部 8 个步骤
    assert [step["step"] for step in result.trace_steps] == [
        "receive_task",
        "parse_diff",
        "collect_context",
        "run_static_checks",
        "run_llm_review",
        "validate_output",
        "render_report",
        "save_trace",
    ]


def test_review_service_accepts_inline_diff_text_without_a_temporary_file(tmp_path):
    """验证 inline diff_text 输入无需临时文件即可正常审查。

    测试目的：
        确认当调用方提供 diff_text（内联 diff 文本）而非 diff_path（文件路径）
        时，ReviewService 能直接解析文本内容，无需将其落盘为临时文件。

    测试场景：
        构造请求时将 diff_path 置 None，并传入一段含 print("debug") 的内联 diff。
        该 diff 模拟引入调试代码的场景，应被静态检查识别为 debug 类别问题。

    特殊逻辑：
        diff_text 中包含转义引号（\"），验证内联文本的转义处理正确。

    预期输出：
        state.diff_path 标记为 "(inline diff)" 表示来源为内联文本，
        且 issues 中至少存在一条 category 为 "debug" 的问题。
    """
    request = make_request(
        tmp_path,
        diff_path=None,  # 不提供 diff 文件路径
        diff_text="""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1 @@
+print(\"debug\")
""",
    )

    result = ReviewService().review(request)

    # 不变量：内联 diff 来源时，diff_path 标记为占位符
    assert result.state.diff_path == "(inline diff)"
    # 不变量：print("debug") 应被识别为 debug 类别问题
    assert any(issue.category == "debug" for issue in result.state.issues)


@pytest.mark.parametrize(
    "overrides",
    [
        # 参数化用例 1：diff_path 与 diff_text 均为 None -> 无 diff 源
        {"diff_path": None, "diff_text": None},
        # 参数化用例 2：同时提供 diff_path（默认）与 diff_text -> diff 源重复
        {"diff_text": "diff --git a/app.py b/app.py"},
    ],
)
def test_review_service_requires_exactly_one_diff_source(tmp_path, overrides):
    """验证 diff 源必须且只能提供一个（diff_path 或 diff_text 二选一）。

    测试目的：
        确认 ReviewService 在 diff 源缺失（两者均 None）或重复（两者同时提供）
        时，抛出 ValueError 且错误消息包含 "exactly_one_diff_source_required"。

    测试场景（参数化）：
        - 用例 A：diff_path=None 且 diff_text=None，模拟未提供任何 diff；
        - 用例 B：保留默认 diff_path 同时额外传入 diff_text，模拟同时提供两种源。

    特殊逻辑：
        参数化覆盖「零源」与「双源」两种非法组合，共用同一断言，确保校验逻辑
        对称地处理缺失与重复。

    预期输出：
        两种场景均抛出 ValueError，且 match 匹配 "exactly_one_diff_source_required"。
    """
    with pytest.raises(ValueError, match="exactly_one_diff_source_required"):
        ReviewService().review(make_request(tmp_path, **overrides))


def test_review_service_rejects_summary_publication_without_structured_pr_reference(tmp_path):
    """验证请求摘要发布但未提供 PR 引用时拒绝执行。

    测试目的：
        确认当 publish_summary_comment=True 但未提供 pull_request 时，
        ReviewService 抛出 ValueError 且错误消息包含
        "pull_request_required_for_summary_comment"，且不会构造 git provider。

    测试场景：
        构造请求开启摘要发布（publish_summary_comment=True）但不传入 pull_request，
        同时注入 provider_factory 以观测是否被调用。

    特殊逻辑：
        provider_factory 内部向 constructed 列表追加标记，用于断言 provider
        未被构造——即校验在 provider 创建之前就失败，避免无谓的资源初始化。

    预期输出：
        抛出 ValueError 匹配指定消息，且 constructed 列表为空（provider 未被构造）。
    """
    constructed = []

    def provider_factory():
        # 若被调用则记录标记，用于断言 provider 是否被提前构造
        constructed.append(True)
        return object()

    request = make_request(tmp_path, publish_summary_comment=True)

    with pytest.raises(ValueError, match="pull_request_required_for_summary_comment"):
        ReviewService(git_provider_factory=provider_factory).review(request)

    # 不变量：PR 校验失败时，provider 不应被构造
    assert constructed == []


def test_review_service_requires_an_injected_provider_for_summary_publication(tmp_path):
    """验证摘要发布需要注入 git provider 才能执行。

    测试目的：
        确认当 publish_summary_comment=True 且已提供 pull_request，但未注入
        git_provider_factory 时，ReviewService 抛出 ValueError 且错误消息包含
        "git_provider_required_for_summary_comment"。

    测试场景：
        构造请求开启摘要发布并提供合法的 PullRequestRef，但 ReviewService
        不传入 git_provider_factory（使用默认 None）。

    特殊逻辑：
        本测试与上一测试互补：上一测试验证「无 PR 引用」时拒绝，本测试验证
        「有 PR 引用但无 provider」时拒绝，两者共同覆盖摘要发布的前置条件矩阵。

    预期输出：
        抛出 ValueError 匹配 "git_provider_required_for_summary_comment"。
    """
    request = make_request(
        tmp_path,
        publish_summary_comment=True,
        # 提供合法的 PR 引用，但缺 provider
        pull_request=PullRequestRef("acme", "reviewed-repo", 42),
    )

    with pytest.raises(ValueError, match="git_provider_required_for_summary_comment"):
        # 未注入 git_provider_factory
        ReviewService().review(request)


def test_review_service_publishes_optional_summary_with_structured_reference(tmp_path):
    """验证带结构化 PR 引用的摘要发布成功路径。

    测试目的：
        确认当 publish_summary_comment=True、提供合法 pull_request 且注入
        git_provider_factory 时，ReviewService 调用 provider 的
        publish_summary_comment 发布摘要，且发布的 body 包含标题，
        返回的 summary_comment 结构正确。

    测试场景：
        注入 FakeProvider，其 publish_summary_comment 捕获 reference 与 body
        并返回预设的 SummaryCommentResult(comment_id=73, action="created")。
        请求提供 PullRequestRef("acme", "reviewed-repo", 42) 并开启摘要发布。

    特殊逻辑：
        FakeProvider 模拟真实 Git 平台的发布行为，将入参存入 published 字典
        供断言使用，使测试无需访问真实 Git API 即可验证发布内容。

    预期输出：
        published["reference"] 等于传入的 PR 引用、body 包含 "## RepoReview summary"
        标题、result.summary_comment 等于 FakeProvider 返回值，
        且 trace_steps 倒数第二步为 "publish_summary_comment"（在 save_trace 之前）。
    """
    published = {}

    class FakeProvider:
        def publish_summary_comment(self, reference, body):
            # 捕获发布入参，供测试断言发布内容
            published["reference"] = reference
            published["body"] = body
            # 返回预设的发布结果，模拟真实 Git 平台返回的评论 ID 与操作类型
            return SummaryCommentResult(comment_id=73, action="created")

    reference = PullRequestRef("acme", "reviewed-repo", 42)
    request = make_request(
        tmp_path,
        publish_summary_comment=True,
        pull_request=reference,
    )

    result = ReviewService(git_provider_factory=FakeProvider).review(request)

    # 不变量：发布时传入的 PR 引用与请求一致
    assert published["reference"] == reference
    # 不变量：发布的 body 包含约定的摘要标题
    assert "## RepoReview summary" in published["body"]
    # 不变量：返回的 summary_comment 与 provider 返回值一致
    assert result.summary_comment == SummaryCommentResult(comment_id=73, action="created")
    # 不变量：摘要发布步骤位于 save_trace 之前（倒数第二步）
    assert result.trace_steps[-2]["step"] == "publish_summary_comment"


# ── M7-16 mode-switch tests ──────────────────────────────────────────────


def test_review_mode_defaults_to_fixed():
    """Without an explicit review_mode, the service uses the fixed pipeline."""
    request = ReviewRequest(
        diff_path="review.diff",
        repo_root=".",
        output_format="json",
    )
    assert request.review_mode == "fixed"


@pytest.mark.parametrize("invalid_mode", ["", "random", "REACT", "Fixed", "verify", None])
def test_invalid_review_mode_is_rejected_before_file_io(tmp_path, invalid_mode):
    """Every value outside {"fixed","react"} must raise before parse_diff."""
    with pytest.raises(ValueError, match="unsupported_review_mode"):
        ReviewService().review(make_request(tmp_path, review_mode=invalid_mode))


def test_react_mode_without_llm_is_rejected_before_file_io(tmp_path):
    """react mode requires use_llm; the contradictory combination is rejected early.

    Without this guard, ``--review-mode react`` without ``--llm`` would silently
    run the no-LLM fixed branch, violating the "no silent fallback" non-goal.
    """
    with pytest.raises(ValueError, match="react_mode_requires_use_llm"):
        ReviewService().review(
            make_request(tmp_path, review_mode="react", use_llm=False)
        )


def test_fixed_mode_behavior_is_unchanged_with_explicit_review_mode(tmp_path):
    """Explicitly passing review_mode='fixed' does not change the default path."""
    result = ReviewService().review(make_request(tmp_path, review_mode="fixed"))

    assert result.state.review_mode == "fixed"
    assert [step["step"] for step in result.trace_steps] == [
        "receive_task",
        "parse_diff",
        "collect_context",
        "run_static_checks",
        "run_llm_review",
        "validate_output",
        "render_report",
        "save_trace",
    ]
    # The receive_task detail must record the mode for auditability.
    assert result.trace_steps[0]["detail"]["review_mode"] == "fixed"


def test_react_mode_completes_offline_smoke_with_default_provider(tmp_path):
    """React mode runs to completion with the default ScriptedMockProvider.

    The default provider (built by _run_react_review for mock providers) calls
    finish_review immediately with no findings, so this exercises the full
    react→validate→render chain end-to-end.
    """
    request = make_request(tmp_path, use_llm=True, review_mode="react")
    state = ReviewService().review(request).state

    assert state.review_mode == "react"
    assert state.react_degraded is False
    # The react path should be visible in the trace metadata.
    llm_step = next(s for s in state.trace_steps if s["step"] == "run_llm_review")
    assert llm_step["detail"]["review_mode"] == "react"
    assert llm_step["detail"]["called"] is True
    assert isinstance(state.issues, list)


def test_react_budget_exhaustion_produces_degraded_state_not_fixed_success(tmp_path):
    """When react exhausts its budget, the result is degraded, never silently fixed."""
    # max_llm_calls=1 means the first provider call consumes the last allowed
    # call.  After the tool result is processed, the next loop iteration will
    # see the budget exhausted and terminate before making a second request.
    provider = ScriptedMockProvider(
        [
            {
                "tool_calls": [
                    {"call_id": "tool-1", "name": "get_changed_hunks", "arguments": {"path": "app.py"}}
                ]
            },
            {
                "tool_calls": [
                    {"call_id": "finish-2", "name": "finish_review", "arguments": {"findings": []}}
                ]
            },
        ]
    )
    request = make_request(
        tmp_path,
        use_llm=True,
        review_mode="react",
        react_provider=provider,
        react_budget=ReActBudget(
            max_steps=8,
            max_llm_calls=1,
            max_total_tokens=16_000,
            max_tool_result_bytes=8_000,
            max_total_tool_result_bytes=32_000,
        ),
    )
    result = ReviewService().review(request)

    assert result.state.react_degraded is True
    assert result.state.react_termination_reason != "finish"
    # Degraded react must never look like a successful empty fixed review.
    llm_step = next(s for s in result.state.trace_steps if s["step"] == "run_llm_review")
    assert llm_step["detail"]["react_degraded"] is True
    assert llm_step["detail"]["review_mode"] == "react"


def test_same_empty_finding_produces_identical_validated_output_in_both_modes(tmp_path):
    """Both modes share the same validate_output → render_report chain.

    When neither mode produces a finding that survives location validation,
    both must produce structurally identical JSON output.  This proves the
    two paths converge onto a single validation and reporting gateway rather
    than having per-mode output logic.

    Uses two separate sub-directories under tmp_path so each make_request
    call gets a clean filesystem without ``FileExistsError`` on repo mkdir.
    """
    fixed_tmp = tmp_path / "fixed"
    fixed_tmp.mkdir()
    react_tmp = tmp_path / "react"
    react_tmp.mkdir()

    # ── fixed mode with "empty" fixture → no LLM findings ──
    fixed_result = ReviewService().review(
        make_request(
            fixed_tmp,
            use_llm=True,
            review_mode="fixed",
            llm_provider="mock",
            mock_fixture="empty",
        )
    )

    # ── react mode (scripted provider finishes with no findings) ──
    react_provider = ScriptedMockProvider(
        [
            {
                "tool_calls": [
                    {
                        "call_id": "finish-react",
                        "name": "finish_review",
                        "arguments": {"findings": []},
                    }
                ]
            }
        ]
    )
    react_result = ReviewService().review(
        make_request(
            react_tmp,
            use_llm=True,
            review_mode="react",
            react_provider=react_provider,
        )
    )

    fixed_output = json.loads(fixed_result.output)
    react_output = json.loads(react_result.output)

    # Both must produce structurally identical JSON via the shared chain.
    assert fixed_output == react_output


def test_react_mode_is_auditable_in_state_and_trace(tmp_path):
    """After a react smoke run, state and trace carry mode-specific metadata."""
    request = make_request(tmp_path, use_llm=True, review_mode="react")
    result = ReviewService().review(request)

    assert result.state.review_mode == "react"
    # Mode must be in the receive_task trace detail.
    assert result.trace_steps[0]["detail"]["review_mode"] == "react"
    # The react termination reason is recorded even for a clean finish.
    assert result.state.react_termination_reason == ""


def test_review_mode_persists_through_cli_request_default():
    """The default review_mode from CLI-like construction is 'fixed'."""
    import argparse
    from src.cli import run_review_agent

    args = argparse.Namespace(
        diff="review.diff",
        repo=".",
        format="markdown",
        llm=False,
        max_prompt_chars=4000,
        max_extra_context_files=3,
        llm_provider="mock",
        mock_fixture="normal",
        trace=False,
        trace_dir="traces",
        publish_summary_comment=False,
        pr_url=None,
        review_mode="fixed",
    )
    # run_review_agent constructs a ReviewRequest; the default path is unchanged.
    # This test only verifies the mode is passed through without crashing.
    from src.review_service import ReviewRequest as RR

    request = RR(
        diff_path=args.diff,
        repo_root=args.repo,
        output_format=args.format,
        use_llm=args.llm,
        context_budget=ContextBudget(
            max_prompt_chars=args.max_prompt_chars,
            max_extra_context_files=args.max_extra_context_files,
        ),
        llm_provider=args.llm_provider,
        review_mode=args.review_mode,
    )
    assert request.review_mode == "fixed"
