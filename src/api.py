"""Minimal HTTP adapter for one stateless review run.

本模块是 RepoReview Agent 的 HTTP 入口适配层，基于 FastAPI 提供一个无状态的
代码审查端点 ``POST /reviews``。设计上强调“安全边界”：
- 客户端只能提交 ``diff`` 文本，不能选择仓库路径、不能开启 LLM、不能发布 GitHub 评论；
- 仓库根路径由服务端在 :func:`create_app` 时绑定，避免客户端任意读取文件系统；
- 错误信息经 :func:`redact_sensitive_structure` 脱敏后再返回，防止泄露内部细节。

这样把“危险能力（文件系统访问、LLM 调用、写评论）”与“公开端点”隔离开。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .review_service import ReviewRequest, ReviewService
from .trace import redact_sensitive_structure


# diff 文本的最大字符数上限（1,000,000）。既防止超大输入拖垮审查流水线，
# 也避免单次请求占用过多内存。
MAX_DIFF_CHARS = 1_000_000


class CreateReviewRequest(BaseModel):
    """The only client-controlled input for a review run.

    客户端唯一可控的输入模型。``extra="forbid"`` 禁止客户端传入额外字段，
    防止通过参数注入意外配置（如试图覆盖仓库路径等）。
    """

    model_config = ConfigDict(extra="forbid")

    # diff 文本长度被严格限制在 1~MAX_DIFF_CHARS，既要求非空又防止过大输入
    diff: str = Field(min_length=1, max_length=MAX_DIFF_CHARS)


class ReviewMetrics(BaseModel):
    """单次审查的指标汇总。

    Args:
        changed_files: 变更文件数。
        findings: 发现的问题数。
        duration_ms: 审查总耗时（毫秒）。
        llm_called: 是否实际调用了 LLM。
    """

    changed_files: int
    findings: int
    duration_ms: int
    llm_called: bool


class ReviewStep(BaseModel):
    """审查流水线中的单个步骤及其耗时。

    Args:
        step: 步骤名称。
        duration_ms: 该步骤耗时（毫秒）。
    """

    step: str
    duration_ms: int


class ReviewResponse(BaseModel):
    """审查端点的响应模型。

    Args:
        task_id: 本次审查任务的唯一 ID。
        findings: 发现的问题列表（每条为 dict）。
        errors: 脱敏后的错误信息列表。
        metrics: 审查指标。
        steps: 审查步骤明细。
    """

    task_id: str
    findings: list[dict[str, Any]]
    errors: list[str]
    metrics: ReviewMetrics
    steps: list[ReviewStep]


def create_app(
    repo_root: str | Path = ".",
    review_service: Optional[ReviewService] = None,
) -> FastAPI:
    """Create an app bound to a server-controlled repository root.

    构造一个 FastAPI 应用，并将仓库根路径在服务端“锁定”。这是安全边界的核心：
    - ``repo_root`` 被解析为绝对路径后绑定到闭包中，客户端无法通过请求覆盖；
    - 端点内部硬编码 ``use_llm=False``，客户端无法触发 LLM 调用；
    - 不接受任何发布评论相关参数，客户端无法通过 API 写 GitHub 评论。

    Args:
        repo_root: 服务端绑定的仓库根路径，客户端不可控。
        review_service: 可选的 ReviewService 实例；为 None 时新建默认实例。
            允许注入便于测试。

    Returns:
        配置好 ``POST /reviews`` 路由的 FastAPI 应用。

    Note:
        The client cannot select a filesystem path, enable LLM calls, or publish
        GitHub comments through this endpoint.
    """

    service = review_service or ReviewService()
    # 在服务端把 repo_root 解析为绝对路径并锁定，客户端无法通过请求改变它
    configured_repo_root = str(Path(repo_root).resolve())
    app = FastAPI(title="RepoReview API", version="0.1.0")

    @app.post("/reviews", response_model=ReviewResponse, status_code=200)
    def create_review(payload: CreateReviewRequest) -> ReviewResponse:
        """处理一次代码审查请求。

        流程：校验输入 → 组装 ReviewRequest（服务端锁定 repo_root、关闭 LLM）
        → 执行审查 → 校验结果非空 → 组装并返回 :class:`ReviewResponse`。

        Args:
            payload: 客户端提交的审查请求，仅含 ``diff`` 字段。

        Returns:
            :class:`ReviewResponse`，含 findings/errors/metrics/steps。

        Raises:
            HTTPException: 422 表示输入非法（审查请求无效或 diff 无变更文件）。
        """
        # 安全边界：repo_root 与 use_llm 均由服务端硬编码，客户端无法影响
        request = ReviewRequest(
            diff_text=payload.diff,
            repo_root=configured_repo_root,
            output_format="json",
            use_llm=False,
        )
        try:
            result = service.review(request)
        except ValueError as exc:
            # 输入校验失败统一返回 422，不泄露具体内部异常细节
            raise HTTPException(status_code=422, detail="invalid_review_request") from exc

        # diff 中无任何变更文件时视为非法输入
        if not result.state.changed_files:
            raise HTTPException(status_code=422, detail="diff_contains_no_changed_files")

        # result.output 是 JSON 字符串，这里重新解析以提取 findings
        rendered = json.loads(result.output)
        # 总耗时 = 各步骤耗时之和
        duration_ms = sum(step["duration_ms"] for step in result.trace_steps)
        steps = [
            ReviewStep(step=step["step"], duration_ms=step["duration_ms"])
            for step in result.trace_steps
        ]
        # 判断是否真正调用了 LLM：检查 run_llm_review 步骤的 detail.called 标志
        llm_called = any(
            step["step"] == "run_llm_review"
            and step.get("detail", {}).get("called") is True
            for step in result.trace_steps
        )
        return ReviewResponse(
            task_id=result.state.task_id,
            findings=rendered["findings"],
            # 错误信息脱敏后再返回，避免泄露 token/路径等敏感内容
            errors=redact_sensitive_structure(result.state.errors),
            metrics=ReviewMetrics(
                changed_files=len(result.state.changed_files),
                findings=len(result.state.issues),
                duration_ms=duration_ms,
                llm_called=llm_called,
            ),
            steps=steps,
        )

    return app


# 模块级默认应用实例，便于 ``uvicorn src.api:app`` 直接启动
app = create_app()
