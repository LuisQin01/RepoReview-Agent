import sys
from types import SimpleNamespace

import pytest

from src.llm_client import (
    LLMConfigurationError,
    LLMRetryableError,
    get_call_model,
)


def test_mock_retryable_failures_recover_with_exponential_backoff():
    delays = []
    call_model = get_call_model(
        "mock",
        mock_fixture="timeout_then_success",
        max_attempts=3,
        retry_base_delay_seconds=0.5,
        sleep=delays.append,
    )

    response = call_model("review this diff")

    assert '"findings"' in response
    assert delays == [0.5, 1.0]


def test_retryable_failure_raises_after_limited_attempts():
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

    assert delays == [0.25, 0.5]


def test_configuration_error_is_not_retried(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    delays = []
    call_model = get_call_model("openai", max_attempts=3, sleep=delays.append)

    with pytest.raises(LLMConfigurationError, match="missing_OPENAI_API_KEY"):
        call_model("review this diff")

    assert delays == []


def test_openai_call_applies_timeout_to_client_and_request(monkeypatch):
    client_arguments = []
    request_arguments = []

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            return SimpleNamespace(output_text='{"findings": []}')

    class FakeOpenAI:
        def __init__(self, **kwargs):
            client_arguments.append(kwargs)
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    call_model = get_call_model("openai", timeout_seconds=7.5, max_attempts=1)

    response = call_model("review this diff")

    assert response == '{"findings": []}'
    assert client_arguments == [
        {"api_key": "test-key", "timeout": 7.5, "max_retries": 0}
    ]
    assert request_arguments[0]["timeout"] == 7.5


def test_openai_timeout_is_retried_then_recovers(monkeypatch):
    request_arguments = []

    class APITimeoutError(Exception):
        pass

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            if len(request_arguments) == 1:
                raise APITimeoutError("provider timed out")
            return SimpleNamespace(output_text='{"findings": []}')

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
    assert delays == [0.25]
    assert [request["timeout"] for request in request_arguments] == [2.0, 2.0]


def test_openai_http_503_is_retried_then_recovers(monkeypatch):
    request_arguments = []

    class ProviderUnavailable(Exception):
        status_code = 503

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            if len(request_arguments) == 1:
                raise ProviderUnavailable("service unavailable")
            return SimpleNamespace(output_text='{"findings": []}')

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
    assert len(request_arguments) == 2
    assert delays == [0.25]


def test_openai_http_503_raises_after_limited_attempts(monkeypatch):
    request_arguments = []

    class ProviderUnavailable(Exception):
        status_code = 503

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)
            raise ProviderUnavailable("service unavailable")

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

    assert len(request_arguments) == 3
    assert delays == [0.25, 0.5]
