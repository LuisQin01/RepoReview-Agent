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
from time import perf_counter

from .git_provider import parse_pull_request_ref
from .github_provider import GitHubPRProvider
from .schemas import ContextBudget
from .review_service import ReviewRequest, ReviewService


def parse_args():
    # 读取命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, help="Path to git diff file")
    parser.add_argument("--repo", default=".", help="Path to the repository root")
    parser.add_argument(
        "--max-prompt-chars",
        "--max-context-chars",
        dest="max_prompt_chars",
        type=int,
        default=4000,
        help="Maximum characters in the complete prompt sent to the LLM",
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
        "--publish-summary-comment",
        action="store_true",
        help="Publish the validated review summary to the specified GitHub PR.",
    )
    parser.add_argument(
        "--pr-url",
        help="GitHub pull request URL used with --publish-summary-comment.",
    )

    parser.add_argument(
        "--llm",
        action="store_true",
        help="Enable LLM reviewer",
    )

    parser.add_argument(
        "--llm-provider",
        choices=["mock","openai"],
        default="mock",
        help="LLM provider to use when --llm is enabled",
    )

    parser.add_argument(
        "--mock-fixture",
        choices=["normal", "bad_json", "timeout", "timeout_then_success", "empty"],
        default="normal",
        help="Mock LLM response fixture to use when --llm-provider=mock",
    )

    parser.add_argument(
        "--trace",
        action="store_true",
        help="Save trace json after review",
    )

    parser.add_argument(
        "--trace-dir",
        default="traces",
        help="Directory to save trace files",
    )

    parser.add_argument(
    "--max-extra-context-files",
    type=int,
    default=3,
    help="Maximum number of extra related files to read for context",
    )

    # 检查参数合法性
    args = parser.parse_args()
    
    # 检查 max_prompt_chars 是否大于0
    if args.max_prompt_chars <= 0:
        parser.error("--max-prompt-chars must be greater than 0")

    if args.max_extra_context_files < 0:
        parser.error("--max-extra-context-files must be greater than or equal to 0")
    if args.publish_summary_comment and not args.pr_url:
        parser.error("--publish-summary-comment requires --pr-url")

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
    context_budget = getattr(args, "context_budget", None)
    if context_budget is None:
        context_budget = ContextBudget(
            max_prompt_chars=getattr(
                args,
                "max_prompt_chars",
                getattr(args, "max_context_chars", 4000),
            ),
            max_extra_context_files=args.max_extra_context_files,
        )

    pull_request = None
    if getattr(args, "publish_summary_comment", False):
        pull_request = parse_pull_request_ref(pr_url=args.pr_url)

    request = ReviewRequest(
        diff_path=args.diff,
        repo_root=args.repo,
        output_format=args.format,
        use_llm=args.llm,
        context_budget=context_budget,
        llm_provider=args.llm_provider,
        mock_fixture=getattr(args, "mock_fixture", "normal"),
        trace_enabled=args.trace,
        trace_dir=args.trace_dir,
        publish_summary_comment=getattr(args, "publish_summary_comment", False),
        pull_request=pull_request,
    )
    result = ReviewService(git_provider_factory=GitHubPRProvider).review(request)
    return result.output, result.trace_steps

def main():
    args = parse_args()

    output, trace_steps = run_review_agent(args)
    write_output(output, args.output)


if __name__ == "__main__":
    main()
