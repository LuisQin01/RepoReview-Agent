# PR: 文档补全 — 可运行 demo、示例产物与架构说明

## 概述

本 PR 新增由**真实运行生成**的示例产物（JSON 报告、Markdown 报告、trace 审计 JSON、eval 指标 JSON）以及一份架构说明文档。所有命令行参数、函数名和输出格式均来自源码实际实现，未编造任何尚未验证的 API。

## 重要声明：需求与实际代码的偏差

用户需求中提到的以下概念在当前代码库中**不存在**：

| 需求提到的概念 | 代码库实际情况 |
|---|---|
| `shellsafe` | 不存在。最接近的是 `reviewers.py` 中 `_looks_like_risky_call()`，它检查 `subprocess.`、`os.remove(` 等危险调用模式 |
| `Scanner` | 不存在该命名的组件。最接近的是 `reviewers.py` 中 `review_changed_files()` |
| `Permission/wrapper` | 不存在 |
| `workspaceexec` | 不存在。最接近的是 `file_context.py` 中 `read_file_context()`，它通过 `Path.resolve()` + `relative_to()` 做文件路径边界检查 |
| `hostexec` | 不存在 |
| `codeexecutor` | 不存在 |
| `audit` | 不存在该命名的组件。最接近的是 `trace.py` 中 `save_trace()`，它将执行轨迹写入 JSON 文件 |
| `OTel` (OpenTelemetry) | 不存在 |
| "12 个样例" | 代码库中只有 5 个 eval case（`evals/cases/` 目录下），不存在 12 个样例 |

本 PR 不虚构上述不存在的概念，仅文档化实际已实现的模块与能力。

---

## 实际架构：模块关系

```
src/
├── cli.py            ← 入口：解析参数、编排 Agent Loop、输出报告
├── diff_parser.py   ← 解析 git diff → ChangedFile[]
├── file_context.py   ← 读取文件上下文，做路径边界检查
├── reviewers.py      ← 静态规则检查 → ReviewIssue[]
├── llm_client.py     ← LLM 调用封装（mock / openai 两种 provider）
├── llm_reviewer.py   ← 构建 prompt、解析 LLM 响应、调用 validation
├── validation.py     ← 校验 LLM 返回的 JSON 结构，自动修复缺失字段
├── reporter.py       ← 将 ReviewIssue[] 渲染为 JSON 或 Markdown
├── schemas.py        ← 数据结构定义（DiffLine, ChangedFile, ReviewIssue, FileContext, LLMReviewResult）
├── trace.py          ← 将执行轨迹保存为 JSON 文件
├── agent_state.py    ← ReviewState 数据类，贯穿整个 Agent Loop
└── eval_runner.py    ← 批量运行 eval case 并汇总指标
```

### 模块调用关系

```
cli.run_review_agent(args)
  │
  ├─→ diff_parser.parse_diff(diff_text) → List[ChangedFile]
  │
  ├─→ file_context.collect_file_contexts(repo_root, changed_files, ...)
  │     └─→ file_context.read_file_context(repo_root, file_path, max_chars)
  │           └─→ Path.resolve() + relative_to() 做路径边界检查
  │
  ├─→ reviewers.review_changed_files(changed_files) → List[ReviewIssue]
  │     ├─→ _is_test_file() / _is_business_code_file()
  │     ├─→ _looks_like_hardcoded_secret() (regex)
  │     ├─→ _looks_like_sensitive_debug_output()
  │     ├─→ _looks_like_risky_call()
  │     └─→ _contains_exception_handling()
  │
  ├─→ [可选] llm_reviewer.review_with_llm(changed_files, contexts, rule_issues, call_model)
  │     ├─→ llm_reviewer.build_llm_prompt() → str
  │     ├─→ llm_client.get_call_model(provider) → callable
  │     │     ├─→ mock_call_model(prompt) → str
  │     │     └─→ real_call_model(prompt) → str (需要 OPENAI_API_KEY)
  │     └─→ llm_reviewer.parse_llm_response(response_text)
  │           └─→ validation.validate_llm_response(response_text) → ValidationResult
  │
  ├─→ cli.validate_issues(issues)
  │
  ├─→ reporter.render_json_report(issues) 或 reporter.render_markdown_report(issues, changed_files, contexts)
  │
  └─→ [可选] trace.save_trace(state, trace_dir)
```

### 工作区路径隔离 vs 进程生命周期

当前代码库**没有** shell 执行、PTY 管理或进程生命周期管理的能力。它是一个纯静态分析工具：

