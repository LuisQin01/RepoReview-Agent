# M8-21：误报导向 Eval Cases

## 所属里程碑

M8：假设—证据—验证审查

## 背景

建立"表面像问题、但确定性上下文能否定假设"的真实负样例集。这些 case 在 single 模式下可能误报，但 verify 模式应通过证据收集排除。

本任务依赖 M8-0（single 基线），不依赖 controller 完成。它与 M8-22（single/verify 对照 Eval）的关系是：M8-22 使用本任务的 case 集运行对照。

本任务对最终两轮验证流水线的作用是：提供可衡量误报改善的负样例集。

## 本轮目标

在 `evals/cases/` 中新增 2-3 个误报导向 case，每个 case 含 input.diff、必要仓库上下文、ground truth（含 `must_not_findings`），说明 single 可能误报原因与应使用的证据类型。

## 当前真实状态

已确认存在：

- `evals/cases/`：现有 6 个 case，含正向（`should_find: true`）和负向（`should_find: false`，`expected_categories: []`）。
- `evals/cases/deleted_exception_handling/`：正向 case 示例（`expected_categories: ["exception_handling"]`）。
- `evals/cases/clean_change_no_issue/`：负向 case 示例（`should_find: false`）。
- `src/eval_runner.py`：`load_expected` 读取 `expected.json`；`run_one_case` 支持 `repository_context`（case 自有 fixture）。
- `src/eval_runner.py`：`_resolve_case_repo_root` 支持 case 自有仓库 fixture（`repository_context.root` + `required_paths`）。

尚未存在：

- `must_not_findings` 字段（现有 expected.json 仅有 `expected_categories`/`should_find`）。
- 误报导向 case（调用方已捕获异常、上层 context manager 管理资源等）。

## 本轮范围

### 允许修改

- `evals/cases/`：新增 2-3 个误报导向 case 目录（含 `input.diff`、`expected.json`、必要仓库上下文文件）。
- `tests/test_eval_runner.py`：新增 case 结构合法性测试。

### 必要时允许修改

- `src/eval_runner.py`：如需支持 `must_not_findings` 字段读取与断言，仅限最小改动。仅当现有 `expected.json` 结构无法表达"禁止 finding"语义时才允许修改。

### 禁止修改

- 现有 6 个 case 的内容与 expected.json。
- `src/review_service.py`、`src/verify_controller.py`。
- `src/verification_protocol.py`、`src/evidence_collectors.py`。
- 任何后续编号任务的范围。
- controller/prompt 来让样例通过。
- 为 case 名称或固定文件名增加特判。
- 把负样例当作空 diff。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 新增 2-3 个误报导向 case，例如：
   - 调用方已捕获异常（表面像未处理异常，但 caller_exception_handling 证据可否定）。
   - 上层 context manager 管理资源（表面像资源泄漏，但上下文可否定）。
   - API 契约保证非空返回（表面像 None 检查缺失，但契约可否定）。
   - 重导出仍提供 import（表面像 import 失败，但目标存在）。
2. 每个 case 含 `input.diff`（真实 diff）。
3. 每个 case 含必要仓库上下文（通过 `repository_context` 声明 case 自有 fixture）。
4. 每个 case 的 `expected.json` 写明：`should_find: false`、`must_not_findings`（禁止 finding 的类别或描述）、single 可能误报原因、应使用的证据类型。
5. ground truth 经人工核验（case 在缺少额外上下文时确有误报风险）。
6. 不为 case 名称或固定文件名增加特判。
7. 重命名 case 不影响逻辑。

## 退出条件

- [ ] 至少 2 个误报导向 case 已创建，含 input.diff、expected.json、仓库上下文。
- [ ] 每个 case 的 expected.json 含 `must_not_findings` 字段。
- [ ] 每个 case 写明 single 可能误报原因与应使用的证据类型。
- [ ] ground truth 经人工核验（case 在缺少额外上下文时确有误报风险）。
- [ ] case 可由 eval_runner 读取且结构合法（测试断言）。
- [ ] 重命名 case 不影响逻辑（测试不依赖 case 名称匹配固定输出）。
- [ ] 有测试防止直接根据 case 名称输出答案。
- [ ] 测试不调用真实 API。

## 必须覆盖的测试

### 正常路径

- 新 case 可被 eval_runner 读取，结构合法。
- `must_not_findings` 字段可被读取。

### 边界情况

- case 的 repository_context fixture 路径限制正确（不越界）。

### 失败路径

- case 缺少必要文件时 eval_runner 报错（不伪造通过）。

### 回归测试

- `py -m pytest tests/test_eval_runner.py -v` 全部通过。
- 现有 6 个 case 的行为不受影响。
- `py -m src.eval_runner --cases evals/cases --repo .` 可运行（至少不因新 case 崩溃）。

## 兼容性约束

- 不修改现有 case 的内容。
- 不修改 `run_one_case` 的核心判定逻辑（仅可能新增 `must_not_findings` 读取）。
- 不修改 review_service 或 controller。

## 安全要求

- case 的 repository_context fixture 路径限制在 case 目录内（不越界）。
- case 内容不含真实密钥或敏感数据。

## 推荐验证命令

```bash
py -m pytest tests/test_eval_runner.py -v
```

```bash
py -m src.eval_runner --cases evals/cases --repo . --llm --llm-provider mock
```

## 完成时必须提供的证据

- 修改文件清单（新增 case 目录与文件）。
- 每个 case 的误报场景说明与证据类型。
- 实际调用链：eval_runner → load_expected → run_one_case。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：case 结构不合法时的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：M8-22。

## 非目标

- 不为 case 名称或固定文件名增加特判。
- 不把负样例当作空 diff。
- 不修改 controller/prompt 来让样例通过。
- 不修改现有 case。

## 后续任务

- M8-22（Single 与 Verify 对照 Eval）依赖 M8-20、M8-21。
