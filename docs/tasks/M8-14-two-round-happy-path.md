# M8-14：两轮 Happy Path 控制器

## 所属里程碑

M8：假设—证据—验证审查

## 背景

两轮协议需要一个固定编排器，串联"假设生成 → 证据收集 → 验证 → 最终校验入口"。M8 的控制器不是自由 ReAct 循环，路径固定为两轮模型调用 + 确定性证据收集。M8 不复用 M7 的自由循环控制器。

本任务依赖 M8-5（第一轮调用）、M8-10（证据收集编排器）、M8-12（verdict 解析器）、M8-13（验证 prompt builder）。它与 M8-15/M8-16/M8-17 的关系是：那些任务在本 happy path 基础上增加短路、降级和校验链接入。

本任务对最终两轮验证流水线的作用是：离线证明两轮协议确实淘汰被拒绝假设，而非换个字段名直接输出第一轮结果。

## 本轮目标

新增 `src/verify_controller.py`，实现固定两轮 happy path 控制器，使用脚本化 mock 串联全流程，只把第二轮 confirmed 的候选 finding 送入后续校验链。

## 当前真实状态

已确认存在：

- `src/react_controller.py`：`ReActController`（M7 自由循环，M8 **不复用**）、`ReActBudget`、`ModelProvider` 协议、`ReActControllerError`。
- `src/llm_client.py`：`ScriptedMockProvider`（实现 `ModelProvider` 协议）。
- `src/review_service.py`：`_run_react_review` 展示了 provider 注入与异常处理模式；`validate_issues` 展示了 finding 校验入口。
- `src/review_tools.py`：`FinishReview._validated_issue` 展示了候选 finding 经既有校验链的模式。
- M8-5 将实现第一轮调用编排；M8-10 将实现证据收集编排器；M8-12 将实现 verdict 解析器；M8-13 将实现验证 prompt builder。

尚未存在：

- `src/verify_controller.py` 不存在。
- `tests/test_verify_controller.py` 不存在。
- 两轮固定编排逻辑。

## 本轮范围

### 允许修改

- 新增 `src/verify_controller.py`。
- 新增 `tests/test_verify_controller.py`。

### 必要时允许修改

- `src/agent_state.py`：如需在 `ReviewState` 上增加最小 verify 计数字段（如 `verify_hypothesis_count`、`verify_confirmed_count`），仅限最小新增，不修改现有字段。

### 禁止修改

- `src/react_controller.py`（M7 控制器，不复用、不修改）。
- `src/review_service.py`（本任务不接入 service，M8-20 接入）。
- `src/verification_protocol.py`、`src/evidence_collectors.py`（只读复用，不修改）。
- `src/cli.py`（本任务不接入 CLI）。
- `src/trace.py`（本任务不实现 trace，M8-19 实现）。
- 任何后续编号任务的范围。
- 空短路、预算/失败降级或模式开关（M8-15/M8-16/M8-18A/M8-20）。
- 真实 provider 调用。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 实现 `VerifyController`，接收 provider（`ModelProvider` 协议）、evidence collector dispatcher、changed_files、contexts、rule_issues、context_budget。
2. 第一轮：调用 M8-5 编排生成假设列表。
3. 证据收集：调用 M8-10 编排器收集证据并按 hypothesis ID 回填。
4. 第二轮：调用 M8-13 builder 生成验证 prompt，调用 provider，调用 M8-12 解析器解析 verdict。
5. 只把 confirmed verdict 的候选 finding 送入既有校验链（`validate_issues` / `validate_issue_locations`）。
6. rejected/inconclusive verdict 绝不产生 finding。
7. 第一轮文本自身不能直接成为结果（测试断言）。
8. 使用 `ScriptedMockProvider` 驱动，调用顺序/次数可断言。
9. controller 之间只传递内部数据类或 JSON 可序列化结构。
10. 不加空短路（M8-15）、预算/失败降级（M8-16/M8-18A）或模式开关（M8-20）。

## 退出条件

- [ ] 两假设样例中一条 confirmed、一条 rejected，最终恰一条 finding。
- [ ] 第二轮请求包含对应证据（测试通过 `ScriptedMockProvider.requests` 断言）。
- [ ] 调用顺序/次数可断言（第一轮 1 次 + 第二轮 1 次 = 2 次 provider 调用）。
- [ ] 第一轮文本自身不可能直接成为结果（测试构造第一轮含"finding"文本但第二轮全 rejected，最终零 finding）。
- [ ] rejected verdict 不产生 finding。
- [ ] inconclusive verdict 不产生 finding。
- [ ] confirmed 的候选 finding 经既有 `validate_issues` 校验。
- [ ] 有离线测试覆盖上述场景，使用 `ScriptedMockProvider`。
- [ ] 测试不调用真实 API，不依赖网络。

## 必须覆盖的测试

### 正常路径

- 两假设（一 confirmed 一 rejected），最终恰一条 finding，且 finding 经校验。

### 边界情况

- 两假设均 confirmed，最终两条 finding。
- 两假设均 rejected，最终零 finding。

### 失败路径

- 第一轮文本含 finding 文本但第二轮全 rejected，最终零 finding（证明第一轮文本不直接成为结果）。
- confirmed 候选 finding 位置不在 changed hunk，被校验链拒绝。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `react_controller.py` 行为不受影响。
- `review_service.py` 的 fixed/react 路径不受影响。
- single 默认路径不受影响。

## 兼容性约束

- 不复用/不修改 `ReActController`。
- 不修改 `review_service.py` 的 pipeline。
- 不修改 `validation.py` 的校验语义。
- 不修改 `single` 默认路径。
- 不修改 `react` 模式行为。
- confirmed finding 必须经既有 `validate_issues` 校验。

## 安全要求

- controller 不直接接触 SDK 对象。
- provider 异常不暴露给最终报告。
- 路径在 collector 边界校验。
- 不存储完整 reasoning 或敏感源码。

## 推荐验证命令

```bash
py -m pytest tests/test_verify_controller.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- controller 输入输出契约与两轮调用顺序。
- 实际调用链：第一轮编排 → 证据收集 → 第二轮 builder/provider/parser → validate_issues。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：本任务为 happy path，不含降级（M8-15/M8-16 实现）。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-15 / M8-16 / M8-17（可分别完成）。

## 非目标

- 不加空短路（M8-15）。
- 不加预算/失败降级（M8-16/M8-18A）。
- 不加模式开关（M8-20）。
- 不接入 review_service 或 CLI。
- 不实现 trace（M8-19）。
- 不复用 M7 自由循环控制器。

## 后续任务

- M8-15（空假设短路）依赖 M8-14。
- M8-16（证据失败降级）依赖 M8-11、M8-14。
- M8-17（Confirmed Finding 接入既有校验链）依赖 M8-14。