- **文件路径隔离**：`file_context.read_file_context()` 通过 `Path.resolve()` 获取绝对路径，然后用 `relative_to(repo_root_path)` 检查文件是否在仓库根目录内。如果路径逃逸（如 `../secret.txt`），返回 `FileContext(exists=False, error="...outside of the repository root...")`。这是**应用层路径校验**，不是操作系统级沙箱。
- **进程生命周期**：不涉及。Agent 本身不 spawn 子进程、不管理 PTY、不执行任意代码。
- **LLM 调用**：`real_call_model()` 通过 `openai` SDK 发送 HTTP 请求，不涉及本地进程执行。

### 策略调整方式

当前支持两种无需修改核心逻辑即可调整的策略旋钮：

**1. CLI 参数（无需改代码）**

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--diff` | str | (必填) | git diff 文件路径 |
| `--repo` | str | `.` | 仓库根目录 |
| `--max-context-chars` | int | `4000` | 每个文件最多读取的字符数 |
| `--format` | json\|markdown | `markdown` | 输出格式 |
| `--output` | str | (stdout) | 输出文件路径 |
| `--llm` | flag | False | 是否启用 LLM 评审 |
| `--llm-provider` | mock\|openai | `mock` | LLM provider |
| `--trace` | flag | False | 是否保存 trace JSON |
| `--trace-dir` | str | `traces` | trace 文件目录 |
| `--max-extra-context-files` | int | `3` | 额外上下文文件数上限 |

**2. 审查规则（需修改 `reviewers.py`）**

静态检查规则目前**硬编码**在 `reviewers.py` 中，包括：
- `debug`：检测 `print()` 和 `debugger`
- `todo`：检测 `TODO` / `FIXME`
- `secret`：检测硬编码 key/token/password/secret（regex + 关键词匹配）
- `sensitive_log`：检测打印敏感字段的调试输出
- `exception_handling`：检测删除异常处理或缺少异常处理的风险调用
- `test_gap`：检测业务代码变更但无测试变更
- `test_only_change`：检测仅测试文件变更

要增删规则，需修改 `review_changed_files()` 函数。**当前不支持配置文件驱动的策略调整。**

---

## 可运行 Demo 命令

以下命令均已实际运行验证，生成产物保存在 `examples/` 目录。

### 1. 生成 JSON 报告（纯静态检查）

```powershell
python -m src.cli --diff examples/simple.diff --repo . --format json --output examples/demo_json_report.json --trace --trace-dir examples/demo_traces
```

产物：`examples/demo_json_report.json`（10 条 findings）

### 2. 生成 Markdown 报告

```powershell
python -m src.cli --diff examples/simple.diff --repo . --format markdown --output examples/demo_markdown_report.md
```

产物：`examples/demo_markdown_report.md`（含 Summary、Changed Files、Context Files、Findings 表格、JSON 输出块）

### 3. 生成带 LLM 的 JSON 报告（mock provider）

```powershell
python -m src.cli --diff examples/simple.diff --repo . --format json --llm --llm-provider mock --output examples/demo_json_llm_report.json --trace --trace-dir examples/demo_traces
```

产物：`examples/demo_json_llm_report.json`（11 条 findings，其中 1 条来自 LLM，`reason` 为 `llm`）

### 4. 运行 eval（5 个 case）

```powershell
python -m src.eval_runner --cases evals/cases --repo .
```

产物：stdout 输出 + `examples/demo_eval_results.json`（5/5 通过，`category_hit_rate: 1.0`）

### 5. 运行 pytest

```powershell
python -m pytest tests/ -v
```

结果：7 passed in 0.16s

---

## 结构化 Report 示例

### JSON 报告格式（`render_json_report`）

```json
{
  "findings": [
    {
      "severity": "high",
      "file": "app.py",
      "line": 3,
      "issue": "新增调试输出可能泄露密码、token 或 secret",
      "reason": "sensitive_log",
      "suggested_fix": "不要打印敏感字段，必要时做脱敏处理",
      "confidence": 1.0
    }
  ]
}
```

字段说明：
- `severity`：`high` / `medium` / `low`（由 `reporter._severity_for_json()` 从 `error`/`warning`/`info` 映射）
- `file`：文件路径，或 `(repository)` 表示仓库级问题
- `line`：行号，`0` 表示仓库级问题
- `issue`：问题描述
- `reason`：问题类别（`debug`/`todo`/`secret`/`sensitive_log`/`exception_handling`/`test_gap`/`test_only_change`/`llm`）
- `suggested_fix`：修复建议
- `confidence`：置信度，静态规则为 `1.0`，LLM 结果为 `1.0`（由 `parse_llm_response` 映射）

### Markdown 报告格式（`render_markdown_report`）

包含以下章节：
1. `# Repo Review Report`
2. `## Summary` — 变更文件数、findings 数、error/warning/info 计数
3. `## Changed Files` — 文件路径、新增行数、删除行数、上下文状态
4. `## Context Files` — 上下文文件路径、类型（changed/related）、加载状态
5. `## Findings` — 按严重程度排序的表格
6. `## JSON Output` — 内嵌 JSON 块

