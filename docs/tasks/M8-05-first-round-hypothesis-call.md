# M8-5：第一轮假设生成调用

## 所属里程碑

M8：假设—证据—验证审查

## 背景

第一轮需要串联 prompt builder、provider 调用和响应解析器，产出已解析的有限假设列表。这是两轮验证协议的第一轮执行点。

本任务依赖 M8-3（解析器）和 M8-4（prompt builder）。它与 M8-10（证据收集编排器）的关系是：M8-10 消费本任务产出的假设列表。

本任务对最终两轮验证流水线的作用是：以 mock provider 离线证明第一轮能产出合法假设、记录失败、空/失败不产生 finding。

## 本轮目标

在 `src/verification_protocol.py` 中实现第一轮假设生成调用编排，串联 builder、provider、parser，返回解析后的假设列表与状态记录。

## 当前真实状态

已确认存在：

- `src/llm_client.py`：`ScriptedMockProvider` 实现 `ModelProvider` 协议（`complete(request) -> ModelResponse`），记录每次请求，脚本耗尽抛 `LLMClientError("mock_script_exhausted")`。
- `src/llm_client.py`：`LLMClientError`、`LLMRetryableError`、`LLMConfigurationError` 异常体系。
- `src/review_service.py`：`_run_react_review` 展示了 provider 注入与异常处理的模式。
- M8-3 将定义解析器；M8-4 将定义 prompt builder。

尚未存在：

- 第一轮假设生成调用编排。
- `src/verify_controller.py`（M8-14 创建）。

## 本轮范围

### 允许修改

- `src/verification_protocol.py`：新增第一轮调用编排函数。
- `tests/test_verification_protocol.py`：新增第一轮调用测试（使用 `ScriptedMockProvider`）。

### 必要时允许修改

- 无。本任务的编排逻辑放在 `verification_protocol.py` 中，不创建 controller。

### 禁止修改

- `src/llm_client.py`、`src/review_service.py`、`src/react_controller.py`。
- 任何后续编号任务的范围。
- collector、第二轮 prompt、controller、CLI、trace、Eval。
- M7 自由循环控制器。
- 真实 OpenAI 调用。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 实现第一轮调用编排函数，接收 changed_files、contexts、rule_issues、provider（`ModelProvider` 协议）、max_prompt_chars，返回假设列表与状态记录。
2. 调用 M8-4 builder 生成 prompt，构造 provider 请求，调用 `provider.complete`。
3. 对 provider 返回的 `ModelResponse.text` 使用 M8-3 解析器解析。
4. 保持 provider 异常的既有语义：`LLMRetryableError`/`LLMClientError` 不被吞掉伪装成功；timeout 沿用既有语义。
5. 无效输出（坏 JSON、空列表、全非法）作为结构化失败记录，不产生假设，不产生 finding。
6. 状态记录含：是否调用、provider 名称、合法假设数、被拒绝数、解析错误列表。
7. 脚本耗尽（`mock_script_exhausted`）作为明确失败，不伪造假设。
8. provider 请求次数可由测试通过 `ScriptedMockProvider.consumed_count` 断言。

## 退出条件

- [ ] 两条合法假设的 provider 响应被解析为 2 个 `ReviewHypothesis`。
- [ ] 空假设列表（`{"hypotheses": []}`）返回空假设列表，状态记录正常。
- [ ] 坏 JSON 返回空假设列表与失败状态。
- [ ] timeout（provider 抛 `LLMRetryableError`）作为失败记录，不产生假设。
- [ ] 部分非法假设的响应保留合法项、记录被拒绝项。
- [ ] 超限假设被拒绝。
- [ ] 空假设和失败均不产生最终 finding。
- [ ] `ScriptedMockProvider.consumed_count` 可断言调用次数为 1。
- [ ] 有离线测试覆盖上述场景，使用 `ScriptedMockProvider`，不调用真实 API。
- [ ] 测试不依赖文件系统或网络。

## 必须覆盖的测试

### 正常路径

- 两条合法假设的响应被正确解析，假设数=2。

### 边界情况

- 空假设列表合法返回。
- 部分非法响应保留合法项。

### 失败路径

- 坏 JSON 返回失败状态，空假设列表。
- timeout 返回失败状态，空假设列表。
- 脚本耗尽返回失败状态，空假设列表。
- 失败不产生 finding。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- 现有 `llm_client.py`、`review_service.py` 行为不受影响。

## 兼容性约束

- 不修改 `ScriptedMockProvider` 行为。
- 不修改 `review_service.py` 的 pipeline。
- 不修改 `react_controller.py`。
- 不修改 `single` 默认路径。

## 安全要求

- provider 异常不暴露给最终报告（仅状态记录）。
- 状态记录不包含完整 prompt 原文或敏感源码。

## 推荐验证命令

```bash
py -m pytest tests/test_verification_protocol.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 编排函数的输入输出契约。
- 实际调用链：builder → provider.complete → parser。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：坏 JSON/timeout/空/脚本耗尽的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-6。

## 非目标

- 不收集证据（M8-10）。
- 不发第二轮调用。
- 不实现 controller（M8-14）。
- 不接入 CLI 或 review_service。
- 不调用真实 provider。

## 后续任务

- M8-6（Evidence Collector 协议与注册表）依赖 M8-1、M8-2。
- M8-10（证据收集编排器）依赖 M8-5、M8-6。
