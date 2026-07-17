# M8-8：调用方异常处理证据收集器

## 所属里程碑

M8：假设—证据—验证审查

## 背景

第一版最小路径的核心确定性证据类型。针对已知调用点，确定其是否位于相关的 `try/except` 中。证据只描述事实（是否在 try 中、捕获什么类型），不自行判断是否为 bug。

本任务依赖 M8-6（collector 协议与注册表）。它与 M8-10（证据收集编排器）的关系是：M8-10 调度本 collector 作为第一版唯一确定性证据来源。

本任务对最终两轮验证流水线的作用是：为"调用方未处理异常"类假设提供确定性事实，使第二轮能 confirmed 或 rejected。

## 本轮目标

实现 `caller_exception_handling` collector，接收已知 file 与 line，使用 AST 判断包围该调用的 `try`，记录捕获类型、bare except 和嵌套结构。

## 当前真实状态

已确认存在：

- `src/file_context.py`：`locate_python_symbol(source, line_no)` 用 AST 定位函数/方法/类，但**不**定位 try/except 结构。本任务需要新增 AST 逻辑。
- `src/file_context.py`：`_is_sensitive_file_path`、`read_file_context` 提供安全读取。
- `src/review_tools.py`：`_normalize_file_context_path` 提供路径规范化。
- `src/trace.py`：`redact_sensitive_values` 提供脱敏。
- M8-6 将定义 `EvidenceCollector` 协议与 dispatcher。

尚未存在：

- `caller_exception_handling` evidence collector。
- AST 定位 try/except 包围结构的逻辑。

## 本轮范围

### 允许修改

- `src/evidence_collectors.py`：新增 `caller_exception_handling` collector 及其 AST 逻辑。
- `tests/test_evidence_collectors.py`：新增 collector 测试（含临时 Python 源文件 fixture）。

### 必要时允许修改

- 无。

### 禁止修改

- `src/file_context.py`（只读复用安全读取，不修改 `locate_python_symbol`）。
- `src/review_tools.py`。
- `src/verification_protocol.py`。
- 任何后续编号任务的范围。
- controller、prompt、CLI、trace、Eval。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 实现 `caller_exception_handling` collector，接收 `EvidenceRequest`（含 file、line_no）。
2. 在 collector 边界归一化路径，拒绝越权与敏感文件。
3. 读取目标 Python 文件，AST 解析。
4. 找到包含 `line_no` 的所有 `ast.Try` 节点（含嵌套）。
5. 返回 `EvidenceItem`：data 含调用点行号、是否在 try 中（布尔）、各层 try 的捕获类型列表（如 `["ValueError", "Exception"]`）、是否有 bare except（布尔）、嵌套层级。
6. 不相关 except（如捕获 `KeyError` 但假设关心 `ValueError`）不被误标为已处理——证据如实记录捕获类型，由第二轮模型判断相关性。
7. 证据只描述事实，不判断是否为 bug。
8. 区分 `not_found`（行号不在任何符号/try 中但文件可读）、`unavailable`（AST 解析失败/非 Python/读取失败）、`forbidden`（越权/敏感）。
9. 行号定位失败是 `unavailable`/`not_found`，不是 confirmed。
10. 返回路径在 repo root 内。

## 退出条件

- [ ] 调用点在匹配异常类型的 try 中时，data 正确记录捕获类型与层级。
- [ ] 调用点在无关异常类型的 try 中时，data 记录实际捕获类型（不误标为已处理）。
- [ ] 调用点无 try 包围时，data 记录 `in_try: false`。
- [ ] bare except 被正确识别并记录。
- [ ] 嵌套 try 结构被正确记录。
- [ ] AST 解析失败返回 `unavailable`，不崩溃。
- [ ] 非 Python 文件返回 `unsupported`。
- [ ] 越权路径返回 `forbidden`。
- [ ] 行号定位失败返回 `unavailable`/`not_found`，而非 confirmed。
- [ ] 证据 data 不包含"是否为 bug"的判断。
- [ ] 有离线测试覆盖上述场景，使用临时 Python 源文件。
- [ ] 测试不依赖网络。

## 必须覆盖的测试

### 正常路径

- 调用点在 `try: ... except ValueError:` 中，data 记录 `["ValueError"]`。
- 调用点在 bare `except:` 中，data 记录 `bare_except: true`。

### 边界情况

- 嵌套 try（外层 except Exception，内层 except ValueError）被正确记录。
- 调用点在 try 的 else/finally 块中时的处理。
- 多个 try 层级。

### 失败路径

- 无 try 包围返回 `in_try: false`（status=`found`）。
- AST 解析失败返回 `unavailable`。
- 非 Python 返回 `unsupported`。
- 越权返回 `forbidden`。
- 不相关 except 不被误标为已处理。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `file_context.py` 的 `locate_python_symbol` 行为不受影响。

## 兼容性约束

- 不修改 `locate_python_symbol`。
- 不修改 `read_file_context`。
- 复用 M8-6 的协议与错误码。
- 不修改 `ReviewIssue` 或 `ContextBudget`。

## 安全要求

- 越权路径与敏感文件被拒绝。
- 返回路径在 repo root 内。
- 源码片段经过敏感值脱敏后才进入 EvidenceItem.data。

## 推荐验证命令

```bash
py -m pytest tests/test_evidence_collectors.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- collector 输入输出契约与 AST 逻辑说明。
- 实际调用链：dispatcher → collector → AST 解析 → EvidenceItem。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：not_found/unavailable/forbidden/unsupported 的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-10。

## 非目标

- 不自动发现全仓库调用方。
- 不构建完整调用图。
- 不判断是否为 bug（由第二轮模型判断）。
- 不实现证据收集编排（M8-10）。
- 不调用第二轮模型。

## 后续任务

- M8-10（证据收集编排器）依赖 M8-5、M8-6，至少一个具体 collector（本任务）已完成。
