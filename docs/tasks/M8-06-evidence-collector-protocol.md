# M8-6：Evidence Collector 协议与注册表

## 所属里程碑

M8：假设—证据—验证审查

## 背景

每种证据类型需要一个可替换、可隔离失败的收集边界。collector 只提供事实，不自行判断是否存在 bug。本任务建立 collector 的协议、注册表与分发机制。

本任务依赖 M8-1（数据模型）和 M8-2（证据白名单）。它与 M8-8（caller_exception_handling collector）的关系是：M8-8 实现本任务定义的协议。

本任务对最终两轮验证流水线的作用是：为确定性证据收集提供统一、可隔离失败的边界。

## 本轮目标

新增 `src/evidence_collectors.py`，定义 `EvidenceCollector` 协议、registry/dispatcher，统一未知 type、异常、超时、结果大小限制与去重策略。

## 当前真实状态

已确认存在：

- `src/review_tools.py`：`ToolDispatcher` 展示了注册、查找、分发和异常转换的模式；`ToolResult` 展示了成功/失败/截断/大小的结构。M8 collector 可参考该模式但不是 model-facing tool。
- `src/review_tools.py`：`_normalize_file_context_path`、`_normalize_review_path` 提供路径规范化；`_is_sensitive_file_path` 提供敏感文件检查。
- M8-1 将定义 `EvidenceItem`、`EvidenceRequest`。
- M8-2 将定义证据类型注册表。

尚未存在：

- `src/evidence_collectors.py` 不存在。
- `tests/test_evidence_collectors.py` 不存在。
- collector 协议与注册表。

## 本轮范围

### 允许修改

- 新增 `src/evidence_collectors.py`。
- 新增 `tests/test_evidence_collectors.py`。

### 必要时允许修改

- 无。

### 禁止修改

- `src/review_tools.py`（只读复用路径规范化函数，不修改）。
- `src/verification_protocol.py`（只读复用 M8-1/M8-2 结构，不修改）。
- 任何后续编号任务的范围。
- 具体 collector 实现（M8-7 ~ M8-9）。
- controller、prompt、CLI、trace、Eval。
- M7 自由循环控制器。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义 `EvidenceCollector` 协议：`collect(request: EvidenceRequest, state) -> EvidenceItem`。
2. 实现 registry/dispatcher：按 `evidence_type` 注册和查找 collector。
3. 未知 type 返回结构化 `EvidenceItem`（status=`unsupported`），不抛异常。
4. collector 异常返回结构化 `EvidenceItem`（status=`unavailable`），不让整次审查异常退出。
5. 结果大小限制：collector 返回的 data 超限时标记 `truncated=True`，保留 actual_size 与 limit。
6. 去重策略：相同 dedup_key 的请求只执行一次（dispatcher 维护已执行请求缓存）。
7. collector 不调用第二轮模型。
8. collector 之间只传递内部数据类或 JSON 可序列化结构。
9. 路径类参数在 collector 边界使用 `_normalize_file_context_path` 归一化，拒绝绝对路径、`..` 穿越、敏感文件。

## 退出条件

- [ ] `EvidenceCollector` 协议已定义，fake collector 可满足。
- [ ] registry 支持注册与查找，重复注册被拒绝。
- [ ] 未知 type 返回 `unsupported` EvidenceItem，不抛异常。
- [ ] collector 抛异常返回 `unavailable` EvidenceItem，不崩溃。
- [ ] 超大结果标记 `truncated=True`。
- [ ] 相同 dedup_key 的请求只执行一次。
- [ ] 路径越权被拒绝（返回 `forbidden` EvidenceItem）。
- [ ] 有离线测试覆盖注册、分发、未知 type、异常、超大结果、重复请求。
- [ ] 测试不依赖网络，文件系统仅在 fake collector 需要时使用临时目录。

## 必须覆盖的测试

### 正常路径

- fake collector 注册后可被分发调用，返回正确 EvidenceItem。

### 边界情况

- 重复注册被拒绝。
- 相同 dedup_key 只执行一次。
- 结果恰好等于大小上限时不截断。

### 失败路径

- 未知 type 返回 `unsupported`。
- collector 异常返回 `unavailable`。
- 超大结果返回 `truncated`。
- 路径越权返回 `forbidden`。
- 失败是结构化 EvidenceItem，不使整次审查异常退出。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `review_tools.py` 行为不受影响。

## 兼容性约束

- 不修改 `review_tools.py` 的 ToolDispatcher 或 ToolResult。
- 不修改 `verification_protocol.py` 的数据类定义。
- 复用 `review_tools.py` 的路径规范化与错误码词汇。

## 安全要求

- collector 边界拒绝绝对路径、`..` 穿越、符号链接越权、敏感文件。
- collector 返回的路径必须在 repo root 内。
- collector 异常诊断不进入 EvidenceItem.data（仅状态码）。

## 推荐验证命令

```bash
py -m pytest tests/test_evidence_collectors.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- collector 协议与 dispatcher 契约。
- 实际调用链：dispatcher.dispatch → collector.collect → EvidenceItem。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：未知 type/异常/超限/越权的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-8（第一版最小路径）/ M8-7（暂缓）。

## 非目标

- 不实现具体 collector（M8-7 ~ M8-9）。
- 不调用第二轮模型。
- 不依赖 M7 controller。
- 不实现证据收集编排（M8-10）。

## 后续任务

- M8-7（符号定义证据收集器）依赖 M8-6，暂缓。
- M8-8（调用方异常处理证据收集器）依赖 M8-6，第一版最小路径。
- M8-9（Import Target 证据收集器）依赖 M8-6，暂缓。
