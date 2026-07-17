# RepoReview Agent：Agent 开发面试八股清单

> 用法：每题准备 60–90 秒答案，统一结构为“结论 → 本项目证据 → 取舍/局限 → 下一步”。不要背诵没有亲手验证过的生产规模、模型指标或多 Agent 经历。

## 一、必须会的 Agent 基础

- [ ] **什么是 LLM Agent？和 workflow 有什么区别？**
  - 要点：workflow 路径由代码预定；agent 在约束内动态选择工具/步骤。两者可以组合。
  - 项目证据：`fixed` 是 workflow；`react` 有受限动态工具选择；M8 `verify` 是固定验证 workflow。

- [ ] **ReAct 是什么？优势和风险？**
  - 要点：交替推理、行动、观察；优势是按需获取信息；风险是循环、越权、成本、错误观察进入上下文。
  - 项目证据：步骤/调用/token/工具结果预算、safe history、`finish_review`。

- [ ] **Function calling 的完整流程是什么？**
  - 要点：模型提出 name+arguments → 本地 schema/权限校验 → 执行受控工具 → 返回结构化结果 → 模型决定下一步/终止。
  - 必须强调：模型输出永远不可信；函数调用不等于授权。

- [ ] **结构化输出能解决什么，不能解决什么？**
  - 要点：约束格式、便于解析和测试；不能保证事实、业务正确性或安全。
  - 项目证据：M8 hypothesis/verdict parser 仍需本地校验和 finding validation。

- [ ] **Agent 的状态、记忆、trace 有什么区别？**
  - 要点：state 是单次运行的可控事实；memory 跨会话保存且需检索/过期/隐私策略；trace 是脱敏审计记录，不是运行时事实来源。
  - 项目证据：`ReviewState`、`trace.py`；当前没有必要引入长期记忆。

- [ ] **什么时候该用多 Agent？**
  - 要点：子任务独立、角色边界明确、产物可验证，且收益超过协调、上下文和成本。
  - 项目证据：当前 M8 用单 Agent + 确定性证据更简单可靠；未先上多 Agent。

## 二、工具、安全与可靠性（高频）

- [ ] **如何设计一个安全的 Agent tool？**
  - 要点：最小权限、明确 schema、路径/范围校验、敏感数据过滤、大小/时间限制、稳定状态码、审计与测试。
  - 项目证据：`ToolDispatcher` 和三个只读工具。

- [ ] **如何防 prompt injection？**
  - 要点：代码、diff、PR 标题/描述、工具返回、LLM 文本全部视为不可信；不能仅靠 prompt 防御；权限必须由工具边界的确定性代码决定。
  - 项目证据：路径白名单、敏感文件拒绝、schema、最终 validation gateway。

- [ ] **路径穿越和符号链接逃逸怎么防？**
  - 要点：将路径规范化为 repo root 相对路径；拒绝绝对路径、`..`、scope 外路径；解析真实路径并检查 symlink escape。

- [ ] **为什么要区分 `not_found`、`unavailable`、`truncated`？**
  - 要点：没找到、无法获取、信息不完整分别代表不同事实；不能把任意一个当作“问题存在”或“问题不存在”。
  - 项目证据：M8-11 和 `inconclusive` 降级。

- [ ] **什么是 fail closed？本项目何处应 fail closed？**
  - 要点：证据不足、越权、非法参数、敏感输入时拒绝动作或拒绝确认 finding，而不是猜测成功。
  - 项目证据：M8 只有 `confirmed` 才能进入 validation。

- [ ] **如何避免 Agent 无限循环与成本失控？**
  - 要点：max steps、LLM calls、token、工具结果大小、超时、失败阈值、明确终止原因。
  - 项目证据：`ReActBudget` 与 M8-18A/18B。

- [ ] **工具失败时为什么不能返回空列表当成功？**
  - 要点：会隐藏失败并制造假阴性/假成功；要返回稳定状态、保留可审计摘要，并让上层决定保守降级。

## 三、M8 与代码审查 Agent 专项（高概率）

- [ ] **为什么要做 hypothesis → evidence → verdict？**
  - 结论：把模型猜测和可发布 finding 分离，降低误报。
  - 证据：第一轮假设不是 finding；collector 提供确定性事实；第二轮三态 verdict；confirmed 还要走既有 validation。

- [ ] **为什么 `inconclusive` 不是失败？**
  - 要点：它诚实表达证据不足，避免系统能力不足被误写为 bug；同时必须统计比例，防止通过大量拒答虚假提高 Precision。

