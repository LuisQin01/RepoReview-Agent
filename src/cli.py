'''
作为命令行的入口，用来负责把用户的输入接入进来
功能：
1. 读取参数
2. 调用 diff_parser
3. 调用 file_context
4. 调用reviewers
5. 打印结果
'''
import argparse
import json
from pathlib import Path
from dataclasses import asdict

from .reporter import render_json_report, render_markdown_report

from .reviewers import review_changed_files
from .diff_parser import parse_diff
from .file_context import collect_file_contexts


def parse_args():
    # 读取命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, help="Path to git diff file")
    parser.add_argument("--repo", default=".", help="Path to the repository root")
    parser.add_argument(
        "--max-context-chars",
        type=int,
        default=4000,
        help="Maximum number of characters to read from each file for context",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="markdown",
    )

    parser.add_argument(
        "--output",
        help="Path to save file. If not specified, print to stdout.",
    )

    parser.add_argument(
        "--llm",
        action="store_true",
        help="Enable LLM reviewer",
    )

    # 检查参数合法性
    args = parser.parse_args()
    
    # 检查 max_context_chars 是否大于0
    if args.max_context_chars <= 0:
        parser.error("--max-context-chars must be greater than 0")

    return args

def read_diff(diff_path):
    return Path(diff_path).read_text(encoding="utf-8")

def write_output(output, output_path):
    if output_path:
        path=Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    else:
        print(output)

def print_result(issues):
    data = [asdict(issue) for issue in issues]
    print(json.dumps(data, ensure_ascii=False, indent=2))

def print_changed_files(changed_files):
    data = {
        "files":[
            {
                "path":changed_file.path,
                "added_lines":len(changed_file.added_lines),
                "deleted_lines":len(changed_file.deleted_lines),
                "patch":changed_file.patch,
            }
            for changed_file in changed_files
        ]
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))

def print_review_input(changed_files, contexts):
    data = {
        "files":[
            {
                "path":changed_file.path,
                "added_lines":len(changed_file.added_lines),
                "deleted_lines":len(changed_file.deleted_lines),
                "patch":changed_file.patch,
            }
            for changed_file in changed_files
        ],
        "contexts":[
            asdict(context)
            for context in contexts
        ]
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))

def record_step(trace_steps, step, detail=None):
    trace_steps.append({
        "step":step,
        "detail":detail or {},
    })

def validate_issues(issues):
    if not isinstance(issues, list):
        raise ValueError("Issues should be a list")
    return issues

def mock_call_model(prompt):
    return """
{
    "findings": [
            {
                "severity": "high",
                "file": "app.py",
                "line": 10,
                "issue": "这里缺少异常处理",
                "reason": "新增代码可能执行失败，但没有看到错误处理逻辑",
                "suggested_fix": "为可能失败的调用添加 try/except 或向上抛出明确异常",
                "confidence": 0.76
            }
        ]
}
"""

def run_review_agent(args):
    trace_steps = []

    # 首先记录接收到的任务参数
    record_step(trace_steps, "receive_task",{
        "diff":args.diff,
        "repo":args.repo,
        "format":args.format,
        "llm":args.llm,
    })

    # 读取 diff 文件
    diff_text = read_diff(args.diff)

    # 解析 diff，得到结构化的 changed_files
    changed_files = parse_diff(diff_text)
    # 记录解析 diff 的结果
    record_step(trace_steps, "parse_diff",{
        "changed_files":len(changed_files),
    })

    # 收集文件上下文，diff只告诉你修改了哪些行，但没有告诉你这些行的上下文是什么样的
    contexts = collect_file_contexts(
        repo_root=args.repo,
        changed_files=changed_files,
        max_chars=args.max_context_chars,
    )
    record_step(trace_steps, "collect_context",{
        "contexts":len(contexts),
    })

    # 根据规则检查 changed_files，得到 rule_issues
    rule_issues=review_changed_files(changed_files)
    issues=list(rule_issues)
    record_step(trace_steps, "run_static_checks",{
        "findings":len(issues),
    })

    if args.llm:
        from .llm_reviewer import review_with_llm
        llm_issues = review_with_llm(
            changed_files=changed_files,
            contexts=contexts,
            rule_issues=rule_issues,
            call_model=mock_call_model,
        )
        issues.extend(llm_issues)
        record_step(trace_steps, "run_llm_review",{
            "called":True,
            "findings":len(llm_issues),
        })
    else:
        record_step(trace_steps, "run_llm_review",{
            "called":False,
        })

    issues = validate_issues(issues)
    record_step(trace_steps, "validate_output",{
        "findings":len(issues),
    })

    if args.format == "json":
        output = render_json_report(issues)
    else:
        output = render_markdown_report(issues, changed_files, contexts)

    record_step(trace_steps, "render_report",{
        "format":args.format,
    })

    record_step(trace_steps, "save_trace",{
        "enabled": False,
        "reason": "后面再实现trace文件落盘"
    })

    return output, trace_steps

def main():
    args = parse_args()

    output, trace_steps = run_review_agent(args)
    write_output(output, args.output)


if __name__ == "__main__":
    main()