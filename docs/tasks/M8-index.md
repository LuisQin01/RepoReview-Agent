# M8：假设—证据—验证审查 — 任务索引

## M8 核心目标

建立固定的两阶段验证模式：第一轮生成结构化假设，随后使用白名单 collector 确定性收集证据，第二轮为每个假设生成 confirmed、rejected 或 inconclusive verdict。只有 confirmed 且通过既有 Finding 校验链的候选 finding 才能进入最终报告。

## 边界

- M8 不是自由 ReAct 循环；不复用 M7 的自由循环控制器。
- 不允许模型任意读取仓库；不允许任意 shell 或代码执行。
- 不构建完整静态调用图。
- collector 只提供事实，不自行判断是否存在 bug。
- rejected 和 inconclusive 绝不能成为最终 finding。
- 最终 finding 必须继续经过既有 validation、去重、排序和 reporter 链。
- `single` 模式默认行为不得被修改。
- `verify` 必须保持显式 opt-in，直到对照 Eval 提供足够证据。

## 24 个 Task 清单

### 基线与协议

| 编号 | 文件 | 依赖 | 状态 |
| --- | --- | --- | --- |
| M8-0 | [M8-00-single-baseline.md](M8-00-single-baseline.md) | 现有 Eval | Ready |
| M8-1 | [M8-01-verification-models.md](M8-01-verification-models.md) | M8-0 | Blocked (M8-0) |
| M8-2 | [M8-02-evidence-type-registry.md](M8-02-evidence-type-registry.md) | M8-1 | Blocked (M8-1) |
| M8-3 | [M8-03-hypothesis-response-parser.md](M8-03-hypothesis-response-parser.md) | M8-1, M8-2 | Blocked (M8-2) |
| M8-4 | [M8-04-hypothesis-prompt-builder.md](M8-04-hypothesis-prompt-builder.md) | M8-2 | Blocked (M8-2) |
| M8-5 | [M8-05-first-round-hypothesis-call.md](M8-05-first-round-hypothesis-call.md) | M8-3, M8-4 | Blocked (M8-3, M8-4) |

### 第一轮假设生成

M8-5 汇合 M8-3 和 M8-4。

### Evidence collector

| 编号 | 文件 | 依赖 | 状态 |
| --- | --- | --- | --- |
| M8-6 | [M8-06-evidence-collector-protocol.md](M8-06-evidence-collector-protocol.md) | M8-1, M8-2 | Blocked (M8-2) |
| M8-7 | [M8-07-symbol-definition-collector.md](M8-07-symbol-definition-collector.md) | M8-6 | **Deferred** |
| M8-8 | [M8-08-caller-exception-collector.md](M8-08-caller-exception-collector.md) | M8-6 | Blocked (M8-6) |
| M8-9 | [M8-09-import-target-collector.md](M8-09-import-target-collector.md) | M8-6 | **Deferred** |
| M8-10 | [M8-10-evidence-collection-orchestrator.md](M8-10-evidence-collection-orchestrator.md) | M8-5, M8-6, 至少一个 collector | Blocked (M8-5, M8-6, M8-8) |
| M8-11 | [M8-11-evidence-failure-inconclusive.md](M8-11-evidence-failure-inconclusive.md) | M8-10 | Blocked (M8-10) |

### 第二轮验证

| 编号 | 文件 | 依赖 | 状态 |
| --- | --- | --- | --- |
| M8-12 | [M8-12-verdict-response-parser.md](M8-12-verdict-response-parser.md) | M8-1, M8-11 | Blocked (M8-11) |
| M8-13 | [M8-13-verification-prompt-builder.md](M8-13-verification-prompt-builder.md) | M8-10, M8-11, M8-12 | Blocked (M8-12) |

### 两轮 controller

| 编号 | 文件 | 依赖 | 状态 |
| --- | --- | --- | --- |
| M8-14 | [M8-14-two-round-happy-path.md](M8-14-two-round-happy-path.md) | M8-5, M8-10, M8-12, M8-13 | Blocked (M8-13) |
| M8-15 | [M8-15-empty-hypothesis-short-circuit.md](M8-15-empty-hypothesis-short-circuit.md) | M8-14 | Blocked (M8-14) |
| M8-16 | [M8-16-evidence-failure-degradation.md](M8-16-evidence-failure-degradation.md) | M8-11, M8-14 | Blocked (M8-14) |
| M8-17 | [M8-17-confirmed-finding-validation.md](M8-17-confirmed-finding-validation.md) | M8-14 | Blocked (M8-14) |

