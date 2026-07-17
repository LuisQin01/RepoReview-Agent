# M8-18B：Token 与 Evidence 预算

## 所属里程碑

M8：假设—证据—验证审查

## 依赖矛盾核验结论

`新增里程碑.md` 的"M8 最小可验收版本"省略了 M8-18A/B，但 M8-19 声明依赖 M8-18B。经核查真实仓库，现有预算能力为 M7 专用，不满足 M8 两轮验证契约（详见"当前真实状态"）。因此：

- M8-18A/B 保留为独立实施任务（**情况 B**：只满足部分契约）。
- M8-19 继续依赖 M8-18B。
- 原最小执行序列需补入 M8-18A/B。

## 背景

两轮审查需要限制假设规模、证据规模和第二轮 prompt 大小。截断证据和未处理假设必须有显式状态，不能静默丢弃。

本任务依赖 M8-18A（两轮调用预算）和 M8-10（证据收集编排器）。它与 M8-19（多轮 trace）的关系是：M8-19 需要记录预算统计。

本任务对最终两轮验证流水线的作用是：防止假设/证据/prompt 规模失控，保证截断与未处理项有显式状态。

## 本轮目标

在 `src/verify_controller.py` 中实现 token 与 evidence 预算：限制假设数、每假设证据数、总 evidence 字符数与第二轮 prompt 大小，为截断证据和未处理假设保留显式状态。

## 当前真实状态

已确认存在（但不满足 M8 契约）：

- `src/react_controller.py`：`ReActBudget` 含 `max_total_tokens`/`max_tool_result_bytes`/`max_total_tool_result_bytes`，但这是 **M7 自由循环**的工具结果预算，不是 M8 的假设/证据预算。
- `src/schemas.py`：`ContextBudget` 仅含 `max_prompt_chars`/`max_extra_context_files`。
- `src/agent_state.py`：`ReviewState` 有 `react_total_tokens`/`react_tool_result_bytes`/`react_tool_results_truncated` 等 M7 专用字段。
- M8-10 将实现证据收集编排器（已有总请求数和总结果大小上限的雏形）。
- M8-18A 将实现两轮调用预算。

尚未存在（M8 契约缺口）：

- **假设数量限制**：`max_hypotheses`。
- **每假设证据数量限制**：`max_evidence_per_hypothesis`。
- **总 evidence 字符数限制**：`max_total_evidence_chars`。
- **第二轮 prompt 大小限制**：`max_verification_prompt_chars`。
- **截断证据和未处理假设的显式状态**（M8 专用，非 M7 的 `react_tool_results_truncated`）。
- **预算统计进入 state 或 trace**（M8 专用字段）。

结论：现有能力只满足"模式复用"，不满足 M8 的假设/证据/prompt 预算契约。本任务为独立实施任务。

## 本轮范围

### 允许修改

- `src/verify_controller.py`：新增 token/evidence 预算逻辑与预算数据类。
- `tests/test_verify_controller.py`：新增预算测试。

### 必要时允许修改

- `src/agent_state.py`：增加 M8 专用预算计数字段（如 `verify_hypotheses_truncated`/`verify_evidence_truncated`），仅限最小新增。
- `src/evidence_collectors.py`：如需编排器暴露更细粒度的预算控制接口，仅限最小改动。

### 禁止修改

- `src/react_controller.py`（M7 预算不复用、不修改）。
- `src/schemas.py`（不修改 `ContextBudget`）。
- `src/review_service.py`。
- `src/verification_protocol.py`。
- 任何后续编号任务的范围。
- 模式开关（M8-20）、trace（M8-19）。
- 不把所有预算耗尽统一转为空 findings。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义 M8 专用 token/evidence 预算数据类（如 `VerifyEvidenceBudget`，含 `max_hypotheses`/`max_evidence_per_hypothesis`/`max_total_evidence_chars`/`max_verification_prompt_chars`）。
2. 限制假设数：超出 `max_hypotheses` 的假设被截断，标记显式状态（如 `hypotheses_truncated`）。
3. 限制每假设证据数：超出 `max_evidence_per_hypothesis` 的证据请求被截断，标记显式状态。
4. 限制总 evidence 字符数：累计超限时停止收集，标记 `evidence_truncated`。
5. 限制第二轮 prompt 大小：验证 prompt 不超过 `max_verification_prompt_chars`。
6. 截断证据标记 `truncated=True`，保留 actual_size 与 limit。
7. 未处理假设（因预算截断未收集证据）有显式状态（如 `unprocessed`）。
8. 预算统计写入 state 或等价 trace 状态。
9. 不把所有预算耗尽统一转为空 findings（保留已 confirmed 的 finding）。

## 退出条件

- [ ] 正常完成时无截断，统计与 trace 计数一致。
- [ ] 假设超限时超出部分被截断，标记显式状态。
- [ ] 证据总量超限时停止收集，标记 `evidence_truncated`。
- [ ] 第二轮 prompt 超限时被截断到预算内。
- [ ] 部分验证时已 confirmed 的 finding 保留。
- [ ] 全部 inconclusive 时零 confirmed finding。
- [ ] 截断证据有 actual_size 与 limit。
- [ ] 未处理假设有显式状态。
- [ ] 最终统计与 trace 的计数一致。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- 预算充足时两轮正常完成，无截断。

### 边界情况

- 假设数恰好等于上限。
- 证据总量恰好等于上限。
- 第二轮 prompt 恰好等于上限。

### 失败路径

- 假设超限，超出部分截断。
- 证据总量超限，停止收集。
- 第二轮 prompt 超限，截断。
- 部分验证时已 confirmed finding 保留。
- 全部 inconclusive 时零 confirmed finding。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `react_controller.py` 的 `ReActBudget` 行为不受影响。
- single/fixed 模式行为不受影响。

## 兼容性约束

- 不修改 `ReActBudget`。
- 不修改 `ContextBudget` 语义。
- 不修改 `react_*` 字段。
- 不修改 single/fixed/react 默认路径。
- 不把所有预算耗尽统一转为空 findings。

## 安全要求

- 截断状态明确标记"信息不完整"，不伪称内容完整。
- 预算统计不泄露敏感信息。

## 推荐验证命令

```bash
py -m pytest tests/test_verify_controller.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 预算数据类与 state 字段。
- 实际调用链：预算检查 → 截断/继续 → 统计写入。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各类预算超限的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-19。

## 非目标

- 不把所有预算耗尽统一转为空 findings。
- 不复用 M7 `ReActBudget`。
- 不实现 trace 记录（M8-19 在本任务统计基础上实现）。
- 不接入 review_service 或 CLI（M8-20）。

## 后续任务

- M8-19（多轮 Trace）依赖 M8-16、M8-18B。
