# M8-10：证据收集编排器

## 所属里程碑

M8：假设—证据—验证审查

## 背景

多个假设可能请求相同证据，编排器需要去重、执行并准确回填到对应假设。单个 collector 失败只影响相关假设，不影响其他假设的证据收集。

本任务依赖 M8-5（第一轮假设生成）和 M8-6（collector 协议），且至少一个具体 collector（第一版为 M8-8）已完成。它与 M8-11（证据失败语义）和 M8-14（两轮 controller）的关系是：M8-11 定义失败状态语义，M8-14 调用本编排器。

本任务对最终两轮验证流水线的作用是：把第一轮假设的证据请求去重、批量执行、按 hypothesis ID 分组回填。

## 本轮目标

在 `src/evidence_collectors.py` 中实现证据收集编排器，构建去重请求计划，执行后按 hypothesis ID 分组回填，受总请求数、总结果大小与上下文预算约束。

## 当前真实状态

已确认存在：

- M8-6 将定义 `EvidenceCollector` 协议、dispatcher 与去重策略。
- M8-8 将实现 `caller_exception_handling` collector。
- M8-1 将定义 `ReviewHypothesis`（含 `evidence_requests`）和 `EvidenceItem`。
- `src/review_tools.py`：`ToolDispatcher` 展示了分发与异常隔离模式。

尚未存在：

- 证据收集编排器。
- 跨假设去重与分组回填逻辑。

## 本轮范围

### 允许修改

- `src/evidence_collectors.py`：新增证据收集编排器。
- `tests/test_evidence_collectors.py`：新增编排器测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/verification_protocol.py`（只读复用数据类，不修改）。
- `src/review_tools.py`。
- 任何后续编号任务的范围。
- controller、prompt、CLI、trace、Eval。
- M7 自由循环控制器。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 实现编排器，接收假设列表（含各自 `evidence_requests`）和 collector dispatcher。
2. 构建去重请求计划：相同 dedup_key 的请求只执行一次，结果共享给所有请求它的假设。
3. 执行后按 hypothesis ID 分组回填 EvidenceItem 到对应假设。
4. 设置总请求数上限：超限时未执行的请求标记为显式状态（如 `truncated`），不静默丢弃。
5. 设置总结果大小上限：累计 evidence 字符数超限时停止收集，标记截断。
6. 单个 collector 失败只影响相关假设：该假设对应证据标记失败状态，其他假设继续。
7. 编排器不调用第二轮 LLM。
8. 不从第一轮假设直接生成 finding。
9. 返回结构含：每个假设的 EvidenceItem 列表、去重映射、被截断/未执行请求的记录。

## 退出条件

- [ ] 同一 dedup_key 的请求只执行一次。
- [ ] 一假设多证据请求时各证据正确回填到该假设。
- [ ] 跨假设共享证据时共享结果正确回填到所有请求方。
- [ ] 部分 collector 失败时失败只影响相关假设，其他假设仍获得证据。
- [ ] 总请求数超限时有记录，未执行请求有显式状态。
- [ ] 总结果大小超限时有记录，截断标记可见。
- [ ] 编排器不调用第二轮 LLM。
- [ ] 有离线测试覆盖上述场景，使用 fake/scripted collector。
- [ ] 测试不依赖网络。

## 必须覆盖的测试

### 正常路径

- 两假设各含不同证据请求，全部回填正确。
- 跨假设共享证据，只执行一次但回填到两假设。

### 边界情况

- 一假设含多条证据请求。
- 总请求数恰好等于上限。
- 总结果大小恰好等于上限。

### 失败路径

- 部分 collector 失败只影响相关假设。
- 总请求数超限，未执行请求有显式状态。
- 总结果大小超限，截断标记可见。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `review_tools.py`、`verification_protocol.py` 行为不受影响。

## 兼容性约束

- 不修改 `verification_protocol.py` 的数据类。
- 不修改 `review_tools.py`。
- 复用 M8-6 的 dispatcher 与错误码。

## 安全要求

- evidence 数据在回填前经过敏感值脱敏（复用 `trace.redact_sensitive_values`）。
- 路径在 collector 边界校验。
- 失败诊断不进入 evidence data。

## 推荐验证命令

```bash
py -m pytest tests/test_evidence_collectors.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 编排器输入输出契约。
- 实际调用链：orchestrator → dispatcher.dispatch → collector.collect → 回填 EvidenceItem。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：部分失败/超限/截断的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-11。

## 非目标

- 不调用第二轮 LLM。
- 不从第一轮假设直接生成 finding。
- 不实现 controller（M8-14）。
- 不实现 verdict 解析（M8-12）。

## 后续任务

- M8-11（证据失败与 inconclusive 语义）依赖 M8-10。
- M8-13（验证 Prompt Builder）依赖 M8-10、M8-11。
- M8-14（两轮 Happy Path 控制器）依赖 M8-5、M8-10、M8-12、M8-13。
