"""``src/llm_client.py`` 的单元测试集合。

本文件聚焦于 LLM 客户端的重试与配置校验逻辑。``get_call_model`` 是一个工厂，
按 provider（``mock`` / ``openai``）返回一个 ``call_model`` 可调用对象，内部
封装了：

1. **指数退避重试**：对可重试失败（超时、5xx）按 ``retry_base_delay_seconds``
   的倍数退避，最多 ``max_attempts`` 次；
2. **配置错误不重试**：缺 API key 等配置问题应直接抛 ``LLMConfigurationError``，
   不进入重试循环；
3. **OpenAI 适配**：将 ``timeout_seconds`` 同时传给 client 构造与每次请求，
   并禁用 SDK 自带重试（``max_retries=0``）以由本模块统一控制。

测试策略
--------
- **退避延迟验证**：通过 ``sleep=delays.append`` 注入收集器，把本应阻塞的
  ``time.sleep`` 替换为追加元素到列表，从而无副作用地断言退避序列
  （如 ``[0.5, 1.0]`` 验证指数退避）；
- **OpenAI 库替身**：用 ``monkeypatch.setitem(sys.modules, "openai", ...)``
  注入 ``FakeOpenAI``，避免依赖真实网络与 SDK，同时捕获 client 与请求参数；
- 通过 ``call_model.last_retry_info`` 验证重试次数、错误列表与是否耗尽。

在整体测试体系中的位置
----------------------
本文件覆盖 LLM 调用层，是 ``test_llm_reviewer.py``（prompt 构建与脱敏）的
下游：reviewer 构造好 prompt 后交给本层 client 发送。本文件确保发送层的
重试语义、超时透传与错误分类可预期。
"""
import sys
from types import SimpleNamespace

import pytest

from src.llm_client import (
    LLMConfigurationError,
    LLMClientError,
    LLMRetryableError,
    OpenAIModelProvider,
    ScriptedMockProvider,
    get_call_model,
)
from src.model_protocol import ModelProtocolError, ModelResponse, ToolCall


def test_scripted_mock_provider_returns_internal_responses_and_records_requests():
    provider = ScriptedMockProvider(
        [
            {
                "tool_calls": [
                    {"call_id": "call-1", "name": "unknown_tool", "arguments": {}}
                ],
                "finish_reason": "tool_calls",
            },
            ModelResponse(text="done", finish_reason="stop"),
        ]
    )

    first = provider.complete({"messages": [{"role": "user", "content": "review"}]})
    second = provider.complete({"messages": []})

    assert first.tool_calls[0].call_id == "call-1"
    assert first.tool_calls[0].name == "unknown_tool"
    assert second.text == "done"
    assert provider.consumed_count == 2
    assert provider.requests == [
        {"messages": [{"role": "user", "content": "review"}]},
        {"messages": []},
    ]


@pytest.mark.parametrize(
    "script_item, error",
    [
        ("{bad json", "model_response_invalid_json"),
        (
            '{"tool_calls": [{"call_id": "call-2", "name": "read", "arguments": "{bad"}]}',
            "tool_call_arguments_invalid_json",
        ),
        (LLMRetryableError("scripted_provider_failure"), "scripted_provider_failure"),
    ],
)
def test_scripted_mock_provider_consumes_bad_items_and_raises(script_item, error):
    provider = ScriptedMockProvider([script_item, ModelResponse(text="unused")])

    with pytest.raises((ModelProtocolError, LLMRetryableError), match=error):
        provider.complete({"turn": 1})

    assert provider.consumed_count == 1
    assert provider.requests == [{"turn": 1}]


def test_scripted_mock_provider_exhaustion_is_an_explicit_error_not_a_response():
    provider = ScriptedMockProvider([])

    with pytest.raises(LLMClientError, match="mock_script_exhausted"):
        provider.complete({"turn": 1})

    assert provider.consumed_count == 0
    assert provider.requests == [{"turn": 1}]


