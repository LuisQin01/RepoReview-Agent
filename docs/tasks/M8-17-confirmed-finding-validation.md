# M8-17：Confirmed Finding 接入既有校验链

## 所属里程碑

M8：假设—证据—验证审查

## 背景

多轮模式不能有第二套、标准更低的 finding 输出路径。confirmed 候选 finding 必须送入同一 schema、changed-hunk/scope 校验、去重、排序与报告器链。rejected 结果永不进入该入口。

本任务依赖 M8-14（两轮 controller）和既有 `validation`/`ranker` 行为。它与 M8-20（模式开关）的关系是：M8-20 将 verify 模式接入 service，最终 finding 仍走本任务的校验链。

本任务对最终两轮验证流水线的作用是：保证 verify 模式的 finding 输出标准不低于 single 模式。

## 本轮目标

在 `src/verify_controller.py` 中将 confirmed 候选 finding 送入既有 schema、changed-hunk/scope 校验、去重、排序与报告器链，确保 rejected 结果永不进入该入口。

## 当前真实状态

已确认存在：

- `src/validation.py`：`validate_issue_locations` 做 inline/summary 校验（行号是否落在 changed hunk）。
- `src/review_service.py`：`validate_issues` 做 list 类型断言 + `validate_issue_locations`；pipeline 步骤 6 `validate_output` 统一校验。
- `src/review_tools.py`：`FinishReview._validated_issue` 展示了候选 finding 经既有 schema + 位置校验后要求 `placement == "inline"` 的模式。
- `src/reporter.py`：`render_markdown_report`、`render_json_report`（未读但从 import 推断存在）。
- M8-14 将实现 `VerifyController`，confirmed verdict 携带候选 finding。

尚未存在：

- verify controller 到既有校验链的显式接入。
- verify 模式 finding 的 provenance 标记。

## 本轮范围

### 允许修改

- `src/verify_controller.py`：新增 confirmed finding 到既有校验链的接入逻辑。
- `tests/test_verify_controller.py`：新增校验链接入测试。

### 必要时允许修改

- 无。本任务复用既有 `validate_issues`/`validate_issue_locations`，不新增第二套校验。

### 禁止修改

- `src/validation.py`（只读复用，不修改校验语义）。
- `src/review_service.py`（本任务不接入 service）。
- `src/reporter.py`。
- `src/react_controller.py`。
- 任何后续编号任务的范围。
- 模式开关（M8-20）、trace（M8-19）。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. confirmed 候选 finding 送入 `validate_issues`（list 类型断言 + `validate_issue_locations`）。
2. 行号不在 changed hunk 的 confirmed finding 按既有语义处理（降级为 summary 或被拒绝，保持与 single 一致）。
3. 非法 severity 的 confirmed finding 按既有语义处理（修复或拒绝，保持与 single 一致）。
4. scope 外文件的 confirmed finding 按既有语义处理。
5. 重复 confirmed finding 按既有去重语义处理。
6. rejected verdict 绝不调用 validator。
7. 最终报告 provenance 可区分 verify 模式来源（如 `source="llm"` + 等价标记）。
8. 不在多轮 controller 复制 validation 逻辑。
9. 不改变既有 rule/LLM finding 语义。

## 退出条件

- [ ] 行号不在 changed hunk 的 confirmed finding 按既有语义处理（测试断言 placement 结果）。
- [ ] 非法 severity 的 confirmed finding 按既有语义处理。
- [ ] scope 外文件的 confirmed finding 按既有语义处理。
- [ ] 重复 confirmed finding 按既有去重语义处理。
- [ ] rejected verdict 不调用 validator（测试通过 spy/mock 断言 validator 调用次数）。
- [ ] 最终报告 provenance 可区分 verify 模式来源。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- confirmed finding 位置在 changed hunk 内，通过校验，placement=inline。
- 多条 confirmed finding 经去重后保留正确数量。

### 边界情况

- confirmed finding 位置不在 changed hunk，按既有语义降级或拒绝。
- 重复 confirmed finding 被去重。

### 失败路径

- 非法 severity 的 confirmed finding 按既有语义处理。
- scope 外文件的 confirmed finding 被拒绝。
- rejected verdict 不调用 validator。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `validation.py` 的既有校验行为不受影响。
- single 模式的 finding 校验行为不受影响。
- fixed/react 模式行为不受影响。

## 兼容性约束

- 不修改 `validate_issues` 或 `validate_issue_locations` 语义。
- 不修改 `ReviewIssue` 的字段。
- 不复制 validation 逻辑到 verify controller。
- 不改变既有 rule/LLM finding 语义。
- 不改变 single/fixed/react 默认路径。

## 安全要求

- confirmed finding 经校验后不泄露敏感信息（沿用既有脱敏）。
- rejected 结果不进入校验链或报告。

## 推荐验证命令

```bash
py -m pytest tests/test_verify_controller.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- confirmed finding 到校验链的接入方式。
- 实际调用链：confirmed verdict → validate_issues → validate_issue_locations → 报告。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各类非法 confirmed finding 的处理。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-18A。

## 非目标

- 不在多轮 controller 复制 validation。
- 不改变既有 rule/LLM finding 语义。
- 不接入 review_service 或 CLI（M8-20）。
- 不实现 trace（M8-19）。

## 后续任务

- M8-18A（两轮调用预算）依赖 M8-14。
- M8-20（single/verify 模式开关）依赖 M8-19，最终 finding 走本任务的校验链。
