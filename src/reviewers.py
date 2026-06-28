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

def review_changed_files(changed_files):
    issues = []

    for changed_file in changed_files:
        for line in changed_file.added_lines:
            content = line.content.lower()

            if "print(" in content:
                issues.append()

            if "debugger" in content:
                issues.append()

            if "todo" in content or "fixme" in content:
                issues.append()
            
            if "password" in content or "secret" in content or "token" in content:
                issues.append()

    return issues