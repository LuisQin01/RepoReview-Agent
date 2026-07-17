# M8-16：证据失败降级

## 所属里程碑

M8：假设—证据—验证审查

## 背景

证据不可用时应保守地输出 inconclusive，而非确认未经验证的假设。本任务规定全部所需证据不可用时的本地直接 inconclusive 行为，以及部分可用时携带缺失状态进入验证轮的规则。

本任务依赖 M8-11（证据失败语义）和 M8-14（两轮 controller）。它与 M8-19（多轮 trace）的关系是：M8-19 需要记录不同失败状态。

本任务对最终两轮验证流水线的作用是：确保证据不可用时不会伪造 confirmed finding。

## 本轮目标

在 `src/verify_controller.py` 中实现证据失败降级：全部所需证据不可用时本地直接 inconclusive，部分可用时携带缺失状态进入验证轮。

## 当前真实状态

已确认存在：

- M8-11 将定义 `not_found`/`unavailable`/`conflicting`/`truncated`/`unsupported` 状态语义。
- M8-14 将实现 `VerifyController` happy path。
- `src/react_controller.py`：`_terminate` 展示了降级标记模式（M8 参考但不复用）。

尚未存在：

- 证据失败降级逻辑。
- 全部不可用 vs 部分可用的区分处理。

## 本轮范围

### 允许修改

- `src/verify_controller.py`：新增证据失败降级逻辑。
- `tests/test_verify_controller.py`：新增降级测试。

### 必要时允许修改

- `src/agent_state.py`：如需增加降级标记字段，仅限最小新增。

### 禁止修改

- `src/react_controller.py`。
- `src/review_service.py`。
- `src/verification_protocol.py`、`src/evidence_collectors.py`。
- 任何后续编号任务的范围。
- 预算降级（M8-18A）、模式开关（M8-20）、trace（M8-19）。
- 真实 provider 调用。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 全部所需证据不可用（全部 `unavailable`/`not_found`/`conflicting`/`unsupported`）时，本地直接将 hypothesis 标记 inconclusive，不进入第二轮。
2. 全部不可用时第二轮调用次数为 0（或按设计有明确零/一次调用）。
3. 部分证据可用时，hypothesis 可以进入验证轮，但验证 prompt 必须携带缺失状态。
4. 不把"无法读文件"解释为问题存在。
5. 不重试到无限次。
6. 降级 hypothesis 的最终未验证 finding 数为 0。
7. 不同失败状态（unavailable/not_found/conflicting/truncated）可追溯（为 M8-19 trace 预留）。
8. 降级不产生 confirmed finding。

## 退出条件

- [ ] 全部证据不可用时，第二轮调用次数为 0（或按设计明确），hypothesis 标记 inconclusive。
- [ ] 部分可用时验证 prompt 含缺失状态（测试通过 `ScriptedMockProvider.requests` 断言）。
- [ ] 降级 hypothesis 的最终未验证 finding 数为 0。
- [ ] 不同失败状态可区分追溯。
- [ ] 降级不产生 confirmed finding。
- [ ] "无法读文件"不被解释为问题存在。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- 部分证据可用时 hypothesis 正常进入验证轮，prompt 含缺失状态。

### 边界情况

- 全部证据 truncated 时 hypothesis 标记 inconclusive（信息不完整）。
- 全部证据 conflicting 时 hypothesis 标记 inconclusive。

### 失败路径

- 全部证据 unavailable 时零第二轮调用，inconclusive。
- 全部证据 not_found 时 inconclusive（不自动确认）。
- 降级不产生 confirmed finding。

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

- 失败状态不泄露宿主机路径或内部诊断。
- 降级原因仅记录稳定状态码。

## 推荐验证命令

```bash
py -m pytest tests/test_verify_controller.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 全部不可用 vs 部分可用的处理规则。
- 实际调用链：证据收集 → 状态判断 → 降级/验证轮。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各失败状态的后果。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-17。

## 非目标

- 不把"无法读文件"解释为问题存在。
- 不重试到无限次。
- 不实现 trace 记录（M8-19）。
- 不实现预算降级（M8-18A）。

## 后续任务

- M8-17（Confirmed Finding 接入既有校验链）依赖 M8-14。
- M8-19（多轮 Trace）需要记录不同失败状态。
