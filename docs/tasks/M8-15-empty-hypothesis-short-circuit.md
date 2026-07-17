# M8-15：空假设短路

## 所属里程碑

M8：假设—证据—验证审查

## 背景

没有合法假设时应该零成本、正常结束，不调用第二轮和 collector。本任务定义空假设的短路完成语义。

本任务依赖 M8-14（两轮 happy path 控制器）。它与 M8-19（多轮 trace）的关系是：M8-19 需要记录短路原因。

本任务对最终两轮验证流水线的作用是：避免无假设时的无效第二轮调用，同时区分"合法空"与"解析失败"。

## 本轮目标

在 `src/verify_controller.py` 中实现空假设短路：第一轮空列表或全部无效时短路完成，记录短路原因，不调用第二轮和 collector。

## 当前真实状态

已确认存在：

- M8-14 将实现 `VerifyController` 的 happy path。
- M8-3 将定义解析结果（含空列表合法、坏 JSON 失败）。
- `src/react_controller.py`：`_terminate` 展示了终止原因记录模式（M8 参考但不复用）。

尚未存在：

- 空假设短路逻辑。
- 短路原因记录。

## 本轮范围

### 允许修改

- `src/verify_controller.py`：新增空假设短路逻辑。
- `tests/test_verify_controller.py`：新增短路测试。

### 必要时允许修改

- `src/agent_state.py`：如需增加 `verify_termination_reason` 字段，仅限最小新增。

### 禁止修改

- `src/react_controller.py`。
- `src/review_service.py`。
- `src/verification_protocol.py`、`src/evidence_collectors.py`。
- 任何后续编号任务的范围。
- 预算降级（M8-16/M8-18A）、模式开关（M8-20）、trace（M8-19）。
- 真实 provider 调用。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 第一轮返回空假设列表（合法空）时短路完成，返回合法空 findings。
2. 第一轮返回全部无效假设（解析后空）时短路完成，返回合法空 findings。
3. 短路时第二次模型调用次数为 0。
4. 短路时 collector 调用次数为 0。
5. 短路原因记录为稳定字符串（如 `no_valid_hypotheses`）。
6. 坏 JSON 与合法空列表仍可区分（坏 JSON 是失败状态，合法空是正常短路）。
7. 短路不把空结果记为 provider 失败。
8. 短路返回的 findings 列表为空（合法空）。

## 退出条件

- [ ] 合法空假设列表（`{"hypotheses": []}`）短路完成，第二次模型调用为 0。
- [ ] 全部无效假设短路完成，第二次模型调用为 0。
- [ ] 短路时 collector 调用为 0。
- [ ] 短路原因记录为 `no_valid_hypotheses`（或等价稳定字符串）。
- [ ] 坏 JSON 不被记为合法空短路（仍为失败状态，可区分）。
- [ ] 短路返回合法空 findings 列表。
- [ ] 有离线测试覆盖上述场景，使用 `ScriptedMockProvider`。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- 合法空假设列表短路完成，零第二轮调用，零 collector 调用。

### 边界情况

- 全部无效假设（部分非法被拒绝后为空）短路完成。
- 坏 JSON 与合法空列表可区分。

### 失败路径

- 坏 JSON 不短路，记为失败状态。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- M8-14 的 happy path 测试不受影响。
- single 默认路径不受影响。

## 兼容性约束

- 不修改 `ReActController`。
- 不修改 `review_service.py`。
- 不修改 single 默认路径。
- 不修改 `validation.py`。

## 安全要求

- 短路原因不泄露 provider 诊断或敏感信息。

## 推荐验证命令

```bash
py -m pytest tests/test_verify_controller.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 短路条件与原因记录。
- 实际调用链：第一轮 → 空判断 → 短路返回。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：合法空 vs 坏 JSON 的区分。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-16 / M8-17（可分别完成）。

## 非目标

- 不收集证据。
- 不调用验证轮。
- 不把空结果记为 provider 失败。
- 不实现 trace 记录（M8-19）。

## 后续任务

- M8-16（证据失败降级）依赖 M8-11、M8-14。
- M8-17（Confirmed Finding 接入既有校验链）依赖 M8-14。
- M8-19（多轮 Trace）需要记录短路原因。
