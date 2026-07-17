# M8-22：Single 与 Verify 对照 Eval

## 所属里程碑

M8：假设—证据—验证审查

## 背景

验证 M8 是否真正降低目标误报，而不是简单拒绝更多问题。必须用固定、可复现评测对照 single/verify，同时报告 Recall、token、调用次数、时延与证据失败率，防止以大量 inconclusive 换取表面低误报。

本任务依赖 M8-20（模式开关）和 M8-21（误报导向 case）。它是 M8 的最终验收任务。

本任务对最终两轮验证流水线的作用是：提供可复现的 single/verify 对照证据，判定 M8 是否可合入。

## 本轮目标

在 `src/eval_runner.py` 中实现 single/verify 对照 Eval，在固定 commit、case、模型、随机性和预算下对照运行，保存逐 case finding、verdict、证据失败与成本数据，只分析结果不修改实现。

## 当前真实状态

已确认存在：

- `src/eval_runner.py`：`run_mode_comparison`（fixed/react 对照）、`build_comparison_record`（机器可读对照记录）、`_build_per_case_diff`（逐 case 差异）、`_build_react_provider`（react 脚本化 provider 工厂）。
- `src/eval_runner.py`：`run_eval`/`run_one_case` 支持 `review_mode` 参数。
- `evals/baselines/m7-0-fixed.json`：M7 fixed 基线。
- M8-0 将生成 single 基线。
- M8-20 将实现 `llm_review_mode`（single/verify）。
- M8-21 将新增误报导向 case。

尚未存在：

- single/verify 对照 Eval 函数（类似 `run_mode_comparison` 但针对 single/verify）。
- verify 模式的脚本化 provider 工厂（类似 `_build_react_provider` 但驱动两轮验证）。
- verdict/证据失败率的指标汇总。

## 本轮范围

### 允许修改

- `src/eval_runner.py`：新增 single/verify 对照函数与 CLI 参数。
- `tests/test_eval_runner.py`：新增对照测试。

### 必要时允许修改

- `src/eval_runner.py`：如需复用 `run_mode_comparison` 的结构，可提取共享逻辑，但不得破坏现有 fixed/react 对照行为。仅当现有函数无法表达 single/verify 对照时才允许修改。

### 禁止修改

- `src/review_service.py`、`src/verify_controller.py`。
- `src/verification_protocol.py`、`src/evidence_collectors.py`。
- `src/cli.py`（除非新增 eval CLI 参数）。
- 任何后续编号任务的范围。
- 不根据结果回调 prompt、阈值或 ground truth。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 实现 single/verify 对照函数，在同一 commit、case 集、模型配置、随机性与预算下运行 single/verify。
2. verify 模式使用脚本化 provider 工厂（驱动两轮验证，类似 `_build_react_provider` 但针对 verify）。
3. 按 case 输出差异并聚合指标。
4. 报告至少包含：Precision、FPR、Recall、F1、rejected/inconclusive 数、平均调用数、token、时延、估算成本、证据失败率。
5. 逐 case 标识新增误报、消除误报与新增漏报。
6. 保留机器可读结果和配置。
7. 若 Recall 明显下降或大量问题变 inconclusive，结论必须如实标注为未达标/需进一步实验。
8. 不在本轮修改实现、prompt 或 case 以追求分数。

## 退出条件

- [ ] single/verify 对照可运行，产出机器可读 JSON。
- [ ] 报告含 Precision、FPR、Recall、F1、rejected/inconclusive 数、平均调用数、token、时延、估算成本、证据失败率。
- [ ] 逐 case 标识新增误报、消除误报与新增漏报。
- [ ] 机器可读结果含配置（commit/case/模型/预算）。
- [ ] 若 Recall 明显下降或大量 inconclusive，结论标注为未达标（不伪造"提升"）。
- [ ] 不修改实现/prompt/case 以追求分数。
- [ ] 有离线测试覆盖对照函数的结构与指标计算。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- 对照函数运行后产出含 single/verify 指标块的 JSON。
- 逐 case 差异正确计算（以 case_id 为键关联）。

### 边界情况

- 全部 case 在两种模式下均零 finding 时，指标计算不崩溃。
- 部分 case 在 verify 模式下 inconclusive 时，inconclusive 数正确统计。

### 失败路径

- 若 verify 模式降级（如预算耗尽），对照仍完成且记录降级状态。
- 结论不伪造"提升"（若无证据支持，标注"未验证"）。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- 现有 fixed/react 对照行为不受影响。
- `py -m src.eval_runner --cases evals/cases --repo .` 可运行。

## 兼容性约束

- 不修改 `run_mode_comparison` 的 fixed/react 对照行为。
- 不修改 review_service 或 controller。
- 不修改现有 case。
- 不根据结果回调 prompt/阈值/ground truth。

## 安全要求

- 对照结果不泄露 prompt 原文或敏感源码。
- 使用 mock provider，不调用真实 API。

## 推荐验证命令

```bash
py -m pytest tests/test_eval_runner.py -v
```

```bash
py -m src.eval_runner --cases evals/cases --repo . --llm --llm-provider mock
```

## 完成时必须提供的证据

- 修改文件清单。
- 对照函数与指标清单。
- 实际调用链：run_single_verify_comparison → run_eval(single) + run_eval(verify) → per_case_diff。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：verify 降级时的对照行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8 里程碑完成，进入验收。

## 非目标

- 不根据结果回调 prompt、阈值或 ground truth。
- 不在本轮修改实现、prompt 或 case。
- 不修改 fixed/react 对照。
- 不调用真实 API。

## 后续任务

- M8 里程碑完成。根据 M8 验收标准（`新增里程碑.md` 0.8 节）进行里程碑级验收。
