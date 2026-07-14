'''
报告渲染层（Reporter）：负责把 ReviewIssue 列表、变更文件、上下文文件等中间结果
转换成面向人类阅读的 Markdown 文本，或面向机器解析的 JSON 文本。

在整体架构中的位置：
- 上游：review_service.py 在 render_report 步骤调用本模块渲染最终输出。
- 下游：渲染结果会写入 state.output，并可由 git_provider 发布到 PR 评论。
- 设计理由：将"问题数据"与"展示文本"解耦，使审查逻辑（reviewers.py）
  无需关心输出格式，便于后续扩展 HTML / SARIF 等新格式。

相关模块：
- reviewers.py 负责审查代码并产出 ReviewIssue 列表。
- trace.py 提供 redact_sensitive_values，用于在渲染前对敏感信息脱敏。
- git_provider.py 提供 SUMMARY_COMMENT_MARKER，用于标记幂等更新的摘要评论。
'''

import json
from collections import Counter

from .git_provider import SUMMARY_COMMENT_MARKER
from .trace import redact_sensitive_values

def _severity_for_json(severity):
    """
    将内部 severity（面向规则引擎）映射为 LLM 约定的 severity（面向评估/对外输出）。

    内部三档 error/warning/info 用于规则引擎分类；
    LLM/评估器使用 high/medium/low 以便与业界 finding 严重度约定保持一致。
    该映射对 eval 至关重要：若输出 JSON 中 severity 不符合约定，
    评估器将无法正确统计 actual_categories。

    Args:
        severity (str): 内部严重度，取值为 "error" / "warning" / "info"。

    Returns:
        str: 映射后的严重度 "high" / "medium" / "low"；
             若入参不在已知集合中，则原样返回（保持向前兼容）。
    """
    # 已知内部 severity -> LLM severity 的固定映射表
    mapping={
        "error":"high",
        "warning":"medium",
        "info":"low",
    }

    # 未知 severity 不做转换，避免静默吞掉新类型导致评估失真
    return mapping.get(severity, severity)

def _escape_table_cell(value):
    """
    转义 Markdown 表格单元格中的特殊字符，避免破坏表格结构。

    Markdown 表格用 ``|`` 分隔列、用换行分隔行；若单元格内容本身含这些字符，
    会导致表格错位。本函数做两步转义：
      1. ``|`` -> ``\\|``，转义列分隔符；
      2. 换行符 ``\\n`` -> ``<br>``，用 HTML 换行标签保留多行展示。

    Args:
        value: 单元格原始值（任意类型，会被 str() 转字符串）。

    Returns:
        str: 转义后可安全放入 Markdown 表格的字符串。
    """
    text=str(value)
    # 先转义 | 防止被当作列分隔符，再把换行转成 <br> 保持单行表格
    return text.replace("|", "\\|").replace("\n", "<br>")

def _context_status(context):
    """
    根据上下文收集结果生成面向人类的状态描述文本。

    用于在报告表格中展示每个上下文文件的加载情况，区分三种状态：
      - 不存在（missing）：文件读取失败，附带 error 信息；
      - 被截断（truncated）：超出预算被截断，附带实际读取字符数；
      - 正常加载（loaded）：附带实际读取字符数，便于排查预算是否充足。

    Args:
        context: FileContext 对象，包含 exists/truncated/chars_read/error 字段。

    Returns:
        str: 形如 "loaded, 1234 chars" 的状态描述字符串。
    """
    if not context.exists:
        # 文件不存在或读取异常：展示错误原因供排查
        return f"missing: {context.error}"
    if context.truncated:
        # 触发截断：展示实际读取字符数，便于评估 context_budget 是否过小
        return f"truncated: {context.chars_read} chars"
    # 正常加载完成
    return f"loaded, {context.chars_read} chars"

