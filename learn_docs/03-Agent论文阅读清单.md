# RepoReview Agent：论文阅读清单

> 目标：用论文解释自己的工程取舍。每篇先读 2–3 小时，不要求推导全部公式或复现训练；要求能回答“它解决什么问题、代价是什么、和我的项目有什么关系”。

## 一、阅读规则

每篇论文按以下顺序：

1. 摘要、引言、方法总图；
2. 实验任务、比较基线、主要指标；
3. 局限/失败模式；
4. 写一张不超过一页的映射卡。

每张映射卡必须回答：

- [ ] 论文想解决的具体问题是什么？
- [ ] 机制的输入、状态、行动、反馈/评测分别是什么？
- [ ] 实验实际证明了什么，又没有证明什么？
- [ ] 对 RepoReview Agent 可借鉴的一点与明确不采用的一点。
- [ ] 一个可以落实到代码、测试或 Eval 的问题。
- [ ] 一段 60 秒面试解释。

## 二、必读 5 篇

### 1. ReAct：推理与行动的受控循环

- 论文：[ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)
- 阅读深度：摘要、引言、方法、至少一个 QA/交互环境实验、局限；不必复现 prompt。
- 必须搞懂：交替的 reason/action/observation 如何帮助模型更新下一步；为什么 observation 可能减少凭空编造，但不等于工具结果天然可信。
- 映射项目：`react_controller.py` 的模型—工具—安全结果—历史—finish 循环。
- 必须能回答：为什么本项目仍限制工具、结果大小、步数和 token？为什么不保存完整 chain-of-thought？

### 2. Toolformer：工具不是“给模型一个函数”这么简单

- 论文：[Toolformer: Language Models Can Teach Themselves to Use Tools](https://arxiv.org/abs/2302.04761)
- 阅读深度：摘要、API 调用数据构造/过滤思想、实验设置和局限；不要求掌握训练细节。
- 必须搞懂：工具使用涉及何时调用、调用哪个、参数是什么、如何利用结果；“会调用”与“应该获得权限”是两回事。
- 映射项目：`review_tools.py` 的 schema、最小权限、稳定错误码、截断与 call ID。
- 必须能回答：新增一个工具前，如何证明它的收益超过权限、安全、成本和评测复杂度？

### 3. Reflexion：反思有代价，不是默认解法

- 论文：[Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)
- 阅读深度：摘要、architecture、反馈信号/记忆、消融实验、局限。
- 必须搞懂：语言反馈如何影响后续尝试；反思会增加调用、上下文、成本和错误放大风险。
- 映射项目：M8 选择“确定性证据 + 三态 verdict”，而非先增加自由反思循环。
- 必须能回答：为什么本项目当前优先 verification，而非 memory/reflection？什么证据出现后才值得加反思？

### 4. AgentBench：如何评估 Agent 而不是只看一次回答

- 论文：[AgentBench: Evaluating LLMs as Agents](https://arxiv.org/abs/2308.03688)
- 阅读深度：任务分类、评测环境、失败归因和局限；不必运行 benchmark。
- 必须搞懂：多轮决策、工具使用和环境反馈为什么需要单独评测；质量、稳定性、失败恢复不能由单一分数表示。
- 映射项目：`eval_runner.py`、逐 case 结果、工具/预算/失败 trace。
- 必须能回答：为什么 RepoReview Agent 要同时报告 Precision、Recall、F1、FPR、inconclusive、token、时延与失败率？

### 5. SWE-bench：真实软件工程任务与评测泄漏

- 论文/基准：[SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://www.swebench.com/original.html)
- 阅读深度：任务构造、实例来源、评价方式、限制；不必在本机运行。
- 必须搞懂：真实仓库上下文、版本固定、测试验证和数据污染为何关键；benchmark 成绩不等于通用工程能力。
- 映射项目：`evals/cases/`、ground truth、固定 commit/config、禁止为 case 写特判。
- 必须能回答：为什么你的 6 个合成 case 只能证明控制流，不能证明真实泛化？怎样扩充它而不把 Eval 调成“迎合实现”？

## 三、按需阅读（不影响 M8）

### 6. Generative Agents：记忆、反思与规划的概念地图

- 论文：[Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442)
- 阅读深度：architecture 图、memory retrieval、reflection、planning 四部分即可。
- 要搞懂：短期状态、长期记忆、反思摘要和规划的职责不同。
- 与项目关系：仅帮助你解释“为什么代码审查的单次 review 暂不需要长期记忆”；不要据此引入向量库。

### 7. LLM Agent Survey：建立术语地图

- 论文：[The Rise and Potential of Large Language Model Based Agents: A Survey](https://arxiv.org/abs/2309.07864)
- 阅读深度：目录、taxonomy 图、评测与挑战章节；把它当索引，不从头精读。
- 要搞懂：profile、memory、planning、action、environment、evaluation 的常见划分，以及这些概念在不同论文中的不一致。
- 与项目关系：帮助整理面试术语，不能替代读上面五篇原始论文。

## 四、推荐阅读顺序与停止条件

1. ReAct → Toolformer：读完后能解释现有 `react` 与工具安全。
2. AgentBench → SWE-bench：读完后能解释项目 Eval 的价值与局限。
3. Reflexion：读完后能解释为何 M8 不先加反思。
4. 需要讨论记忆时读 Generative Agents；需要补术语时读 Survey。

停止条件不是“读完所有参考文献”，而是能完成五张映射卡，并把其中三张直接用于 M8 设计、测试或面试答案。
