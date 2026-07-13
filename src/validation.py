import json
import math
from dataclasses import dataclass, field

VALID_SEVERITIES = {"high", "medium", "low"}


@dataclass
class ValidationResult:
    findings: list[dict]=field(default_factory=list)
    valid: bool=True
    repaired: bool=False
    errors: list[str]=field(default_factory=list)


def validate_issue_locations(issues, changed_files):
    """Keep only findings that can be published against changed hunks.

    Repository-level rule findings are intentionally retained because they are
    not inline findings and are produced by trusted deterministic rules.
    """
    hunks_by_path = {}
    for changed_file in changed_files:
        hunks_by_path.setdefault(changed_file.path, []).extend(changed_file.hunks)

    validated_issues = []
    for issue in issues:
        if (
            getattr(issue, "source", None) == "rule"
            and getattr(issue, "file_path", None) == "(repository)"
            and getattr(issue, "line_no", None) == 0
        ):
            validated_issues.append(issue)
            continue

        file_path = getattr(issue, "file_path", None)
        line_no = getattr(issue, "line_no", None)
        if (
            not isinstance(file_path, str)
            or isinstance(line_no, bool)
            or not isinstance(line_no, int)
        ):
            continue

        if any(
            hunk.start_line <= line_no <= hunk.end_line
            for hunk in hunks_by_path.get(file_path, [])
        ):
            validated_issues.append(issue)

    return validated_issues

def validate_llm_response(response_text: str) -> ValidationResult:
    result = ValidationResult()

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        result.valid = False
        result.errors.append("llm_json_parse_error")
        return result

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
    
    # 验证每个发现
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            result.repaired = True
            result.errors.append(f"llm_finding_{index}_not_dict")
            continue

        normalized = dict(finding)

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
        try:
            normalized["confidence"]=float(normalized.get("confidence", 0.5))
        except (TypeError, ValueError):
            normalized["confidence"]=0.5
            result.repaired=True
            result.errors.append(f"llm_finding_{index}_invalid_confidence")

        if not math.isfinite(normalized["confidence"]):
            normalized["confidence"]=0.5
            result.repaired=True
            result.errors.append(f"llm_finding_{index}_non_finite_confidence")
        
        if normalized["confidence"]<0 or normalized["confidence"]>1:
            normalized["confidence"]=max(0.0,min(1.0, normalized["confidence"]))
            result.repaired=True
            result.errors.append(f"llm_finding_{index}_confidence_out_of_range")
    
        # 确保每个发现都有 issue、reason 和 suggested_fix 字段，如果缺少则修复为默认值
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
