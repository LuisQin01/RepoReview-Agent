'''
运行轨迹记录模块（trace）：用于 debug 排查与后续 eval 评估。

在整体架构中的位置：
- 上游：review_service.py 在每个 pipeline 步骤后调用 record_step 记录耗时与详情，
  最终由 save_trace 持久化到 traces 目录。
- 下游：eval 流程读取 trace JSON 文件，据此统计 actual_categories、
  是否调用模型、各步骤耗时等指标。

记录这些内容：
1. 本次输入 diff 是什么
2. 解析出了哪些文件
3. 审查器发现了哪些问题
4. 耗时多久
5. 是否调用了模型
6. 模型 prompt 和 response 是什么

设计理由：将"执行过程"与"执行结果"一同持久化，便于离线复盘 LLM 决策质量、
定位性能瓶颈；同时所有写入都经脱敏处理，避免密钥/Token 落盘泄露。
'''

import json
import re

from datetime import datetime
from pathlib import Path
from time import perf_counter

# 带标签的敏感值模式：匹配形如 "Authorization: Bearer xxx" / "api_key=xxx"
# 的结构化键值对。第 1 捕获组保留标签前缀（含冒号/等号），第 2 部分替换为 [REDACTED]。
# 使用 re.compile 预编译以提升热路径性能（trace 脱敏会被频繁调用）。
_SENSITIVE_VALUE_PATTERNS = (
    # 匹配 HTTP Authorization 头中的 Bearer token
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    # 匹配常见键名（api_key / access_token / token / secret / password）
    # 后接 = 或 : 分隔符的赋值，支持可选引号包裹的值
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password)\s*"
        r"(?:['\"]\s*)?[=:]\s*)(?:['\"])?[^\s,'\";}\]]+"
    ),
)
# 裸 token 模式：匹配无明显标签但可通过前缀特征识别的凭证。
# 不依赖键名，仅靠 token 前缀（ghp_ / github_pat_ / sk-）识别，作为兜底防护。
_BARE_SENSITIVE_VALUE_PATTERNS = (
    # GitHub Personal Access Token（经典）前缀 ghp_/gho_/ghs_/ghr_/ghu_ 等
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # GitHub fine-grained PAT 前缀 github_pat_
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # OpenAI API key 前缀 sk-，含 proj-/svcacct- 变体
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
)


