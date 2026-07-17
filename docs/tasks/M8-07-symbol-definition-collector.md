# M8-7：符号定义证据收集器

## 所属里程碑

M8：假设—证据—验证审查

## 状态标记

**不属于当前第一版最小路径，待核心 verify 流程完成后再评估。**

第一版重点使用 `caller_exception_handling` 作为确定性证据类型。本任务保留完整验收标准，但在核心 verify 流程完成前暂缓执行。

## 背景

`symbol_definition` collector 为给定 file/line/symbol 提供可靠的 Python 定义证据，供第二轮验证使用。

本任务依赖 M8-6（collector 协议与注册表）。它与 M8-10（证据收集编排器）的关系是：M8-10 可调度本 collector。

本任务对最终两轮验证流水线的作用是：为"假设某函数定义缺失或有误"类假设提供确定性定义证据。

## 本轮目标

实现 `symbol_definition` collector，返回给定 file/line 对应的 Python 符号限定名、行范围、受预算限制的定义片段及来源。

## 当前真实状态

已确认存在：

- `src/file_context.py`：`locate_python_symbol(source, line_no)` 返回 `PythonSymbol`（含 name/kind/start_line/end_line/source/qualified_name/class_name）。该函数使用 AST 解析，语法错误返回 None。
- `src/review_tools.py`：`SearchPythonSymbolTool` 已将 `locate_python_symbol` 包装为 M7 model-facing tool，返回符号定位元数据（不含 source 文本）。
- `src/file_context.py`：`_is_sensitive_file_path`、`read_file_context` 提供安全读取。
- M8-6 将定义 `EvidenceCollector` 协议与 dispatcher。

尚未存在：

- `symbol_definition` evidence collector。
- collector 返回定义片段（含 source）的能力。

## 本轮范围

### 允许修改

- `src/evidence_collectors.py`：新增 `symbol_definition` collector。
- `tests/test_evidence_collectors.py`：新增 collector 测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/file_context.py`（只读复用 `locate_python_symbol`、`read_file_context`，不修改）。
- `src/review_tools.py`。
- 任何后续编号任务的范围。
- controller、prompt、CLI、trace、Eval。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 实现 `symbol_definition` collector，接收 `EvidenceRequest`（含 file、line_no）。
2. 在 collector 边界归一化路径，拒绝越权与敏感文件。
3. 读取目标 Python 文件（复用安全读取机制），AST 解析定位符号。
4. 返回 `EvidenceItem`：data 含 qualified_name、kind、start_line、end_line、定义片段（受预算限制的 source 文本）、来源路径。
5. 区分 `not_found`（符号不存在）、`unavailable`（无法解析/非 Python/读取失败）、`forbidden`（越权/敏感）。
6. `not_found` 不等于 `unavailable`。
7. 定义片段受字符预算限制，超限时标记 `truncated=True`。
8. 返回路径在 repo root 内。

## 退出条件

- [ ] 函数定义可被定位并返回 qualified_name 与行范围。
- [ ] 类方法可被定位。
- [ ] 不存在的符号返回 `not_found`。
- [ ] 语法错误的文件返回 `unavailable`。
- [ ] 非 Python 文件返回 `unsupported`。
- [ ] 越权路径返回 `forbidden`。
- [ ] 大定义片段被截断并标记 `truncated=True`。
- [ ] 返回路径在 repo root 内。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试使用临时文件，不依赖网络。

## 必须覆盖的测试

### 正常路径

- 函数、类方法的符号定位与定义片段返回。

### 边界情况

- 大定义片段截断。
- 嵌套符号（方法在类内）。

### 失败路径

- 不存在符号返回 `not_found`。
- 语法错误返回 `unavailable`。
- 非 Python 返回 `unsupported`。
- 越权返回 `forbidden`。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `file_context.py` 的 `locate_python_symbol` 行为不受影响。

## 兼容性约束

- 不修改 `locate_python_symbol` 或 `read_file_context`。
- 不修改 `SearchPythonSymbolTool`。
- 复用 M8-6 的协议与错误码。

## 安全要求

- 越权路径与敏感文件被拒绝。
- 返回路径在 repo root 内。
- 定义片段经过敏感值脱敏（复用 `trace.redact_sensitive_values`）。

## 推荐验证命令

```bash
py -m pytest tests/test_evidence_collectors.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- collector 输入输出契约。
- 实际调用链：dispatcher → collector → locate_python_symbol → EvidenceItem。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：not_found/unavailable/forbidden/truncated 的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：待核心 verify 流程完成后评估。

## 非目标

- 不搜索整个调用图。
- 不根据模型推测补全定义。
- 不实现证据收集编排（M8-10）。
- 不调用第二轮模型。

## 后续任务

- M8-10（证据收集编排器）在至少一个具体 collector 完成后可集成本 collector。
