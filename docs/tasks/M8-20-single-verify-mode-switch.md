# M8-20：single / verify 模式开关

## 所属里程碑

M8：假设—证据—验证审查

## 背景

在同一 review service 中显式、可对照地选择单轮或验证模式。`single` 保持当前行为不变，`verify` 为新增的两轮验证模式。verify 必须保持显式 opt-in，失败不可伪装成 single 成功。

本任务依赖 M8-19（多轮 trace）。它与 M8-22（single/verify 对照 Eval）的关系是：M8-22 使用本任务的模式开关运行对照。

本任务对最终两轮验证流水线的作用是：将 verify 控制器接入 service 与 CLI，使两种模式可对照运行。

## 本轮目标

在 `src/review_service.py`、`src/cli.py` 和 `src/agent_state.py` 中增加受限 `llm_review_mode`（`single`/`verify`）配置、CLI 参数和 state/report 元数据，两种模式共用 ContextBudget、finding validation、ranker 和输出。

## 当前真实状态

已确认存在：

- `src/review_service.py`：`_VALID_REVIEW_MODES = frozenset({"fixed", "react"})`；`ReviewRequest` 有 `review_mode` 字段；`_run_react_review` 展示了模式分流模式；pipeline 步骤 5 按 `review_mode` 分流。
- `src/cli.py`：`--review-mode` choices=["fixed", "react"]，default="fixed"。
- `src/agent_state.py`：`ReviewState` 有 `review_mode` 字段。
- M8-14 将实现 `VerifyController`；M8-19 将实现多轮 trace。

尚未存在：

- `llm_review_mode`（`single`/`verify`）配置。
- `_run_verify_review` 函数。
- CLI 的 `--llm-review-mode` 参数。
- verify 模式的 state/report 元数据。

注意：当前 `review_mode`（fixed/react）与 M8 的 `llm_review_mode`（single/verify）是不同维度。`single` 对应当前的 `use_llm=True, review_mode="fixed"` 单次 LLM 路径；`verify` 对应新增的两轮路径。实施时需明确二者关系，避免混淆。

## 本轮范围

### 允许修改

- `src/review_service.py`：新增 `llm_review_mode` 字段、`_run_verify_review` 函数、模式分流逻辑。
- `src/cli.py`：新增 `--llm-review-mode` 参数。
- `src/agent_state.py`：新增 `llm_review_mode` 字段。
- `tests/test_review_service.py`：新增模式开关测试。
- `tests/test_cli_smoke.py`：新增 CLI smoke 测试。

### 必要时允许修改

- `src/verify_controller.py`：如需适配 service 调用接口，仅限最小改动。

### 禁止修改

- `src/react_controller.py`。
- `src/verification_protocol.py`、`src/evidence_collectors.py`。
- `src/validation.py`、`src/reporter.py`。
- 任何后续编号任务的范围。
- single 默认行为（不得修改）。
- fixed/react 模式行为。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 新增 `llm_review_mode` 配置，取值 `single`（默认）/`verify`，非法值被拒绝。
2. `single` 模式行为与当前 `use_llm=True, review_mode="fixed"` 完全一致（不修改默认 single 行为）。
3. `verify` 模式调用 `_run_verify_review`，串联 `VerifyController`。
4. 两种模式共用 `ContextBudget`、finding validation（`validate_issues`）、ranker 和 reporter。
5. verify 失败不可伪装成 single 成功（显式降级状态）。
6. 模式写入 state 与 report 元数据（trace/report 可见模式标识）。
7. CLI 新增 `--llm-review-mode` 参数，默认 `single`。
8. 无参数时仍为 single（默认行为不变）。

## 退出条件

- [ ] 默认（无参数）仍为 single，行为与当前一致。
- [ ] 非法 `llm_review_mode` 值被拒绝。
- [ ] verify 模式调用 `VerifyController`。
- [ ] 两种模式有离线 smoke test（使用 mock provider）。
- [ ] 模式在 trace/report 元数据可见。
- [ ] verify 失败时结果状态与错误可见，且没有未经验证 finding。
- [ ] verify 失败不伪装成 single 成功。
- [ ] 两种模式的最终 finding 均经 `validate_issues` 校验。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- single 模式 smoke test，行为与当前一致。
- verify 模式 smoke test（mock provider），产出经校验的 finding。

### 边界情况

- 无参数时默认 single。
- 非法模式值被拒绝。

### 失败路径

- verify 失败时显式降级状态，不伪装 single 成功。
- verify 失败时无未经验证 finding。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- single 默认路径行为不回归（关键断言不变）。
- fixed/react 模式行为不回归。
- CLI smoke test 现有断言不受影响。

## 兼容性约束

- 不修改 single 默认行为。
- 不修改 fixed/react 模式行为。
- 不修改 `validate_issues` 或 `reporter`。
- 不修改 `ContextBudget`。
- verify 必须保持显式 opt-in。

## 安全要求

- verify 失败状态不泄露 provider 诊断。
- 模式标识不暴露敏感配置。

## 推荐验证命令

```bash
py -m pytest tests/test_review_service.py tests/test_cli_smoke.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- `llm_review_mode` 配置与分流逻辑。
- 实际调用链：CLI → ReviewRequest → ReviewService → _run_verify_review → VerifyController → validate_issues。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：verify 失败的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-21。

## 非目标

- 不修改默认 single 行为。
- 不让 verify 失败被报告为 single 成功。
- 不修改 fixed/react 模式。
- 不修改 validation/reporter 语义。

## 后续任务

- M8-21（误报导向 Eval Cases）依赖 M8-0，不依赖 controller 完成。
- M8-22（Single 与 Verify 对照 Eval）依赖 M8-20、M8-21。
