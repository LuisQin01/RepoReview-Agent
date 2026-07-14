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

from .reporter import render_json_report, render_markdown_report
from .schemas import ContextBudget

from .reviewers import review_changed_files
from .diff_parser import parse_diff
from .file_context import collect_file_contexts
from .llm_reviewer import review_with_llm
from .llm_client import get_call_model, LLMClientError
from .validation import validate_issue_locations


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

def record_step(state, step, detail=None, started_at_perf=None):
    if started_at_perf is None:
        started_at_perf = perf_counter()
    duration_ms=int((perf_counter()-started_at_perf)*1000)
    state.trace_steps.append({
        "step":step,
        "duration_ms":duration_ms,
        "detail":detail or {},
    })


def _retry_detail(call_model):
    retry_info = getattr(call_model, "last_retry_info", {})
    return {
        "attempts": retry_info.get("attempts", 0),
        "retries": retry_info.get("retries", 0),
        "retry_errors": retry_info.get("retry_errors", []),
        "exhausted": retry_info.get("exhausted", False),
    }

def validate_issues(issues, changed_files):
    if not isinstance(issues, list):
        raise ValueError("Issues should be a list")
    return validate_issue_locations(issues, changed_files)

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
    from .agent_state import ReviewState
    from .trace import save_trace

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

    state = ReviewState(
        diff_path=args.diff,
        repo_root=args.repo,
        output_format=args.format,
        use_llm=args.llm,
        context_budget=context_budget,
        llm_provider=args.llm_provider,
        trace_enabled=args.trace,
        trace_dir=args.trace_dir,
    )

    # 首先记录接收到的任务参数
    record_step(state, "receive_task",{
        "diff":state.diff_path,
        "repo":state.repo_root,
        "format":state.output_format,
        "llm":state.use_llm,
        "llm_provider":state.llm_provider,
    }, started_at_perf=state.started_at_perf)

    # 读取 diff 文件
    step_started_at_perf = perf_counter()
    state.diff_text = read_diff(state.diff_path)

    # 解析 diff，得到结构化的 changed_files
    state.changed_files = parse_diff(state.diff_text)
    # 记录解析 diff 的结果
    record_step(state, "parse_diff",{
        "changed_files":len(state.changed_files),
    }, started_at_perf=step_started_at_perf)

    # 收集文件上下文，diff只告诉你修改了哪些行，但没有告诉你这些行的上下文是什么样的
    step_started_at_perf = perf_counter()
    state.contexts = collect_file_contexts(
        repo_root=state.repo_root,
        changed_files=state.changed_files,
        context_budget=state.context_budget,
    )
    record_step(state, "collect_context",{
        "contexts":len(state.contexts),
        "selected_contexts":[
            {
                "path":context.path,
                "source":context.source,
                "selection_reason":context.selection_reason,
                "exists":context.exists,
                "truncated":context.truncated,
                "chars_read":context.chars_read,
                "error":context.error,
            }
            for context in state.contexts
        ],
    }, started_at_perf=step_started_at_perf)

    # 根据规则检查 changed_files，得到 rule_issues
    step_started_at_perf = perf_counter()
    state.rule_issues=review_changed_files(state.changed_files)
    state.issues=list(state.rule_issues)
    record_step(state, "run_static_checks",{
        "findings":len(state.issues),
    }, started_at_perf=step_started_at_perf)

    if state.use_llm:
        step_started_at_perf = perf_counter()
        call_model = None
        try:    
            call_model=get_call_model(
                state.llm_provider,
                mock_fixture=getattr(args, "mock_fixture", "normal"),
            )

            state.llm_issues, validation = review_with_llm(
                changed_files=state.changed_files,
                contexts=state.contexts,
                rule_issues=state.rule_issues,
                call_model=call_model,
                max_prompt_chars=state.context_budget.max_prompt_chars,
            )
            state.errors.extend(validation.errors)
            state.issues.extend(state.llm_issues)

            record_step(state, "run_llm_review",{
                "called":True,
                "provider":state.llm_provider,
                "findings":len(state.llm_issues),
                "valid":validation.valid,
                "repaired":validation.repaired,
                "errors":validation.errors,
                **_retry_detail(call_model),
            }, started_at_perf=step_started_at_perf)
        except LLMClientError as exc:
            state.errors.append(str(exc))
            record_step(state, "run_llm_review",{
                "called":True,
                "provider":state.llm_provider,
                "findings":0,
                "error":str(exc),
                **_retry_detail(call_model),
            }, started_at_perf=step_started_at_perf)
    else:
        step_started_at_perf = perf_counter()
        record_step(state, "run_llm_review",{
            "called":False,
        }, started_at_perf=step_started_at_perf)

    step_started_at_perf = perf_counter()
    state.issues = validate_issues(state.issues, state.changed_files)
    record_step(state, "validate_output",{
        "findings":len(state.issues),
    }, started_at_perf=step_started_at_perf)

    step_started_at_perf = perf_counter()
    if state.output_format == "json":
        state.output = render_json_report(state.issues)
    else:
        state.output = render_markdown_report(state.issues, state.changed_files, state.contexts)

    record_step(state, "render_report",{
        "format":state.output_format,
    }, started_at_perf=step_started_at_perf)

    if state.trace_enabled:
        save_started_at_perf = perf_counter()
        save_trace(
            state,
            state.trace_dir,
            final_step={
                "step": "save_trace",
                "detail": {
                    "enabled": True,
                    "trace_dir": state.trace_dir,
                },
                "started_at_perf": save_started_at_perf,
            },
        )
    else:
        step_started_at_perf = perf_counter()
        record_step(state, "save_trace",{
            "enabled":False,
        }, started_at_perf=step_started_at_perf)

    return state.output, state.trace_steps

def main():
    args = parse_args()

    output, trace_steps = run_review_agent(args)
    write_output(output, args.output)


if __name__ == "__main__":
    main()