def test_mock_retryable_failures_recover_with_exponential_backoff():
    """验证可重试失败在指数退避后恢复，并记录 retry_info。

    测试目的
    --------
    使用 mock provider 的 ``timeout_then_success`` fixture（首次超时、第二次
    成功），验证：
    - 重试按 ``retry_base_delay_seconds`` 的指数倍数退避；
    - 最终返回有效响应；
    - ``last_retry_info`` 正确记录尝试次数、错误列表与未耗尽状态。

    特殊逻辑
    --------
    ``sleep=delays.append`` 把 ``time.sleep`` 替换为列表追加，使我们能断言
    退避序列 ``[0.5, 1.0]``（0.5 → 0.5*2）而无需真实等待。

    预期输出
    --------
    - 响应含 ``"findings"``；
    - ``delays == [0.5, 1.0]`` 体现指数退避；
    - ``last_retry_info`` 中 ``attempts=3``、``retries=2``、
      ``exhausted=False``。
    """
    delays = []
    call_model = get_call_model(
        "mock",
        mock_fixture="timeout_then_success",
        max_attempts=3,
        retry_base_delay_seconds=0.5,
        sleep=delays.append,  # 注入收集器，避免真实睡眠
    )

    response = call_model("review this diff")

    assert '"findings"' in response  # 最终拿到有效 JSON 响应
    assert delays == [0.5, 1.0]  # 指数退避：0.5、0.5*2
    assert call_model.last_retry_info == {
        "attempts": 3,  # 总尝试上限
        "retries": 2,  # 重试两次后成功
        "retry_errors": ["mock_timeout", "mock_timeout"],  # 两次失败原因
        "exhausted": False,  # 未耗尽（最终成功）
    }


def test_retryable_failure_raises_after_limited_attempts():
    """验证重试耗尽后抛出 ``LLMRetryableError``。

    测试目的
    --------
    使用 mock 的 ``timeout`` fixture（始终超时），验证当 ``max_attempts`` 用尽
    后：
    - 抛出 ``LLMRetryableError``，错误信息含失败原因；
    - 退避序列符合指数退避；
    - ``last_retry_info`` 标记 ``exhausted=True``，且错误列表含全部尝试。

    预期输出
    --------
    - 抛出 ``LLMRetryableError``，匹配 ``mock_timeout``；
    - ``delays == [0.25, 0.5]``（0.25、0.25*2，最后一次失败后不再退避）；
    - ``retry_errors`` 含 3 次 ``mock_timeout``，``exhausted=True``。
    """
    delays = []
    call_model = get_call_model(
        "mock",
        mock_fixture="timeout",
        max_attempts=3,
        retry_base_delay_seconds=0.25,
        sleep=delays.append,
    )

    with pytest.raises(LLMRetryableError, match="mock_timeout"):
        call_model("review this diff")

    assert delays == [0.25, 0.5]  # 两次退避（最后一次失败后不再 sleep）
    assert call_model.last_retry_info == {
        "attempts": 3,
        "retries": 2,
        "retry_errors": ["mock_timeout", "mock_timeout", "mock_timeout"],  # 三次尝试均失败
        "exhausted": True,  # 重试已耗尽
    }


