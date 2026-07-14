'''
用来记录运行过程，方便进行debug和后面做eval

记录这些内容：
1. 本次输入 diff 是什么
2. 解析出了哪些文件
3. 审查器发现了哪些问题
4. 耗时多久
5. 是否调用了模型
6. 模型 prompt 和 response 是什么
'''

import json
import re

from datetime import datetime
from pathlib import Path
from time import perf_counter

_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password)\s*"
        r"(?:['\"]\s*)?[=:]\s*)(?:['\"])?[^\s,'\";}\]]+"
    ),
)
_BARE_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
)


def redact_sensitive_values(value):
    """Redact labeled and recognizable bare credentials without truncating."""
    text = str(value)
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    for pattern in _BARE_SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_sensitive_structure(value):
    """Recursively redact string values before structured trace persistence."""
    if isinstance(value, dict):
        return {key: redact_sensitive_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_structure(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_values(value)
    return value


def sanitize_trace_text(value, max_chars=300):
    text = redact_sensitive_values(value)
    if len(text)<=max_chars:
        return text
    return text[:max_chars]+"..."

def _llm_called(steps):
    return any(
        step.get("step")=="run_llm_review"
        and step.get("detail", {}).get("called") is True
        for step in steps
    )

def save_trace(state, trace_dir="traces", final_step=None):
    trace_root=Path(trace_dir)
    trace_root.mkdir(parents=True, exist_ok=True)

    timestamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_path=trace_root/f"{timestamp}_{state.task_id}.json"

    duration_ms=int((perf_counter()-state.started_at_perf)*1000)

    payload={
        "task_id":state.task_id,
        "steps":redact_sensitive_structure(state.trace_steps),
        "input_files":[
            {
                "path":redact_sensitive_values(changed_file.path),
                "added_lines":len(changed_file.added_lines),
                "deleted_lines":len(changed_file.deleted_lines),
            }
            for changed_file in state.changed_files
        ],
        "context_files":[
            {
                "path":redact_sensitive_values(context.path),
                "exists":context.exists,
                "chars_read":context.chars_read,
                "truncated":context.truncated,
                "error":redact_sensitive_values(context.error),
                "source":redact_sensitive_values(context.source),
                "selection_reason":redact_sensitive_values(context.selection_reason),
            }
            for context in state.contexts
        ],
        "llm_called":_llm_called(state.trace_steps),
        "llm_provider": state.llm_provider,
        "findings_count": len(state.issues),
        "duration_ms": duration_ms,
        "errors": [sanitize_trace_text(error) for error in state.errors],
    }

    trace_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if final_step is not None:
        state.trace_steps.append(
            {
                "step": final_step["step"],
                "duration_ms": int(
                    (perf_counter() - final_step["started_at_perf"]) * 1000
                ),
                "detail": final_step.get("detail", {}),
            }
        )
        payload["steps"] = redact_sensitive_structure(state.trace_steps)
        trace_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    state.trace_path=str(trace_path)
    return trace_path

