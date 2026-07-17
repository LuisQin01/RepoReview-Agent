# M8-18A：两轮调用预算

## 所属里程碑

M8：假设—证据—验证审查

## 依赖矛盾核验结论

`新增里程碑.md` 的"M8 最小可验收版本"省略了 M8-18A/B，但 M8-19 声明依赖 M8-18B。经核查真实仓库，现有预算能力为 M7 专用，不满足 M8 两轮验证契约（详见"当前真实状态"）。因此：

- M8-18A/B 保留为独立实施任务（**情况 B**：只满足部分契约）。
- M8-19 继续依赖 M8-18B。
- 原最小执行序列需补入 M8-18A/B。

## 背景

两轮验证需要独立的调用预算：在第一轮后额度不足时安全阻止第二轮。M8 的预算机制不能复用 M7 的 `ReActBudget`（M7 是自由循环，M8 是固定两轮）。

本任务依赖 M8-14（两轮 controller）。它与 M8-18B（token/evidence 预算）的关系是：M8-18B 依赖本任务的调用预算基础。

本任务对最终两轮验证流水线的作用是：防止无限制的模型调用，保证预算耗尽时安全降级。

## 本轮目标

在 `src/verify_controller.py` 中实现两轮调用预算：每次模型调用前检查剩余额度，无额度时阻止第二轮并记录终止原因。

## 当前真实状态

已确认存在（但不满足 M8 契约）：

- `src/react_controller.py`：`ReActBudget`（frozen dataclass，含 `max_llm_calls`/`max_total_tokens`/`max_steps`）和 `_pre_call_termination`（每次调用前检查预算）。但这是 **M7 自由循环**的预算，M8 不复用 M7 控制器。
- `src/schemas.py`：`ContextBudget`（`max_prompt_chars`/`max_extra_context_files`），仅控制 prompt 大小，不含调用次数预算。
- `src/agent_state.py`：`ReviewState` 有 `react_llm_calls`/`react_total_tokens`/`react_termination_reason`/`react_degraded` 等 M7 专用字段。

尚未存在（M8 契约缺口）：

- **两轮调用预算**：第一轮后额度不足时阻止第二轮的逻辑（`ReActBudget` 是逐步检查，不是两轮间检查）。
- M8 专用的预算数据类与 state 字段。
- `llm_call_budget_exhausted` 终止原因。

结论：现有能力只满足"模式复用"（frozen budget + pre-call check 的设计模式），不满足 M8 的两轮调用预算契约。本任务为独立实施任务。

## 本轮范围

### 允许修改

- `src/verify_controller.py`：新增两轮调用预算逻辑与预算数据类。
- `tests/test_verify_controller.py`：新增预算测试。

### 必要时允许修改

- `src/agent_state.py`：如需增加 M8 专用预算计数字段（如 `verify_llm_calls`/`verify_termination_reason`/`verify_degraded`），仅限最小新增，不修改现有 `react_*` 字段。

### 禁止修改

- `src/react_controller.py`（M7 的 `ReActBudget` 不复用、不修改）。
- `src/schemas.py`（不修改 `ContextBudget`）。
- `src/review_service.py`（本任务不接入 service）。
- `src/verification_protocol.py`、`src/evidence_collectors.py`。
- 任何后续编号任务的范围。
- token/evidence 预算（M8-18B）、模式开关（M8-20）、trace（M8-19）。
- 固定预算语义（不绕过）。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义 M8 专用调用预算数据类（如 `VerifyCallBudget`，含 `max_llm_calls`，默认 2 表示两轮）。
2. 在每次模型调用前检查剩余额度。
3. 第一轮消耗最后一次额度后，验证轮零调用。
4. 无额度时所有未验证假设标记为降级状态（inconclusive），不伪造 confirmed finding。
5. 终止原因记录为稳定字符串 `llm_call_budget_exhausted`。
6. 已有 confirmed finding 不被伪造（预算耗尽不产生新 finding）。
7. 预算统计写入 state 或等价 trace 状态。
8. 不绕过固定预算（`ContextBudget` 仍控制 prompt 大小）。

## 退出条件

- [ ] 第一轮消耗最后一次额度后，验证轮零调用（测试通过 `ScriptedMockProvider.consumed_count` 断言）。
- [ ] 预算耗尽不产生 confirmed finding。
- [ ] 终止原因记录为 `llm_call_budget_exhausted`。
- [ ] 已有 confirmed finding 不被伪造。
- [ ] 预算统计写入 state 字段。
- [ ] fixed/single 模式行为不回归（`py -m pytest tests/ -v` 通过）。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- 预算充足时两轮正常完成。

### 边界情况

- 预算恰好为 2 时两轮正常完成。
- 预算为 1 时第一轮后终止。

### 失败路径

- 预算耗尽后验证轮零调用。
- 预算耗尽不产生 confirmed finding。
- 终止原因记录正确。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `react_controller.py` 的 `ReActBudget` 行为不受影响。
- single/fixed 模式行为不受影响。

## 兼容性约束

- 不修改 `ReActBudget`。
- 不修改 `ContextBudget` 语义。
- 不修改 `react_*` 字段。
- 不修改 single/fixed/react 默认路径。
- 不绕过固定预算。

## 安全要求

- 预算终止原因不泄露 provider 诊断。
- 降级状态不伪造成功。

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
- 实际调用链：pre-call 检查 → 预算判断 → 终止/继续。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：预算耗尽的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-18B。

## 非目标

- 不同时实现 token/evidence 预算（M8-18B）。
- 不绕过固定预算。
- 不复用 M7 `ReActBudget`。
- 不接入 review_service 或 CLI（M8-20）。

## 后续任务

- M8-18B（Token 与 Evidence 预算）依赖 M8-18A、M8-10。