def test_configuration_error_is_not_retried(monkeypatch):
    """验证配置错误（缺 API key）不进入重试循环。

    测试目的
    --------
    缺失 ``OPENAI_API_KEY`` 属于配置错误，而非瞬时故障。应立即抛出
    ``LLMConfigurationError``，不应触发任何退避 sleep。

    特殊逻辑
    --------
    ``monkeypatch.delenv`` 清除环境变量，``delays`` 用于断言未发生任何重试
    退避。

    预期输出
    --------
    - 抛出 ``LLMConfigurationError``，匹配 ``missing_OPENAI_API_KEY``；
    - ``delays == []``，证明未重试。
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # 确保环境变量不存在
    delays = []
    call_model = get_call_model("openai", max_attempts=3, sleep=delays.append)

    with pytest.raises(LLMConfigurationError, match="missing_OPENAI_API_KEY"):
        call_model("review this diff")

    assert delays == []  # 配置错误不应触发任何退避


def test_openai_call_applies_timeout_to_client_and_request(monkeypatch):
    """验证 ``timeout_seconds`` 同时作用于 client 构造与每次请求。

    测试目的
    --------
    为保证超时可控，``timeout_seconds`` 必须同时传入 ``OpenAI`` 客户端构造
    （影响连接级超时）与 ``responses.create`` 请求（影响单次调用超时），并
    设置 ``max_retries=0`` 禁用 SDK 自带重试，由本模块统一重试。

    特殊逻辑
    --------
    - ``FakeOpenAI`` 捕获 client 构造参数到 ``client_arguments``；
    - ``FakeResponses.create`` 捕获请求参数到 ``request_arguments``；
    - 通过 ``monkeypatch.setitem(sys.modules, "openai", ...)`` 注入替身，模拟
      ``import openai`` 的行为。

    预期输出
    --------
    - 响应为 ``'{"findings": []}'``；
    - client 构造参数为 ``{"api_key", "timeout", "max_retries"=0}``；
    - 请求参数含 ``timeout=7.5``。
    """
    client_arguments = []
    request_arguments = []

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)  # 捕获每次请求的参数
            return SimpleNamespace(output_text='{"findings": []}')

    class FakeOpenAI:
        def __init__(self, **kwargs):
            client_arguments.append(kwargs)  # 捕获 client 构造参数
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # 隔离 base_url：显式移除，使 client 构造参数不含 base_url 分支，
    # 避免本地 .env 的 OPENAI_BASE_URL 污染断言（base_url 仅在显式配置时才传入）。
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    # 将 openai 模块替换为含 FakeOpenAI 的替身，模拟真实导入
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    call_model = get_call_model("openai", timeout_seconds=7.5, max_attempts=1)

    response = call_model("review this diff")

    assert response == '{"findings": []}'
    assert client_arguments == [
        {"api_key": "test-key", "timeout": 7.5, "max_retries": 0}  # max_retries=0 禁用 SDK 自带重试
    ]
    assert request_arguments[0]["timeout"] == 7.5  # 单次请求也透传超时


def test_openai_timeout_is_retried_then_recovers(monkeypatch):
    """验证 OpenAI 调用超时后被重试并恢复。

    测试目的
    --------
    当 ``responses.create`` 抛出 ``APITimeoutError`` 时，应按指数退避重试，
    且每次重试请求都携带相同的 ``timeout`` 值，最终在第二次成功返回。

    特殊逻辑
    --------
    - 自定义 ``APITimeoutError`` 模拟 OpenAI SDK 的超时异常；
    - ``FakeResponses.create`` 首次抛异常、第二次返回成功响应；
    - ``delays`` 收集退避序列。

    预期输出
    --------
    - 响应为 ``'{"findings": []}'``；
    - ``delays == [0.25]``（仅一次退避）；
    - 两次请求的 ``timeout`` 均为 ``2.0``。
    """
    request_arguments = []

    class APITimeoutError(Exception):
        pass  # 模拟 openai.APITimeoutError

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            if len(request_arguments) == 1:
                raise APITimeoutError("provider timed out")  # 首次超时
            return SimpleNamespace(output_text='{"findings": []}')  # 重试成功

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    delays = []
    call_model = get_call_model(
        "openai",
        timeout_seconds=2.0,
        max_attempts=3,
        retry_base_delay_seconds=0.25,
        sleep=delays.append,
    )

    assert call_model("review this diff") == '{"findings": []}'
    assert delays == [0.25]  # 仅一次退避
    assert [request["timeout"] for request in request_arguments] == [2.0, 2.0]  # 超时值每次请求一致


def test_openai_http_503_is_retried_then_recovers(monkeypatch):
    """验证 OpenAI 503 错误被重试后恢复。

    测试目的
    --------
    当 provider 返回 503（服务不可用）时，应视为可重试错误，按指数退避重试
    并在第二次恢复。

    特殊逻辑
    --------
    - ``ProviderUnavailable`` 携带 ``status_code=503``，模拟 SDK 异常；
    - 首次抛异常、第二次成功。

    预期输出
    --------
    - 响应为 ``'{"findings": []}'``；
    - 共发起 2 次请求；
    - ``delays == [0.25]``。
    """
    request_arguments = []

    class ProviderUnavailable(Exception):
        status_code = 503  # 模拟 SDK 异常携带的状态码

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            if len(request_arguments) == 1:
                raise ProviderUnavailable("service unavailable")  # 首次 503
            return SimpleNamespace(output_text='{"findings": []}')  # 重试成功

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    delays = []
    call_model = get_call_model(
        "openai",
        max_attempts=3,
        retry_base_delay_seconds=0.25,
        sleep=delays.append,
    )

    assert call_model("review this diff") == '{"findings": []}'
    assert len(request_arguments) == 2  # 共两次请求（首次失败 + 重试成功）
    assert delays == [0.25]  # 仅一次退避


def test_openai_http_503_raises_after_limited_attempts(monkeypatch):
    """验证 OpenAI 503 重试耗尽后抛出 ``LLMRetryableError``。

    测试目的
    --------
    当 503 持续发生且 ``max_attempts`` 用尽时，应抛出 ``LLMRetryableError``，
    错误信息需包含原始失败原因，便于上层诊断。

    特殊逻辑
    --------
    ``FakeResponses.create`` 始终抛 ``ProviderUnavailable``，模拟持续不可用。

    预期输出
    --------
    - 抛出 ``LLMRetryableError``，匹配 ``openai_call_failed:service unavailable``；
    - 共发起 3 次请求（与 ``max_attempts`` 一致）；
    - ``delays == [0.25, 0.5]``（指数退避）。
    """
    request_arguments = []

    class ProviderUnavailable(Exception):
        status_code = 503

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            raise ProviderUnavailable("service unavailable")  # 始终失败

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    delays = []
    call_model = get_call_model(
        "openai",
        max_attempts=3,
        retry_base_delay_seconds=0.25,
        sleep=delays.append,
    )

    with pytest.raises(LLMRetryableError, match="openai_call_failed:service unavailable"):
        call_model("review this diff")

    assert len(request_arguments) == 3  # 三次尝试均失败
    assert delays == [0.25, 0.5]  # 两次退避（最后一次失败后不再 sleep）


# ---------------------------------------------------------------------------
# OpenAIModelProvider — M7-10 real provider function-calling adaptation
#
# All tests below inject a FakeOpenAI via monkeypatch so no real API is called.
# The fake mimics the OpenAI Responses API response shape (output items +
# usage + status) that _parse_sdk_response reads via getattr.
# ---------------------------------------------------------------------------


def _tool_schema(name, description, parameters_schema):
    """Build a minimal tool-like object for schema conversion tests."""
    return SimpleNamespace(name=name, description=description, parameters_schema=parameters_schema)


def _sdk_message_item(text):
    """Build a Responses-API message output item carrying output_text."""
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _sdk_function_call_item(call_id, name, arguments):
    """Build a Responses-API function_call output item (arguments is a JSON string)."""
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments)


def _sdk_response(output_items, *, usage=None, status="completed"):
    """Build a minimal SDK response object compatible with _parse_sdk_response."""
    return SimpleNamespace(output=output_items, usage=usage, status=status)


def _make_fake_openai(sdk_responses, captured):
    """Create a FakeOpenAI class that returns scripted SDK responses.

    ``captured`` is a dict mutated in place so tests can assert on client
    construction kwargs and create() kwargs after the call.
    """

    class FakeResponses:
        def create(self, **kwargs):
            captured["create_kwargs"].append(kwargs)
            if captured["index"] >= len(sdk_responses):
                raise RuntimeError("fake_sdk_exhausted")
            item = sdk_responses[captured["index"]]
            captured["index"] += 1
            if isinstance(item, Exception):
                raise item
            return item

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"].append(kwargs)
            self.responses = FakeResponses()

    return FakeOpenAI


def _two_tools():
    return [
        _tool_schema(
            "get_changed_hunks",
            "Return changed hunks for one path.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        _tool_schema(
            "finish_review",
            "Terminate the review.",
            {
                "type": "object",
                "properties": {"findings": {"type": "array"}},
                "required": ["findings"],
                "additionalProperties": False,
            },
        ),
    ]


def _setup_provider(monkeypatch, sdk_responses, tools=None):
    """Wire a FakeOpenAI into sys.modules and return (provider, captured)."""
    captured = {"create_kwargs": [], "client_kwargs": [], "index": 0}
    fake_openai = _make_fake_openai(sdk_responses, captured)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=fake_openai))
    provider = OpenAIModelProvider(tools or _two_tools(), timeout_seconds=5.0)
    return provider, captured


def test_openai_provider_converts_internal_tool_schemas_to_sdk_format():
    """Schema conversion happens at construction and is inspectable."""
    provider = OpenAIModelProvider(_two_tools())

    assert provider.sdk_tools == (
        {
            "type": "function",
            "name": "get_changed_hunks",
            "description": "Return changed hunks for one path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "finish_review",
            "description": "Terminate the review.",
            "parameters": {
                "type": "object",
                "properties": {"findings": {"type": "array"}},
                "required": ["findings"],
                "additionalProperties": False,
            },
        },
    )


def test_openai_provider_rejects_invalid_tool_schema():
    """Bad tool schemas fail at construction, not during a live SDK call."""
    with pytest.raises(LLMConfigurationError, match="tool_name_must_be_a_non_empty_string"):
        OpenAIModelProvider([_tool_schema("", "desc", {})])
    with pytest.raises(LLMConfigurationError, match="tool_parameters_schema_must_be_an_object"):
        OpenAIModelProvider([_tool_schema("ok", "desc", None)])


def test_openai_provider_parses_text_only_response_without_tool_calls(monkeypatch):
    """No-call: SDK returns only a text message → ModelResponse with text, empty calls."""
    provider, captured = _setup_provider(
        monkeypatch,
        [_sdk_response([_sdk_message_item("Nothing to report.")], usage=SimpleNamespace(input_tokens=8, output_tokens=3, total_tokens=11))],
    )

    response = provider.complete({"history": [{"role": "user", "content": "review"}]})

    assert isinstance(response, ModelResponse)
    assert response.text == "Nothing to report."
    assert response.tool_calls == ()
    assert response.finish_reason == "completed"
    assert response.usage == {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
    # The converted tool schemas must reach the SDK as the tools parameter.
    assert captured["create_kwargs"][0]["tools"] == list(provider.sdk_tools)
    assert captured["create_kwargs"][0]["timeout"] == 5.0
    assert captured["client_kwargs"][0]["max_retries"] == 0


def test_openai_provider_parses_single_tool_call(monkeypatch):
    """Single-call: one function_call item → ModelResponse with one ToolCall."""
    provider, _ = _setup_provider(
        monkeypatch,
        [_sdk_response(
            [_sdk_function_call_item("call-1", "get_changed_hunks", '{"path": "src/app.py"}')],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )],
    )

    response = provider.complete({"history": [{"role": "user", "content": "review"}]})

    assert response.text is None
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.call_id == "call-1"
    assert call.name == "get_changed_hunks"
    assert call.arguments == {"path": "src/app.py"}


def test_openai_provider_parses_multiple_tool_calls_and_text(monkeypatch):
    """Multi-call + text: mixed output → ModelResponse retains both."""
    provider, _ = _setup_provider(
        monkeypatch,
        [_sdk_response(
            [
                _sdk_message_item("I need two checks."),
                _sdk_function_call_item("call-1", "get_changed_hunks", '{"path": "src/app.py"}'),
                _sdk_function_call_item("call-2", "read_file_context", '{"path": "src/utils.py"}'),
            ],
            usage=SimpleNamespace(input_tokens=20, output_tokens=10, total_tokens=30),
        )],
    )

    response = provider.complete({"history": [{"role": "user", "content": "review"}]})

    assert response.text == "I need two checks."
    assert [c.call_id for c in response.tool_calls] == ["call-1", "call-2"]
    assert [c.name for c in response.tool_calls] == ["get_changed_hunks", "read_file_context"]
    assert response.tool_calls[0].arguments == {"path": "src/app.py"}
    assert response.tool_calls[1].arguments == {"path": "src/utils.py"}
    assert response.usage["total_tokens"] == 30


def test_openai_provider_bad_json_arguments_raises_model_protocol_error(monkeypatch):
    """Bad JSON: malformed arguments string → ModelProtocolError, not a silent {}."""
    provider, _ = _setup_provider(
        monkeypatch,
        [_sdk_response([_sdk_function_call_item("call-1", "get_changed_hunks", "{bad json")])],
    )

    with pytest.raises(ModelProtocolError, match="tool_call_arguments_invalid_json"):
        provider.complete({"history": []})


def test_openai_provider_timeout_maps_to_retryable_error(monkeypatch):
    """Timeout: SDK raises APITimeoutError → LLMRetryableError (controller owns retry)."""

    class APITimeoutError(Exception):
        pass

    provider, _ = _setup_provider(monkeypatch, [APITimeoutError("provider timed out")])

    with pytest.raises(LLMRetryableError, match="openai_call_failed:provider timed out"):
        provider.complete({"history": []})


def test_openai_provider_http_503_maps_to_retryable_error(monkeypatch):
    """Provider error: 503 → LLMRetryableError, classified via _is_retryable_provider_error."""

    class ProviderUnavailable(Exception):
        status_code = 503

    provider, _ = _setup_provider(monkeypatch, [ProviderUnavailable("service unavailable")])

    with pytest.raises(LLMRetryableError, match="openai_call_failed:service unavailable"):
        provider.complete({"history": []})


def test_openai_provider_non_retryable_error_maps_to_client_error(monkeypatch):
    """Non-retryable SDK error → LLMClientError (not retried by the controller)."""

    class BadRequest(Exception):
        status_code = 400

    provider, _ = _setup_provider(monkeypatch, [BadRequest("bad request")])

    with pytest.raises(LLMClientError, match="openai_call_failed:bad request"):
        provider.complete({"history": []})


def test_openai_provider_missing_api_key_raises_configuration_error(monkeypatch):
    """Missing API key → LLMConfigurationError before any SDK import or call."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIModelProvider(_two_tools())

    with pytest.raises(LLMConfigurationError, match="missing_OPENAI_API_KEY"):
        provider.complete({"history": []})


