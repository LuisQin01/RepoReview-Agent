'''单次代码审查运行的可变状态容器定义。

【模块职责】
定义 ``ReviewState`` 数据结构，作为一次"代码审查运行"（review run）的可变状态中心，
贯穿整个 pipeline 的所有阶段。各阶段从该状态读取输入、向其写入中间产物，
最终由报告阶段汇总产出。该模块是 pipeline 各环节之间共享数据的唯一载体。

【在整体架构中的位置】
位于业务层，依赖 schemas 模块定义的 ChangedFile / ContextBudget / FileContext /
ReviewIssue 等数据结构。被 agent 主流程创建并在各处理函数间传递，是连接
diff_parser、context retriever、rule checker、llm reviewer、reporter 等阶段
的"主状态总线"。

【设计理由】
- 使用单一可变 dataclass 而非函数返回值链式传递：审查 pipeline 阶段多、产物杂
  （diff 文本、结构化文件、上下文、规则问题、LLM 问题、trace、错误等），
  统一容器让各阶段只读写自己关心的字段，避免函数签名爆炸与重复传参。
- task_id 用 uuid4 生成：保证单次运行有全局唯一标识，便于 trace 落盘、
  日志关联与并发场景下区分不同审查任务。
- started_at_perf 用 perf_counter 记录起始时间：perf_counter 是单调高精度计时器，
  适合统计单次 pipeline 的各阶段耗时，比 time.time() 更精确且不受系统时钟跳变影响。
'''

from dataclasses import dataclass, field
from typing import List
from time import perf_counter
from uuid import uuid4

from .schemas import ChangedFile, ContextBudget, FileContext, ReviewIssue

@dataclass
class ReviewState:
    '''单次代码审查运行的状态容器，串联 pipeline 各阶段中间产物。

    该类既保存运行配置（diff_path / repo_root / use_llm / context_budget 等
    不可变输入），也保存各阶段产出与中间结果（changed_files / contexts /
    rule_issues / llm_issues / issues / output / trace_steps / errors 等）。
    trace 相关字段用于可选的可解释性追踪。

    设计理由：
    - 配置字段（前 5 个无默认值的字段）放在最前并设为必填，确保每次审查运行
      必须明确指定核心配置；其余字段均有默认值，逐步在 pipeline 中填充。
    - rule_issues 与 llm_issues 分开存储再合并到 issues，便于按来源做去重、
      排序与统计，也方便 trace 中展示"哪类规则/LLM 贡献了哪些问题"。
    - task_id / started_at_perf / trace_* 字段为可观测性服务，默认即生成，
      即使不开启 trace 也能用于日志与耗时统计。

    Attributes:
        diff_path: 待审查 diff 文件路径（输入配置）。
        repo_root: 仓库根目录绝对路径，用于读取上下文文件（输入配置）。
        output_format: 输出格式，如 "text" / "markdown" / "json"（输入配置）。
        use_llm: 是否启用 LLM 审查阶段（输入配置；False 时仅走规则审查）。
        context_budget: LLM prompt 预算配置（ContextBudget，输入配置）。
        llm_provider: LLM 提供方标识，默认 "mock"（用于无真实 LLM 时的占位/测试）。
        diff_text: 读取到的原始 diff 文本（diff 阶段填充）。
        changed_files: 解析后的变更文件列表（diff_parser 阶段填充）。
        contexts: 检索到的文件上下文列表（context retriever 阶段填充）。
        rule_issues: 规则引擎发现的问题列表（rule checker 阶段填充）。
        llm_issues: LLM 审查发现的问题列表（llm reviewer 阶段填充）。
        issues: 合并去重后的最终问题列表（聚合阶段填充，对外输出）。
        output: 最终生成的报告文本（reporter 阶段填充）。
        trace_steps: 各阶段 trace 步骤记录（dict 列表，用于可解释性输出）。
        errors: pipeline 运行中累积的错误信息列表。
        trace_enabled: 是否开启 trace 落盘（输入配置，默认关闭）。
        trace_dir: trace 文件输出目录，默认 "traces"。
        task_id: 本次运行的唯一标识，默认由 uuid4 前 8 位生成。
        started_at_perf: 本次运行起始的高精度时间戳，默认 perf_counter()。
        trace_path: 本次运行 trace 文件的落地路径（开启 trace 时填充）。
    '''
    diff_path:str
    repo_root:str
    output_format:str
    use_llm:bool
    context_budget:ContextBudget
    llm_provider:str="mock"

    diff_text:str=""
    changed_files:List[ChangedFile]=field(default_factory=list)
    contexts:List[FileContext]=field(default_factory=list)

    rule_issues:List[ReviewIssue]=field(default_factory=list)
    llm_issues:List[ReviewIssue]=field(default_factory=list)
    issues:List[ReviewIssue]=field(default_factory=list)

    output:str=""
    trace_steps:List[dict]=field(default_factory=list)
    errors:List[str]=field(default_factory=list)

    trace_enabled:bool=False
    trace_dir:str="traces"
    # task_id 取 uuid4 十六进制前 8 位：长度短足以区分同批次任务，又便于在文件名与日志中使用。
    task_id:str=field(default_factory=lambda:uuid4().hex[:8])
    # perf_counter 为单调高精度计时器，记录起始时刻用于计算 pipeline 总耗时与各阶段耗时。
    started_at_perf:float=field(default_factory=perf_counter)
    trace_path:str=""