- [ ] **如何保证 `rejected/inconclusive` 不会出现在报告里？**
  - 要点：类型契约限制、parser 拒绝携带 finding、controller 只转发 confirmed、validation/reporting 是唯一出口、回归测试覆盖。

- [ ] **为何 M8 不能直接复用 ReAct controller？**
  - 要点：ReAct 是模型选择路径的自由工具循环；M8 是固定两轮 + 确定性证据收集。语义、trace 阶段、失败降级和评估目标都不同。

- [ ] **如何判断一个异常是否被调用方处理？**
  - 要点：第一版只针对已知文件/行，用 AST 观察包围调用的 try/except、捕获类型、bare except、嵌套关系；只描述事实，不自动判 bug。

- [ ] **如何做代码审查 finding 的定位与去重？**
  - 要点：schema、文件/行、changed-hunk/scope 校验、稳定 ID/定位、去重、排序、reporter；模型说的行号不可直接信。

## 四、评测与实验（几乎必问）

- [ ] **Precision、Recall、F1、FPR 分别是什么？为什么要同时看？**
  - Precision：报出的结果中有多少正确；Recall：真实问题中找回多少；F1：二者折中；FPR：负样例被误报的比例。
  - 项目语境：M8 的目标是降低误报，不能只看 Precision，还要看 Recall、inconclusive、成本。

- [ ] **怎么评估一个 Agent，而不是一次性 prompt？**
  - 要点：分层 case、固定输入/版本/配置/预算、逐 case 输出、失败原因、质量+成本+延迟+安全拒绝率；离线回归与真实模型实验分开。

- [ ] **为什么 mock Eval 不足？**
  - 要点：它能确定性验证控制流、边界和降级，但不能证明真实模型的策略、波动、真实成本或泛化；两种证据都需要。

- [ ] **如何避免 Eval 泄漏/刷分？**
  - 要点：不按 case 名或固定文本特判；冻结 case/配置；先记录基线再做单变量改动；保留逐 case diff；增加负样例与新 case。

- [ ] **如何比较 fixed、react、single、verify？**
  - 要点：同一 commit、case 集、模型/脚本、随机性和预算；报告质量、调用数、token、p95 延迟、成本、失败/降级；不能只挑一个分数。

## 五、框架与架构取舍（高频）

- [ ] **为什么不直接用 LangChain？**
  - 要点：它能统一模型/工具/Agent 抽象，但会隐藏/引入抽象；当前已有协议和安全边界，先完成 M8 更小、更可测。

- [ ] **什么时候用 LangGraph？**
  - 要点：长运行状态、checkpoint、失败恢复、人工审批、复杂可视状态图；不是两轮固定流程的必需品。

- [ ] **是否需要读框架源码？**
  - 要点：先掌握 public API 和本项目需求；在明确扩展点/错误语义时定点读，不通读。

- [ ] **如何做 provider abstraction？**
  - 要点：controller 只依赖内部 JSON-safe `ModelResponse`/`ToolCall`；SDK 响应、HTTP、重试在 adapter 边界；mock 与真实 provider 同契约。

- [ ] **为什么默认模式不直接改成更 Agentic 的模式？**
  - 要点：兼容性与可控风险；先显式 opt-in，收集可复现证据后再讨论默认切换。

## 六、建议背诵的简答模板

### “你的 Agent 有多自主？”

> 它是受限自主，而不是无边界自主。模型在 `react` 模式中可以选择三个只读工具并决定何时 finish，但工具权限、参数 schema、路径范围、预算和最终 finding 校验都由确定性代码控制。M8 进一步把模型的第一轮输出降为 hypothesis，只有经白名单证据和 confirmed verdict 的结果才可能进入报告。我们用这种设计换取可审计性和更低的误报风险。

### “为什么不上多 Agent / LangGraph？”

> 当前主要失败模式是证据不足导致的误报，不是任务分解能力不足。M8 的固定两轮验证能直接处理它，并有明确的离线 Eval。多 Agent 和框架会增加协调、状态一致性、成本和测试复杂度；等到长任务恢复、人工审批或相互独立子任务成为真实需求时，再用隔离实验评估。

### “如何保证模型不会乱报问题？”

> 不能靠 prompt 保证。代码、diff 和模型输出都被当作不可信输入；工具调用有本地 schema/权限限制，M8 中模型先提出 hypothesis，再由确定性 collector 获取证据，只有第二轮 confirmed 候选能进入既有 validation、去重、排序和 reporter。证据不可用则显式 `inconclusive`，不生成 finding。