---

## Trace / Audit 示例

Trace 文件由 `trace.save_trace(state, trace_dir)` 生成，格式为 JSON（非 JSONL）。

文件名格式：`{timestamp}_{task_id}.json`，例如 `20260713_125922_49278048.json`

### Trace JSON 结构

```json
{
  "task_id": "49278048",
  "steps": [
    {
      "step": "receive_task",
      "elapsed_ms": 0,
      "detail": { "diff": "...", "repo": ".", "format": "json", "llm": false, "llm_provider": "mock" }
    },
    { "step": "parse_diff", "elapsed_ms": 1, "detail": { "changed_files": 1 } },
    { "step": "collect_context", "elapsed_ms": 219, "detail": { "contexts": 1 } },
    { "step": "run_static_checks", "elapsed_ms": 220, "detail": { "findings": 10 } },
    { "step": "run_llm_review", "elapsed_ms": 220, "detail": { "called": false } },
    { "step": "validate_output", "elapsed_ms": 220, "detail": { "findings": 10 } },
    { "step": "render_report", "elapsed_ms": 220, "detail": { "format": "json" } },
    { "step": "save_trace", "elapsed_ms": 220, "detail": { "enabled": true, "trace_dir": "..." } }
  ],
  "input_files": [ { "path": "app.py", "added_lines": 6, "deleted_lines": 1 } ],
  "context_files": [ { "path": "app.py", "exists": false, "chars_read": 0, "truncated": false, "error": "..." } ],
  "llm_called": false,
  "llm_provider": "mock",
  "findings_count": 10,
  "duration_ms": 221,
  "errors": []
}
```

### 如何读取 Trace

- `steps[]`：按时间顺序记录 8 个固定步骤及其耗时
- `input_files[]`：本次审查涉及的变更文件
- `context_files[]`：实际读取的上下文文件及其加载状态
- `llm_called`：是否调用了 LLM
- `findings_count`：最终 findings 总数
- `duration_ms`：总耗时
- `errors[]`：执行过程中的错误（如有）

---

## Eval 案例说明

代码库包含 5 个 eval case（非 12 个），位于 `evals/cases/`：

| Case ID | 检测类别 | should_find | 实际 findings 数 | 通过 |
|---|---|---|---|---|
| `clean_change_no_issue` | (无) | false | 0 | ✓ |
| `deleted_exception_handling` | `exception_handling` | true | 3 | ✓ |
| `hardcoded_secret` | `secret` | true | 2 | ✓ |
| `missing_test` | `test_gap` | true | 1 | ✓ |
| `sensitive_log` | `sensitive_log` | true | 4 | ✓ |

汇总指标（最近一次运行）：
- `cases`: 5
- `category_hit_rate`: 1.0
- `false_positive_count`: 0
- `json_valid_rate`: 1.0
- `average_findings`: 2.0
- `average_duration_ms`: ~117

### 如何运行 eval

```powershell
python -m src.eval_runner --cases evals/cases --repo .
```

可选带 LLM：

```powershell
python -m src.eval_runner --cases evals/cases --repo . --llm --llm-provider mock
```

---

## 纵深防御声明

RepoReview Agent 是一个**静态代码审查工具**，它通过规则匹配和（可选的）LLM 分析来发现 diff 中的潜在问题。它**不是**沙箱、不是进程隔离系统、不是安全边界。

- 文件路径检查（`read_file_context` 中的 `relative_to` 校验）是**应用层防护**，不能替代操作系统级的文件权限或容器隔离
- LLM 调用（`real_call_model`）通过 HTTP 发送给 OpenAI API，不涉及本地代码执行
- 本工具的审查结果应作为人工 review 的辅助参考，不能替代安全审计或沙箱隔离

---

## 本 PR 新增/修改的文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `examples/demo_json_report.json` | 新增（真实运行生成） | 纯静态检查 JSON 报告 |
| `examples/demo_markdown_report.md` | 新增（真实运行生成） | Markdown 报告 |
| `examples/demo_json_llm_report.json` | 新增（真实运行生成） | 带 LLM (mock) 的 JSON 报告 |
| `examples/demo_traces/*.json` | 新增（真实运行生成） | Trace/审计 JSON 文件 |
| `examples/demo_eval_results.json` | 新增（真实运行生成） | Eval 指标 JSON |
| `docs/PR_DESCRIPTION.md` | 新增 | 本文件（PR 描述草稿） |
| `docs/ACCEPTANCE_CHECKLIST.md` | 新增 | 验收清单 |

未修改任何源码文件。
