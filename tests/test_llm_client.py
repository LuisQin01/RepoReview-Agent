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
    LLMRetryableError,
    get_call_model,
)


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
