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


def parse_args():
    # 读取命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, help="Path to git diff file")
    return parser.parse_args()

def read_diff(diff_path):
    return Path(diff_path).read_text(encoding="utf-8")

def print_result(issues):
    data = [asdict(issue) for issue in issues]
    print(json.dumps(data, ensure_ascii=False, indent=2))

def main():
    args = parse_args()

    diff_text = read_diff(args.diff)

    changed_files = parse_diff(diff_text)
    issues = review_changed_files(changed_files)

    print_result(issues)


if __name__ == "__main__":
    main()