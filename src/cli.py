'''
作为命令行的入口，用来负责把用户的输入接入进来
功能：
1. 读取参数
2. 调用 diff_parser
3. 调用 file_context
4. 调用reviewers
5. 打印结果

本模块在整体架构中属于“入口适配层”：把命令行参数翻译为内部领域对象
（:class:`ContextBudget` / :class:`PullRequestRef` / :class:`ReviewRequest`），
再交给 :class:`ReviewService` 执行审查流水线。它本身不包含业务逻辑，
只做参数解析、合法性校验与结果输出，便于把核心流水线复用到 API、
评估器等其他入口。
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
    """解析并校验命令行参数。

    定义 RepoReview Agent 的全部 CLI 选项，并在解析后做跨参数的合法性校验
    （单个参数的约束如 ``type=int`` 由 argparse 负责，跨参数约束在此处手动检查）。

    Returns:
        解析后的 ``argparse.Namespace``。

    Raises:
        argparse.ArgumentError: 当参数非法时通过 ``parser.error`` 退出。
    """
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
    # 跨参数校验：发布摘要评论必须同时提供 PR URL
    if args.publish_summary_comment and not args.pr_url:
        parser.error("--publish-summary-comment requires --pr-url")

    return args

def read_diff(diff_path):
    """读取 diff 文件文本。

    Args:
        diff_path: diff 文件路径。

    Returns:
        文件内容字符串（utf-8 解码）。
    """
    return Path(diff_path).read_text(encoding="utf-8")

def write_output(output, output_path):
    """输出审查结果。

    指定 ``output_path`` 时写入文件（自动创建父目录）；否则打印到 stdout。

    Args:
        output: 要输出的字符串内容。
        output_path: 目标文件路径；为 None 时打印到标准输出。
    """
    if output_path:
        path=Path(output_path)
        # 自动创建多级父目录，避免因目录不存在导致写入失败
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    else:
        print(output)

def print_result(issues):
    """以 JSON 形式打印审查问题列表。

    Args:
        issues: Issue 对象列表，通过 :func:`dataclasses.asdict` 转 dict 后输出。
    """
    data = [asdict(issue) for issue in issues]
    print(json.dumps(data, ensure_ascii=False, indent=2))

def print_changed_files(changed_files):
    """以 JSON 形式打印变更文件摘要（路径、增删行数、patch）。

    Args:
        changed_files: 变更文件对象列表。
    """
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
    """以 JSON 形式打印审查输入（变更文件 + 额外上下文）。

    用于调试时查看实际送入审查流水线的输入。

    Args:
        changed_files: 变更文件对象列表。
        contexts: 上下文对象列表。
    """
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
    """遗留的 mock 模型调用，返回固定 JSON findings。

    仅用于早期开发/调试，正式流程中由 ReviewService 注入的 LLM provider 取代。
    保留是为了向后兼容旧脚本。

    Args:
        prompt: 提示词（本 mock 不使用）。

    Returns:
        固定的 JSON 字符串，包含一条示例 finding。
    """
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
    """组装审查请求并执行 ReviewService。

    将 CLI 参数翻译为领域对象：
    - 由 ``max_prompt_chars``/``max_extra_context_files`` 构造 :class:`ContextBudget`；
    - 当需要发布摘要评论时，由 ``pr_url`` 解析出 :class:`PullRequestRef`；
    - 组装 :class:`ReviewRequest` 交给 :class:`ReviewService` 执行。

    注意：``GitHubPRProvider`` 以工厂形式注入 ReviewService，便于流水线内部
    在需要时按需创建 Provider 实例。

    Args:
        args: 已解析的命令行参数 Namespace。

    Returns:
        ``(output, trace_steps)`` 元组，分别为渲染后的输出字符串与追踪步骤列表。
    """
    # 优先使用外部已设置的 context_budget（如测试注入），否则按参数构造默认预算
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

    # 仅在需要发布摘要评论时才解析 PR 引用，避免无谓的 URL 校验
    pull_request = None
    if getattr(args, "publish_summary_comment", False):
        pull_request = parse_pull_request_ref(pr_url=args.pr_url)

    # 组装审查请求：把所有 CLI 选项集中到 ReviewRequest 中传递给流水线
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
    # 将 GitHubPRProvider 作为工厂注入，ReviewService 内部按需实例化 Provider
    result = ReviewService(git_provider_factory=GitHubPRProvider).review(request)
    return result.output, result.trace_steps

def main():
    """CLI 主入口：解析参数 → 执行审查 → 输出结果。"""
    args = parse_args()

    output, trace_steps = run_review_agent(args)
    write_output(output, args.output)


if __name__ == "__main__":
    main()
