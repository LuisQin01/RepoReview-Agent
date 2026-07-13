'''
reporter.py负责展示结果，把问题列表变成markdown文本，或者json文本
reviewers.py负责审查代码
'''

import json
from collections import Counter

def _severity_for_json(severity):
    mapping={
        "error":"high",
        "warning":"medium",
        "info":"low",
    }
        
    return mapping.get(severity, severity)

def _escape_table_cell(value):
    text=str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")

def _context_status(context):
    if not context.exists:
        return f"missing: {context.error}"
    if context.truncated:
        return f"truncated: {context.chars_read} chars"
    return f"loaded, {context.chars_read} chars"

def issue_to_finding(issue):
    return {
        "severity":_severity_for_json(issue.severity),
        "category":issue.category,
        "file":issue.file_path,
        "line":issue.line_no,
        "issue":issue.message,
        "reason":issue.reason,
        "suggested_fix":issue.suggestion,
        "confidence":issue.confidence,
        "evidence":issue.evidence,
        "source":issue.source,
    }

def render_json_report(issues):
    data={
        "findings":[
            issue_to_finding(issue)
            for issue in issues
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)

def render_markdown_report(issues, changed_files, contexts):
    '''
    负责把现在的ReviewIssue列表变成markdown文本
    '''
    severity_counts=Counter(issue.severity for issue in issues)
    contexts_by_path={
        context.path:context
        for context in contexts
    }
    changed_paths={changed_file.path for changed_file in changed_files}
    lines=[
        "# Repo Review Report",
        "",
        "## Summary",
        "",
        f"- Changed files: {len(changed_files)}",
        f"- Findings: {len(issues)}",
        f"- Errors: {severity_counts.get('error', 0)}",
        f"- Warnings: {severity_counts.get('warning', 0)}",
        f"- Info: {severity_counts.get('info', 0)}",
        "",
        "## Changed Files",
        "",
        "| File | Added Lines | Deleted Lines | Context |",
        "| --- | ---: | ---: | --- |",
    ]

    for changed_file in changed_files:
        context=contexts_by_path.get(changed_file.path)
        status=_context_status(context) if context else "no collected"

        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table_cell(changed_file.path),
                    str(len(changed_file.added_lines)),
                    str(len(changed_file.deleted_lines)),
                    _escape_table_cell(status),
                ]
            )
            + " |"
        )

    lines.extend([
        "",
        "## Context Files",
        "",
    ])

    if not contexts:
        lines.append("No context files collected.")
    else:
        lines.extend([
            "| File | Type | Status |",
            "| --- | --- | --- |",
        ])

        for context in contexts:
            context_type="changed" if context.path in changed_paths else "related"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_table_cell(context.path),
                        context_type,
                        _escape_table_cell(_context_status(context)),
                    ]
                )
                + " |"
            )


    lines.extend([
        "",
        "## Findings",
        "",
    ])

    if not issues:
        lines.append("No issues found.")
    else:
        lines.extend([
        "| Severity | File | Line | Category | Issue | Reason | Suggestion | Confidence | Evidence | Source |",
        "| --- | --- | ---: | --- | --- | --- | --- | ---: | --- | --- |",
        ])
            
        severity_order={"error":0, "warning":1, "info":2}

        sorted_issues=sorted(
            issues,
            key=lambda issue:(
                severity_order.get(issue.severity, 99),
                issue.file_path,
                issue.line_no,
                issue.category,
            ),
        )

        for issue in sorted_issues:
            lines.append(
                 "| "
                + " | ".join(
                    [
                        _escape_table_cell(issue.severity),
                        _escape_table_cell(issue.file_path),
                        str(issue.line_no),
                        _escape_table_cell(issue.category),
                        _escape_table_cell(issue.message),
                        _escape_table_cell(issue.reason),
                        _escape_table_cell(issue.suggestion),
                        _escape_table_cell("" if issue.confidence is None else issue.confidence),
                        _escape_table_cell(issue.evidence),
                        _escape_table_cell(issue.source),
                    ]
                )
                + " |"
            )
    lines.extend([
         "",
        "## JSON Output",
        "",
        "```json",
        render_json_report(issues),
        "```",
        "",
    ])

    return "\n".join(lines)