def test_openai_provider_passes_converted_history_as_sdk_input(monkeypatch):
    """The controller's JSON-safe history is converted to SDK input items."""
    provider, captured = _setup_provider(
        monkeypatch,
        [_sdk_response([_sdk_function_call_item("call-1", "finish_review", '{"findings": []}')])],
    )

    provider.complete(
        {
            "history": [
                {"role": "user", "content": "review this diff"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"call_id": "prev-1", "name": "get_changed_hunks", "arguments": {"path": "src/app.py"}},
                    ],
                },
                {
                    "role": "tool",
                    "call_id": "prev-1",
                    "name": "get_changed_hunks",
                    "result": {"success": True, "data": {"path": "src/app.py"}},
                },
            ]
        }
    )

    input_items = captured["create_kwargs"][0]["input"]
    assert input_items[0] == {"role": "user", "content": "review this diff"}
    # Assistant with no content but tool_calls emits only function_call items.
    assert input_items[1] == {
        "type": "function_call",
        "call_id": "prev-1",
        "name": "get_changed_hunks",
        "arguments": '{"path": "src/app.py"}',
    }
    # Tool results become function_call_output with a JSON string.
    assert input_items[2]["type"] == "function_call_output"
    assert input_items[2]["call_id"] == "prev-1"


def test_openai_provider_with_no_tools_omits_tools_parameter(monkeypatch):
    """An empty tool set must not send tools=None or tools=[] to the SDK."""
    captured = {"create_kwargs": [], "client_kwargs": [], "index": 0}
    fake_openai = _make_fake_openai(
        [_sdk_response([_sdk_message_item("done")])], captured,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=fake_openai))
    provider = OpenAIModelProvider([])

    provider.complete({"history": [{"role": "user", "content": "hi"}]})

    assert "tools" not in captured["create_kwargs"][0]


def test_openai_provider_missing_usage_returns_empty_dict(monkeypatch):
    """A response without a usage object yields an empty usage dict, not None."""
    provider, _ = _setup_provider(
        monkeypatch,
        [_sdk_response([_sdk_message_item("ok")], usage=None)],
    )

    response = provider.complete({"history": []})
    assert response.usage == {}


def test_openai_provider_satisfies_model_provider_protocol():
    """OpenAIModelProvider is structurally compatible with the ModelProvider Protocol."""
    from src.react_controller import ModelProvider

    provider = OpenAIModelProvider(_two_tools())
    assert isinstance(provider, ModelProvider)