def redact_sensitive_values(value):
    """
    对字符串中的敏感凭证做正则替换为 [REDACTED]，且不截断原文。

    分两轮替换：
      1. 带标签模式（_SENSITIVE_VALUE_PATTERNS）：保留键名前缀，仅替换值为 [REDACTED]，
         例如 "api_key=sk-xxx" -> "api_key=[REDACTED]"；
      2. 裸 token 模式（_BARE_SENSITIVE_VALUE_PATTERNS）：整体替换为 [REDACTED]，
         例如 "ghp_xxx" -> "[REDACTED]"。

    不做截断是为了保留完整上下文，便于排查问题；截断由 sanitize_trace_text 负责。

    Args:
        value: 任意输入，会先经 str() 转字符串再做匹配。

    Returns:
        str: 脱敏后的字符串；无匹配时原样返回。
    """
    text = str(value)
    # 第一轮：带标签替换，第 1 捕获组（标签前缀）保留，值替换为 [REDACTED]
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    # 第二轮：裸 token 整体替换为 [REDACTED]
    for pattern in _BARE_SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_sensitive_structure(value):
    """
    递归遍历 dict / list / tuple / str 结构，对其中所有字符串做脱敏。

    用于在 trace 持久化前对结构化 detail 做整体脱敏：record_step 的 detail
    可能嵌套 dict/list，需逐层下钻到 str 再调用 redact_sensitive_values。

    非字符串类型（int/float/bool/None 等）原样返回，避免破坏数据类型。

    Args:
        value: 任意 Python 对象（dict/list/tuple/str/其他）。

    Returns:
        与原结构同形态的新对象，其中所有字符串已脱敏。
        注意：tuple 会被重建为新的 tuple，dict/list 同理（浅层复制 + 递归）。
    """
    if isinstance(value, dict):
        # 递归处理 dict 的每个 value（key 不处理，假设 key 不含敏感信息）
        return {key: redact_sensitive_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        # 递归处理 list 的每个元素
        return [redact_sensitive_structure(item) for item in value]
    if isinstance(value, tuple):
        # tuple 不可变，需重建为新的 tuple
        return tuple(redact_sensitive_structure(item) for item in value)
    if isinstance(value, str):
        # 到达字符串叶子节点，做实际脱敏
        return redact_sensitive_values(value)
    # 非字符串标量原样返回
    return value


def sanitize_trace_text(value, max_chars=300):
    """
    对文本先脱敏再截断到 max_chars，用于 trace 中错误信息等长文本。

    两步处理顺序很重要：先脱敏再截断，确保截断后的片段不会
    恰好暴露敏感值的前缀；同时避免脱敏后的 [REDACTED] 被二次截断。

    Args:
        value: 任意输入，会先经 str() 转字符串。
        max_chars (int): 最大保留字符数，超出部分以 "..." 结尾标识截断。默认 300。

    Returns:
        str: 脱敏并按需截断后的字符串。
    """
    # 先脱敏，避免敏感值在截断后残留可识别片段
    text = redact_sensitive_values(value)
    if len(text)<=max_chars:
        return text
    # 截断并加省略号标识，便于读者知晓内容不完整
    return text[:max_chars]+"..."

def _llm_called(steps):
    """
    判断本次运行是否实际调用了 LLM。

    通过遍历 trace_steps 查找 step=="run_llm_review" 且 detail.called 为 True
    的记录来确定。这是 eval 的关键标志：用于区分"规则 only"与"规则+LLM"两种模式
    的结果质量差异。

    Args:
        steps (list[dict]): trace_steps 列表，每项含 step 与 detail 字段。

    Returns:
        bool: 只要存在一次成功的 LLM 调用即为 True，否则 False。
    """
    return any(
        step.get("step")=="run_llm_review"
        and step.get("detail", {}).get("called") is True
        for step in steps
    )

def save_trace(state, trace_dir="traces", final_step=None):
    """
    将运行状态序列化为 JSON 写入 traces 目录，返回 trace 文件路径。

    文件名格式为 "{时间戳}_{task_id}.json"，时间戳精确到秒，task_id 保证
    同秒不同任务不冲突。payload 含任务元信息、各步骤耗时、输入/上下文文件、
    是否调用 LLM、findings 计数、总耗时与错误列表。

    final_step 机制：save_trace 自身的耗时也需记录，但需在写文件之后才能追加，
    故支持传入 final_step 在首次写入后追加并重写一次文件，保证 trace 完整。

    Args:
        state: ReviewState 对象，含 task_id/trace_steps/changed_files/contexts/
               issues/errors/started_at_perf/llm_provider 等字段。
        trace_dir (str): trace 输出目录，默认 "traces"。不存在时自动创建。
        final_step (dict|None): 需在首次写入后追加的最终步骤，含 step/
               started_at_perf/detail 字段；为 None 时只写一次。

    Returns:
        Path: 写入的 trace 文件路径对象。
    """
    # 确保输出目录存在，parents=True 递归创建，exist_ok=True 容忍已存在
    trace_root=Path(trace_dir)
    trace_root.mkdir(parents=True, exist_ok=True)

    # 时间戳精确到秒，与 task_id 组合保证文件名唯一
    timestamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_path=trace_root/f"{timestamp}_{state.task_id}.json"

    # 总耗时：从 state 初始化时刻到当前，转为毫秒整数
    duration_ms=int((perf_counter()-state.started_at_perf)*1000)

    # 组装 payload：所有字段均经脱敏处理，防止敏感信息落盘
    payload={
        "task_id":state.task_id,
        # steps 做递归脱敏，detail 中可能含 prompt/response 文本
        "steps":redact_sensitive_structure(state.trace_steps),
        # 输入文件清单：路径脱敏，行数保留
        "input_files":[
            {
                "path":redact_sensitive_values(changed_file.path),
                "added_lines":len(changed_file.added_lines),
                "deleted_lines":len(changed_file.deleted_lines),
            }
            for changed_file in state.changed_files
        ],
        # 上下文文件清单：记录采集状态，便于 eval 分析上下文质量
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
        # 是否调用 LLM 的布尔标志，eval 据此分组统计
        "llm_called":_llm_called(state.trace_steps),
        "llm_provider": state.llm_provider,
        "findings_count": len(state.issues),
        "duration_ms": duration_ms,
        # errors 逐条脱敏并截断，防止单条错误过长撑爆 trace 文件
        "errors": [sanitize_trace_text(error) for error in state.errors],
    }

    # 首次写入：序列化为带缩进的 JSON，ensure_ascii=False 保留中文
    trace_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if final_step is not None:
        # 追加 final_step（如 save_trace 自身），并重写文件以包含完整步骤序列
        state.trace_steps.append(
            {
                "step": final_step["step"],
                "duration_ms": int(
                    (perf_counter() - final_step["started_at_perf"]) * 1000
                ),
                "detail": final_step.get("detail", {}),
            }
        )
        # 更新 payload 的 steps 后重写文件
        payload["steps"] = redact_sensitive_structure(state.trace_steps)
        trace_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 回写 trace 路径到 state，便于上游引用与日志输出
    state.trace_path=str(trace_path)
    return trace_path

