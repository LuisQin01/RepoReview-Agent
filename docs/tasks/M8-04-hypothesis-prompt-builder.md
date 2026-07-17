# M8-4：假设生成 Prompt Builder

## 所属里程碑

M8：假设—证据—验证审查

## 背景

第一轮的目标是让模型只提出待验证假设，而不是直接声称最终 finding。prompt 必须明确约束模型只能输出 schema 所需字段，并声明证据白名单与禁止行为。

本任务依赖 M8-2（证据白名单）。它与 M8-5（第一轮调用）的关系是：M8-5 使用本 builder 构造的 prompt 调用模型。

本任务对最终两轮验证流水线的作用是：从输入侧约束第一轮只产出假设，而非结论。

## 本轮目标

在 `src/verification_protocol.py` 中实现第一轮假设生成 prompt builder，将 diff、预算内上下文与规则结果构造为 prompt，约束模型输出假设 schema。

## 当前真实状态

已确认存在：

- `src/llm_reviewer.py`：`build_llm_prompt` 构造单轮 findings prompt；`_apply_prompt_budget` 做确定性截断；`_sanitize_changed_file_for_prompt` 做敏感文件脱敏。本任务可复用截断与脱敏逻辑。
- `src/schemas.py`：`ContextBudget.max_prompt_chars` 控制预算。
- M8-2 将定义证据白名单与 schema。

尚未存在：

- 假设生成 prompt builder。
- 假设输出 schema 的 prompt 表达。

## 本轮范围

### 允许修改

- `src/verification_protocol.py`：新增假设生成 prompt builder。
- `tests/test_verification_protocol.py`：新增 builder 测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/llm_reviewer.py`（只读复用 `_apply_prompt_budget`、`_sanitize_changed_file_for_prompt`，不修改）。
- 任何后续编号任务的范围。
- provider 调用、parser、collector、controller、CLI、trace、Eval。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. builder 接收 changed_files、contexts、rule_issues 和 max_prompt_chars，输出 prompt 字符串。
2. prompt 必须包含：声明输入不可信、假设数量上限、证据白名单（三种类型及其参数）、禁止请求任意文件/执行代码/读取完整仓库。
3. prompt 必须声明输出只允许假设 schema 所需字段（hypothesis_id、file_path、line_no、description、confidence、evidence_requests）。
4. 复用 `_sanitize_changed_file_for_prompt` 对敏感文件 diff 脱敏。
5. 复用 `_apply_prompt_budget` 做确定性截断，超长时保留可见截断标记。
6. 截断后 prompt 长度严格不超过 max_prompt_chars。
7. 用户输入（diff/context）与系统指令明确隔离，防止 prompt 注入。

## 退出条件

- [ ] builder 输出 prompt 字符串，长度严格 <= max_prompt_chars。
- [ ] prompt 包含证据白名单三种类型的声明。
- [ ] prompt 包含禁止请求任意文件/执行代码的声明。
- [ ] prompt 包含假设数量上限声明。
- [ ] prompt 包含输出 schema 字段约束声明。
- [ ] 超长输入被截断且带可见标记。
- [ ] 敏感文件 diff 被脱敏（不进入 prompt）。
- [ ] 有离线测试验证上述约束，不通过脆弱的整段 prompt 字符串比较，而是检查关键约束子串与长度。
- [ ] 测试不依赖文件系统或网络。

## 必须覆盖的测试

### 正常路径

- builder 对合法输入生成 prompt，包含关键约束子串。

### 边界情况

- 超长输入被截断到预算内，且含截断标记。
- 空规则结果（rule_issues=[]）时 prompt 仍合法。
- 空 contexts 时 prompt 仍合法。

### 失败路径

- 敏感文件路径的 diff 不出现在 prompt 中（含占位符）。
- max_prompt_chars 极小时不崩溃（复用 `_apply_prompt_budget` 的边界处理）。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `llm_reviewer.py` 的 `build_llm_prompt` 行为不受影响。

## 兼容性约束

- 不修改 `build_llm_prompt` 或 `_apply_prompt_budget`。
- 不修改 `ContextBudget` 语义。
- 不修改 `ReviewIssue`。

## 安全要求

- 敏感文件 diff 必须被脱敏，不进入 prompt。
- prompt 不得包含宿主机绝对路径。
- 用户输入与系统指令隔离。

## 推荐验证命令

```bash
py -m pytest tests/test_verification_protocol.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- prompt 包含的约束清单。
- 实际调用链：本任务为纯 prompt 构造，无外部调用链。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：截断与脱敏行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-5。

## 非目标

- 不执行第一轮模型调用（M8-5）。
- 不写第二轮验证 prompt（M8-13）。
- 不修改单轮 findings prompt。
- 不收集证据。

## 后续任务

- M8-5（第一轮假设生成调用）依赖 M8-3、M8-4。
