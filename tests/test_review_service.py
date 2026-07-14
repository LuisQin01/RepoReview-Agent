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

import pytest

from src.git_provider import PullRequestRef, SummaryCommentResult
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
