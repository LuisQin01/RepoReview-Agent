"""LLM 客户端与重试模块。

模块职责：
    本模块封装与 LLM 服务商（目前为 OpenAI）的交互，提供统一的 call_model 接口，
    并在交互失败时做指数退避重试。它是 LLM 审查（llm_reviewer）调用模型的底层通道。

在整体架构中的位置：
        llm_reviewer ──prompt──▶ get_call_model(...) ──▶ call_with_retries ──▶ real_call_model ──▶ OpenAI API
                                       │                      │
                                       │                      └─▶ retry_info（重试详情）
                                       └─▶ 返回带 .last_retry_info 属性的 callable

核心设计：
    1. 分层异常体系：
       - LLMClientError（基类，所有客户端错误的根）
         ├─ LLMRetryableError：可重试错误（超时 / 连接错误 / 429 / 5xx），重试可能恢复；
         └─ LLMConfigurationError：配置错误（缺 API Key / 不支持的 provider），重试无意义。
       call_with_retries 只捕获 LLMRetryableError，其他直接抛出。
    2. 指数退避：每次重试等待 base_delay * 2**attempt，避免在服务端抖动时打爆 API。
    3. retry_info 透传：重试详情记录到可变 dict，最终挂在返回 callable 的
       last_retry_info 属性上，供 trace / 日志读取，不污染返回值。
    4. provider 工厂：get_call_model 按 provider 创建 callable，便于切换 mock / openai。
"""
from __future__ import annotations

import json
import os
import time
from functools import partial
from typing import Mapping, Sequence

from .model_protocol import JSONValue, ModelProtocolError, ModelResponse, ToolCall


# 默认单次请求超时（秒）：平衡响应速度与偶发慢请求容忍度
DEFAULT_TIMEOUT_SECONDS = 10.0
# 默认最大尝试次数（含首次）：3 次 = 1 次正常调用 + 2 次重试
DEFAULT_MAX_ATTEMPTS = 3
# 默认重试基础退避（秒）：首次重试等待 0.25s，之后 0.5s、1s、2s … 指数增长
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.25


class LLMClientError(RuntimeError):
    """所有 LLM 客户端错误的基类。

    捕获本类即可处理“所有客户端相关失败”，无需逐一列举子类。
    """


class LLMRetryableError(LLMClientError):
    """An LLM provider failure that may succeed on a later attempt.

    可重试错误：典型的瞬时故障（超时、连接错误、429 限流、5xx 服务端错误）。
    call_with_retries 会捕获本类并按指数退避重试。
    """


class LLMConfigurationError(LLMClientError):
    """A local configuration error that retrying cannot resolve.

    配置错误：例如缺少 OPENAI_API_KEY、指定了不支持的 provider / fixture。
    这类错误重试无意义，直接向上抛出。
    """


