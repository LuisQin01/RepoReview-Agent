# M8-9：Import Target 证据收集器

## 所属里程碑

M8：假设—证据—验证审查

## 状态标记

**不属于当前第一版最小路径，待核心 verify 流程完成后再评估。**

第一版重点使用 `caller_exception_handling` 作为确定性证据类型。本任务保留完整验收标准，但在核心 verify 流程完成前暂缓执行。

## 背景

`import_target` collector 将 import 映射为仓库内候选目标或明确的不确定状态，为"import 目标不存在或语义不符"类假设提供确定性事实。

本任务依赖 M8-6（collector 协议与注册表）。它与 M8-10（证据收集编排器）的关系是：M8-10 可调度本 collector。

本任务对最终两轮验证流水线的作用是：为 import 相关假设提供仓库内候选路径或不确定状态。

## 本轮目标

实现 `import_target` collector，处理绝对/相对 import，返回仓库内候选路径或明确的不确定状态。

## 当前真实状态

已确认存在：

- `src/file_context.py`：`_extract_import_file_candidates(content, current_file_path)` 用正则解析 import 语句（from...import / import），返回候选文件路径列表；`_module_to_candidate_paths` 映射模块名到 .py 和 __init__.py；`_resolve_relative_base` 处理相对导入点数。
- `src/file_context.py`：`_is_sensitive_file_path`、路径规范化。
- M8-6 将定义 `EvidenceCollector` 协议与 dispatcher。

尚未存在：

- `import_target` evidence collector。
- 将 import 解析结果作为 EvidenceItem 返回的能力。

## 本轮范围

### 允许修改

- `src/evidence_collectors.py`：新增 `import_target` collector。
- `tests/test_evidence_collectors.py`：新增 collector 测试。

### 必要时允许修改

- 无。

### 禁止修改

- `src/file_context.py`（只读复用 import 解析辅助函数，不修改）。
- `src/review_tools.py`。
- 任何后续编号任务的范围。
- controller、prompt、CLI、trace、Eval。
- 为通过单个 Eval case 编写特判。

## 功能要求

1. 实现 `import_target` collector，接收 `EvidenceRequest`（含 file、import_statement 或等价标识）。
2. 处理绝对 import、相对 import。
3. 将模块名映射为仓库内候选路径（复用 `_module_to_candidate_paths`）。
4. 只返回 repo root 内候选路径。
5. 返回 `EvidenceItem`：data 含 import 语句、候选路径列表、状态（`found`/`not_found`/`unsupported`/`conflicting`）。
6. 模块不存在返回 `not_found`。
7. 外部依赖（如 `import os`）返回 `unsupported`，不误报"模块不存在"。
8. 多个候选返回 `conflicting`（不确定状态）。
9. 循环 import 不导致无限递归。
10. 不递归无限解析 import 链。
11. 不读取外部包。

## 退出条件

- [ ] 绝对 import 映射到仓库内候选路径。
- [ ] 相对 import 正确计算基准目录。
- [ ] 模块不存在返回 `not_found`。
- [ ] 外部依赖返回 `unsupported`，不误报 `not_found`。
- [ ] 多个候选返回 `conflicting`。
- [ ] 越权路径被拒绝。
- [ ] 返回路径在 repo root 内。
- [ ] 有离线测试覆盖上述场景。
- [ ] 测试不依赖网络。

## 必须覆盖的测试

### 正常路径

- 绝对 import 映射到 .py 文件。
- 相对 import 映射到正确目录。

### 边界情况

- 多候选返回 `conflicting`。
- `from . import x` 形式。

### 失败路径

- 模块不存在返回 `not_found`。
- 外部依赖返回 `unsupported`。
- 越权返回 `forbidden`。

### 回归测试

- `py -m pytest tests/ -v` 全部通过。
- `file_context.py` 的 import 解析函数行为不受影响。

## 兼容性约束

- 不修改 `_extract_import_file_candidates` 或 `_module_to_candidate_paths`。
- 复用 M8-6 的协议与错误码。

## 安全要求

- 返回路径在 repo root 内。
- 不读取外部包。

## 推荐验证命令

```bash
py -m pytest tests/test_evidence_collectors.py -v
```

```bash
py -m pytest tests/ -v
```

## 完成时必须提供的证据

- 修改文件清单。
- collector 输入输出契约。
- 实际调用链：dispatcher → collector → import 解析 → EvidenceItem。
- 测试命令和真实结果。
- 每项退出条件对应的证据。
- 失败和降级语义：not_found/unsupported/conflicting/forbidden 的行为。
- 未执行验证及原因。
- 剩余风险。
- 当前 git diff 范围。
- 下一个依赖已满足的任务：待核心 verify 流程完成后评估。

## 非目标

- 不递归无限解析 import 链。
- 不读取外部包。
- 不构建完整 import 图。
- 不调用第二轮模型。

## 后续任务

- M8-10（证据收集编排器）在至少一个具体 collector 完成后可集成本 collector。