### 预算与 trace

| 编号 | 文件 | 依赖 | 状态 |
| --- | --- | --- | --- |
| M8-18A | [M8-18A-llm-call-budget.md](M8-18A-llm-call-budget.md) | M8-14 | Blocked (M8-14) |
| M8-18B | [M8-18B-token-evidence-budget.md](M8-18B-token-evidence-budget.md) | M8-18A, M8-10 | Blocked (M8-18A) |
| M8-19 | [M8-19-multiround-trace.md](M8-19-multiround-trace.md) | M8-16, M8-18B | Blocked (M8-18B) |

### 模式接入

| 编号 | 文件 | 依赖 | 状态 |
| --- | --- | --- | --- |
| M8-20 | [M8-20-single-verify-mode-switch.md](M8-20-single-verify-mode-switch.md) | M8-19 | Blocked (M8-19) |

### Eval

| 编号 | 文件 | 依赖 | 状态 |
| --- | --- | --- | --- |
| M8-21 | [M8-21-false-positive-eval-cases.md](M8-21-false-positive-eval-cases.md) | M8-0 | Blocked (M8-0) |
| M8-22 | [M8-22-single-verify-comparison-eval.md](M8-22-single-verify-comparison-eval.md) | M8-20, M8-21 | Blocked (M8-20, M8-21) |

## 依赖顺序

```text
M8-0
→ M8-1
→ M8-2
→ M8-3
→ M8-4
→ M8-5

M8-6
→ M8-8（第一版最小路径）
→ M8-10
→ M8-11

M8-12
→ M8-13

M8-5 + M8-10 + M8-12 + M8-13
→ M8-14
→ M8-15 / M8-16 / M8-17
→ M8-18A → M8-18B
→ M8-19
→ M8-20
→ M8-21（可与 M8-0 后并行）
→ M8-22
```

斜杠表示可分别完成，不表示同一 task 同时实现多个编号。

## 第一版最小路径

第一版重点使用 `caller_exception_handling` 作为确定性证据类型。

推荐执行顺序：

```text
M8-0 → M8-1 → M8-2 → M8-3 → M8-4 → M8-5
→ M8-6 → M8-8 → M8-10 → M8-11
→ M8-12 → M8-13
→ M8-14 → M8-15 / M8-16 / M8-17
→ M8-18A → M8-18B → M8-19
→ M8-20 → M8-21 → M8-22
```

### 状态说明

- **Ready**：M8-0。
- **Blocked**：依赖未满足的任务（括号内为阻塞依赖）。
- **Deferred**：M8-7（symbol_definition）、M8-9（import_target）——不属于当前第一版最小路径，待核心 verify 流程完成后再评估。保留完整验收标准，不删除，不混入 M8-8。

## M8-18A/B 与 M8-19 依赖核验结论

`新增里程碑.md` 的"M8 最小可验收版本"省略了 M8-18A/B，但 M8-19 声明依赖 M8-18B。

经核查真实仓库（**情况 B**：只满足部分契约）：

- `src/react_controller.py` 的 `ReActBudget`（max_llm_calls/max_total_tokens/max_tool_result_bytes）是 M7 自由循环专用，M8 不复用 M7 控制器。
- `src/schemas.py` 的 `ContextBudget`（max_prompt_chars/max_extra_context_files）仅控制 prompt 大小。
- `src/agent_state.py` 的 `react_*` 字段是 M7 专用。

现有能力只满足"设计模式复用"（frozen budget + pre-call check），不满足以下 M8 契约：

- 两轮调用预算（第一轮后额度不足时阻止第二轮）。
- 假设数量限制。
- 每假设证据数量限制。
- 总 evidence 字符数限制。
- 第二轮 prompt 大小限制。
- 截断证据和未处理假设的显式 M8 状态。
- M8 预算统计进入 state 或 trace。

结论：

- M8-18A/B 保留为独立实施任务。
- M8-19 继续依赖 M8-18B。
- 原最小执行序列需补入 M8-18A/B。

建议：在后续里程碑文档维护时，将 M8-18A/B 明确纳入"M8 最小可验收版本"序列。
