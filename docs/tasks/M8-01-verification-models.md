# M8-1：假设、证据与 Verdict 数据模型

## 所属里程碑

M8：假设—证据—验证审查

## 背景

两轮验证协议需要强约束、低歧义的数据边界。第一轮生成假设，确定性 collector 收集证据，第二轮为每个假设生成 verdict。这些数据结构是 M8-2 到 M8-5、M8-12 到 M8-13 的协议基础。

本任务独立完成协议数据模型的定义，不写 prompt、不调用模型、不收集证据。它与 M8-2（证据白名单）的关系是：M8-2 在本任务定义的 `EvidenceRequest` 基础上登记证据类型。

本任务对最终两轮验证流水线的作用是：为假设、证据请求、证据项、verdict 和 trace 条目建立可序列化、可校验的数据类。

## 本轮目标

新增 `src/verification_protocol.py`，定义 `ReviewHypothesis`、`EvidenceRequest`、`EvidenceItem`、`VerificationVerdict`、`DialogueTraceEntry` 数据类，并实现字段校验。

## 当前真实状态

已确认存在：

- `src/schemas.py`：`ReviewIssue`（file_path/line_no/severity/category/message/suggestion/reason/confidence/evidence/source/placement）、`ContextBudget`、`PythonSymbol`。
- `src/review_tools.py`：`ToolResult` 使用 `EXPECTED_TOOL_ERROR_CODES = {"invalid_arguments", "forbidden", "not_found", "unavailable", "truncated", "unsupported"}`，并有 `INTERNAL_ERROR_CODE = "internal_error"`。
- `src/model_protocol.py`：`JSONValue` 类型别名。

尚未存在：

- `src/verification_protocol.py` 不存在。
- `tests/test_verification_protocol.py` 不存在。
- 无任何假设/证据/verdict 数据结构。

## 本轮范围

### 允许修改

- 新增 `src/verification_protocol.py`。
- 新增 `tests/test_verification_protocol.py`。

### 必要时允许修改

- 无。本任务仅新增文件。

### 禁止修改

- `src/schemas.py`（继续承载跨流水线共用模型；M8 协议不塞入其中）。
- 任何后续编号任务的范围。
- M7 控制器、prompt、collector、CLI、trace、Eval。
- 真实 provider 调用。
- 任意 shell 或代码执行。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义 `ReviewHypothesis`：含稳定 `hypothesis_id`（字符串）、`file_path`（字符串）、`line_no`（正整数或 0）、`description`（非空字符串）、`evidence_requests`（`EvidenceRequest` 列表，可为空）、`confidence`（[0,1] 浮点）。
2. 定义 `EvidenceRequest`：含 `evidence_type`（字符串，后续由 M8-2 白名单校验）、`params`（JSON 可序列化 dict）、`dedup_key`（字符串，由 type+params 派生，不依赖字典偶然顺序）。
3. 定义 `EvidenceItem`：含 `request`（关联的 `EvidenceRequest` 或其 `dedup_key`）、`status`（取值 `found`/`not_found`/`unavailable`/`truncated`/`unsupported`/`conflicting`）、`data`（JSON 可序列化，可为 None）、`actual_size`（非负整数）、`limit`（非负整数）、`truncated`（布尔）。
4. 定义 `VerificationVerdict`：含 `hypothesis_id`（字符串）、`status`（取值 `confirmed`/`rejected`/`inconclusive`）、`candidate_finding`（仅 `confirmed` 时允许携带，类型为 `ReviewIssue` 或等价 dict；`rejected`/`inconclusive` 不得携带）、`reason`（字符串）。
5. 定义 `DialogueTraceEntry`：含阶段标识、稳定 ID 关联、受控摘要字段（不存储完整 reasoning 或敏感源码）。
6. 所有数据类必须可安全 JSON 序列化（通过 `json.dumps` 验证）。
7. 校验逻辑：confidence 越界拒绝；verdict 状态非法拒绝；`rejected`/`inconclusive` 携带 candidate_finding 拒绝；越界字段拒绝。
8. 使用 `dataclass`，保持与项目现有风格一致。

## 退出条件

- [ ] `src/verification_protocol.py` 存在且定义了上述 5 个数据类。
- [ ] `confirmed` verdict 可携带合法候选 finding；`rejected`/`inconclusive` verdict 携带 finding 时被拒绝。
- [ ] confidence 越界（<0 或 >1 或 NaN/Inf）被拒绝。
- [ ] 所有数据类实例可通过 `json.dumps` 序列化。
- [ ] `EvidenceRequest.dedup_key` 对相同 type+params 稳定，不依赖字典插入顺序。
- [ ] 有离线测试覆盖合法构造、非法 confidence、非法 verdict 状态、rejected/inconclusive 携带 finding、不可序列化数据被拒绝。
- [ ] 测试不依赖文件系统或网络。

## 必须覆盖的测试

### 正常路径

- 合法 `ReviewHypothesis`、`EvidenceRequest`、`EvidenceItem`、`VerificationVerdict`（confirmed/rejected/inconclusive）均可构造。
- confirmed verdict 携带合法 `ReviewIssue`。

### 边界情况

- confidence 为 0.0 和 1.0（边界值合法）。
- `evidence_requests` 为空列表的 hypothesis 合法。
- `EvidenceItem.data` 为 None 合法。
- `dedup_key` 对 params 字典不同插入顺序保持一致。

### 失败路径

- confidence 为 -0.1、1.1、NaN、Inf 时构造被拒绝。
- verdict 状态为 "maybe" 等非法值时被拒绝。
- `rejected`/`inconclusive` verdict 携带 candidate_finding 时被拒绝。
- 不可 JSON 序列化的数据（如含 NaN 的结构）被拒绝。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- 现有 `schemas.py`、`validation.py` 测试不受影响。

## 兼容性约束

- 不修改 `ReviewIssue` 的字段或语义。
- 不修改 `ContextBudget`。
- 不修改 `review_tools.py` 的错误码集合（M8 可复用相同状态码词汇）。
- 不修改 `model_protocol.py`。

## 安全要求

- `DialogueTraceEntry` 不存储完整 reasoning 或敏感源码。
- 所有面向模型的路径字段后续在 collector 边界校验（本任务定义结构，不实现路径校验）。

## 推荐验证命令

```bash
py -m pytest tests/test_verification_protocol.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 新增数据类的字段与校验规则。
- 实际调用链：本任务为纯数据定义，无外部调用链。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：非法输入的拒绝行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-2。

## 非目标

- 不写 prompt。
- 不调用模型。
- 不收集证据。
- 不实现证据类型白名单（M8-2）。
- 不实现 parser（M8-3）。
- 不用一个大量可空字段的 `DialogueTurn` 承担全部职责。

## 后续任务

- M8-2（Evidence Type 白名单与参数 Schema）依赖 M8-1。
