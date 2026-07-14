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

模块职责：
    本模块实现 RepoReview Agent 中“规则审查器”部分，专门负责不依赖 LLM 的静态规则检查。
    它直接消费 diff 解析阶段产出的 changed_files（包含 added_lines / deleted_lines / patch / hunks），
    按行扫描并匹配一组预定义的正则/关键字规则，输出 ReviewIssue 列表。

在整体架构中的位置：
    位于 diff 解析（diff_parser）之后、LLM 审查（llm_reviewer）之前/并行：
      ┌────────────┐   changed_files   ┌──────────────┐   rule_issues   ┌──────────────┐
      │ diff_parser│ ───────────────▶ │  reviewers   │ ──────────────▶ │   reporter   │
      └────────────┘                   │ (本模块)     │                 │ (汇总展示)   │
                                       └──────┬───────┘                 └──────────────┘
                                              │ rule_issues
                                              ▼
                                       ┌──────────────┐
                                       │ llm_reviewer │  规则结果作为 prompt 上下文喂给 LLM
                                       └──────────────┘
    规则结果有两个用途：
      1. 直接作为最终审查意见输出（source="rule"）；
      2. 作为 LLM 审查的输入上下文（rule_findings），帮助 LLM 聚焦于规则未能覆盖的深层问题。

设计理由：
    - 选择“正则 + 关键字”而非 AST：diff 是按行给到的文本片段，AST 需要完整文件且跨语言成本高；
      正则方案足够覆盖常见坏味道（debug 输出、硬编码 secret、SQL 拼接等），且实现简单、可解释。
    - 工厂函数 _issue / _repo_issue 统一构造 ReviewIssue，确保 source="rule" 标记一致，
      便于后续在 reporter / validation 阶段按来源分流处理。
    - test_gap 检测采用“业务文件 vs 测试文件变更比例”的启发式，不追求精确，仅作为提示。
