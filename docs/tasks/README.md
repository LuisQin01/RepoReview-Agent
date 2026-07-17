# Task 文件使用规则

本目录保存可独立执行、独立验收的具体 task 文件。它把项目的长期工程规则应用到一个小而清楚的工作单元中，帮助形成可展示、可解释、可复现的 Agent 开发实践记录。

## 里程碑索引

- [M8：假设—证据—验证审查](M8-index.md) — 24 个 task，含依赖顺序、第一版最小路径与 M8-18A/B 依赖核验结论。

## Task 的边界

- 一个 task 文件只对应一个边界清楚、能够独立验收的小任务；不要用一个 task 覆盖整个项目或整个里程碑。
- task 文件只保存这个任务会变化的内容，例如当前真实状态、范围、目标、退出条件、测试和风险。
- 长期规则由 [AGENTS.md](../../AGENTS.md) 和 [workflows](../workflows/) 维护；task 不重复其中的安全、实现、验收和修复规则。
- 普通且边界清楚的任务可以直接执行，不必先使用 `/plan`。只有任务边界、关键契约或实现方案尚不清楚时，才需要额外规划。

## 执行与验收闭环

- 一个 `/goal` 只完成一个 task，不覆盖整个项目或整个里程碑。
- Goal 内完成实施、测试、自检，以及最多一次内部修复；内部修复后必须重新验证。
- Goal 完成后，再用 `/review` 独立检查当前 diff。
- `/review` 默认只输出 findings，不自动修改代码。
- review finding 的 P0、P1、P2 分类标准只以 [acceptance.md](../workflows/acceptance.md) 为准。
- P0/P1 按 [repair.md](../workflows/repair.md) 修复；修复后重新测试和验收。
- P2 只记录，默认不修复，也不阻止进入下一个 task。

推荐流程：

```text
创建 task
→ /goal
→ 实施、测试和自检
→ /review
→ 修复 P0/P1
→ 重新测试和验收
→ 进入下一个 task
```

## 精简 Goal 示例

```text
/goal

完成 docs/tasks/<任务文件>.md 定义的任务。

执行前读取：

- AGENTS.md
- docs/workflows/implementation.md
- docs/workflows/acceptance.md
- docs/workflows/repair.md
- 当前 task 文件

只处理当前任务。

按照 implementation.md 完成实现和测试，
按照 acceptance.md 自检，
如有 P0/P1，则按照 repair.md 进行最多一次内部修复并重新验证。
P2 只记录，不主动修复。

只有全部退出条件有真实证据，且不存在未修复的 P0/P1 时，
才能宣布任务完成。

如果存在无法解决的 P0/P1，停止并报告阻塞，不得扩大范围或虚假通过。
```

## 创建 task

从 [task-template.md](task-template.md) 复制一份文件并填写。先从当前代码、测试和 `git diff` 确认“当前真实状态”，再写可验证的退出条件；计划中的能力不能当作已实现能力。任务完成报告应保留实际命令和结果，使后续 `/review` 可以仅凭 task、diff 和代码复核。
