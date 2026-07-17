# M8-13：验证 Prompt Builder

## 所属里程碑

M8：假设—证据—验证审查

## 背景

第二轮必须仅依据"原始假设 + 已收集证据"作出逐项 verdict。prompt 必须以稳定 hypothesis ID 关联假设与证据，标注证据状态和截断，明确不能引入证据外事实。

本任务依赖 M8-10（证据收集编排器）、M8-11（证据失败语义）和 M8-12（verdict 解析器）。它与 M8-14（两轮 controller）的关系是：M8-14 使用本 builder 构造第二轮 prompt。

本任务对最终两轮验证流水线的作用是：从输入侧约束第二轮只基于已收集证据作出 verdict。

## 本轮目标

在 `src/verification_protocol.py` 中实现第二轮验证 prompt builder，构建有稳定 hypothesis ID 的验证 prompt，标注证据状态和截断。

## 当前真实状态

已确认存在：

- `src/llm_reviewer.py`：`_apply_prompt_budget` 做确定性截断；`_sanitize_changed_file_for_prompt` 做脱敏。
- M8-1 将定义 `ReviewHypothesis`、`EvidenceItem`。
- M8-10 将实现证据收集编排器（产出按 hypothesis ID 分组的证据）。
- M8-11 将定义证据失败状态语义。
- M8-12 将定义 verdict 输出 schema。

尚未存在：

- 验证 prompt builder。
- 证据状态在 prompt 中的标注方式。

## 本轮范围

### 允许修改

- `src/verification_protocol.py`：新增验证 prompt builder。
- `tests/test_verification_protocol.py`：新增 builder 测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/llm_reviewer.py`（只读复用 `_apply_prompt_budget`，不修改）。
- `src/review_tools.py`。
- 任何后续编号任务的范围。
- provider 调用、collector、controller、CLI、trace、Eval。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. builder 接收假设列表（含 `hypothesis_id`）、按 hypothesis ID 分组的 EvidenceItem 列表和 max_prompt_chars，输出 prompt 字符串。
2. prompt 以稳定 hypothesis ID 关联每个假设与其证据，不按列表位置猜测。
3. 每条证据标注状态（found/not_found/unavailable/conflicting/truncated/unsupported）和截断标记。
4. prompt 明确声明：不能引入证据外事实、每项必须返回 verdict、只有 confirmed 可给 finding。
5. prompt 声明输出 schema（verdicts 列表，每项含 hypothesis_id、status、candidate_finding、reason）。
6. 缺失证据在 prompt 中显式出现（不隐藏缺失）。
7. 截断证据显式标注"信息不完整"。
8. 复用 `_apply_prompt_budget` 做确定性截断，prompt 不超过配置预算。
9. 敏感源码不进入 prompt（证据 data 在 collector 边界已脱敏）。

## 退出条件

- [ ] prompt 以 hypothesis ID 关联假设与证据（测试验证 ID 出现且正确配对）。
- [ ] 缺失证据在 prompt 中显式出现。
- [ ] 截断证据标注"信息不完整"。
- [ ] prompt 包含"不能引入证据外事实"声明。
- [ ] prompt 包含"只有 confirmed 可给 finding"声明。
- [ ] prompt 包含输出 schema 约束。
- [ ] prompt 长度不超过 max_prompt_chars。
- [ ] 有离线测试验证上述约束，不通过脆弱的整段 prompt 字符串比较。
- [ ] 测试不依赖文件系统或网络。

## 必须覆盖的测试

### 正常路径

- 两假设各含 found 证据，prompt 正确关联 ID 与证据。

### 边界情况

- 部分证据 unavailable 时 prompt 显式标注缺失。
- 证据 truncated 时 prompt 标注"信息不完整"。
- 超长证据被截断到预算内。

### 失败路径

- 敏感源码不进入 prompt。
- 无证据的假设在 prompt 中仍出现（标注无证据）。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `llm_reviewer.py` 的 `build_llm_prompt` 行为不受影响。

## 兼容性约束

- 不修改 `build_llm_prompt` 或 `_apply_prompt_budget`。
- 不修改 `ContextBudget` 语义。
- 不修改 `ReviewIssue`。

## 安全要求

- 敏感源码不进入 prompt。
- prompt 不包含宿主机绝对路径。
- 证据 data 在进入 prompt 前已脱敏。

## 推荐验证命令

```bash
py -m pytest tests/test_verification_protocol.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- prompt 包含的约束清单与 ID 关联方式。
- 实际调用链：本任务为纯 prompt 构造，无外部调用链。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：截断与缺失证据的处理。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-14。

## 非目标

- 不执行控制器。
- 不保存内部推理。
- 不调用模型。
- 不收集证据。

## 后续任务

- M8-14（两轮 Happy Path 控制器）依赖 M8-5、M8-10、M8-12、M8-13。
