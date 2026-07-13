import json
import os
from functools import partial


class LLMClientError(RuntimeError):
    pass

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
        raise LLMClientError("mock_timeout")
    if fixture == "bad_json":
        return "{not valid json"

    try:
        return json.dumps(MOCK_RESPONSE_FIXTURES[fixture], ensure_ascii=False)
    except KeyError as exc:
        raise LLMClientError(f"unsupported_mock_fixture:{fixture}") from exc

def real_call_model(prompt:str)->str:
    api_key=os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMClientError("missing_OPENAI_API_KEY")
    
    model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        from openai import OpenAI
        client=OpenAI(api_key=api_key)
        response=client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content":"You are a strict code review assistant. Return JSON only.",
                },
                {
                    "role":"user",
                    "content":prompt,
                },
            ],
        )
        if not response.output_text:
            raise LLMClientError("openai_empty_response")
        
        return response.output_text
    
    except LLMClientError:
        raise
    except Exception as exc:
        raise LLMClientError(f"openai_call_failed:{exc}") from exc
    
def get_call_model(provider: str, *, mock_fixture: str = "normal"):
    if provider == "mock":
        return partial(mock_call_model, fixture=mock_fixture)
    if provider=="openai":
        return real_call_model
    
    raise LLMClientError(f"unsupported_llm_provider:{provider}")
