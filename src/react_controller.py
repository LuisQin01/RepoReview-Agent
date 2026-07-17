"""The minimal provider -> tool -> history -> finish review loop.

This controller is deliberately an explicit opt-in.  It does not change the
legacy text-review path or add provider retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from time import perf_counter
from typing import Mapping, Protocol, runtime_checkable

from .agent_state import ReviewState
from .file_context import _is_sensitive_file_path
from .model_protocol import JSONValue, ModelResponse
from .review_tools import (
    FinishResult,
    FinishReview,
    ToolDispatcher,
    _normalize_file_context_path,
)
from .trace import redact_sensitive_structure


class ReActControllerError(ValueError):
    """A stable controller-boundary failure with a public status code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReActBudget:
    """Hard limits for one explicit ReAct review lifecycle.

    Tool-result limits are UTF-8 JSON bytes at the controller boundary: this
    is the only common unit after different tools have produced their own
    domain-specific ``ToolResult.result_size`` values.
    """

    max_steps: int = 8
    max_llm_calls: int = 8
    max_total_tokens: int = 16_000
    max_tool_result_bytes: int = 8_000
    max_total_tool_result_bytes: int = 32_000

    _MIN_TOOL_RESULT_BYTES = 256

    def __post_init__(self) -> None:
        for field_name, value in (
            ("max_steps", self.max_steps),
            ("max_llm_calls", self.max_llm_calls),
            ("max_total_tokens", self.max_total_tokens),
            ("max_tool_result_bytes", self.max_tool_result_bytes),
            ("max_total_tool_result_bytes", self.max_total_tool_result_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name}_must_be_a_positive_integer")
        if self.max_tool_result_bytes < self._MIN_TOOL_RESULT_BYTES:
            raise ValueError("max_tool_result_bytes_must_allow_a_truncation_marker")


@dataclass(frozen=True)
class _GuardedToolResult:
    """One shared, JSON-safe projection for history and structured trace."""

    history_result: dict[str, JSONValue]
    trace_summary: dict[str, JSONValue]
    result_bytes: int
    truncated: bool


@runtime_checkable
class ModelProvider(Protocol):
    """Provider-independent boundary used by the minimal ReAct controller."""

    def complete(self, request: Mapping[str, JSONValue]) -> ModelResponse:
        """Return one normalized response for one JSON-safe request."""


class ReActController:
    """Drive one review until a validated ``finish_review`` call terminates it.

    Requests contain a JSON-serializable ``history`` list.  Every ordinary tool
    result is associated with the originating ``call_id`` before the next model
    request, so a provider cannot rely on list position to interpret results.
    """

    def __init__(
        self,
        provider: ModelProvider,
        dispatcher: ToolDispatcher,
        finish_review: FinishReview,
        *,
        budget: ReActBudget | None = None,
        state: ReviewState | None = None,
    ) -> None:
        if not isinstance(provider, ModelProvider):
            raise TypeError("provider_must_implement_model_provider_protocol")
        self._provider = provider
        self._dispatcher = dispatcher
        self._finish_review = finish_review
        self._budget = budget or ReActBudget()
        self._state = state
        self._terminated = False
        self._counters: dict[str, int] = {}
        self._trace_sequence = 0

    def run(self, initial_request: Mapping[str, JSONValue]) -> FinishResult:
        """Run until finish or a recorded budget degradation terminates it.

        A controller instance owns one review lifecycle.  Reusing it after a
        terminal finish raises ``already_finished`` before request validation,
        provider calls, or tool dispatch.

        ``initial_request`` must be a JSON object whose optional ``history`` is
        a JSON list.  Provider errors propagate unchanged; this loop owns no
        retry policy.  A response without tool calls cannot prove a
        completed review and therefore raises ``unavailable`` instead of
        returning an invented empty result.
        """
        if self._finish_review.is_finished:
            # Termination is irreversible: no later invocation may re-enter the loop.
            raise ReActControllerError("already_finished")
        if self._terminated:
            raise ReActControllerError("already_terminated")

        request, history = self._initial_request(initial_request)
        self._reset_state()

        # ── ReAct 主循环：模型每轮可调用 0~N 个工具，直到 finish_review 终止 ──
        # 核心不变量：任何一轮调用模型之前，先经过预算熔断检查；
        # 一旦预算耗尽，立即返回一个“非完成”的降级结果，绝不假装审查成功。
        while True:
            # 熔断检查：若某类预算（步数/调用数/token/工具结果字节）已达上限，
            # 直接返回降级结果。注意这是“调用模型之前”的守门，保证不会超支。
            termination = self._pre_call_termination()
            if termination is not None:
                return termination

            # 两个计数器先于模型调用递增：即使本次调用随后失败，
            # 也会计入“已用步数/调用数”，避免无限重试刷爆预算。
            self._increment("react_steps")
            self._increment("react_llm_calls")
            response = self._provider.complete(request)
            token_count = self._response_token_count(response)
            if token_count is None:
                # 用量异常（缺失/非数值）：无法核算 token 预算，按不可用降级，
                # 而非猜测一个 0 继续跑——宁可不审查也不制造虚假成功。
                return self._terminate("unavailable")
            self._increment("react_total_tokens", token_count)
            if not response.tool_calls:
                # 关键边界：模型返回了响应但一个工具都没调用、也没终止。
                # 这种响应无法证明“审查已完成”，因此抛 unavailable，
                # 而不是凭空返回一个空 findings 列表（空列表会被误读为“审查通过”）。
                raise ReActControllerError("unavailable")

            # 将本轮模型的 assistant 消息（含其产生的 tool_calls）追加进历史，
            # 保证下一轮请求能带上完整上下文。注意：这里记录的是“声明”的工具调用，
            # 真正执行的工具结果在下面循环中逐个回灌。
            history.append(self._assistant_event(response))
            for call in response.tool_calls:
                if call.name == self._finish_review.name:
                    # 本轮出现“结束审查”调用：交给 FinishReview 做校验与收敛。
                    result = self._finish_review.finish(call.arguments)
                    if result.finished:
                        # 终止具有不可逆性：本响应里后面的工具调用一律不再执行，
                        # 直接以 finish 记录并立即返回，避免“已结束又跑工具”的混乱。
                        self._record_termination(
                            "finish",
                            success=True,
                            tool_name=call.name,
                            arguments=call.arguments,
                            result_summary={
                                "status": result.status,
                                "finding_count": result.finding_count,
                                "received_count": result.received_count,
                                "rejected_count": result.rejected_count,
                                "truncated": result.truncated,
                            },
                            model_usage=response.usage,
                        )
                        return result
                    # 入参非法（如缺 findings 字段）不会终止循环：把拒绝原因作为
                    # 一条 tool 结果回灌给模型，让它纠正后重新发起正确的 finish 调用。
                    history.append(self._finish_event(call.call_id, result.status))
                    continue

                # 普通工具调用：先派发执行，再走“单一边界守卫”脱敏/截断后才进历史。
                started_at = perf_counter()
                tool_result = self._dispatcher.dispatch(call.name, call.arguments)
                guarded_result = self._guard_tool_result(
                    call.call_id, call.name, tool_result.to_model_dict()
                )
                if guarded_result.truncated:
                    self._increment("react_tool_results_truncated")
                self._record_tool_result(
                    call_id=call.call_id,
                    name=call.name,
                    arguments=call.arguments,
                    guarded_result=guarded_result,
                    duration_ms=self._duration_ms(started_at),
                    model_usage=response.usage,
                )
                if self._tool_bytes() + guarded_result.result_bytes > self._budget.max_total_tool_result_bytes:
                    # 累计护栏：跨多个工具的回灌结果总字节一旦超上限即终止。
                    # 在回灌历史之前用“当前累计 + 本次结果”判断，防止同一响应里
                    # 的第二个工具绕过总额上限，制造超长历史污染后续模型输入。
                    return self._terminate("max_tool_result_bytes_exhausted")
                self._increment("react_tool_result_bytes", guarded_result.result_bytes)
                # 仅当未超累计上限时，才把本次工具结果作为 tool 消息回灌进历史，
                # 供下一轮模型请求使用（工具结果与 call_id 绑定，不靠列表顺序关联）。
                history.append(
                    self._tool_event(call.call_id, call.name, guarded_result.history_result)
                )

            request["history"] = self._json_copy(history)

    def _pre_call_termination(self) -> FinishResult | None:
        """Stop before another provider request once a finite budget is spent."""
        if self._value("react_steps") >= self._budget.max_steps:
            return self._terminate("max_steps_exhausted")
        if self._value("react_llm_calls") >= self._budget.max_llm_calls:
            return self._terminate("max_llm_calls_exhausted")
        if self._value("react_total_tokens") >= self._budget.max_total_tokens:
            return self._terminate("max_tokens_exhausted")
        if self._tool_bytes() >= self._budget.max_total_tool_result_bytes:
            return self._terminate("max_tool_result_bytes_exhausted")
        return None

    def _terminate(self, reason: str) -> FinishResult:
        """Return a non-finish result so exhaustion can never look successful."""
        self._terminated = True
        if self._state is not None:
            self._state.react_termination_reason = reason
            self._state.react_degraded = True
        self._record_termination(
            reason,
            success=False,
            result_summary={
                "steps": self._value("react_steps"),
                "llm_calls": self._value("react_llm_calls"),
                "total_tokens": self._value("react_total_tokens"),
                "tool_result_bytes": self._value("react_tool_result_bytes"),
                "tool_results_truncated": self._value("react_tool_results_truncated"),
            },
        )
        return FinishResult(
            finished=False,
            status=reason,
            findings=(),
            finding_count=0,
            finding_limit=0,
            received_count=0,
            rejected_count=0,
            truncated=True,
        )

    def _reset_state(self) -> None:
        self._counters = {
            "react_steps": 0,
            "react_llm_calls": 0,
            "react_total_tokens": 0,
            "react_tool_result_bytes": 0,
            "react_tool_results_truncated": 0,
        }
        self._trace_sequence = 0
        if self._state is not None:
            self._state.react_steps = 0
            self._state.react_llm_calls = 0
            self._state.react_total_tokens = 0
            self._state.react_tool_result_bytes = 0
            self._state.react_tool_results_truncated = 0
            self._state.react_termination_reason = ""
            self._state.react_degraded = False

    def _value(self, name: str) -> int:
        return self._counters.get(name, 0)

    def _increment(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._value(name) + amount
        if self._state is not None:
            setattr(self._state, name, self._counters[name])

    def _tool_bytes(self) -> int:
        return self._value("react_tool_result_bytes")

    def _tool_event(
        self,
        call_id: str,
        name: str,
        result: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        """Attach the one already-guarded result to its model-history event."""
        return {
            "role": "tool",
            "call_id": call_id,
            "name": name,
            "result": result,
        }

    def _guard_tool_result(
        self,
        call_id: str,
        name: str,
        result: dict[str, JSONValue],
    ) -> _GuardedToolResult:
        """Apply the single M3-derived boundary before history or trace sees data.

        这是整个控制器唯一的“出口边界”：任何工具的结果在被写进模型历史（history）
        或写进 trace 之前，都必须经过本函数统一净化。所有“敏感脱敏 / 路径归一化 /
        失败信息剥离 / 字节预算截断”都在这里集中完成，工具自身不需要关心这些，
        从而保证净化逻辑只有一处、不会遗漏。

        处理顺序（层层收敛）：
          1) 递归脱敏结构化数据（redact_sensitive_structure）；
          2) 校验并归一化所有 path 类字段，敏感或非法路径整体替换为 forbidden 结果；
          3) 按 success 与否，剥离工具原始诊断，只保留稳定 code + 结构化 data；
          4) 若净化后体积超单条结果字节预算，则丢弃 data 并标记为 truncated，
             用“显式不完整”代替“偷偷截断”，避免不完整内容被误读为结论。
        """
        # 步骤 1：对工具返回的结构化数据做递归脱敏（密钥/Token 等被替换占位符）。
        # _json_copy 同时确保所有值都是合法 JSON，杜绝非 JSON 对象进入边界。
        safe_result = self._json_copy(redact_sensitive_structure(result))
        if not isinstance(safe_result, dict):  # ToolResult already promises an object.
            raise ReActControllerError("internal_error")

        # 步骤 2：遍历结果中所有 path/old_path/file_path 字段，归一化为仓库相对路径，
        # 一旦遇到敏感文件或非法路径，整个结果被替换成统一的 forbidden 占位结果。
        try:
            safe_result = self._sanitize_result_paths(safe_result)
        except ValueError:
            # A rejected path must not survive as a diagnostic in either sink.
            safe_result = self._forbidden_result()

        # 步骤 3：按成功/失败分流，统一清洗为“模型所见”的投影。
        if safe_result.get("success") is False:
            # 失败结果只保留稳定的 error_code，绝不把工具原始诊断（可能含主机信息）
            # 下发给模型——失败是“带状态码的状态”，不是主机诊断的载体。
            # Tool failures are status-bearing, never a carrier for host diagnostics.
            safe_result = self._failure_result(safe_result)
        else:
            # 成功结果只保留结构化 data 与用量，summary 改写为中性提示，
            # 绝不回显不可信的源文本。
            safe_result = self._success_result(safe_result)

        # 步骤 4：单条结果字节预算熔断。计算净化后的 UTF-8 JSON 字节数；若超出
        # max_tool_result_bytes，则丢弃 data 并标记 truncated——宁可少给也不要把
        # 一个被“切了一半”的代码片段喂给模型（半截代码可能被误判为完整结论）。
        result_bytes = self._json_size(safe_result)
        was_truncated = bool(safe_result["truncated"])
        if result_bytes > self._budget.max_tool_result_bytes:
            # Dropping data instead of slicing preserves JSON validity and makes the
            # missing portion explicit, so incomplete source cannot look conclusive.
            safe_result = self._truncated_result(safe_result, result_bytes)
            result_bytes = self._json_size(safe_result)
            was_truncated = True

        # 组装成“历史投影 + trace 摘要 + 字节数”三元组返回给调用方。
        # 注意 history_result 与 trace_summary 都来自同一个 safe_result，
        # 二者共享同一份净化结果，避免历史与 trace 出现不一致。
        return _GuardedToolResult(
            history_result=safe_result,
            trace_summary={
                "call_id": call_id,
                "tool_name": name,
                "success": safe_result["success"],
                "error_code": safe_result["error_code"],
                "truncated": safe_result["truncated"],
                "result_size": safe_result["result_size"],
                "result_limit": safe_result["result_limit"],
                "summary": safe_result["model_summary"],
            },
            result_bytes=result_bytes,
            truncated=was_truncated,
        )

    @staticmethod
    def _forbidden_result() -> dict[str, JSONValue]:
        """Return the non-leaking result used for sensitive or invalid paths."""
        return {
            "success": False,
            "model_summary": "The requested tool result is not available.",
            "error_code": "forbidden",
            "truncated": False,
            "result_size": 0,
            "result_limit": 0,
            "data": None,
            "usage": {},
        }

    @staticmethod
    def _failure_result(result: dict[str, JSONValue]) -> dict[str, JSONValue]:
        """Keep a stable failure code while discarding untrusted diagnostics."""
        error_code = result.get("error_code")
        if not isinstance(error_code, str):
            error_code = "internal_error"
        return {
            "success": False,
            "model_summary": "The tool did not complete successfully.",
            "error_code": error_code,
            "truncated": bool(result.get("truncated")),
            "result_size": ReActController._non_negative_int(result.get("result_size")) or 0,
            "result_limit": ReActController._non_negative_int(result.get("result_limit")) or 0,
            "data": None,
            "usage": ReActController._numeric_usage(result.get("usage")),
        }

    @staticmethod
    def _success_result(result: dict[str, JSONValue]) -> dict[str, JSONValue]:
        """Keep only structured data; summaries never echo untrusted source text."""
        return {
            "success": True,
            "model_summary": (
                "Tool result is available, but the returned information is incomplete."
                if bool(result.get("truncated"))
                else "Tool result is available."
            ),
            "error_code": None,
            "truncated": bool(result.get("truncated")),
            "result_size": ReActController._non_negative_int(result.get("result_size")) or 0,
            "result_limit": ReActController._non_negative_int(result.get("result_limit")) or 0,
            "data": result.get("data"),
            "usage": ReActController._numeric_usage(result.get("usage")),
        }

    def _truncated_result(
        self,
        result: dict[str, JSONValue],
        actual_size: int,
    ) -> dict[str, JSONValue]:
        return {
            "success": result["success"],
            "model_summary": "Tool result was truncated by the ReAct result budget.",
            "error_code": result["error_code"],
            "truncated": True,
            "result_size": actual_size,
            "result_limit": self._budget.max_tool_result_bytes,
            "data": None,
            "usage": {},
        }

    @staticmethod
    def _numeric_usage(value: JSONValue | object) -> dict[str, JSONValue]:
        """Usage is audit metadata, so retain only non-negative numeric counters."""
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if isinstance(key, str)
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
            and item >= 0
        }

    @staticmethod
    def _sanitize_result_paths(value: JSONValue) -> JSONValue:
        """Normalize path fields or reject them before they reach either sink.

        递归遍历工具结果（dict/list/标量），对其中所有 path 类字段做“归一化 +
        敏感/越界拒绝”处理。只要命中一个非法路径就抛 ValueError，由调用方
        _guard_tool_result 统一把整条结果降级为 forbidden，保证非法路径既不会进入
        模型历史，也不会进入 trace。
        """
        if isinstance(value, dict):
            sanitized: dict[str, JSONValue] = {}
            for key, item in value.items():
                # 只对显式登记为路径的字段做特殊处理，其余字段原样递归净化。
                if key in {"path", "old_path", "file_path"} and item is not None:
                    if not isinstance(item, str):
                        # 路径必须是字符串，其它类型视为格式非法。
                        raise ValueError("tool_result_path_must_be_a_string")
                    # 归一化为仓库相对规范路径（会拒绝绝对路径与 ".." 越界）。
                    normalized_path = _normalize_file_context_path(item)
                    if _is_sensitive_file_path(normalized_path):
                        # 命中敏感文件清单（如密钥文件）直接拒绝，路径不上链。
                        raise ValueError("tool_result_path_is_sensitive")
                    sanitized[key] = normalized_path
                else:
                    # 非路径字段：继续向下递归，确保嵌套结构里的路径也被覆盖。
                    sanitized[key] = ReActController._sanitize_result_paths(item)
            return sanitized
        elif isinstance(value, list):
            # 列表同样逐元素递归。
            return [ReActController._sanitize_result_paths(item) for item in value]
        return value

    def _record_tool_result(
        self,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, JSONValue],
        guarded_result: _GuardedToolResult,
        duration_ms: int,
        model_usage: dict[str, JSONValue],
    ) -> None:
        self._record_trace(
            "react_tool_result",
            {
                "call_id": call_id,
                "tool_name": name,
                "arguments": self._arguments_summary(arguments),
                "result": guarded_result.trace_summary,
                "success": guarded_result.history_result["success"],
                "duration_ms": duration_ms,
                "usage": self._numeric_usage(guarded_result.history_result["usage"]),
                "model_usage": self._numeric_usage(model_usage),
            },
        )

    def _record_termination(
        self,
        reason: str,
        *,
        success: bool,
        tool_name: str | None = None,
        arguments: dict[str, JSONValue] | None = None,
        result_summary: dict[str, JSONValue] | None = None,
        model_usage: dict[str, JSONValue] | None = None,
    ) -> None:
        self._record_trace(
            "react_termination",
            {
                "reason": reason,
                "success": success,
                "tool_name": tool_name,
                "arguments": self._arguments_summary(arguments or {}),
                "result": result_summary or {},
                "usage": self._numeric_usage(model_usage or {}),
            },
        )

    def _record_trace(self, step: str, detail: dict[str, JSONValue]) -> None:
        if self._state is None:
            return
        self._trace_sequence += 1
        self._state.trace_steps.append(
            {
                "step": step,
                "step_index": self._trace_sequence,
                "duration_ms": int(detail.pop("duration_ms", 0)),
                "detail": detail,
            }
        )

    @staticmethod
    def _arguments_summary(arguments: dict[str, JSONValue]) -> dict[str, JSONValue]:
        """Record argument shape only: raw model arguments can contain credentials."""
        summary: dict[str, JSONValue] = {"argument_keys": sorted(arguments)}
        path = arguments.get("path")
        if isinstance(path, str):
            try:
                normalized_path = _normalize_file_context_path(path)
            except ValueError:
                summary["path"] = "[REDACTED]"
            else:
                summary["path"] = (
                    "[REDACTED]" if _is_sensitive_file_path(normalized_path) else normalized_path
                )
        return summary

    @staticmethod
    def _duration_ms(started_at: float) -> int:
        return max(0, int((perf_counter() - started_at) * 1000))

    @staticmethod
    def _json_size(value: JSONValue) -> int:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _response_token_count(response: ModelResponse) -> int | None:
        """Read a normalized token total without trusting malformed provider usage."""
        usage = response.usage
        total = usage.get("total_tokens")
        if total is not None:
            return ReActController._non_negative_int(total)

        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
        input_count = ReActController._non_negative_int(input_tokens)
        output_count = ReActController._non_negative_int(output_tokens)
        if input_count is None or output_count is None:
            return None
        return input_count + output_count

    @staticmethod
    def _non_negative_int(value: JSONValue) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _initial_request(
        initial_request: Mapping[str, JSONValue],
    ) -> tuple[dict[str, JSONValue], list[JSONValue]]:
        """Copy an untrusted boundary request and normalize its optional history."""
        if not isinstance(initial_request, Mapping):
            raise ReActControllerError("invalid_arguments")
        request = ReActController._json_copy(dict(initial_request))
        if not isinstance(request, dict):  # Defensive: JSON copies of mappings stay objects.
            raise ReActControllerError("invalid_arguments")
        history = request.get("history", [])
        if not isinstance(history, list):
            raise ReActControllerError("invalid_arguments")
        request["history"] = history
        return request, history

    @staticmethod
    def _json_copy(value: JSONValue) -> JSONValue:
        """Reject non-JSON boundary values before they reach a provider."""
        try:
            return json.loads(json.dumps(value, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ReActControllerError("invalid_arguments") from exc

    @staticmethod
    def _assistant_event(response: ModelResponse) -> dict[str, JSONValue]:
        """Preserve the provider response that produced the following tool results."""
        return {
            "role": "assistant",
            "content": response.text,
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": ReActController._arguments_summary(call.arguments),
                }
                for call in response.tool_calls
            ],
        }

    @staticmethod
    def _finish_event(call_id: str, status: str) -> dict[str, JSONValue]:
        """Tell the provider why an invalid finish request did not terminate."""
        return {
            "role": "tool",
            "call_id": call_id,
            "name": "finish_review",
            "result": {"success": False, "error_code": status},
        }
