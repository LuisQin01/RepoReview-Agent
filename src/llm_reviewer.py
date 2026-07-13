import json
from dataclasses import asdict

from .schemas import ContextBudget, ReviewIssue
from .validation import validate_llm_response

_TRUNCATION_MARKER = "\n[TRUNCATED: prompt budget reached]\n"


def _apply_prompt_budget(prompt, max_prompt_chars):
    """Return a deterministic, visibly truncated prompt within its limit."""
    if max_prompt_chars <= 0:
        raise ValueError("max_prompt_chars must be greater than 0")
    if len(prompt) <= max_prompt_chars:
        return prompt

    # Very small budgets cannot hold the full marker.  The ellipsis remains a
    # visible truncation indicator while preserving the hard length contract.
    if max_prompt_chars < len(_TRUNCATION_MARKER):
        return "…"[:max_prompt_chars]
    return prompt[:max_prompt_chars - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def build_llm_prompt(
        changed_files,
        contexts,
        rule_issues,
        max_prompt_chars=None,
    ):
    """Build the complete LLM input and enforce its character budget.

    Render order is intentional: fixed instructions come first, followed by
    patches and changed lines, file contexts, and rule findings.  If the
    limit is reached, the explicit marker shows that trailing input was
    omitted.  The returned string is the authoritative budgeted artifact.
    """
    if max_prompt_chars is None:
        max_prompt_chars = ContextBudget().max_prompt_chars

    payload={
        "changed_files":[
            {
                "path":changed_file.path,
                "patch":changed_file.patch,
                "added_lines":[asdict(line) for line in changed_file.added_lines],
                "deleted_lines":[asdict(line) for line in changed_file.deleted_lines],
            }
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
    prompt = _apply_prompt_budget(prompt, max_prompt_chars)
    if len(prompt) > max_prompt_chars:
        raise RuntimeError("LLM prompt exceeded max_prompt_chars")
    return prompt

def _severity_from_llm(severity):
    mapping = {
        "high": "error",
        "medium": "warning",
        "low": "info"
    }
    return mapping.get(severity, "warning")

def parse_llm_response(response_text):
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
    prompt=build_llm_prompt(
        changed_files=changed_files,
        contexts=contexts,
        rule_issues=rule_issues,
        max_prompt_chars=max_prompt_chars,
    )

    response_text=call_model(prompt)
    return parse_llm_response(response_text)
