"""LLM 审查编排模块。

模块职责：
    本模块负责把“变更文件 + 文件上下文 + 规则审查结果”组织成 LLM 可消费的 prompt，
    调用模型拿到响应，再把响应解析为 ReviewIssue。它是规则审查（reviewers）与
    LLM 调用（llm_client）之间的编排层。

在整体架构中的位置：
        changed_files ─┐
        contexts      ─┼─▶ build_llm_prompt ─▶ call_model ─▶ parse_llm_response ─▶ ReviewIssue(source="llm")
        rule_issues   ─┘                       (来自 llm_client)    (调用 validation)

    本模块做了三层关键防护：
      L1 预算防护：build_llm_prompt 强制字符预算，超长时确定性截断并保留可见标记；
      L2 输出防护：parse_llm_response 委托 validation.validate_llm_response 做容错修复；
      L3 脱敏防护：_sanitize_changed_file_for_prompt 对敏感文件 diff 脱敏后再入 prompt。

设计理由：
    - 渲染顺序固定（指令 → patches → contexts → rule_findings）：让 LLM 优先看到稳定指令，
      截断发生时优先丢弃尾部（rule_findings），保留对发现 bug 最关键的 diff 信息。
    - 截断保留可见标记：LLM 能感知到输入被截断，避免基于残缺输入编造结论。
    - 敏感文件脱敏在 prompt 构造阶段完成，而非依赖下游，确保密钥/凭据不会进入模型上下文。
"""
import json
from dataclasses import asdict

from .file_context import _is_sensitive_file_path
from .schemas import ContextBudget, ReviewIssue
from .validation import validate_llm_response

# 截断标记：放在 prompt 末尾，明确告知 LLM 输入被截断，避免它基于残缺输入编造结论。
# 标记本身也计入预算，保证截断后的总长度严格 <= max_prompt_chars。
_TRUNCATION_MARKER = "\n[TRUNCATED: prompt budget reached]\n"
# 敏感文件 diff 的脱敏占位符：替换真实 patch，防止密钥/凭据进入 LLM 上下文。
_REDACTED_DIFF_PLACEHOLDER = "[REDACTED: sensitive file diff suppressed]"


