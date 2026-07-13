'''
用来负责产生审查意见，这是最核心的部分

简单审查规则：
1. 发现 print/debugger
2. 发现 TODO/FIXME
3. 发现 broad except
4. 发现疑似硬编码 secret/password/token
5. 发现删除测试文件
6. 发现 SQL 拼接风险
'''

'''
day4:先不使用LLM，基于diff来做一些基础审查
'''
import re
from pathlib import Path

from .schemas import ReviewIssue

CODE_EXTENTIONS={
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".go", ".rs", ".rb", ".php", ".cs",
}

def _issue(line, severity, category, message, suggestion):
    return ReviewIssue(
        file_path=line.file_path,
        line_no=line.line_no,
        severity=severity,
        category=category,
        message=message,
        suggestion=suggestion,
        source="rule",
    )

def _repo_issue(severity, category, message, suggestion):
    return ReviewIssue(
        file_path="(repository)",
        line_no=0,
        severity=severity,
        category=category,
        message=message,
        suggestion=suggestion,
        source="rule",
    )

def _normalized_path(path):
    # 用/把\\替换，同时全部转换成小写
    return path.replace("\\", "/").lower()

def _is_test_file(path):
    # 判断是否是测试文件
    normalized=_normalized_path(path)
    name=Path(normalized).name

    return (
        normalized.startswith("test/")
        or "/tests/" in normalized
        or name.startswith("test_")
        or name.endswith("_test.js")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
    )

def _is_business_code_file(path):
    if _is_test_file(path):
        return False
    
    suffix=Path(path).suffix.lower()
    return suffix in CODE_EXTENTIONS

def _contains_exception_handling(changed_file):
    patch=changed_file.patch.lower()

    return (
        "try:" in patch
        or "except" in patch
        or "except:" in patch
        or "raise" in patch
    )

def _looks_like_hardcoded_secret(content):
    return re.search(
        r'(api[_-]?key|access[_-]?token|secret|password|token)\s*=\s*[\'"][^\'"]+[\'"]',
        content,
        re.IGNORECASE,
    ) is not None

def _looks_like_sensitive_debug_output(content):
    lowered = content.lower()

    return (
        "print(" in lowered
        and any(word in lowered for word in ["password", "secret", "token", "api_key"])
    )

def _looks_like_risky_call(content):
    lowered=content.lower()

    risky_patterns=[
        "open(",
        "request.",
        "httpx.",
        "urllib.",
        "json.loads(",
        "yaml.safe_load(",
        "subprocess.",
        ".execute(",
        "os.remove(",
        "os.unlink(",
    ]

    return any(pattern in lowered for pattern in risky_patterns)

def review_changed_files(changed_files):
    issues = []

    changed_tests = [
        changed_file for changed_file in changed_files
        if _is_test_file(changed_file.path)
    ]

    changed_business_files=[
        changed_file for changed_file in changed_files
        if _is_business_code_file(changed_file.path)
    ]

    if changed_business_files and not changed_tests:
        issues.append(
            _repo_issue(
                severity="warning",
                category="test_gap",
                message="本次修改包含业务代码，但是没有看到测试文件变更",
                suggestion="如果行为发生变化，建议补充或更新对应测试",
            )
        )
    
    if changed_tests and not changed_business_files:
        issues.append(
            _repo_issue(
                severity="info",
                category="test_only_change",
                message="本次修改只包含测试文件，没有看到业务代码变更",
                suggestion="确认这是单纯补测试，还是遗漏了对应业务代码修改",
            )
        )

    for changed_file in changed_files:
        has_exception_handling=_contains_exception_handling(changed_file)

        for line in changed_file.added_lines:
            content = line.content.lower()

            if "print(" in content:
                issues.append(
                    ReviewIssue(
                        file_path=line.file_path,
                        line_no=line.line_no,
                        severity="warning",
                        category="debug",
                        message="新增代码当中包含print调试输出",
                        suggestion="删除print，或者改成正式的日志记录",
                        source="rule",
                    )
                )

            if "debugger" in content:
                issues.append(
                    ReviewIssue(
                        file_path=line.file_path,
                        line_no=line.line_no,
                        severity="warning",
                        category="debug",
                        message="新增代码当中包含debugger调试输出",
                        suggestion="删除debugger，或者改成正式的日志记录",
                        source="rule",
                    )
                )

            if "todo" in content or "fixme" in content:
                issues.append(
                    _issue(
                        line,
                        severity="info",
                        category="todo",
                        message="新增代码当中包含TODO/FIXME",
                        suggestion="现在解决它，或者将其链接到一个跟踪的后续任务",
                    )
                )
            
            if _looks_like_hardcoded_secret(content):
                issues.append(
                    _issue(
                        line,
                        severity="error",
                        category="secret",
                        message="新增代码疑似包含硬编码 key/token/password/secret",
                        suggestion="不要硬编码敏感信息，改成从环境变量或安全配置读取。",
                    )
                )

            if _looks_like_sensitive_debug_output(content):
                issues.append(
                    _issue(
                        line,
                        severity="error",
                        category="sensitive_log",
                        message="新增调试输出可能泄露密码、token 或 secret",
                        suggestion="不要打印敏感字段，必要时做脱敏处理"
                    )
                )
            
            if _looks_like_risky_call(content) and not has_exception_handling:
                issues.append(
                    _issue(
                        line,
                        severity="warning",
                        category="exception_handling",
                        message="新增代码包含可能失败的 I/O、网络、解析或系统调用，但 patch 中没有明显异常处理",
                        suggestion="考虑添加 try/except，或让错误以清晰方式向上传递。",
                    )
                )

            if "password" in content or "secret" in content or "token" in content:
                issues.append(
                    _issue(
                        line,
                        severity="error",
                        category="secret",
                        message="新增代码当中包含疑似硬编码的密码/密钥/令牌",
                        suggestion="请不要在代码中硬编码密码/密钥/令牌，改成从配置文件或者环境变量读取",
                    )
                )
        for line in changed_file.deleted_lines:
            lowered = line.content.lower()

            if (
                "try:" in lowered
                or "except" in lowered
                or "except" in lowered
                or "raise" in lowered
            ):
                issues.append(
                    _issue(
                        line,
                        severity="warning",
                        category="exception_handling",
                        message="本次修改删除了异常处理相关代码",
                        suggestion="确认删除异常处理不会让错误静默失败或直接崩溃"
                    )
                )

    return issues