def issue_to_finding(issue):
    """
    将一个 ReviewIssue 转换为面向 JSON 输出的 finding 字典。

    该结构是评估器（eval）解析 actual_categories 的数据来源，字段必须完整：
      - category 字段不可或缺，否则 eval 的 actual_categories 为空导致评估错误；
      - 所有文本字段都经过 redact_sensitive_values 脱敏，避免泄露密钥/Token；
      - severity 已通过 _severity_for_json 映射为 high/medium/low。

    Args:
        issue: ReviewIssue 对象，包含 severity/file_path/line_no/category/
               message/reason/suggestion/confidence/evidence/source/placement 字段。

    Returns:
        dict: 符合评估器约定的 finding 字典，包含 11 个字段。
    """
    return {
        # severity 需映射为 LLM 约定的 high/medium/low
        "severity":_severity_for_json(issue.severity),
        # category 必须保留，是 eval 统计 actual_categories 的依据
        "category":redact_sensitive_values(issue.category),
        # 文件路径可能含敏感信息（如私有仓路径），统一脱敏
        "file":redact_sensitive_values(issue.file_path),
        # 行号保持原始数值，便于定位
        "line":issue.line_no,
        # 以下文本字段均做脱敏，防止 issue 文本中夹带密钥
        "issue":redact_sensitive_values(issue.message),
        "reason":redact_sensitive_values(issue.reason),
        "suggested_fix":redact_sensitive_values(issue.suggestion),
        # confidence 可能为 None，由下游自行处理
        "confidence":issue.confidence,
        "evidence":redact_sensitive_values(issue.evidence),
        # source/placement 为枚举值，不涉及敏感信息
        "source":issue.source,
        "placement":issue.placement,
    }

def render_json_report(issues):
    """
    将 ReviewIssue 列表渲染为 JSON 字符串。

    输出结构为 ``{"findings": [...]}``，每个 finding 由 issue_to_finding 生成。
    使用 ensure_ascii=False 保留中文等非 ASCII 字符，indent=2 便于人工阅读。

    Args:
        issues (list[ReviewIssue]): 审查发现的问题列表。

    Returns:
        str: 格式化后的 JSON 字符串。
    """
    data={
        "findings":[
            issue_to_finding(issue)
            for issue in issues
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)

def render_markdown_report(issues, changed_files, contexts):
    '''
    渲染完整的 Markdown 审查报告。

    报告按固定顺序包含以下章节：
      1. Summary：变更文件数、问题数、按 severity 统计的计数；
      2. Changed Files：每个变更文件的增删行数与上下文采集状态；
      3. Context Files：采集到的上下文文件类型（changed/related）与状态；
      4. Findings：所有问题按 severity(error<warning<info) -> file -> line -> category
         排序后输出为表格；
      5. JSON Output：附上等价 JSON，便于机器解析与 eval。

    设计理由：Markdown 表格便于人类在 PR 中阅读，同时附 JSON 块保证
    评估器可从同一份输出中解析出 actual_categories，避免双份数据源不一致。

    Args:
        issues (list[ReviewIssue]): 审查发现的问题列表。
        changed_files (list[ChangedFile]): diff 解析出的变更文件列表。
        contexts (list[FileContext]): 采集到的上下文文件列表。

    Returns:
        str: 完整的 Markdown 报告文本。
    '''
    # 统计各 severity 出现次数，用于 Summary 章节
    severity_counts=Counter(issue.severity for issue in issues)
    # 以文件路径为键建立 contexts 索引，便于按变更文件查找对应上下文状态
    contexts_by_path={
        context.path:context
        for context in contexts
    }
    # 变更文件路径集合，用于在 Context Files 章节区分 changed / related
    changed_paths={changed_file.path for changed_file in changed_files}
    # 按章节顺序追加行，最后用 \n 拼接成完整报告
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

    # 逐行渲染变更文件表格，展示路径、增删行数及上下文采集状态
    for changed_file in changed_files:
        context=contexts_by_path.get(changed_file.path)
        # 若该变更文件未采集到上下文，标记为 "no collected"
        status=_context_status(context) if context else "no collected"

        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table_cell(redact_sensitive_values(changed_file.path)),
                    str(len(changed_file.added_lines)),
                    str(len(changed_file.deleted_lines)),
                    _escape_table_cell(redact_sensitive_values(status)),
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
        # 未采集到任何上下文时给出提示，避免渲染空表格
        lines.append("No context files collected.")
    else:
        lines.extend([
            "| File | Type | Status |",
            "| --- | --- | --- |",
        ])

        for context in contexts:
            # 通过路径是否在 changed_paths 中判断类型：变更文件 or 关联文件
            context_type="changed" if context.path in changed_paths else "related"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_table_cell(redact_sensitive_values(context.path)),
                        context_type,
                        _escape_table_cell(
                            redact_sensitive_values(_context_status(context))
                        ),
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
        # 无问题时给出提示，便于人工确认审查已执行
        lines.append("No issues found.")
    else:
        lines.extend([
        "| Severity | File | Line | Category | Issue | Reason | Suggestion | Confidence | Evidence | Source |",
        "| --- | --- | ---: | --- | --- | --- | --- | ---: | --- | --- |",
        ])

        # severity 排序权重表：error 排最前，其次 warning，最后 info
        severity_order={"error":0, "warning":1, "info":2}

        # 多级排序：先按 severity 权重，再按 file_path、line_no、category 稳定排序，
        # 确保同等问题集中展示，便于人工逐文件审阅
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
                        _escape_table_cell(redact_sensitive_values(issue.file_path)),
                        str(issue.line_no),
                        _escape_table_cell(redact_sensitive_values(issue.category)),
                        _escape_table_cell(redact_sensitive_values(issue.message)),
                        _escape_table_cell(redact_sensitive_values(issue.reason)),
                        _escape_table_cell(redact_sensitive_values(issue.suggestion)),
                        # confidence 为 None 时展示空串，避免渲染 "None"
                        _escape_table_cell("" if issue.confidence is None else issue.confidence),
                        _escape_table_cell(redact_sensitive_values(issue.evidence)),
                        _escape_table_cell(issue.source),
                    ]
                )
                + " |"
            )
    # 末尾附上等价 JSON 输出，供评估器解析 actual_categories
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


