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

    # 检查参数合法性
    args = parser.parse_args()
    
    # 检查 max_context_chars 是否大于0
    if args.max_context_chars <= 0:
        parser.error("--max-context-chars must be greater than 0")

    return args

def read_diff(diff_path):
    return Path(diff_path).read_text(encoding="utf-8")

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


def main():
    args = parse_args()
    
    diff_text = read_diff(args.diff)

    changed_files = parse_diff(diff_text)
    issues = review_changed_files(changed_files)
    contexts = collect_file_contexts(
        repo_root=args.repo,
        changed_files=changed_files,
        max_chars=args.max_context_chars,
    )

    print_result(issues)
    # print_changed_files(changed_files)
    # print_review_input(changed_files, contexts)


if __name__ == "__main__":
    main()