class ScriptedMockProvider:
    """Return one internal response or failure for each offline scripted request.

    This is intentionally separate from ``get_call_model``: the legacy fixed
    pipeline consumes text, while the future controller consumes ModelResponse.
    """

    def __init__(
        self,
        script: Sequence[ModelResponse | Mapping[str, JSONValue] | str | Exception],
    ) -> None:
        self._script = tuple(script)
        self._next_index = 0
        self.requests: list[dict[str, JSONValue]] = []

    @property
    def consumed_count(self) -> int:
        """Return how many script entries were consumed by actual requests."""
        return self._next_index

    def complete(self, request: Mapping[str, JSONValue]) -> ModelResponse:
        """Record one request and consume exactly one scripted result or failure."""
        try:
            request_copy = json.loads(json.dumps(dict(request), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ModelProtocolError("mock_request_must_be_json_serializable") from exc
        if not isinstance(request_copy, dict):
            raise ModelProtocolError("mock_request_must_be_an_object")
        self.requests.append(request_copy)
        if self._next_index >= len(self._script):
            raise LLMClientError("mock_script_exhausted")

        item = self._script[self._next_index]
        self._next_index += 1
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ModelResponse):
            return item
        if isinstance(item, str):
            return ModelResponse.from_json(item)
        if isinstance(item, Mapping):
            return ModelResponse.from_dict(item)
        raise ModelProtocolError("mock_script_item_unsupported")


class OpenAIModelProvider:
    """Real OpenAI provider adapter for the ReAct controller.

    Only this class touches the OpenAI SDK within the ReAct path.  It converts
    internal tool schemas to the SDK ``tools`` format, sends the controller's
    JSON-safe request history as SDK input, and parses SDK responses into
    ``ModelResponse``.  It does not execute tools, validate or repair tool-call
    arguments, or implement retry loops — the controller and the existing
    ``call_with_retries`` own those concerns.

    Each element of *tools* must expose ``name`` (str), ``description`` (str)
    and ``parameters_schema`` (dict) attributes, matching the ``ReviewTool``
    protocol and ``FinishReview``.
    """

    def __init__(
        self,
        tools: Sequence,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._sdk_tools = self._convert_tool_schemas(tools)
        self._timeout_seconds = timeout_seconds

    @property
    def sdk_tools(self) -> tuple[dict[str, JSONValue], ...]:
        """Return the converted SDK tool schemas (for inspection and tests)."""
        return self._sdk_tools

    @staticmethod
    def _convert_tool_schemas(tools: Sequence) -> tuple[dict[str, JSONValue], ...]:
        """Convert internal tool schemas to the OpenAI SDK ``tools`` format."""
        converted: list[dict[str, JSONValue]] = []
        for tool in tools:
            name = getattr(tool, "name", None)
            description = getattr(tool, "description", None)
            parameters = getattr(tool, "parameters_schema", None)
            if not isinstance(name, str) or not name:
                raise LLMConfigurationError("tool_name_must_be_a_non_empty_string")
            if not isinstance(description, str):
                raise LLMConfigurationError("tool_description_must_be_a_string")
            if not isinstance(parameters, dict):
                raise LLMConfigurationError("tool_parameters_schema_must_be_an_object")
            converted.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            )
        return tuple(converted)

    def complete(self, request: Mapping[str, JSONValue]) -> ModelResponse:
        """Call the OpenAI SDK and return a normalized ``ModelResponse``."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMConfigurationError("missing_OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

        try:
            # Lazy import: only the real ReAct path needs the SDK package.
            from openai import OpenAI

            # max_retries=0: the controller owns retry policy, not the SDK.
            client = OpenAI(api_key=api_key, timeout=self._timeout_seconds, max_retries=0)
            input_items = self._convert_request_to_input(request)
            create_kwargs: dict[str, object] = {
                "model": model,
                "input": input_items,
                "timeout": self._timeout_seconds,
            }
            if self._sdk_tools:
                create_kwargs["tools"] = list(self._sdk_tools)
            sdk_response = client.responses.create(**create_kwargs)
            return self._parse_sdk_response(sdk_response)
        except (LLMClientError, ModelProtocolError):
            # Protocol errors (bad JSON in arguments) propagate unchanged so the
            # controller can distinguish a malformed response from a provider fault.
            raise
        except Exception as exc:
            error_type = LLMRetryableError if _is_retryable_provider_error(exc) else LLMClientError
            raise error_type(f"openai_call_failed:{exc}") from exc

    @staticmethod
    def _convert_request_to_input(
        request: Mapping[str, JSONValue],
    ) -> list[dict[str, JSONValue]]:
        """Convert the controller's JSON-safe history to OpenAI SDK input items."""
        history = request.get("history", [])
        if not isinstance(history, list):
            history = []
        input_items: list[dict[str, JSONValue]] = []
        for event in history:
            if not isinstance(event, Mapping):
                continue
            role = event.get("role")
            if role in ("user", "system", "developer"):
                content = event.get("content")
                if content is not None:
                    input_items.append({"role": role, "content": content})
            elif role == "assistant":
                content = event.get("content")
                if content is not None:
                    input_items.append({"role": "assistant", "content": content})
                for call in event.get("tool_calls", []):
                    if not isinstance(call, Mapping):
                        continue
                    # SDK function_call arguments must be a JSON string.
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("call_id", ""),
                            "name": call.get("name", ""),
                            "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                        }
                    )
            elif role == "tool":
                # SDK function_call_output requires a string output field.
                result = event.get("result", {})
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": event.get("call_id", ""),
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
        return input_items

    @staticmethod
    def _parse_sdk_response(sdk_response: object) -> ModelResponse:
        """Convert an OpenAI SDK response object to a ``ModelResponse``."""
        text: str | None = None
        tool_calls: list[ToolCall] = []
        for item in getattr(sdk_response, "output", None) or []:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for content in getattr(item, "content", None) or []:
                    if getattr(content, "type", None) == "output_text":
                        content_text = getattr(content, "text", None)
                        if content_text:
                            text = content_text if text is None else text + content_text
            elif item_type == "function_call":
                # ToolCall.from_raw_arguments parses the JSON string and rejects
                # bad JSON with ModelProtocolError, never silently substituting {}.
                tool_calls.append(
                    ToolCall.from_raw_arguments(
                        call_id=getattr(item, "call_id", None),
                        name=getattr(item, "name", None),
                        arguments=getattr(item, "arguments", ""),
                    )
                )
        usage = OpenAIModelProvider._extract_usage(sdk_response)
        finish_reason = getattr(sdk_response, "status", None)
        return ModelResponse(
            text=text,
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _extract_usage(sdk_response: object) -> dict[str, JSONValue]:
        """Map SDK usage counters to the internal usage dict, skipping non-integers."""
        usage_obj = getattr(sdk_response, "usage", None)
        if usage_obj is None:
            return {}
        usage: dict[str, JSONValue] = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(usage_obj, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                usage[key] = value
        return usage


# Mock 响应夹具：供测试与 CLI 冒烟使用，覆盖“正常”“空结果”“超时”“非法 JSON”等典型场景。
# 每个夹具对应 LLM 可能返回的一种形态，便于在不真实调用 OpenAI 的情况下验证解析链路。
MOCK_RESPONSE_FIXTURES = {
    "normal": {
        "findings": [
            {
                "severity": "high",
                "file": "app.py",
                "line": 10,
                "issue": "这里缺少异常处理",
                "reason": "新增代码可能执行失败，但没有看到错误处理逻辑",
                "suggested_fix": "为可能失败的调用添加 try/except 或向上抛出明确异常",
                "confidence": 0.76,
                "evidence": "app.py:10",
            }
        ]
    },
    "empty": {"findings": []},
}


def mock_call_model(prompt: str, fixture: str = "normal") -> str:
    """无状态的 mock 调用，按 fixture 返回预设响应，供测试使用。

    特殊 fixture：
      - "timeout"：抛 LLMRetryableError，模拟超时；
      - "bad_json"：返回非法 JSON 字符串，模拟 LLM 输出格式异常；
      - 其他：从 MOCK_RESPONSE_FIXTURES 取对应响应序列化返回，找不到则抛 LLMConfigurationError。

    Args:
        prompt: prompt 字符串（mock 实现不消费，仅为对齐签名）。
        fixture: 夹具名，默认 "normal"。

    Returns:
        str: 模拟的 LLM 响应文本。

    Raises:
        LLMRetryableError: fixture == "timeout"。
        LLMConfigurationError: fixture 不在 MOCK_RESPONSE_FIXTURES 中。
    """
    if fixture == "timeout":
        raise LLMRetryableError("mock_timeout")
    if fixture == "bad_json":
        return "{not valid json"

    try:
        return json.dumps(MOCK_RESPONSE_FIXTURES[fixture], ensure_ascii=False)
    except KeyError as exc:
        raise LLMConfigurationError(f"unsupported_mock_fixture:{fixture}") from exc


def make_mock_call_model(fixture: str):
    """构建有状态的 mock callable，用于重试路径测试与 CLI 冒烟测试。

    通过闭包变量 failures_remaining 维持状态：
      - fixture == "timeout_then_success"：前 2 次抛超时，第 3 次返回正常响应，
        专门验证 call_with_retries 的重试逻辑；
      - 其他 fixture：直接委托 mock_call_model，无失败注入。

    Args:
        fixture: 夹具名，决定失败注入策略。

    Returns:
        Callable[[str], str]: 带状态的 call_model 函数。
    """
    failures_remaining = 2 if fixture == "timeout_then_success" else 0

    def call_model(prompt: str) -> str:
        nonlocal failures_remaining
        # 还有待触发的失败次数 → 抛可重试错误，模拟超时
        if failures_remaining:
            failures_remaining -= 1
            raise LLMRetryableError("mock_timeout")
        # 失败次数用尽后返回正常响应
        if fixture == "timeout_then_success":
            return mock_call_model(prompt, fixture="normal")
        return mock_call_model(prompt, fixture=fixture)

    return call_model


def _is_retryable_provider_error(exc: Exception) -> bool:
    """判断一个异常是否属于“可重试的服务商错误”。

    判定规则（满足任一即可重试）：
      1. 标准库网络异常族：TimeoutError / ConnectionError / OSError；
      2. 通过类名识别 OpenAI SDK 的超时/连接错误（避免硬依赖 openai 包的类型）；
      3. 带 status_code 且为 429（限流）或 >= 500（服务端错误）。

    采用类名匹配而非 isinstance，是为了在 openai 包未安装或版本变动时
    仍能工作，降低耦合。

    Args:
        exc: 待判定的异常实例。

    Returns:
        bool: True 表示该异常可重试。
    """
    # 规则1：标准库网络异常族
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    # 规则2：按类名识别 SDK 异常，避免硬依赖 openai 类型
    if type(exc).__name__ in {"APITimeoutError", "APIConnectionError"}:
        return True

    # 规则3：HTTP 状态码 429（限流）或 5xx（服务端错误）
    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or isinstance(status_code, int) and status_code >= 500


def real_call_model(prompt: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """真实调用 OpenAI Responses API，返回模型输出文本。

    流程：
      1. 从环境变量读取 API Key（缺失则抛 LLMConfigurationError，不可重试）；
      2. 读取模型名（默认 gpt-4.1-mini）；
      3. 构造 OpenAI 客户端（max_retries=0，重试交由 call_with_retries 统一管理，避免双层重试）；
      4. 调用 responses.create，system 提示词要求仅返回 JSON；
      5. 空响应抛 LLMClientError，其他异常按 _is_retryable_provider_error 分类。

    Args:
        prompt: 用户 prompt 文本，作为 user 消息发送。
        timeout_seconds: 单次请求超时秒数。

    Returns:
        str: 模型返回的文本（预期为 JSON）。

    Raises:
        LLMConfigurationError: 缺少 OPENAI_API_KEY。
        LLMClientError: 响应为空或不可重试的调用失败。
        LLMRetryableError: 可重试的调用失败（超时/连接/429/5xx）。
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigurationError("missing_OPENAI_API_KEY")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        # 延迟导入 openai：仅在真实调用路径需要，避免 mock 路径强依赖该包
        from openai import OpenAI

        # max_retries=0 关闭 SDK 内置重试，统一由 call_with_retries 控制，避免双层重试导致等待时间不可控
        client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": "You are a strict code review assistant. Return JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            timeout=timeout_seconds,
        )
        # 空响应视为客户端错误（不可重试），避免重试无意义地空转
        if not response.output_text:
            raise LLMClientError("openai_empty_response")

        return response.output_text
    except LLMClientError:
        # 已是体系内异常，原样向上抛出，避免被下面的分类逻辑二次包装
        raise
    except Exception as exc:
        # 其余异常按“是否可重试”分流为对应异常类型，统一带 openai_call_failed: 前缀便于排查
        error_type = LLMRetryableError if _is_retryable_provider_error(exc) else LLMClientError
        raise error_type(f"openai_call_failed:{exc}") from exc


def call_with_retries(
    call_model,
    prompt: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
    sleep=time.sleep,
    retry_info=None,
) -> str:
    """对 call_model 做指数退避重试，只重试 LLMRetryableError。

    重试策略：
      - 最多尝试 max_attempts 次（含首次）；
      - 仅捕获 LLMRetryableError，其他异常（LLMConfigurationError / LLMClientError）立即抛出；
      - 每次重试前 sleep(base_delay * 2**attempt)，指数退避避免打爆服务端；
      - retry_info 字典实时记录 attempts/retries/retry_errors/exhausted，供调用方观测。

    性能考虑：sleep 通过参数注入（默认 time.sleep），便于测试用假 sleep 加速；
    指数退避让重试间隔随次数翻倍，平衡恢复概率与总耗时。

    Args:
        call_model: 实际调用模型的函数，签名 (prompt) -> str。
        prompt: 传给 call_model 的 prompt。
        max_attempts: 最大尝试次数（含首次），必须 >= 1。
        retry_base_delay_seconds: 首次重试基础退避秒数，之后按 2**attempt 增长。
        sleep: 睡眠函数，默认 time.sleep；测试可注入假实现加速。
        retry_info: 用于记录重试详情的可变 dict；None 时内部创建。

    Returns:
        str: call_model 成功时的返回值。

    Raises:
        ValueError: 参数非法（max_attempts < 1 或退避为负）。
        LLMRetryableError: 重试耗尽后最后一次失败仍为可重试错误。
        其他异常: call_model 抛出的非可重试异常原样传播。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_base_delay_seconds < 0:
        raise ValueError("retry_base_delay_seconds must be non-negative")

    # 初始化 retry_info：供调用方观测本次调用的重试情况
    if retry_info is None:
        retry_info = {}
    retry_info.clear()
    retry_info.update(
        attempts=0,
        retries=0,
        retry_errors=[],
        exhausted=False,
    )

    for attempt in range(max_attempts):
        retry_info["attempts"] = attempt + 1
        try:
            return call_model(prompt)
        except LLMRetryableError as exc:
            # 记录每次失败原因，便于事后排查
            retry_info["retry_errors"].append(str(exc))
            # 已是最后一次尝试：标记耗尽并抛出，让上游知道重试已用尽
            if attempt == max_attempts - 1:
                retry_info["exhausted"] = True
                raise
            retry_info["retries"] += 1
            # 指数退避：第 0 次重试等 base_delay*1，第 1 次等 base_delay*2，第 2 次等 base_delay*4 …
            sleep(retry_base_delay_seconds * (2**attempt))

    # 理论不可达：循环正常结束意味着未 return 也未 raise，防御性抛错
    raise RuntimeError("unreachable")


def get_call_model(
    provider: str,
    *,
    mock_fixture: str = "normal",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
    sleep=time.sleep,
):
    """工厂函数：按 provider 创建带重试的 callable，供 llm_reviewer 调用。

    返回的 callable 包装了 call_with_retries，并把最后一次重试详情挂到
    callable.last_retry_info 属性上，供 trace / 日志在不污染返回值的前提下读取。

    Args:
        provider: 提供商标识，"mock" 走本地夹具，"openai" 走真实 API。
        mock_fixture: provider=="mock" 时使用的夹具名。
        timeout_seconds: 单次请求超时（仅 openai 生效）。
        max_attempts: 最大尝试次数（含首次）。
        retry_base_delay_seconds: 首次重试基础退避秒数。
        sleep: 睡眠函数，用于重试退避（测试可注入假实现）。

    Returns:
        Callable[[str], str]: 带 last_retry_info 属性的 call_model 函数。

    Raises:
        LLMConfigurationError: provider 不在 {"mock", "openai"} 中。
    """
    # 按 provider 创建底层 call_model（不含重试）
    if provider == "mock":
        call_model = make_mock_call_model(mock_fixture)
    elif provider == "openai":
        # 用 partial 固定 timeout_seconds，得到 (prompt)->str 签名，与 mock 对齐
        call_model = partial(real_call_model, timeout_seconds=timeout_seconds)
    else:
        raise LLMConfigurationError(f"unsupported_llm_provider:{provider}")

    def call_model_with_retries(prompt: str) -> str:
        # 每次调用新建 retry_info，记录本次重试详情
        retry_info = {}
        try:
            return call_with_retries(
                call_model,
                prompt,
                max_attempts=max_attempts,
                retry_base_delay_seconds=retry_base_delay_seconds,
                sleep=sleep,
                retry_info=retry_info,
            )
        finally:
            # 无论成功/失败/异常，都把重试详情挂到函数属性上，供外部 trace 读取
            call_model_with_retries.last_retry_info = retry_info

    # 初始化 last_retry_info，保证“未调用前”也能安全读取
    call_model_with_retries.last_retry_info = {
        "attempts": 0,
        "retries": 0,
        "retry_errors": [],
        "exhausted": False,
    }
    return call_model_with_retries
