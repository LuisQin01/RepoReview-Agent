import json
from dataclasses import asdict

from .schemas import ReviewIssue
from .validation import validate_llm_response

def build_llm_prompt(changed_files, contexts, rule_issues):
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
    return f"""
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
      "confidence": 0.8
    }}
  ]
}}

输入如下：
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()

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
                suggestion=finding["suggested_fix"]
            )
        )
    return issues, validation

def review_with_llm(changed_files, contexts, rule_issues, call_model):
    prompt=build_llm_prompt(
        changed_files=changed_files,
        contexts=contexts,
        rule_issues=rule_issues
    )
    
    response_text=call_model(prompt)
    return parse_llm_response(response_text)