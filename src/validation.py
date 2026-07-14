"""LLM 响应校验与修复模块。

模块职责：
    本模块是 RepoReview Agent 的“安全防线”之一，负责把 LLM 返回的原始文本解析为可信任的结构，
    并对行号/位置进行二次校验。所有来自 LLM 的数据都被视为不可信输入，必须经过本模块处理后才
    能进入后续的展示与发布流程。

在整体架构中的位置：
    位于 llm_client（取模型响应）与 llm_reviewer / reporter（构造 ReviewIssue 与展示）之间：
        LLM 原始响应 ──▶ validate_llm_response ──▶ 规范化 findings ──▶ parse_llm_response
        ReviewIssue ──▶ validate_issue_locations ──▶ inline / summary 分流 ──▶ reporter

核心能力：
    1. validate_llm_response：解析 JSON、做容错修复（丢弃 LLM 自带 source、修正 severity、
       补默认字段、限制 confidence 范围），返回 ValidationResult。
    2. validate_issue_locations：行号安全防线，决定每条意见是 inline（命中变更 hunk）
       还是 summary（仓库级或无安全行号），防止 LLM 编造的行号被发到代码评审平台。

设计理由：
    - LLM 输出不可信：可能输出非法 JSON、缺失字段、越界行号、伪造 source。每一步都做防御。
    - “修复而非丢弃”策略：只要能修复就修复并记录到 errors，最大化保留 LLM 的有效发现，
      同时通过 repaired=True 让上游知道发生过修复。
    - source 字段一律丢弃再由系统重新赋值：防止 LLM 伪装成 rule 来源绕过来源分流逻辑。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

# LLM 允许输出的严重等级白名单；超出此集合的值会被修复为 medium
VALID_SEVERITIES = {"high", "medium", "low"}


@dataclass
class ValidationResult:
    """LLM 响应校验结果。

    Attributes:
        findings: 经过规范化、修复后的 finding 字典列表，可直接用于构造 ReviewIssue。
        valid: 是否整体可用。False 表示无法解析（如 JSON 错误、结构错误），findings 为空。
        repaired: 是否发生过任何修复动作（哪怕只有一处）。True 提示上游关注数据可信度。
        errors: 修复/失败原因清单，每条形如 llm_finding_<index>_<reason>，便于排查。
    """
    findings: list[dict]=field(default_factory=list)
    valid: bool=True
    repaired: bool=False
    errors: list[str]=field(default_factory=list)


def validate_issue_locations(issues, changed_files):
    """把每条审查意见分配为 inline 或 summary 发布目标（行号安全防线核心）。

    发布分流规则：
      - 仓库级规则意见（file_path="(repository)", line_no=0, source="rule"）→ summary；
      - file_path 不是字符串、line_no 不是 int（含 bool，因为 bool 是 int 子类需单独排除）→ summary，
        并把不安全的值修正为安全占位（"(unlocatable)" / 0），防止被误当成 inline 锚点；
      - file_path 与 line_no 都合法，且 line_no 落在某个变更 hunk 区间内 → inline；
      - 其余（合法但未命中 hunk，例如 LLM 编造的行号）→ summary。

    Args:
        issues: 待分配的 ReviewIssue 列表（会被原地修改 placement 属性）。
        changed_files: 变更文件列表，用于构建 file_path → hunks 索引以加速命中判断。

    Returns:
        list: 同一批 issues，每条已设置 placement ∈ {"inline", "summary"}。
    """
    # 第 1 步：构建 file_path → hunks 索引，避免对每条意见都遍历全部变更文件（O(n*m) → O(n)）
    hunks_by_path = {}
    for changed_file in changed_files:
        hunks_by_path.setdefault(changed_file.path, []).extend(changed_file.hunks)

    validated_issues = []
    for issue in issues:
        # 分支 A：仓库级规则意见，直接走 summary，无需做行号命中判断
        if (
            getattr(issue, "source", None) == "rule"
            and getattr(issue, "file_path", None) == "(repository)"
            and getattr(issue, "line_no", None) == 0
        ):
            issue.placement = "summary"
            validated_issues.append(issue)
            continue

        # 分支 B：file_path / line_no 类型不安全 —— 一律降级为 summary，并修正为安全占位值
        file_path = getattr(issue, "file_path", None)
        line_no = getattr(issue, "line_no", None)
        if (
            not isinstance(file_path, str)
            or isinstance(line_no, bool)
            or not isinstance(line_no, int)
        ):
            # Summary rendering must not receive an untrusted path or line
            # value that could be mistaken for an inline location.
            issue.placement = "summary"
            if not isinstance(file_path, str):
                issue.file_path = "(unlocatable)"
            if isinstance(line_no, bool) or not isinstance(line_no, int):
                issue.line_no = 0
            validated_issues.append(issue)
            continue

        # 分支 C：类型合法，进一步判断行号是否落在某个变更 hunk 区间内
        # 命中 → inline（可信锚点）；未命中（典型如 LLM 编造的行号）→ summary
        if any(
            hunk.start_line <= line_no <= hunk.end_line
            for hunk in hunks_by_path.get(file_path, [])
        ):
            issue.placement = "inline"
            validated_issues.append(issue)
        else:
            issue.placement = "summary"
            validated_issues.append(issue)

    return validated_issues

def validate_llm_response(response_text: str) -> ValidationResult:
    """解析并校验 LLM 返回的原始文本，产出可信的 findings 列表。

    校验流程（每一步失败都尽量修复并记录，而非直接丢弃）：
      1. JSON 解析：失败则整体 valid=False，直接返回；
      2. 顶层结构：必须是 dict 且含 findings 字段且为 list，否则整体失败；
      3. 逐条 finding 修复：
         - 丢弃 LLM 自带的 source（安全设计，防止伪装来源）；
         - severity 不在白名单 → 修正为 medium；
         - file / line / confidence 缺失或类型错误 → 补默认值；
         - confidence 必须是有限值且在 [0,1] 区间，否则夹紧；
         - issue / reason / suggested_fix / evidence 缺失或非字符串 → 置空串。

    Args:
        response_text: LLM 返回的原始字符串，预期为 JSON。

    Returns:
        ValidationResult: 含 findings(规范化后)、valid、repaired、errors。
    """
    result = ValidationResult()

    # 第 1 步：JSON 解析。LLM 偶发返回非 JSON（如带 markdown 包裹）时整体失败。
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        result.valid = False
        result.errors.append("llm_json_parse_error")
        return result

    # 第 2 步：顶层结构校验。必须是 dict、含 findings、findings 是 list，任一不满足即整体失败。
    if not isinstance(data, dict):
        result.valid = False
        result.errors.append("llm_response_not_dict")
        return result

    if "findings" not in data:
        result.valid = False
        result.errors.append("llm_missing_findings")
        return result

    findings = data["findings"]

    if not isinstance(findings, list):
        result.valid = False
        result.errors.append("llm_findings_not_list")
        return result

    # 第 3 步：逐条修复 finding。索引 index 会写入 error 标签，便于定位是第几条出错。
    # 验证每个发现
    for index, finding in enumerate(findings):
        # 3.1 非 dict 的 finding 无法修复结构，直接跳过并记录
        if not isinstance(finding, dict):
            result.repaired = True
            result.errors.append(f"llm_finding_{index}_not_dict")
            continue

        # 3.2 浅拷贝，避免修改 LLM 原始数据；后续所有修复都在 normalized 上进行
        normalized = dict(finding)

        # 3.3 安全设计：丢弃 LLM 自带的 source 字段。source 由系统按调用路径
        # （rule / llm）重新赋值，防止 LLM 伪装成 rule 来源绕过来源分流逻辑。
        if "source" in normalized:
            normalized.pop("source")
            result.repaired = True
            result.errors.append(f"llm_finding_{index}_ignored_source")

        # 拿到的严重性不在有效范围内，修复为 medium
        if normalized.get("severity") not in VALID_SEVERITIES:
            normalized["severity"] = "medium"
            result.repaired = True
            result.errors.append(f"llm_finding_{index}_invalid_severity")

        # 如果缺少 file 字段，修复为默认值
        if not normalized.get("file"):
            normalized["file"] = "(unknown)"
            result.repaired = True
            result.errors.append(f"llm_finding_{index}_missing_file")

        # 如果缺少 line 字段，修复为默认值 0
        if "line" not in normalized:
            normalized["line"] = 0
            result.repaired = True
            result.errors.append(f"llm_finding_{index}_missing_line")

        # line 强转 int；LLM 可能给字符串、None 或浮点，转失败则归零。
        # `or 0` 处理 None / 空串等 falsy 值，避免 int(None) 抛错。
        try:
            normalized["line"]=int(normalized.get("line") or 0)
        except (TypeError, ValueError):
            normalized["line"]=0
            result.repaired=True
            result.errors.append(f"llm_finding_{index}_invalid_line")

        # 如果缺少 confidence 字段，修复为默认值
        if "confidence" not in normalized:
            normalized["confidence"] = 0.5
            result.repaired = True
            result.errors.append(f"llm_finding_{index}_missing_confidence")

        # 增加类型转换和范围限制
        # confidence 强转 float；LLM 可能给字符串，转失败则用默认 0.5
        try:
            normalized["confidence"]=float(normalized.get("confidence", 0.5))
        except (TypeError, ValueError):
            normalized["confidence"]=0.5
            result.repaired=True
            result.errors.append(f"llm_finding_{index}_invalid_confidence")

        # 过滤 NaN / Inf：math.isfinite 排除这些非数值，防止后续比较与展示异常
        if not math.isfinite(normalized["confidence"]):
            normalized["confidence"]=0.5
            result.repaired=True
            result.errors.append(f"llm_finding_{index}_non_finite_confidence")

        # 区间夹紧：confidence 必须落在 [0,1]，超出则截断到边界
        if normalized["confidence"]<0 or normalized["confidence"]>1:
            normalized["confidence"]=max(0.0,min(1.0, normalized["confidence"]))
            result.repaired=True
            result.errors.append(f"llm_finding_{index}_confidence_out_of_range")

        # 确保每个发现都有 issue、reason 和 suggested_fix 字段，如果缺少则修复为默认值
        # 文本类字段统一要求为字符串：缺失则补空串，类型错误则强制置空串
        for field_name in ["issue", "reason", "suggested_fix", "evidence"]:
            if field_name not in normalized:
                normalized[field_name] = ""
                result.repaired = True
                result.errors.append(f"llm_finding_{index}_missing_{field_name}")
            elif not isinstance(normalized[field_name], str):
                normalized[field_name] = ""
                result.repaired = True
                result.errors.append(f"llm_finding_{index}_invalid_{field_name}_type")
        # 重点：修复后的 finding 要放进结果里
        result.findings.append(normalized)

    return result
