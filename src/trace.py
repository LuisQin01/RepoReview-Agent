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

from datetime import datetime
from pathlib import Path
from time import perf_counter

def _short_text(value, max_chars=300):
    text=str(value)
    if len(text)<=max_chars:
        return text
    return text[:max_chars]+"..."

def _llm_called(steps):
    return any(
        step.get("step")=="run_llm_review"
        and step.get("detail", {}).get("called") is True
        for step in steps
    )

def save_trace(state, trace_dir="traces"):
    trace_root=Path(trace_dir)
    trace_root.mkdir(parents=True, exist_ok=True)

    timestamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_path=trace_root/f"{timestamp}_{state.task_id}.json"

    duration_ms=int((perf_counter()-state.started_at_perf)*1000)

    payload={
        "task_id":state.task_id,
        "steps":state.trace_steps,
        "input_files":[
            {
                "path":changed_file.path,
                "added_lines":len(changed_file.added_lines),
                "deleted_lines":len(changed_file.deleted_lines),
            }
            for changed_file in state.changed_files
        ],
        "context_files":[
            {
                "path":context.path,
                "exists":context.exists,
                "chars_read":context.chars_read,
                "truncated":context.truncated,
                "error":context.error,
                "source":context.source,
                "selection_reason":context.selection_reason,
            }
            for context in state.contexts
        ],
        "llm_called":_llm_called(state.trace_steps),
        "llm_provider": state.llm_provider,
        "findings_count": len(state.issues),
        "duration_ms": duration_ms,
        "errors": [_short_text(error) for error in state.errors],
    }

    trace_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    state.trace_path=str(trace_path)
    return trace_path

