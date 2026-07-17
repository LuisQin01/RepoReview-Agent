# M8-3：第一轮假设响应解析器

## 所属里程碑

M8：假设—证据—验证审查

## 背景

第一轮模型输出是不可信 JSON，必须严格转换为有限的合法假设集合。解析器是"永不信任模型文本"原则在第一轮的执行点。

本任务依赖 M8-1（数据模型）和 M8-2（证据白名单）。它与 M8-5（第一轮调用）的关系是：M8-5 串联 builder、provider 和本解析器。

本任务对最终两轮验证流水线的作用是：确保只有结构合法、证据请求合规的假设能进入证据收集阶段，被拒绝的假设绝不流入后续。

## 本轮目标

在 `src/verification_protocol.py` 中实现第一轮假设响应解析器，将不可信 JSON 严格转换为有限的有效 `ReviewHypothesis` 列表，并记录解析错误。

## 当前真实状态

已确认存在：

- `src/validation.py`：`validate_llm_response` 解析 findings JSON，采用"修复并记录"策略；`ValidationResult` 含 valid/repaired/errors。本任务需采用不同策略（见功能要求）。
- `src/llm_reviewer.py`：`parse_llm_response` 将 finding dict 转 `ReviewIssue`。
- M8-1 将定义 `ReviewHypothesis`、`EvidenceRequest`。
- M8-2 将定义证据类型注册表与校验函数。

尚未存在：

- 假设响应解析器。
- 假设响应的 JSON schema 约定。

## 本轮范围

### 允许修改

- `src/verification_protocol.py`：新增假设响应解析器与解析结果结构。
- `tests/test_verification_protocol.py`：新增解析器测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/validation.py`（只读复用 JSON 解析经验，不修改既有 findings 解析语义）。
- `src/llm_reviewer.py`。
- 任何后续编号任务的范围。
- provider 调用、prompt、collector、controller、CLI、trace、Eval。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义解析结果结构（如 `HypothesisParseResult`），含 `hypotheses`（合法 `ReviewHypothesis` 列表）、`errors`（解析错误列表）、`valid`（布尔）。
2. 解析顶层结构：必须是 dict 且含 `hypotheses` 字段且为 list；否则整体 `valid=False`。
3. 逐条解析假设：验证 `hypothesis_id`（非空字符串）、`file_path`（字符串）、`line_no`（整数）、`description`（非空字符串）、`confidence`（[0,1] 有限值）、`evidence_requests`（list）。
4. 对每条 `evidence_requests` 使用 M8-2 的白名单校验：未知 type、缺参、错误类型、超限均拒绝该请求。
5. 部分无效时的语义：定义一致策略——保留合法假设与合法证据请求，记录被拒绝项的错误（含稳定索引或 ID），不因单条非法而全拒绝。
6. 坏 JSON、非 dict 顶层、非 list hypotheses 均为整体 `valid=False`，返回空假设列表。
7. 解析器不调用 provider、不收集证据。
8. 返回的假设数量受白名单数量上限约束（M8-2）。

## 退出条件

- [ ] 合法假设列表可被解析为 `ReviewHypothesis` 实例。
- [ ] 坏 JSON 返回 `valid=False` 且空假设列表。
- [ ] 非 dict 顶层返回 `valid=False`。
- [ ] `hypotheses` 非 list 返回 `valid=False`。
- [ ] 缺字段的假设被拒绝并记录错误，合法假设保留。
- [ ] 非法 confidence 被拒绝。
- [ ] 未知证据 type 的请求被拒绝并记录。
- [ ] 部分非法假设不会导致全部假设被拒绝。
- [ ] 超出数量上限的假设被拒绝。
- [ ] 有离线测试覆盖合法/空列表/非 dict/非 list/坏 JSON/缺字段/非法 confidence/未知 type/部分非法/超限。
- [ ] 测试明确断言被拒绝项不流入 hypotheses 列表。
- [ ] 测试不依赖文件系统或网络。

## 必须覆盖的测试

### 正常路径

- 两条合法假设（各含合法证据请求）全部解析成功。
- 空 hypotheses 列表合法（valid=True, hypotheses=[]）。

### 边界情况

- confidence 为 0.0 和 1.0 合法。
- hypothesis_id 重复时的处理策略（拒绝重复或重新赋值，需明确且测试）。
- 单假设含多条不同类型证据请求。

### 失败路径

- 坏 JSON、非 dict 顶层、非 list hypotheses 整体失败。
- 缺字段、非法 confidence 的假设被拒绝但合法假设保留。
- 未知证据 type 被拒绝。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `validation.py` 的既有 findings 解析行为不受影响。

## 兼容性约束

- 不修改 `validate_llm_response` 的语义。
- 不修改 `ReviewIssue`。
- 复用 M8-2 的白名单校验与错误码词汇。

## 安全要求

- 解析器不读取文件系统。
- 错误记录不回显完整模型原始文本（仅记录索引/ID/原因）。

## 推荐验证命令

```bash
py -m pytest tests/test_verification_protocol.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 解析结果结构与部分无效语义。
- 实际调用链：本任务为纯解析，无外部调用链。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各类解析失败的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-4。

## 非目标

- 不调用 provider。
- 不收集证据。
- 不写 prompt（M8-4）。
- 不实现第一轮调用编排（M8-5）。

## 后续任务

- M8-4（假设生成 Prompt Builder）依赖 M8-2。
- M8-5（第一轮假设生成调用）依赖 M8-3、M8-4。
