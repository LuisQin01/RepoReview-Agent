'''
1. 读取 evals/cases 下的每个 case 目录
2. 每个 case 读取 input.diff 和 expected.json
3. 构造 args，调用已有的 run_review_agent(args)
4. 解析 review 输出里的 findings
5. 提取实际命中的 category
6. 和 expected_categories 对比
7. 汇总输出指标
'''

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from time import perf_counter

from .cli import run_review_agent

def load_expected(case_dir: Path):
    expected_path = case_dir / "expected.json"
    return json.loads(expected_path.read_text(encoding="utf-8"))

def extract_categories(findings):
    categories = set()
    for finding in findings:
        category = finding.get("category") or finding.get("reason")
        if category:
            categories.add(category)
    return categories

def run_one_case(
        case_dir: Path,
        repo_root: Path,
        use_cache: bool = False,
        llm_provider: str = "mock",
        ):
    expected = load_expected(case_dir)

    args=SimpleNamespace(
        diff=str(case_dir / "input.diff"),
        repo=str(repo_root),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=use_llm,
        llm_provider=llm_provider,
        trace=False,
        trace_dir="traces"
        max_extract_context_files=3,
    )

    started=perf_counter()

    try:
        output, trace_steps=run_review_agent(args)
        duration_ms=int((perf_counter() - started) * 1000)

        data=json.loads(output)
        findings=data.get("findings", [])
        json_valid=True
        error=""
    except Exception as exc:
        duration_ms=int((perf_counter() - started) * 1000)
        findings=[]
        json_valid=False
        error=str(exc)

    
