# M8-19：多轮 Trace

## 所属里程碑

M8：假设—证据—验证审查

## 依赖矛盾核验结论

本任务依赖 M8-18B。经核查，M8-18B 为独立实施任务（情况 B），因此本任务在 M8-18B 完成前为 **Blocked**。

## 背景

从 trace 应能还原两轮验证每个阶段的数量、状态、耗时与成本，而不泄露内部推理或敏感证据原文。

本任务依赖 M8-16（证据失败降级）和 M8-18B（token/evidence 预算）。它与 M8-20（模式开关）的关系是：M8-20 接入 service 时需要 trace 可观测。

本任务对最终两轮验证流水线的作用是：提供可审计、可复盘、可脱敏的多轮 trace。

## 本轮目标

在 `src/verify_controller.py` 和 `src/trace.py` 中实现多轮 trace 记录，覆盖假设生成、证据收集、验证、finding 校验各阶段的摘要与统计。

## 当前真实状态

已确认存在：

- `src/trace.py`：`save_trace` 持久化 trace JSON；`redact_sensitive_structure` 递归脱敏；`redact_sensitive_values` 脱敏凭证；`sanitize_trace_text` 截断错误文本；`_llm_called` 判断是否调用 LLM。
- `src/review_service.py`：`record_step` 记录各步骤耗时与详情（detail 经 `redact_sensitive_structure` 脱敏）。
- `src/react_controller.py`：`_record_trace`/`_record_tool_result`/`_record_termination` 展示了 M7 trace 记录模式（M8 参考模式但不复用 M7 控制器）。
- `src/agent_state.py`：`ReviewState.trace_steps` 为 trace 步骤列表。
- M8-14 ~ M8-18B 将实现 controller 与预算，产出各阶段状态。

尚未存在：

- M8 两轮验证的 trace 阶段名与摘要结构。
- 有效/无效假设、请求数、成功/失败证据、三态 verdict、token 与时延的统计。

## 本轮范围

### 允许修改

- `src/verify_controller.py`：新增 trace 记录调用。
- `src/trace.py`：如需新增 M8 trace 辅助函数（如统计汇总），仅限最小新增。
- `tests/test_verify_controller.py`：新增 trace 测试。
- `tests/test_trace.py`：新增 M8 trace 脱敏测试。

### 必要时允许修改

- `src/agent_state.py`：如需增加 M8 trace 汇总字段，仅限最小新增。

### 禁止修改

- `src/react_controller.py`（M7 trace 记录不复用、不修改）。
- `src/review_service.py`（本任务不接入 service）。
- `src/verification_protocol.py`、`src/evidence_collectors.py`。
- 任何后续编号任务的范围。
- 模式开关（M8-20）。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 记录以下阶段的摘要（使用受控摘要字段，不存原始敏感证据或完整 reasoning）：
   - `hypothesis_generation`：合法/无效假设数、解析错误数。
   - `evidence_collection`：请求数、成功/失败证据数、截断标记。
   - `hypothesis_validation`（验证轮）：confirmed/rejected/inconclusive 数。
   - `verification`：总体验证状态。
   - `finding_validation`：经校验的 finding 数。
2. 统计有效/无效假设、请求数、成功/失败证据、三态 verdict、token 与时延。
3. 成功、空短路（`no_valid_hypotheses`）、证据失败、预算耗尽（`llm_call_budget_exhausted`）均生成一致结构的 trace。
4. 统计可由测试重算（trace 中的计数与实际一致）。
5. 敏感信息测试证明 trace 已脱敏（不包含完整源码、秘密、完整 reasoning）。
6. 关闭 trace 时主流程行为不变。
7. trace 阶段名与 M7 隔离（不混用 `react_*` 阶段名）。

## 退出条件

- [ ] 成功完成时 trace 含 `hypothesis_generation`/`evidence_collection`/`hypothesis_validation`/`finding_validation` 阶段摘要。
- [ ] 空短路时 trace 显示 `no_valid_hypotheses`。
- [ ] 证据失败时 trace 显示失败状态。
- [ ] 预算耗尽时 trace 显示 `llm_call_budget_exhausted`。
- [ ] 统计可由测试重算（trace 计数与实际一致）。
- [ ] 敏感信息测试证明 trace 不含完整源码/秘密/完整 reasoning。
- [ ] 关闭 trace 时主流程行为不变。
- [ ] trace 阶段名与 M7 隔离。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- 成功完成时 trace 结构完整，统计可重算。

### 边界情况

- 空短路时 trace 含 `no_valid_hypotheses`。
- 关闭 trace 时主流程不变。

### 失败路径

- 证据失败时 trace 显示失败状态。
- 预算耗尽时 trace 显示 `llm_call_budget_exhausted`。
- trace 不含敏感信息（脱敏测试）。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `trace.py` 的既有 `save_trace`/`redact_sensitive_structure` 行为不受影响。
- M7 trace 行为不受影响。
- single 默认路径不受影响。

## 兼容性约束

- 不修改 `save_trace` 的既有 payload 结构（仅新增 M8 字段）。
- 不修改 `redact_sensitive_structure`/`redact_sensitive_values`。
- 不修改 `react_*` trace 阶段。
- 不修改 single 默认路径。

## 安全要求

- trace 不存储原始敏感证据或完整 reasoning。
- 使用 `decision_summary`/`model_rationale_summary` 等受控摘要字段。
- 所有 trace 内容经 `redact_sensitive_structure` 脱敏。

## 推荐验证命令

```bash
py -m pytest tests/test_verify_controller.py tests/test_trace.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- trace 阶段名与摘要结构。
- 实际调用链：controller → record_step/trace 记录 → save_trace。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各终止原因的 trace 表现。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-20。

## 非目标

- 不存原始敏感证据或完整 reasoning。
- 不改变控制器决策。
- 不修改 M7 trace。
- 不接入 review_service 或 CLI（M8-20）。

## 后续任务

- M8-20（single/verify 模式开关）依赖 M8-19。
