import json
import os
import time
from functools import partial


DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.25


class LLMClientError(RuntimeError):
    pass


class LLMRetryableError(LLMClientError):
    """An LLM provider failure that may succeed on a later attempt."""


class LLMConfigurationError(LLMClientError):
    """A local configuration error that retrying cannot resolve."""


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
    if fixture == "timeout":
        raise LLMRetryableError("mock_timeout")
    if fixture == "bad_json":
        return "{not valid json"

    try:
        return json.dumps(MOCK_RESPONSE_FIXTURES[fixture], ensure_ascii=False)
    except KeyError as exc:
        raise LLMConfigurationError(f"unsupported_mock_fixture:{fixture}") from exc


def make_mock_call_model(fixture: str):
    """Build a stateful mock callable for retry-path tests and CLI smoke tests."""
    failures_remaining = 2 if fixture == "timeout_then_success" else 0

    def call_model(prompt: str) -> str:
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise LLMRetryableError("mock_timeout")
        if fixture == "timeout_then_success":
            return mock_call_model(prompt, fixture="normal")
        return mock_call_model(prompt, fixture=fixture)

    return call_model


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if type(exc).__name__ in {"APITimeoutError", "APIConnectionError"}:
        return True

    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or isinstance(status_code, int) and status_code >= 500


def real_call_model(prompt: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigurationError("missing_OPENAI_API_KEY")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        from openai import OpenAI

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
        if not response.output_text:
            raise LLMClientError("openai_empty_response")

        return response.output_text
    except LLMClientError:
        raise
    except Exception as exc:
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
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_base_delay_seconds < 0:
        raise ValueError("retry_base_delay_seconds must be non-negative")

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
            retry_info["retry_errors"].append(str(exc))
            if attempt == max_attempts - 1:
                retry_info["exhausted"] = True
                raise
            retry_info["retries"] += 1
            sleep(retry_base_delay_seconds * (2**attempt))

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
    if provider == "mock":
        call_model = make_mock_call_model(mock_fixture)
    elif provider == "openai":
        call_model = partial(real_call_model, timeout_seconds=timeout_seconds)
    else:
        raise LLMConfigurationError(f"unsupported_llm_provider:{provider}")

    def call_model_with_retries(prompt: str) -> str:
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
            call_model_with_retries.last_retry_info = retry_info

    call_model_with_retries.last_retry_info = {
        "attempts": 0,
        "retries": 0,
        "retry_errors": [],
        "exhausted": False,
    }
    return call_model_with_retries
