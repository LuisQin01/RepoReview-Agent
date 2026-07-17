# M8-12：第二轮 Verdict 响应解析器

## 所属里程碑

M8：假设—证据—验证审查

## 背景

第二轮模型输出必须被严格限制为逐 hypothesis 的三态 verdict（confirmed/rejected/inconclusive）。解析器是"永不信任模型文本"原则在第二轮的执行点。

本任务依赖 M8-1（数据模型）和 M8-11（证据失败语义）。它与 M8-14（两轮 controller）的关系是：M8-14 使用本解析器解析第二轮响应。

本任务对最终两轮验证流水线的作用是：确保只有 confirmed 且携带合法候选 finding 的 verdict 能进入最终校验链，rejected/inconclusive 绝不产出 finding。

## 本轮目标

在 `src/verification_protocol.py` 中实现第二轮 verdict 响应解析器，将不可信 JSON 严格转换为逐 hypothesis 的 `VerificationVerdict` 列表。

## 当前真实状态

已确认存在：

- `src/validation.py`：`validate_llm_response` 解析 findings JSON（修复策略）；`validate_issue_locations` 做 inline/summary 校验。
- `src/llm_reviewer.py`：`parse_llm_response` 将 finding dict 转 `ReviewIssue`。
- `src/review_tools.py`：`FinishReview._validated_issue` 展示了候选 finding 经既有 schema + 位置校验的模式。
- M8-1 将定义 `VerificationVerdict`（status + candidate_finding）。

尚未存在：

- verdict 响应解析器。
- hypothesis ID 覆盖与唯一性校验逻辑。

## 本轮范围

### 允许修改

- `src/verification_protocol.py`：新增 verdict 解析器与解析结果结构。
- `tests/test_verification_protocol.py`：新增解析器测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/validation.py`（只读复用校验函数，不修改语义）。
- `src/llm_reviewer.py`。
- `src/review_tools.py`。
- 任何后续编号任务的范围。
- provider 调用、collector、controller、CLI、trace、Eval。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义解析结果结构，含 `verdicts`（合法 `VerificationVerdict` 列表）、`errors`（解析错误列表）、`valid`（布尔）。
2. 解析顶层结构：必须是 dict 且含 `verdicts` 字段且为 list；否则整体 `valid=False`。
3. 逐条解析 verdict：验证 `hypothesis_id`（字符串）、`status`（confirmed/rejected/inconclusive）。
4. confirmed verdict 必须携带合法候选 finding：在解析边界使用既有 schema（`validate_llm_response`）校验 finding 结构，最终位置校验仍保持在统一校验链（M8-17 在 controller 落实）。
5. rejected/inconclusive verdict 携带 finding 时被拒绝。
6. hypothesis ID 覆盖与唯一性：每个输入假设必须恰好有一个 verdict；重复 verdict、漏 verdict 均记录错误。
7. 越界 hypothesis ID（不在输入假设中）的 verdict 被拒绝。
8. 非法 finding 的 confirmed verdict 被拒绝（该 verdict 丢弃并记录错误）。
9. 解析器不调用模型，不绕过/复制 finding validation。
10. 关联只能按 hypothesis ID 建立，不依赖列表位置或模型返回顺序。

## 退出条件

- [ ] confirmed verdict 携带合法候选 finding 时解析成功。
- [ ] rejected verdict 解析成功（不得携带 finding）。
- [ ] inconclusive verdict 解析成功（不得携带 finding）。
- [ ] rejected/inconclusive 携带 finding 时被拒绝。
- [ ] 越界 hypothesis ID 的 verdict 被拒绝。
- [ ] 重复 verdict 被拒绝。
- [ ] 漏 verdict 被记录错误。
- [ ] 非法 finding 的 confirmed verdict 被拒绝。
- [ ] 顶层类型错误（非 dict、非 list）返回 `valid=False`。
- [ ] 坏 JSON 返回 `valid=False`。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不依赖文件系统或网络。

## 必须覆盖的测试

### 正常路径

- 三条假设分别 confirmed/rejected/inconclusive，全部解析成功。
- confirmed verdict 携带合法 finding。

### 边界情况

- 单假设单 confirmed verdict。
- 全部 rejected。

### 失败路径

- rejected/inconclusive 携带 finding 被拒绝。
- 越界 ID 被拒绝。
- 重复 verdict 被拒绝。
- 漏 verdict 记录错误。
- 非法 finding 的 confirmed 被拒绝。
- 坏 JSON/非 dict/非 list 整体失败。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `validation.py` 的既有校验行为不受影响。

## 兼容性约束

- 不修改 `validate_llm_response` 或 `validate_issue_locations`。
- 不修改 `ReviewIssue`。
- 不绕过/复制 finding validation。

## 安全要求

- 解析器不读取文件系统。
- 错误记录不回显完整模型原始文本。

## 推荐验证命令

```bash
py -m pytest tests/test_verification_protocol.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 解析结果结构与 ID 覆盖/唯一性规则。
- 实际调用链：本任务为纯解析，无外部调用链。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各类解析失败的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-13。

## 非目标

- 不调用模型。
- 不绕过/复制 finding validation。
- 不实现 controller（M8-14）。
- 不实现验证 prompt（M8-13）。

## 后续任务

- M8-13（验证 Prompt Builder）依赖 M8-10、M8-11、M8-12。
- M8-14（两轮 Happy Path 控制器）依赖 M8-5、M8-10、M8-12、M8-13。
