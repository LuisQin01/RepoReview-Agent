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

from .schemas import ReviewIssue


def _issue(line, severity, category, message, suggestion):
    return ReviewIssue(
        file_path=line.file_path,
        line_no=line.line_no,
        severity=severity,
        category=category,
        message=message,
        suggestion=suggestion,
    )


def review_changed_files(changed_files):
    issues = []

    for changed_file in changed_files:
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

    return issues
