# M8-0：单轮 LLM 基线

## 所属里程碑

M8：假设—证据—验证审查

## 背景

M8 的目标是降低 `single` 单轮审查模式中"把猜测直接报为 finding"的误报。要衡量 verify 模式是否真正降低误报，必须先有一个可复现、可比较的 `single` 基线。

本任务独立于 M7，不要求 M7 已完成。它与后续 M8-21（误报导向 Eval case）和 M8-22（single/verify 对照）的关系是：M8-22 的对照必须以 M8-0 的基线配置为 single 侧基准。

本任务对最终两轮验证流水线的作用是：提供 precision/recall/FPR/token/调用次数/时延的量化起点，使后续所有 verify 结论有据可依。

## 本轮目标

用固定的 case 集、配置和 commit 运行 `single` 审查模式（即 `use_llm=True`、`review_mode="fixed"` 的单次 LLM 审查路径），保存机器可读基线结果，包含逐 case 指标与负样例统计。

## 当前真实状态

已确认存在：

- `src/eval_runner.py`：`run_eval`、`run_one_case` 支持 `use_llm=True`、`review_mode="fixed"`；`build_fixed_baseline_record` 可生成机器可读基线 JSON。
- `evals/cases/`：现有 6 个 case，其中 `clean_change_no_issue` 为负样例（`should_find: false`）。
- `evals/baselines/m7-0-fixed.json`：M7 的 fixed 基线（无 LLM），schema_version 为 `m7_fixed_baseline.v1`。
- `src/llm_client.py`：`mock_call_model`、`get_call_model` 支持 mock provider，可离线运行。

尚未存在：

- 针对单轮 LLM（`single`）模式的基线文件。现有 `m7-0-fixed.json` 是规则 only（`use_llm=False`），与 M8-0 所需的 single（`use_llm=True`）基线不同。
- `eval_runner.py` 尚无专门为 single 模式生成基线记录的 schema_version（当前固定基线 schema 标注的是 fixed 无 LLM）。

## 本轮范围

### 允许修改

- `evals/baselines/` 下新增 M8 single 基线结果文件（如 `m8-0-single.json`）。
- `src/eval_runner.py`：如需新增 single 基线记录构建函数或 CLI 参数，仅限最小改动。
- `tests/test_eval_runner.py`：新增对 single 基线记录结构的断言测试。

### 必要时允许修改

- `src/eval_runner.py` 的 `build_fixed_baseline_record`：如需复用其结构生成 single 基线，可提取共享逻辑，但不得破坏现有 fixed 基线行为。仅当现有函数无法表达 single 模式配置（`use_llm=True`、`review_mode="fixed"`）时才允许修改。

### 禁止修改

- 任何 M8-1 及后续编号任务的范围。
- `src/llm_reviewer.py`、`src/llm_client.py` 的生产逻辑。
- 现有 fixed 基线的行为与 `m7-0-fixed.json`。
- 真实 OpenAI 调用。
- `review_service.py` 的 pipeline 编排。
- 为通过单个 case 编写特判。

## 功能要求

1. 使用 `evals/cases/` 全部 case，以 `use_llm=True`、`llm_provider="mock"`、`review_mode="fixed"` 运行 eval，生成 single 基线。
2. 基线记录为机器可读 JSON，至少包含：schema_version（标识为 M8 single 基线）、commit、worktree_state、configuration（cases/repo/use_llm/llm_provider/context_budget）、environment、metrics（precision/recall/f1/false_positive_rate/average_findings/average_duration_ms/p95_duration_ms/total_tokens/total_llm_calls/estimated_cost_usd）、results（逐 case）。
3. 基线必须包含负样例统计（负向 case 数量、负向误报数、负向误报率）。
4. 记录可复现命令与输入配置。
5. 基线结果文件可由测试或脚本读取并断言关键字段存在。

## 退出条件

- [ ] single 基线 JSON 文件存在于 `evals/baselines/`，可被 `json.loads` 读取。
- [ ] 基线记录包含 precision、recall、f1、false_positive_rate、average_findings、average_duration_ms、p95_duration_ms、total_tokens、total_llm_calls 字段且值为数值。
- [ ] 基线记录包含逐 case results 列表，每个 case 有 case_id、passed、actual_categories、findings_count、false_positive、duration_ms。
- [ ] 基线记录包含负样例统计（negative_case_count、false_positive_negative_case_count 或等价字段）。
- [ ] 基线记录包含可复现命令字符串。
- [ ] 有测试断言基线记录结构合法（字段存在、类型正确）。
- [ ] 运行命令可复现且不调用真实 OpenAI。

## 必须覆盖的测试

### 正常路径

- 运行 single 基线生成后，基线 JSON 可被读取且关键字段存在。

### 边界情况

- 负样例 case 在基线结果中被正确标记为 `is_negative_case: true`。

### 失败路径

- 若 eval 运行异常，基线不生成伪造成功记录。

### 回归测试

- 现有 `m7-0-fixed.json` 相关测试不受影响。
- `py -m pytest tests/test_eval_runner.py -v` 全部通过。

## 兼容性约束

- 不得修改 `review_mode="fixed"` 的默认行为。
- 不得修改现有 fixed 基线 schema 与 `m7-0-fixed.json`。
- 不得修改 `ReviewService` pipeline。

## 安全要求

- 基线运行使用 mock provider，不调用真实 OpenAI。
- 基线记录不得包含 prompt 原文或敏感源码（沿用现有 trace 脱敏机制）。

## 推荐验证命令

```bash
py -m pytest tests/test_eval_runner.py -v
```

```bash
py -m src.eval_runner --cases evals/cases --repo . --llm --llm-provider mock
```

## 完成时必须提供的证据

- 修改文件清单。
- 新增的 single 基线 JSON 文件路径。
- 实际调用链：eval_runner → ReviewService(use_llm=True, review_mode="fixed") → review_with_llm → mock_call_model。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：eval 异常时的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-1。

## 非目标

- 不实现假设、证据或第二轮调用。
- 不修改 single 模式的审查逻辑或 prompt。
- 不新增 case。
- 不修改 M7 react 基线。

## 后续任务

- M8-1（假设、证据与 Verdict 数据模型）依赖 M8-0。
