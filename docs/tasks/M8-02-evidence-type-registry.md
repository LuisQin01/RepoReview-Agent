# M8-2：Evidence Type 白名单与参数 Schema

## 所属里程碑

M8：假设—证据—验证审查

## 背景

模型在第一轮可请求的证据类型必须是有限白名单，否则会退化为任意仓库探索。本任务为第一版登记确定性证据类型及其参数 schema，限定模型可请求范围。

本任务依赖 M8-1 的 `EvidenceRequest` 结构。它与 M8-3（解析器）的关系是：M8-3 在解析假设时使用本任务的白名单校验证据请求合法性。

本任务对最终两轮验证流水线的作用是：闭合证据类型集合，使 collector 只需处理已登记类型。

## 本轮目标

在 `src/verification_protocol.py` 中新增证据类型注册表，为第一版登记 `symbol_definition`、`caller_exception_handling`、`import_target` 三种类型，各自定义参数 schema、数量上限与去重键。

## 当前真实状态

已确认存在：

- M8-1 将定义 `EvidenceRequest`（含 `evidence_type`、`params`、`dedup_key`）。
- `src/review_tools.py`：`ToolDispatcher._value_matches_schema` / `_object_matches_schema` 提供了 JSON Schema 子集校验（object/array/string/integer/number/boolean/null + additionalProperties=False 白名单语义），可复用。
- `src/review_tools.py`：`_normalize_review_path` / `_normalize_file_context_path` 提供路径规范化。

尚未存在：

- 证据类型注册表与白名单。
- 各类型的参数 schema 定义。

## 本轮范围

### 允许修改

- `src/verification_protocol.py`：新增证据类型注册表与校验逻辑。
- `tests/test_verification_protocol.py`：新增白名单与 schema 校验测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/review_tools.py`（只读复用其 schema 校验函数，不修改）。
- 任何后续编号任务的范围。
- collector 实现（M8-6 ~ M8-9）。
- prompt、controller、CLI、trace、Eval。
- 真实 provider 调用。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义证据类型注册表，第一版登记三种类型：`symbol_definition`、`caller_exception_handling`、`import_target`。
2. 每种类型定义参数 JSON Schema（使用 `additionalProperties: false` 白名单语义），至少包含路径类参数与行号类参数。
   - `symbol_definition`：参数含 `file`（string）、`line_no`（integer）或等价定位参数。
   - `caller_exception_handling`：参数含 `file`（string）、`line_no`（integer），表示已知调用点位置。
   - `import_target`：参数含 `file`（string）、`import_statement`（string）或等价 import 标识。
3. 每种类型定义单假设内最大请求数量上限。
4. 定义去重键生成函数：基于 `evidence_type` + 规范化 `params` 生成稳定字符串，不依赖字典插入顺序。
5. 校验函数：给定 `EvidenceRequest`，判断其 type 是否在白名单、params 是否匹配 schema、数量是否超限；返回合法/拒绝及稳定错误码（复用 `invalid_arguments`/`unsupported`）。
6. 未知 type、缺参、错误类型、超限、重复请求均按明确策略处理（拒绝并返回错误码）。

## 退出条件

- [ ] 三种证据类型的参数 schema 已定义且使用 `additionalProperties: false`。
- [ ] 合法请求通过校验。
- [ ] 未知 type 被拒绝（错误码 `unsupported`）。
- [ ] 缺必填参数被拒绝（错误码 `invalid_arguments`）。
- [ ] 类型错误（如 line_no 传字符串）被拒绝。
- [ ] 超限请求被拒绝。
- [ ] 重复请求（相同 dedup_key）被识别。
- [ ] 去重键不依赖字典插入顺序（测试用不同顺序构造相同 params，键一致）。
- [ ] 有离线测试覆盖上述所有情况。
- [ ] 测试不依赖文件系统或网络。

## 必须覆盖的测试

### 正常路径

- 三种类型的合法请求均通过校验。

### 边界情况

- params 含可选字段时的处理。
- 数量恰好等于上限时合法。
- 去重键对不同字典顺序稳定。

### 失败路径

- 未知 type 被拒绝。
- 缺必填参数被拒绝。
- 类型错误被拒绝。
- 超出数量上限被拒绝。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `review_tools.py` 的 schema 校验行为不受影响。

## 兼容性约束

- 不修改 `review_tools.py` 的 schema 校验函数实现。
- 不修改 `ReviewIssue` 或 `ContextBudget`。
- 复用 `review_tools.py` 的错误码词汇。

## 安全要求

- 参数 schema 不允许模型传入任意路径；路径校验在 collector 边界实现（本任务仅定义 schema 结构）。
- 去重键不得泄露宿主机绝对路径。

## 推荐验证命令

```bash
py -m pytest tests/test_verification_protocol.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 三种证据类型的 schema 与上限定义。
- 去重键生成逻辑。
- 实际调用链：本任务为纯协议定义，无外部调用链。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各类拒绝的错误码。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-3。

## 非目标

- 不承诺完整 `call_chain` 证据类型。
- 不实现具体 collector（M8-6 ~ M8-9）。
- 不调用模型。
- 不写 prompt。

## 后续任务

- M8-3（第一轮假设响应解析器）依赖 M8-1、M8-2。
- M8-4（假设生成 Prompt Builder）依赖 M8-2。