def _apply_prompt_budget(prompt, max_prompt_chars):
    """对 prompt 做确定性截断，保证返回字符串长度严格不超过预算。

    截断策略（保证可见性 + 严格长度）：
      1. prompt 本身未超预算 → 原样返回；
      2. 预算 < 截断标记长度 → 无法放下完整标记，用省略号 … 填充至预算长度；
      3. 正常情况 → 截断到 (预算 - 标记长度)，再拼接截断标记，使总长恰好等于预算。

    Args:
        prompt: 原始 prompt 字符串。
        max_prompt_chars: 最大字符数，必须 > 0。

    Returns:
        str: 长度 <= max_prompt_chars 的 prompt，超长时带可见截断标记。

    Raises:
        ValueError: max_prompt_chars <= 0。
    """
    if max_prompt_chars <= 0:
        raise ValueError("max_prompt_chars must be greater than 0")
    if len(prompt) <= max_prompt_chars:
        return prompt

    # Very small budgets cannot hold the full marker.  The ellipsis remains a
    # visible truncation indicator while preserving the hard length contract.
    if max_prompt_chars < len(_TRUNCATION_MARKER):
        return "…"[:max_prompt_chars]
    return prompt[:max_prompt_chars - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _sanitize_changed_file_for_prompt(changed_file):
    """对单个变更文件做脱敏处理，生成可放入 prompt 的安全结构。

    L3 防护层：diff 来自 git，绕过了 read_file_context 的敏感文件拦截，
    因此在此处补做脱敏。需要同时检查新路径和旧路径（rename 源）——
    因为一个敏感文件被重命名为普通名字后，其 deleted_lines/patch 中
    仍携带敏感内容。

    Args:
        changed_file: 变更文件对象，需提供 path、old_path、patch、added_lines、deleted_lines。

    Returns:
        dict: 脱敏后的结构。
              - 敏感文件：patch 替换为占位符，added_lines/deleted_lines 清空；
              - 普通文件：原样输出，行对象用 asdict 转为可序列化 dict。
    """
    # Diffs arrive from git and bypass read_file_context, so redact sensitive paths here.
    # Check both the new path and the old path (rename source) — a sensitive file
    # renamed to a non-sensitive name still carries secret content in deleted_lines/patch.
    if _is_sensitive_file_path(changed_file.path) or (
        changed_file.old_path
        and _is_sensitive_file_path(changed_file.old_path)
    ):
        return {
            "path": changed_file.path,
            "patch": _REDACTED_DIFF_PLACEHOLDER,
            "added_lines": [],
            "deleted_lines": [],
        }
    return {
        "path": changed_file.path,
        "patch": changed_file.patch,
        "added_lines": [asdict(line) for line in changed_file.added_lines],
        "deleted_lines": [asdict(line) for line in changed_file.deleted_lines],
    }


def build_llm_prompt(
        changed_files,
        contexts,
        rule_issues,
        max_prompt_chars=None,
    ):
    """构建完整的 LLM 输入 prompt，并强制字符预算。

    渲染顺序是刻意设计的：固定指令在前，随后是 patches 与变更行、文件上下文、
    最后是规则发现。若达到预算上限，显式的截断标记会表明尾部输入被省略。
    返回的字符串是经过预算控制的权威产物。

    Args:
        changed_files: 变更文件列表，会先经 _sanitize_changed_file_for_prompt 脱敏。
        contexts: 文件上下文列表（FileContext），用 asdict 序列化进 payload。
        rule_issues: 规则审查结果（ReviewIssue），作为 LLM 的参考输入，放在 payload 末尾。
        max_prompt_chars: prompt 字符预算上限；None 时取 ContextBudget().max_prompt_chars。

    Returns:
        str: 已脱敏、已截断的 prompt 字符串，长度 <= max_prompt_chars。

    Raises:
        RuntimeError: 截断后仍超出预算（理论上不应发生，作为硬性兜底断言）。
    """
    if max_prompt_chars is None:
        max_prompt_chars = ContextBudget().max_prompt_chars

    # 组装 payload：变更文件（已脱敏）→ 上下文 → 规则发现，顺序与渲染顺序一致。
    payload={
        "changed_files":[
            _sanitize_changed_file_for_prompt(changed_file)
            for changed_file in changed_files
        ],
        "contexts":[asdict(context) for context in contexts],
        "rule_findings":[asdict(issue) for issue in rule_issues],
    }
    prompt = f"""
你是一个严谨的代码审查助手。

请根据 git diff、文件上下文和已有规则检查结果，继续发现更深层的问题。
重点关注：
1. 潜在 bug
2. 安全风险
3. 异常处理遗漏
4. 测试缺失
5. 逻辑边界条件

你必须只输出 JSON，不要输出 Markdown，不要解释。

JSON 格式固定如下：
{{
  "findings": [
    {{
      "severity": "high | medium | low",
      "file": "src/example.py",
      "line": 12,
      "issue": "问题描述",
      "reason": "为什么这是问题",
      "suggested_fix": "建议如何修复",
      "confidence": 0.8,
      "evidence": "支持该问题的代码或行号"
    }}
  ]
}}

source 字段由系统根据调用路径赋值；不要在 JSON 中输出 source。

输入如下：
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()
    # 强制预算截断，保证 prompt 不会超过模型/系统限制
    prompt = _apply_prompt_budget(prompt, max_prompt_chars)
    # 兜底断言：截断后仍超长说明 _apply_prompt_budget 有 bug，直接抛错而非发给模型
    if len(prompt) > max_prompt_chars:
        raise RuntimeError("LLM prompt exceeded max_prompt_chars")
    return prompt

def _severity_from_llm(severity):
    """把 LLM 输出的 severity（high/medium/low）映射为系统内部严重等级。

    LLM 约定使用 high/medium/low，而系统内部 ReviewIssue 使用 error/warning/info，
    二者语义对齐：high→error，medium→warning，low→info。
    未知值兜底为 warning（中等优先级，既不夸大也不忽略）。

    Args:
        severity: LLM 输出的严重等级字符串。

    Returns:
        str: 系统内部严重等级，取值 error / warning / info。
    """
    mapping = {
        "high": "error",
        "medium": "warning",
        "low": "info"
    }
    return mapping.get(severity, "warning")

def parse_llm_response(response_text):
    """把 LLM 响应文本解析为 ReviewIssue 列表，并返回校验细节。

    委托 validation.validate_llm_response 做容错解析，再把规范化后的 finding
    转成 ReviewIssue（source="llm"、category="llm"），与规则审查结果在数据模型上对齐。

    Args:
        response_text: LLM 返回的原始字符串。

    Returns:
        tuple: (issues, validation)
            - issues: list[ReviewIssue]，source="llm" 的审查意见；
            - validation: ValidationResult，含 valid/repaired/errors，供上游决定是否告警。
    """
    validation = validate_llm_response(response_text)

    issues = []
    for finding in validation.findings:
        issues.append(
            ReviewIssue(
                file_path=finding["file"],
                line_no=int(finding["line"] or 0),
                severity=_severity_from_llm(finding["severity"]),
                category="llm",
                message=finding["issue"],
                suggestion=finding["suggested_fix"],
                reason=finding["reason"],
                confidence=finding["confidence"],
                evidence=finding["evidence"],
                source="llm",
            )
        )
    return issues, validation

def review_with_llm(
        changed_files,
        contexts,
        rule_issues,
        call_model,
        max_prompt_chars=None,
    ):
    """LLM 审查的顶层编排入口，串联 构建 prompt → 调用模型 → 解析响应。

    Args:
        changed_files: 变更文件列表。
        contexts: 文件上下文列表。
        rule_issues: 规则审查结果，作为 LLM 的参考输入。
        call_model: 可调用对象，接收 prompt 返回模型响应文本（通常带重试，来自 llm_client.get_call_model）。
        max_prompt_chars: prompt 字符预算上限，None 时使用默认值。

    Returns:
        tuple: parse_llm_response 的返回值 (issues, validation)。
    """
    prompt=build_llm_prompt(
        changed_files=changed_files,
        contexts=contexts,
        rule_issues=rule_issues,
        max_prompt_chars=max_prompt_chars,
    )

    response_text=call_model(prompt)
    return parse_llm_response(response_text)