'''
import re
from pathlib import Path

from .schemas import ReviewIssue

# 支持的业务代码后缀集合；用来判定一个文件是否属于“业务代码文件”，
# 进而参与 test_gap 启发式判断。扩展名以小写形式比较，保持大小写无关。
CODE_EXTENTIONS={
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".go", ".rs", ".rb", ".php", ".cs",
}

def _issue(line, severity, category, message, suggestion):
    """行级问题的工厂函数，把单行变更信息封装为 ReviewIssue。

    设计上固定 source="rule"，使所有规则审查产出的意见在后续阶段可被一致识别，
    便于与 LLM 审查（source="llm"）的结果做来源分流。

    Args:
        line: ChangedLine 对象，提供 file_path 和 line_no，用于把意见锚定到具体行。
        severity: 严重等级，取值 error / warning / info。
        category: 规则类别，例如 debug / todo / secret / sensitive_log 等。
        message: 给开发者看的问题描述。
        suggestion: 给出的修复建议。

    Returns:
        ReviewIssue: 已锚定行号、source="rule" 的审查意见。
    """
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
    """仓库级问题的工厂函数，生成不锚定到具体行号的 ReviewIssue。

    用于无法定位到某一行的问题（如 test_gap、test_only_change）。
    约定 file_path="(repository)"、line_no=0，表示这是仓库整体的提示。
    validation.validate_issue_locations 会据此把这类意见分配为 summary 发布目标。

    Args:
        severity: 严重等级，取值 error / warning / info。
        category: 规则类别，例如 test_gap / test_only_change。
        message: 给开发者看的问题描述。
        suggestion: 给出的修复建议。

    Returns:
        ReviewIssue: file_path="(repository)"、line_no=0、source="rule" 的审查意见。
    """
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
    """把路径归一化：反斜杠转正斜杠，并转小写。

    便于在 Windows / Linux 之间做跨平台的路径匹配，同时避免大小写差异漏判。
    """
    # 用/把\\替换，同时全部转换成小写
    return path.replace("\\", "/").lower()

def _is_test_file(path):
    """启发式判断给定路径是否是测试文件。

    通过文件路径前缀、目录名（test/、/tests/）以及文件名后缀
    （test_*.py、*_test.js、*.test.js、*.spec.js 等）综合判断。
    覆盖 Python 与常见前端测试命名约定，足以应对大多数项目结构。

    Args:
        path: 待判定的文件路径。

    Returns:
        bool: True 表示该文件是测试文件。
    """
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
    """判断给定路径是否是业务代码文件。

    业务代码文件 = 后缀在 CODE_EXTENTIONS 中 且 不是测试文件。
    用来配合 test_gap 启发式：业务文件有变更而测试文件无变更时给出提示。

    Args:
        path: 待判定的文件路径。

    Returns:
        bool: True 表示该文件是业务代码文件。
    """
    if _is_test_file(path):
        return False

    suffix=Path(path).suffix.lower()
    return suffix in CODE_EXTENTIONS

def _contains_exception_handling(changed_file):
    """粗略判断本次 patch 是否包含异常处理结构。

    通过关键字扫描整个 patch 文本，覆盖 try / except / except: / raise。
    仅用于“是否需要警告缺少异常处理”的辅助判断，不追求精确语法分析。

    Args:
        changed_file: 变更文件对象，需包含 .patch 字符串。

    Returns:
        bool: True 表示 patch 中出现过异常处理相关关键字。
    """
    patch=changed_file.patch.lower()

    return (
        "try:" in patch
        or "except" in patch
        or "except:" in patch
        or "raise" in patch
    )

def _looks_like_hardcoded_secret(content):
    """用正则识别疑似硬编码的 secret / token / password / api_key。

    匹配形如 `api_key = "xxx"` / `password = "xxx"` 的赋值语句，要求右侧是字符串字面量。
    采用 IGNORECASE，避免 API_Key / APIKEY 等大小写变体漏判。

    Args:
        content: 已转小写的单行代码内容。

    Returns:
        bool: True 表示命中疑似硬编码 secret 的模式。
    """
    return re.search(
        r'(api[_-]?key|access[_-]?token|secret|password|token)\s*=\s*[\'"][^\'"]+[\'"]',
        content,
        re.IGNORECASE,
    ) is not None

def _looks_like_sensitive_debug_output(content):
    """识别“打印敏感字段”的调试输出。

    判定条件：行内同时出现 print( 与 password/secret/token/api_key 之一，
    典型场景如 `print(user.password)`，避免敏感信息进入日志。

    Args:
        content: 单行代码内容（函数内部已转小写）。

    Returns:
        bool: True 表示疑似打印了敏感字段。
    """
    lowered = content.lower()

    return (
        "print(" in lowered
        and any(word in lowered for word in ["password", "secret", "token", "api_key"])
    )

def _looks_like_risky_call(content):
    """识别可能失败的高风险调用（I/O / 网络 / 解析 / 系统）。

    这些调用在生产环境可能抛出异常，若调用方无异常处理，会导致进程崩溃或错误静默。
    配合 has_exception_handling 共同判定是否需要给出“缺少异常处理”的提示。

    Args:
        content: 单行代码内容（函数内部已转小写）。

    Returns:
        bool: True 表示命中任意一个高风险调用模式。
    """
    lowered=content.lower()

    # 风险调用清单：文件 / HTTP / 解析 / 子进程 / 数据库 / 系统删除等
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
    """对所有变更文件执行规则审查，返回 ReviewIssue 列表。

    审查流程分两阶段：
      1. 仓库级启发式：基于“业务文件 vs 测试文件变更比例”给出 test_gap / test_only_change 提示；
      2. 按文件按行扫描 added_lines / deleted_lines，逐条匹配规则（debug、TODO、secret、敏感日志、
         缺少异常处理、删除异常处理等）。

    Args:
        changed_files: 变更文件列表，每个元素需提供 path、patch、added_lines、deleted_lines。

    Returns:
        list[ReviewIssue]: 本次审查发现的所有规则意见，source 均为 "rule"。
    """
    issues = []

    # 第 1 阶段：仓库级启发式——统计本次提交涉及的测试文件与业务文件
    changed_tests = [
        changed_file for changed_file in changed_files
        if _is_test_file(changed_file.path)
    ]

    changed_business_files=[
        changed_file for changed_file in changed_files
        if _is_business_code_file(changed_file.path)
    ]

    # 业务代码有改动但没有测试变更 → 提醒补测试（warning 级别）
    if changed_business_files and not changed_tests:
        issues.append(
            _repo_issue(
                severity="warning",
                category="test_gap",
                message="本次修改包含业务代码，但是没有看到测试文件变更",
                suggestion="如果行为发生变化，建议补充或更新对应测试",
            )
        )

    # 仅测试变更但无业务代码 → 提醒确认是否遗漏业务代码（info 级别）
    if changed_tests and not changed_business_files:
        issues.append(
            _repo_issue(
                severity="info",
                category="test_only_change",
                message="本次修改只包含测试文件，没有看到业务代码变更",
                suggestion="确认这是单纯补测试，还是遗漏了对应业务代码修改",
            )
        )

    # 第 2 阶段：逐文件、逐行扫描，匹配具体规则
    for changed_file in changed_files:
        # 缓存当前文件 patch 是否含异常处理结构，避免每行重复扫描 patch
        has_exception_handling=_contains_exception_handling(changed_file)

        # ---- 扫描新增行 ----
        for line in changed_file.added_lines:
            content = line.content.lower()

            # 规则1：print 调试输出（warning）
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

            # 规则2：debugger 断点（warning）
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

            # 规则3：TODO / FIXME 标记（info，提醒后续跟踪）
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

            # 规则4：疑似硬编码 secret / token / api_key（error，最高优先级安全风险）
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

            # 规则5：打印敏感字段（error，防日志泄露）
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

            # 规则6：高风险调用且整文件无异常处理（warning）
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

            # 规则7：行内出现 password/secret/token 关键字（error，与规则4互补，覆盖更多形态）
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
        # ---- 扫描删除行 ----
        for line in changed_file.deleted_lines:
            lowered = line.content.lower()

            # 规则8：删除了异常处理相关代码（warning，防止错误被静默吞掉）
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
