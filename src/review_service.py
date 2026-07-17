"""
编排服务层（ReviewService）：是整个 RepoReview Agent pipeline 的核心。

职责：按固定顺序串联代码审查的各步骤，并对每步计时与记录轨迹（trace）。
该模块体现了"模板方法模式"：review() 方法定义了固定的执行骨架
（receive_task -> parse_diff -> collect_context -> run_static_checks
 -> run_llm_review(可选) -> validate_output -> render_report
 -> publish_summary_comment(可选) -> save_trace），各步骤调用具体组件完成。

在整体架构中的位置：
- 上游：CLI / API / workflow 适配器构造 ReviewRequest 并调用 ReviewService.review()。
- 下游：调用 diff_parser / file_context / reviewers / llm_reviewer /
  reporter / git_provider / trace 等具体组件执行各步骤。

设计理由：将"流程编排"与"具体能力实现"分离，便于单步替换或测试，
同时通过 record_step 统一记录每步耗时与详情，支撑性能分析与 eval。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Callable, Optional

from .agent_state import ReviewState
from .diff_parser import parse_diff
from .file_context import collect_file_contexts
from .git_provider import GitProvider, PullRequestRef, SummaryCommentResult
from .llm_client import LLMClientError, ScriptedMockProvider, get_call_model
from .llm_reviewer import review_with_llm
from .react_controller import ReActBudget, ReActController
from .reporter import (
    render_json_report,
    render_markdown_report,
    render_summary_comment,
)
from .review_tools import (
    ChangedHunksTool,
    FinishReview,
    ReadFileContextTool,
    SearchPythonSymbolTool,
    ToolDispatcher,
)
from .reviewers import review_changed_files
from .schemas import ContextBudget
from .trace import (
    redact_sensitive_structure,
    sanitize_trace_text,
    save_trace,
)
from .validation import validate_issue_locations

# Valid review_mode values.  Every other input must be rejected before state creation.
_VALID_REVIEW_MODES = frozenset({"fixed", "react"})


@dataclass(frozen=True)
class ReviewRequest:
    """结构化输入，被 CLI / API / workflow 三种适配器共用。

    使用 frozen=True 保证请求对象不可变，避免在 pipeline 执行过程中
    被意外篡改输入，便于并发安全与调试追溯。

    强制约束：diff_path 与 diff_text 必须二选一（详见 review() 中的校验），
    适配器根据自身场景选择更合适的来源。

    字段说明：
        diff_path: diff 文件路径，CLI 适配器常用；为 None 时表示使用内联 diff。
        repo_root: 仓库根目录，用于采集上下文文件，默认当前目录。
        diff_text: 内联 diff 文本，HTTP 适配器可直接透传请求体，避免落临时文件。
        output_format: 输出格式，"markdown" 或 "json"。
        use_llm: 是否启用 LLM 审查步骤；为 False 时跳过 run_llm_review。
        context_budget: 上下文预算（字符数等），控制采集范围与 prompt 长度。
        llm_provider: LLM 提供方标识，如 "mock" / "openai" 等。
        mock_fixture: mock provider 使用的测试 fixture 名。
        trace_enabled: 是否持久化 trace 到文件。
        trace_dir: trace 文件输出目录。
        publish_summary_comment: 是否发布 PR 摘要评论。
        pull_request: 目标 PR 引用，发布评论时必填。
    """

    # Exactly one diff source must be provided.  CLI adapters use ``diff_path``;
    # HTTP adapters can pass untrusted request content through ``diff_text``
    # without creating a temporary file on the server.
    diff_path: Optional[str] = None
    repo_root: str = "."
    diff_text: Optional[str] = None
    output_format: str = "markdown"
    use_llm: bool = False
    context_budget: ContextBudget = field(default_factory=ContextBudget)
    llm_provider: str = "mock"
    mock_fixture: str = "normal"
    trace_enabled: bool = False
    trace_dir: str = "traces"
    publish_summary_comment: bool = False
    pull_request: Optional[PullRequestRef] = None
    # Which review pipeline to run.  "fixed" (default) uses the existing single-call
    # LLM review; "react" delegates to the tool-calling ReAct controller.  Values
    # outside {"fixed","react"} are rejected before any file I/O or provider call.
    review_mode: str = "fixed"
    # Budget configuration for the ReAct controller.  Only used when review_mode=="react";
    # when None, the controller's own defaults apply.
    react_budget: Optional[ReActBudget] = None
    # Provider for the ReAct controller.  Only used when review_mode=="react".
    # Tests inject a ScriptedMockProvider here; production code leaves it None
    # so _run_react_review resolves the provider from llm_provider.
    react_provider: object = field(default=None, repr=False, compare=False)




@dataclass
class ReviewResult:
    """单次审查运行的结构化结果，包含完整中间状态与可选的评论发布结果。

    包装 ReviewState 与 summary_comment，对外暴露 output 与 trace_steps
    两个常用属性的快捷访问，避免调用方直接深入 state 内部结构。

    字段说明：
        state: 审查全过程的中间状态，含 diff、issues、output、trace_steps 等。
        summary_comment: 发布 PR 摘要评论的结果；未发布时为 None。
    """

    state: ReviewState
    summary_comment: Optional[SummaryCommentResult] = None

    @property
    def output(self) -> str:
        """最终渲染输出文本（markdown 或 json），直接可用作产物。"""
        return self.state.output

    @property
    def trace_steps(self) -> list[dict]:
        """各步骤的耗时与详情列表，用于性能分析与 eval。"""
        return self.state.trace_steps


def record_step(state, step, detail=None, started_at_perf=None):
    """
    将一个 pipeline 步骤的耗时与详情追加到 state.trace_steps。

    所有步骤统一通过本函数记录，便于后续性能分析与 eval 复盘。
    detail 会被 redact_sensitive_structure 递归脱敏，避免敏感信息落入 trace。

    Args:
        state: ReviewState 对象，trace_steps 列表挂在其上。
        step (str): 步骤名，如 "parse_diff" / "run_llm_review" 等。
        detail (dict): 步骤详情，如计数、配置、错误等；默认空 dict。
        started_at_perf (float|None): 步骤开始的 perf_counter 时间戳；
            由调用方在步骤开始前捕获并传入，确保计时精准。
            为 None 时以当前时刻作为起点（耗时近似为 0）。
    """
    if started_at_perf is None:
        # 未传入起始时间戳时，用当前时刻兜底，避免计时为负
        started_at_perf = perf_counter()
    # 将秒级耗时转为毫秒整数，便于人类阅读与聚合统计
    duration_ms = int((perf_counter() - started_at_perf) * 1000)
    state.trace_steps.append(
        {
            "step": step,
            "duration_ms": duration_ms,
            # 对 detail 做递归脱敏，防止 trace 文件泄露密钥/Token
            "detail": redact_sensitive_structure(detail or {}),
        }
    )


def _retry_detail(call_model):
    """
    提取 LLM 调用器的重试信息，用于 trace 记录。

    LLM 调用可能因网络抖动或限流而重试，记录重试细节便于排查
    "为何本次 LLM 审查耗时较长"或"为何结果不稳定"。

    Args:
        call_model: LLM 调用器对象，可能挂载 last_retry_info 属性。

    Returns:
        dict: 包含 attempts（总尝试次数）、retries（重试次数）、
              retry_errors（各次重试的错误文本，已脱敏截断）、
              exhausted（是否耗尽重试次数）。
    """
    # 使用 getattr 安全读取，mock/无重试场景下返回空 dict
    retry_info = getattr(call_model, "last_retry_info", {})
    return {
        "attempts": retry_info.get("attempts", 0),
        "retries": retry_info.get("retries", 0),
        # 错误文本可能含敏感信息，需逐条脱敏截断
        "retry_errors": [
            sanitize_trace_text(error)
            for error in retry_info.get("retry_errors", [])
        ],
        "exhausted": retry_info.get("exhausted", False),
    }


def validate_issues(issues, changed_files):
    """
    校验 issues 列表类型，并验证每个 issue 的行号定位是否合理。

    先做基本类型断言防止下游解析报错，再委托 validate_issue_locations
    检查行号是否落在变更文件的实际改动范围内（剔除幻觉行号）。

    Args:
        issues: 待校验的问题列表。
        changed_files (list[ChangedFile]): 变更文件列表，用于校验行号范围。

    Returns:
        list: 通过校验后的问题列表。

    Raises:
        ValueError: 当 issues 不是 list 时抛出。
    """
    if not isinstance(issues, list):
        # 类型断言：及早失败，避免后续迭代时才报错难以定位
        raise ValueError("Issues should be a list")
    return validate_issue_locations(issues, changed_files)


def _run_react_review(
    state: ReviewState,
    request: ReviewRequest,
    *,
    react_provider: object | None = None,
) -> list:
    """Run one ReAct tool-calling review and return validated findings.

    React failures produce ``react_degraded=True`` on ``state`` and return an
    empty list; they **never** fall back to the fixed pipeline.  A dedicated
    provider injection parameter supports offline deterministic testing.
    """
    # Build the tool dispatcher so the model can query changed hunks, file
    # context, and Python symbols within the review scope defined by the diff.
    review_scope = [changed_file.path for changed_file in state.changed_files]
    dispatcher = ToolDispatcher()
    dispatcher.register(ChangedHunksTool(state.changed_files, review_scope))
    dispatcher.register(ReadFileContextTool(state.repo_root))
    dispatcher.register(SearchPythonSymbolTool(state.repo_root, review_scope))

    finish_review = FinishReview(state.changed_files)

    # Resolve the provider.  Test callers inject a scripted provider;
    # production currently only supports the "mock" provider for react mode.
    if react_provider is not None:
        provider = react_provider
    elif state.llm_provider == "mock":
        # Default script: immediately finish with no findings so a caller can
        # compose its own smoke script without rebuilding the dispatcher.
        provider = ScriptedMockProvider(
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "finish-default",
                            "name": "finish_review",
                            "arguments": {"findings": []},
                        }
                    ]
                }
            ]
        )
    else:
        # M7-10 function-calling adapter is required before a real provider can
        # drive the ReAct loop.  Block this early with a clear reason.
        raise ValueError(
            f"react_mode_requires_mock_provider_for_now:{state.llm_provider}"
        )

    budget = request.react_budget or ReActBudget()
    controller = ReActController(
        provider=provider,
        dispatcher=dispatcher,
        finish_review=finish_review,
        budget=budget,
        state=state,
    )

    # Build the initial request.  The mock provider drives the loop via its
    # script rather than consuming the prompt; the prompt is included so that a
    # future real-provider adapter can use it.
    initial_request = {
        "review_id": state.task_id,
        "system": (
            "You are a code review agent. Review the changed files using the "
            "available tools. Call finish_review when done."
        ),
        "changed_files": [changed_file.path for changed_file in state.changed_files],
        "history": [],
    }

    result = controller.run(initial_request)

    if not result.finished:
        # React did not finish; state.react_degraded was already set by the
        # controller's _terminate call.  Return no findings so this degradation
        # is never mistaken for a successful empty review.
        return []

    return list(result.findings)


class ReviewService:
    """执行一次确定性的审查工作流，不耦合 CLI 专属输入。

    通过依赖注入 git_provider_factory，使评论发布能力可替换/可 mock，
    便于在 CI、本地、测试环境复用同一套编排逻辑。

    该类的 review() 方法是模板方法模式的核心：定义固定步骤骨架，
    具体能力由各组件实现，便于单步替换或测试。
    """

    def __init__(
        self,
        git_provider_factory: Optional[Callable[[], GitProvider]] = None,
    ):
        """
        初始化 ReviewService。

        Args:
            git_provider_factory: 创建 GitProvider 实例的可调用工厂。
                为 None 时表示不支持发布 PR 评论；当需要发布摘要评论时必须提供。
                使用工厂而非实例，便于延迟创建与每次调用使用新实例。
        """
        self._git_provider_factory = git_provider_factory

    def review(self, request: ReviewRequest) -> ReviewResult:
        """
        执行完整的代码审查 pipeline，返回结构化结果。

        这是整个 Agent 的核心入口，按固定顺序执行以下步骤（模板方法模式）：
          1. receive_task：接收任务并初始化 state，记录输入元信息；
          2. parse_diff：读取并解析 diff，得到变更文件列表；
          3. collect_context：按预算采集上下文文件，供规则与 LLM 使用；
          4. run_static_checks：运行规则引擎静态检查，得到 rule_issues；
          5. run_llm_review（可选）：调用 LLM 审查并合并 llm_issues，
             LLM 失败时走降级路径记录 error，不中断整体流程；
          6. validate_output：校验 issues 列表类型与行号定位合理性；
          7. render_report：按 output_format 渲染最终输出文本；
          8. publish_summary_comment（可选）：发布 PR 摘要评论；
          9. save_trace：持久化 trace 到文件（若启用）。

        每步均用 perf_counter 计时并 record_step 记录，支撑性能分析与 eval。

        Args:
            request: ReviewRequest 结构化输入。

        Returns:
            ReviewResult: 包含完整中间 state 与可选 summary_comment。

        Raises:
            ValueError: 当输入参数不满足约束时抛出（diff 来源不唯一、
                输出格式不支持、缺少 PR 引用、缺少 git provider 等）。
        """
        # --- 参数校验阶段：及早失败，避免执行到中途才报错 ---
        # diff_path 与 diff_text 必须二选一，二者同时为 None 或同时非 None 均非法
        if (request.diff_path is None) == (request.diff_text is None):
            raise ValueError("exactly_one_diff_source_required")
        # 输出格式白名单校验
        if request.output_format not in {"json", "markdown"}:
            raise ValueError("unsupported_output_format")
        # 发布评论必须提供 PR 引用
        if request.publish_summary_comment and request.pull_request is None:
            raise ValueError("pull_request_required_for_summary_comment")
        # 发布评论必须提供 git provider 工厂
        if request.publish_summary_comment and self._git_provider_factory is None:
            raise ValueError("git_provider_required_for_summary_comment")
        # review_mode must be one of the known values; reject everything else before any I/O.
        if request.review_mode not in _VALID_REVIEW_MODES:
            raise ValueError("unsupported_review_mode")
        # react mode drives a multi-turn LLM tool-calling loop; without use_llm it
        # would silently fall through to the no-LLM fixed branch, violating the
        # "no silent fallback" non-goal.  Reject the contradictory combination early.
        if request.review_mode == "react" and not request.use_llm:
            raise ValueError("react_mode_requires_use_llm")

        # --- 初始化运行状态 ---
        state = ReviewState(
            diff_path=request.diff_path or "(inline diff)",
            repo_root=request.repo_root,
            output_format=request.output_format,
            use_llm=request.use_llm,
            context_budget=request.context_budget,
            llm_provider=request.llm_provider,
            trace_enabled=request.trace_enabled,
            trace_dir=request.trace_dir,
            review_mode=request.review_mode,
        )

        # 步骤1：receive_task —— 记录任务接收，计时从 state 初始化时刻开始
        record_step(
            state,
            "receive_task",
            {
                "diff": state.diff_path,
                "repo": state.repo_root,
                "format": state.output_format,
                "llm": state.use_llm,
                "llm_provider": state.llm_provider,
                "review_mode": state.review_mode,
            },
            started_at_perf=state.started_at_perf,
        )

        # 步骤2：parse_diff —— 读取 diff 文本并解析为变更文件列表
        step_started_at_perf = perf_counter()
        state.diff_text = (
            request.diff_text
            if request.diff_text is not None
            else Path(state.diff_path).read_text(encoding="utf-8")
        )
        state.changed_files = parse_diff(state.diff_text)
        record_step(
            state,
            "parse_diff",
            {"changed_files": len(state.changed_files)},
            started_at_perf=step_started_at_perf,
        )

        # 步骤3：collect_context —— 按预算采集上下文文件，供规则与 LLM 使用
        step_started_at_perf = perf_counter()
        state.contexts = collect_file_contexts(
            repo_root=state.repo_root,
            changed_files=state.changed_files,
            context_budget=state.context_budget,
        )
        record_step(
            state,
            "collect_context",
            {
                "contexts": len(state.contexts),
                # 记录每个上下文的选择理由与状态，便于 eval 分析上下文质量
                "selected_contexts": [
                    {
                        "path": context.path,
                        "source": context.source,
                        "selection_reason": context.selection_reason,
                        "exists": context.exists,
                        "truncated": context.truncated,
                        "chars_read": context.chars_read,
                        "error": context.error,
                    }
                    for context in state.contexts
                ],
            },
            started_at_perf=step_started_at_perf,
        )

        # 步骤4：run_static_checks —— 规则引擎静态检查，issues 初始为规则结果
        step_started_at_perf = perf_counter()
        state.rule_issues = review_changed_files(state.changed_files)
        state.issues = list(state.rule_issues)
        record_step(
            state,
            "run_static_checks",
            {"findings": len(state.issues)},
            started_at_perf=step_started_at_perf,
        )

        # 步骤5：run_llm_review —— 可选的 LLM 审查步骤
        # 按 review_mode 显式分流：react 走工具调用循环，fixed 走单次 LLM 审查。
        # 关键约束（非目标）：react 失败时绝不静默回退到 fixed 流水线，而是显式
        # 降级（react_degraded=True，返回空 findings），避免把“失败”伪装成“成功审查”。
        # Branch on review_mode so the react path is explicit and never a silent fallback.
        if state.use_llm and state.review_mode == "react":
            step_started_at_perf = perf_counter()
            try:
                react_issues = _run_react_review(state, request, react_provider=request.react_provider)
                state.issues.extend(react_issues)
                record_step(
                    state,
                    "run_llm_review",
                    {
                        "called": True,
                        "provider": state.llm_provider,
                        "review_mode": "react",
                        "findings": len(react_issues),
                        "react_degraded": state.react_degraded,
                        "react_termination_reason": (
                            state.react_termination_reason if state.react_degraded else "finish"
                        ),
                    },
                    started_at_perf=step_started_at_perf,
                )
            except Exception as exc:
                # React failure is explicit degradation, never a silent fixed-success substitution.
                state.errors.append(sanitize_trace_text(exc))
                state.react_degraded = True
                state.react_termination_reason = getattr(exc, "code", "react_review_failed")
                record_step(
                    state,
                    "run_llm_review",
                    {
                        "called": True,
                        "provider": state.llm_provider,
                        "review_mode": "react",
                        "findings": 0,
                        "react_degraded": True,
                        "error": sanitize_trace_text(exc),
                    },
                    started_at_perf=step_started_at_perf,
                )
        elif state.use_llm:
            step_started_at_perf = perf_counter()
            call_model = None
            try:
                # 按 provider 创建调用器，mock 场景下使用 fixture
                call_model = get_call_model(
                    state.llm_provider,
                    mock_fixture=request.mock_fixture,
                )
                state.llm_issues, validation = review_with_llm(
                    changed_files=state.changed_files,
                    contexts=state.contexts,
                    rule_issues=state.rule_issues,
                    call_model=call_model,
                    max_prompt_chars=state.context_budget.max_prompt_chars,
                )
                # 记录 LLM 校验错误，并合并 LLM 发现的问题
                state.errors.extend(validation.errors)
                state.issues.extend(state.llm_issues)
                record_step(
                    state,
                    "run_llm_review",
                    {
                        "called": True,
                        "provider": state.llm_provider,
                        "findings": len(state.llm_issues),
                        "valid": validation.valid,
                        "repaired": validation.repaired,
                        "errors": validation.errors,
                        **_retry_detail(call_model),
                    },
                    started_at_perf=step_started_at_perf,
                )
            except LLMClientError as exc:
                # 降级路径：LLM 调用失败时不中断流程，仅记录 error 并跳过 llm_issues
                state.errors.append(str(exc))
                record_step(
                    state,
                    "run_llm_review",
                    {
                        "called": True,
                        "provider": state.llm_provider,
                        "findings": 0,
                        "error": sanitize_trace_text(exc),
                        **_retry_detail(call_model),
                    },
                    started_at_perf=step_started_at_perf,
                )
        else:
            # 未启用 LLM：仍记录该步骤（called=False），保证 trace 步骤序列完整
            step_started_at_perf = perf_counter()
            record_step(
                state,
                "run_llm_review",
                {"called": False},
                started_at_perf=step_started_at_perf,
            )

        # 步骤6：validate_output —— 校验 issues 类型与行号定位合理性
        step_started_at_perf = perf_counter()
        state.issues = validate_issues(state.issues, state.changed_files)
        record_step(
            state,
            "validate_output",
            {"findings": len(state.issues)},
            started_at_perf=step_started_at_perf,
        )

        # 步骤7：render_report —— 按输出格式渲染最终文本
        step_started_at_perf = perf_counter()
        if state.output_format == "json":
            state.output = render_json_report(state.issues)
        else:
            state.output = render_markdown_report(
                state.issues, state.changed_files, state.contexts
            )
        record_step(
            state,
            "render_report",
            {"format": state.output_format},
            started_at_perf=step_started_at_perf,
        )

        # 步骤8：publish_summary_comment —— 可选的 PR 摘要评论发布
        summary_comment = None
        if request.publish_summary_comment:
            step_started_at_perf = perf_counter()
            # 先渲染摘要文本，再通过 git provider 发布（幂等更新）
            summary_body = render_summary_comment(state.issues, state.changed_files)
            summary_comment = self._git_provider_factory().publish_summary_comment(
                request.pull_request, summary_body
            )
            record_step(
                state,
                "publish_summary_comment",
                {
                    "action": summary_comment.action,
                    "comment_id": summary_comment.comment_id,
                },
                started_at_perf=step_started_at_perf,
            )

        # 步骤9：save_trace —— 持久化 trace（若启用）
        if state.trace_enabled:
            # trace 自身的保存耗时也需记录，故先捕获起始时间戳再调用 save_trace
            save_started_at_perf = perf_counter()
            save_trace(
                state,
                state.trace_dir,
                final_step={
                    "step": "save_trace",
                    "detail": {"enabled": True, "trace_dir": state.trace_dir},
                    "started_at_perf": save_started_at_perf,
                },
            )
        else:
            # 未启用 trace：记录该步骤以保持步骤序列完整，便于对齐分析
            step_started_at_perf = perf_counter()
            record_step(
                state,
                "save_trace",
                {"enabled": False},
                started_at_perf=step_started_at_perf,
            )

        return ReviewResult(state=state, summary_comment=summary_comment)