def render_summary_comment(issues, changed_files):
    """渲染精简版 PR 摘要评论，可安全地重复发布与更新。

    与完整 Markdown 报告相比，摘要评论的特点：
      - 顶部带 SUMMARY_COMMENT_MARKER 标记，git_provider 据此定位已有评论，
        实现幂等更新（重复运行不会刷屏，而是更新同一条评论）；
      - 只展示精简字段（Severity/File/Line/Placement/Category/Issue），
        避免在 PR 评论中堆砌完整证据，保持评论简洁；
      - 行号列对 inline placement 显示真实行号，对 summary-only 显示提示文本。

    设计理由：PR 评论是面向开发者的轻量反馈通道，需控制信息密度；
    完整报告通过 state.output 输出到产物文件，供深入排查。

    Args:
        issues (list[ReviewIssue]): 审查发现的问题列表。
        changed_files (list[ChangedFile]): 变更文件列表，仅用于统计数量。

    Returns:
        str: 带 SUMMARY_COMMENT_MARKER 前缀的 Markdown 摘要评论文本。
    """
    # 统计各 severity 数量，用于评论顶部的概览
    severity_counts = Counter(issue.severity for issue in issues)
    # severity 排序权重，与完整报告保持一致
    severity_order = {"error": 0, "warning": 1, "info": 2}
    lines = [
        # 幂等更新标记：git_provider 通过该字符串定位并更新已有评论
        SUMMARY_COMMENT_MARKER,
        "## RepoReview summary",
        "",
        "- Changed files: {}".format(len(changed_files)),
        "- Findings: {}".format(len(issues)),
        "- Errors: {}".format(severity_counts.get("error", 0)),
        "- Warnings: {}".format(severity_counts.get("warning", 0)),
        "- Info: {}".format(severity_counts.get("info", 0)),
        "",
        "### Findings",
        "",
    ]

    if not issues:
        # 无问题时直接返回，保持评论简洁
        lines.append("No findings.")
        return "\n".join(lines)

    lines.extend([
        "| Severity | File | Line | Placement | Category | Issue |",
        "| --- | --- | ---: | --- | --- | --- |",
    ])
    # 与完整报告相同的多级排序，保证摘要与详情顺序一致
    for issue in sorted(
        issues,
        key=lambda issue: (
            severity_order.get(issue.severity, 99),
            issue.file_path,
            issue.line_no,
            issue.category,
        ),
    ):
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table_cell(issue.severity),
                    _escape_table_cell(redact_sensitive_values(issue.file_path)),
                    # inline 问题显示真实行号；非 inline 显示 summary only 提示
                    str(issue.line_no) if issue.placement == "inline" else "summary only",
                    _escape_table_cell(issue.placement),
                    _escape_table_cell(redact_sensitive_values(issue.category)),
                    _escape_table_cell(redact_sensitive_values(issue.message)),
                ]
            )
            + " |"
        )
    return "\n".join(lines)
