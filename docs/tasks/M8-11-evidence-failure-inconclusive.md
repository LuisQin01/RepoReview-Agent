# M8-11：证据失败与 inconclusive 语义

## 所属里程碑

M8：假设—证据—验证审查

## 背景

"没有证据"与"系统无法获得证据"是关键差别。证据不足或收集失败绝不能自动确认 finding。本任务统一定义各失败状态的可验证含义。

本任务依赖 M8-10（证据收集编排器）。它与 M8-14（两轮 controller）和 M8-16（证据失败降级）的关系是：M8-14/M8-16 使用本任务定义的语义决定 hypothesis 的处理方式。

本任务对最终两轮验证流水线的作用是：确保证据不可用时保守地输出 inconclusive，而非确认未经验证的假设。

## 本轮目标

在 `src/evidence_collectors.py` 中统一 `not_found`、`unavailable`、`conflicting`、`truncated`、`unsupported` 状态的可验证含义，规定证据不足/失败绝不自动确认 finding。

## 当前真实状态

已确认存在：

- M8-1 将定义 `EvidenceItem.status`。
- M8-6 将定义 collector 失败返回结构化 `EvidenceItem`。
- M8-10 将实现编排器。
- `src/review_tools.py`：`EXPECTED_TOOL_ERROR_CODES` 已包含 `not_found`/`unavailable`/`truncated`/`unsupported`；M8 新增 `conflicting` 状态。

尚未存在：

- 证据失败状态的统一语义定义与编排层测试。
- "证据不足不自动确认"的显式规则实现。

## 本轮范围

### 允许修改

- `src/evidence_collectors.py`：新增/完善失败状态语义与编排层判断逻辑。
- `tests/test_evidence_collectors.py`：新增失败状态测试。

### 必要时允许修改

- `src/verification_protocol.py`：如需在 `EvidenceItem.status` 枚举中补充 `conflicting`，仅限最小改动。

### 禁止修改

- `src/review_tools.py`（不修改其错误码集合）。
- 任何后续编号任务的范围。
- controller、prompt、CLI、trace、Eval。
- M7 自由循环控制器。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 定义各失败状态的可验证含义：
   - `not_found`：证据目标不存在（如符号未找到）。
   - `unavailable`：系统无法获得证据（如 AST 解析失败、读取失败）。
   - `conflicting`：证据有多个候选无法确定（如多 import 目标）。
   - `truncated`：证据因预算被截断，信息不完整。
   - `unsupported`：证据类型或文件类型不支持。
2. 规定：证据不足/失败时 hypothesis 不能产生 confirmed finding。
3. 规定：全部所需证据不可用时 hypothesis 应在后续被标记 inconclusive（M8-16 在 controller 落实）。
4. 规定：部分可用时可以进入验证轮，但必须携带缺失状态。
5. collector 异常不崩溃编排器。
6. trace 可区分每种失败原因（为 M8-19 预留，本任务定义语义，不实现 trace）。
7. 无可用证据的 hypothesis 绝不产生 confirmed finding（测试断言）。

## 退出条件

- [ ] 每种失败状态（not_found/unavailable/conflicting/truncated/unsupported）有构造测试。
- [ ] 每种状态在编排器中有对应处理测试。
- [ ] collector 异常不崩溃编排器。
- [ ] 无可用证据的 hypothesis 不能产生 confirmed finding（测试断言）。
- [ ] 全部证据 `unavailable` 时 hypothesis 被标记为不可验证（为 inconclusive 预留）。
- [ ] 部分证据可用时仍可进入后续阶段，但携带缺失状态。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不依赖网络。

## 必须覆盖的测试

### 正常路径

- 全部证据 `found` 时 hypothesis 可正常进入验证轮。

### 边界情况

- 混合状态（部分 found、部分 unavailable）时携带缺失状态。
- 全部 `truncated` 时信息不完整，不自动确认。

### 失败路径

- 全部 `unavailable` 时 hypothesis 不可验证。
- 全部 `not_found` 时 hypothesis 不可自动确认。
- `conflicting` 时不自动确认。
- collector 异常不崩溃。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `review_tools.py`、`verification_protocol.py` 行为不受影响。

## 兼容性约束

- 不修改 `review_tools.py` 的错误码集合。
- 不修改 `ReviewIssue` 或 `ContextBudget`。
- 不修改 single 默认路径。

## 安全要求

- 失败状态不泄露宿主机路径或内部诊断。
- 截断状态明确标记"信息不完整"，不伪称内容完整。

## 推荐验证命令

```bash
py -m pytest tests/test_evidence_collectors.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- 各失败状态语义定义。
- 实际调用链：orchestrator → 状态判断 → hypothesis 标记。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：各状态的后果。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-12。

## 非目标

- 不新增模型调用。
- 不把收集异常吞为肯定结论。
- 不实现 controller 降级逻辑（M8-16）。
- 不实现 trace 记录（M8-19）。

## 后续任务

- M8-12（第二轮 Verdict 响应解析器）依赖 M8-1、M8-11。
- M8-16（证据失败降级）依赖 M8-11、M8-14。
