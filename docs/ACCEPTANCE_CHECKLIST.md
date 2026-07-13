# 验收清单

## 1. 示例产物完整性

| # | 检查项 | 验证方式 | 状态 |
|---|---|---|---|
| 1.1 | `examples/demo_json_report.json` 存在且为合法 JSON | `python -c "import json; json.load(open('examples/demo_json_report.json', encoding='utf-8'))"` | ☐ |
| 1.2 | JSON 报告包含 `"findings"` 数组且至少 1 条 | 人工检查或 `python -c "import json; d=json.load(open('examples/demo_json_report.json',encoding='utf-8')); assert len(d['findings'])>=1"` | ☐ |
| 1.3 | `examples/demo_markdown_report.md` 存在且包含 `# Repo Review Report` | 人工检查文件首行 | ☐ |
| 1.4 | Markdown 报告包含 `## Summary`、`## Changed Files`、`## Findings`、`## JSON Output` 四个章节 | 人工检查 | ☐ |
| 1.5 | `examples/demo_json_llm_report.json` 存在且包含 `reason: "llm"` 的 finding | `python -c "import json; d=json.load(open('examples/demo_json_llm_report.json',encoding='utf-8')); assert any(f['reason']=='llm' for f in d['findings'])"` | ☐ |
| 1.6 | `examples/demo_traces/` 目录下存在至少 2 个 trace JSON 文件 | `Get-ChildItem examples/demo_traces -Filter *.json` | ☐ |
| 1.7 | 每个 trace JSON 包含 `task_id`、`steps`、`input_files`、`context_files`、`duration_ms` 字段 | 人工检查 | ☐ |
| 1.8 | `examples/demo_eval_results.json` 存在且为合法 JSON | `python -c "import json; json.load(open('examples/demo_eval_results.json', encoding='utf-8'))"` | ☐ |

## 2. CLI 可运行性

| # | 检查项 | 验证命令 | 状态 |
|---|---|---|---|
| 2.1 | `python -m src.cli --help` 输出帮助信息且退出码为 0 | `python -m src.cli --help` | ☐ |
| 2.2 | JSON 格式输出可正常生成 | `python -m src.cli --diff examples/simple.diff --repo . --format json` | ☐ |
| 2.3 | Markdown 格式输出可正常生成 | `python -m src.cli --diff examples/simple.diff --repo . --format markdown` | ☐ |
| 2.4 | `--output` 可将结果写入文件 | `python -m src.cli --diff examples/simple.diff --repo . --format json --output /tmp/test.json` | ☐ |
| 2.5 | `--trace` 可生成 trace 文件 | `python -m src.cli --diff examples/simple.diff --repo . --format json --trace --trace-dir /tmp/test_traces` | ☐ |
| 2.6 | `--llm --llm-provider mock` 可正常运行 | `python -m src.cli --diff examples/simple.diff --repo . --format json --llm --llm-provider mock` | ☐ |
| 2.7 | 缺少 `--diff` 参数时报错 | `python -m src.cli` (预期非零退出码) | ☐ |

## 3. Eval 可运行性

| # | 检查项 | 验证命令 | 状态 |
|---|---|---|---|
| 3.1 | `python -m src.eval_runner --cases evals/cases --repo .` 退出码为 0 | 执行命令 | ☐ |
| 3.2 | `category_hit_rate` 输出为 1.00 | 检查 stdout | ☐ |
| 3.3 | `false_positive_count` 输出为 0 | 检查 stdout | ☐ |
| 3.4 | `json_valid_rate` 输出为 1.00 | 检查 stdout | ☐ |
| 3.5 | 5 个 case 全部 passed=true | 检查 JSON 输出 | ☐ |

## 4. 测试通过

| # | 检查项 | 验证命令 | 状态 |
|---|---|---|---|
| 4.1 | `python -m pytest tests/ -v` 全部通过 | 执行命令 | ☐ |
| 4.2 | 测试数量 ≥ 7 | 检查 pytest 输出 | ☐ |
| 4.3 | 无跳过的测试 | 检查 pytest 输出 | ☐ |

## 5. 文档准确性

| # | 检查项 | 验证方式 | 状态 |
|---|---|---|---|
| 5.1 | PR 描述中所有函数名均可在 `src/` 中找到定义 | 人工对照源码 | ☐ |
| 5.2 | PR 描述中所有 CLI 参数均可在 `cli.py` 的 `parse_args()` 中找到 | 人工对照源码 | ☐ |
| 5.3 | PR 描述中所有 CLI 参数均可在 `eval_runner.py` 的 `parse_args()` 中找到 | 人工对照源码 | ☐ |
| 5.4 | PR 描述明确声明 `shellsafe`/`workspaceexec`/`hostexec`/`codeexecutor`/`OTel` 等概念在代码库中不存在 | 人工检查 `docs/PR_DESCRIPTION.md` | ☐ |
| 5.5 | PR 描述未编造任何不存在的退出码 | 人工检查 | ☐ |
| 5.6 | PR 描述包含纵深防御声明 | 人工检查 | ☐ |

## 6. 安全边界声明

| # | 检查项 | 验证方式 | 状态 |
|---|---|---|---|
| 6.1 | 文档说明文件路径检查是应用层防护，非 OS 级沙箱 | 人工检查 PR 描述 | ☐ |
| 6.2 | 文档说明本工具不执行任意代码、不管理进程生命周期 | 人工检查 PR 描述 | ☐ |
| 6.3 | 文档说明审查结果不能替代安全审计或沙箱隔离 | 人工检查 PR 描述 | ☐ |
