'''
用来定义项目当中的数据结构

让模块之间传递的数据有统一的格式

【模块职责】
本模块是整个 RepoReview Agent 项目的"数据契约层"，集中定义了 pipeline 各阶段之间
传递的所有核心数据结构。从 git diff 解析、文件上下文检索、规则审查、LLM 审查，
到最终结果聚合，所有模块都依赖这里定义的类型，从而避免类型不一致和隐式耦合。

【在整体架构中的位置】
位于 src/ 包的最底层，不依赖其它业务模块（仅依赖 dataclasses/typing 标准库），
被 diff_parser、agent_state、context retriever、rule checker、llm reviewer、
reporter 等几乎所有上游模块导入，是整个系统的"通用语言"。

【设计理由】
- 统一使用 dataclass：相比裸 dict，dataclass 提供字段校验、类型提示、IDE 自动补全，
  且可读性更好；相比 Pydantic，标准库 dataclass 零依赖、足够满足本项目需求。
- 部分关键结构（DiffHunk、ContextBudget）使用 frozen=True 不可变设计，
  防止在 pipeline 多阶段流转中被意外篡改，提升可追溯性与线程安全性。
- ReviewIssue 的 source/placement 字段由系统赋值，绝不接收 LLM 输出，
  以避免被审查内容污染溯源信息（详见该类的字段说明）。
'''

from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class DiffLine:
    '''git diff 中单行变更的统一表示。

    用于描述一个文件中被新增或删除的具体行，既承载行号信息也承载行内容，
    便于后续规则审查（如检测密钥泄漏、危险函数调用）按行定位问题。

    设计理由：将 file_path 一并放入行对象，可让下游在处理单行时无需再
    依赖外部上下文即可知道它归属哪个文件，简化多文件批处理逻辑。

    Attributes:
        file_path: 该行所属文件路径（变更后的新路径）。
        line_no: 行号；新增行用新文件中的行号，删除行用旧文件中的行号。
        content: 去掉 diff 前缀（+/-）后的实际行内容。
    '''
    file_path:str
    line_no:int
    content:str

@dataclass(frozen=True)
class DiffHunk:
    """A range of lines in the new version of a file described by one hunk.

    ``end_line`` is inclusive.  A zero-length new-file hunk is represented by
    ``end_line == start_line - 1``.

    中文说明：
    表示 git diff 中一个 ``@@ ... @@`` hunk 所覆盖的"新文件行号区间"，
    是后续上下文检索（按 hunk 拉取上下文行）和行号对齐的核心依据。

    设计理由：使用 ``frozen=True`` 不可变 dataclass。hunk 区间一旦由
    diff_parser 解析得出即属于"事实数据"，不应在后续阶段被修改，
    不可变设计可避免误改并允许作为字典 key / 集合元素使用。

    Attributes:
        start_line: hunk 在新文件中的起始行号（含）。
        end_line: hunk 在新文件中的结束行号（含）。
            纯删除型 hunk（新文件无新增行）记为 ``start_line - 1``。
    """
    start_line:int
    end_line:int

@dataclass
class ChangedFile:
    '''一个被 git diff 涉及的变更文件的完整结构化表示。

    由 diff_parser.parse_diff 产出，是 pipeline 中"文件粒度"的中间产物，
    贯穿上下文检索、规则审查、LLM 审查与报告生成各阶段。

    设计理由：
    - 将 added_lines / deleted_lines / hunks / patch 等多种视角的数据
      聚合到同一对象中，避免下游各模块各自重复解析 patch 字符串，降低重复计算。
    - old_path / is_rename 专门用于 rename 场景，让审查结果能正确归位到
      变更后的新路径，同时保留旧路径用于追溯。

    Attributes:
        path: 变更后的新文件路径（审查结果以此路径为准）。
        added_lines: 本次新增的所有行（每行一个 DiffLine，含行号与内容）。
        deleted_lines: 本次删除的所有行（行号为旧文件中的行号）。
        patch: 原始 patch 文本（``\\n`` 拼接），用于 LLM prompt 与日志回放。
        old_path: 仅在文件重命名时有值，表示重命名前的旧路径；否则为 None。
        is_rename: 是否为重命名场景；为 True 时 old_path 一定有值。
        hunks: 该文件包含的所有 DiffHunk 区间列表，默认空列表。
    '''
    path:str
    added_lines:List[DiffLine]
    deleted_lines:List[DiffLine]
    patch:str
    old_path:Optional[str]=None
    is_rename:bool=False
    hunks:List[DiffHunk]=field(default_factory=list)

@dataclass
class ReviewIssue:
    '''
    针对某一行代码发现的一个review问题

    这是审查 pipeline 最终对外暴露的"问题单元"，由规则引擎（rule）或 LLM 审查
    产生，经去重、排序后写入最终报告。该结构同时承载规则与 LLM 两类来源，
    因此需要可靠的溯源字段。

    【安全设计 - 极其重要】
    source 与 placement 两个字段是"系统侧赋值"的溯源/分发元数据，绝不能接收
    LLM 输出。原因：LLM 输出不可信，若允许 LLM 自行声明 source="rule"，
    可能把 LLM 幻觉问题伪装成确定性规则问题，污染审计与统计。系统在组装 issue
    时由代码显式注入这两字段，反序列化/合并时也需对它们做白名单过滤。

    file_path:问题出现在哪个文件
    line_no:问题出现在哪一行
    severity:问题的严重程度，是warning还是error或者info
    category:问题的类别
    message:问题的详细描述
    suggestion:改进建议
    reason:问题产生的理由说明（可选），便于读者理解为何这是问题
    confidence:LLM 给出的置信度 0~1（可选），规则产生的问题通常为 None
    evidence:支撑该问题的证据片段，例如相关代码行或引用

    '''
    file_path:str
    line_no:int
    severity:str
    category:str
    message:str
    suggestion:str
    reason:str=""
    confidence:Optional[float]=None
    evidence:str=""
    # System-assigned provenance: "rule" or "llm". Never accept it from LLM output.
    # 系统侧赋值的问题来源标记，取值 "rule"（规则引擎）或 "llm"（LLM 审查）；
    # 出于安全考虑，绝不接收 LLM 自报的来源，防止溯源被伪造。
    source:str=""
    # System-assigned publication target: "inline" or "summary". Never accept it from LLM output.
    # 系统侧赋值的发布位置：inline 表示逐行内联展示，summary 表示汇总到报告摘要；
    # 同样不接受 LLM 输出，避免 LLM 自行决定是否被弱化展示。
    placement:str="inline"

@dataclass
class FileContext:
    '''
    用来存储某个文件的上下文信息

    由上下文检索阶段（context retriever）产出，描述被读入用于辅助 LLM 审查的
    单个文件的快照。检索阶段会从仓库中读取候选文件并填充该结构，再依据
    ContextBudget 截断后拼装进 LLM prompt。

    设计理由：把"是否成功读取 / 是否截断 / 内容 / 选取理由"等元信息与内容
    一并打包，便于在 trace 与报告中解释"为什么给了 LLM 这些上下文"，
    提升可解释性与可调试性。

    Attributes:
        path: 文件在仓库中的相对路径。
        exists: 文件是否真实存在于工作区（False 表示读取失败或已删除）。
        content: 读取到的文件文本内容（可能已被截断）。
        truncated: 是否因超过预算而被截断；True 时 content 仅为文件前缀。
        chars_read: 实际读取的字符数，用于预算核算与统计。
        error: 读取时发生的错误信息（无错误为空串）。
        source: 上下文来源标记，如 "hunk_adjacent" / "imported" 等。
        selection_reason: 该文件被选入上下文的具体理由，便于可解释性输出。
    '''
    path:str
    exists: bool
    content: str
    truncated: bool
    chars_read:int
    error:str=""
    source:str=""
    selection_reason:str=""

@dataclass
class PythonSymbol:
    '''A Python class, function, or method located from a source line.

    中文说明：
    表示从源码中定位到的一个 Python 符号（类 / 函数 / 方法）及其行号区间，
    主要用于"按行号反查所属符号"，从而把 review issue 关联到具体的函数或类，
    便于给出更精准的建议与上下文。

    设计理由：以行号区间 [start_line, end_line] 作为索引依据，可支持 O(log n)
    的区间查找；保留 qualified_name 与 class_name 让报告能直接展示符号的全限定名。

    Attributes:
        name: 符号的简单名字（如函数名 func）。
        kind: 符号类型，如 "class" / "function" / "method"。
        start_line: 符号体在文件中的起始行号（含）。
        end_line: 符号体在文件中的结束行号（含）。
        source: 该符号的源码文本片段，便于直接展示给 LLM 或报告。
        qualified_name: 符号的全限定名（如 pkg.mod.Cls.method）。
        class_name: 若为方法，所属类名；否则为 None。
    '''
    name:str
    kind:str
    start_line:int
    end_line:int
    source:str
    qualified_name:str
    class_name:Optional[str]=None

@dataclass(frozen=True, init=False)
class ContextBudget:
    '''Limits the complete prompt sent to the LLM for one review run.

    File-context retrieval uses this value as an upper bound, but only the
    fully serialized prompt is authoritative.  ``max_context_chars`` is kept
    as a read-only compatibility alias for callers using the former name.

    中文说明：
    控制单次审查运行最终发给 LLM 的完整 prompt 的预算上限。上下文检索阶段
    以此为上界裁剪候选文件，但真正权威的是最终序列化后的 prompt 总长度——
    即检索只是尽量贴近预算，最终是否超限以序列化结果为准。

    【设计理由】
    - ``frozen=True`` + ``init=False``：frozen 使预算实例不可变，避免在 pipeline
      中被任何阶段偷偷调大导致 LLM 超支；init=False 禁用默认生成的 __init__，
      改用自定义 __init__ 以支持 max_context_chars 旧别名的兼容逻辑。
    - 兼容性别名 max_context_chars：早期字段名为 max_context_chars，后语义升级为
      max_prompt_chars（强调它限制的是整个 prompt 而非只是上下文）。为不破坏
      已有调用方，保留旧名为只读 property，平滑过渡。
    - 参数校验集中在 __init__：构造时即校验正数等约束，fail-fast，避免运行中
      因非法预算导致难以排查的截断行为。

    Attributes:
        max_prompt_chars: 最终发给 LLM 的 prompt 最大字符数（默认 4000）。
        max_extra_context_files: 除主 patch 外允许额外纳入上下文的文件数上限（默认 3）。
    '''
    max_prompt_chars:int
    max_extra_context_files:int

    def __init__(
            self,
            max_prompt_chars=4000,
            max_extra_context_files=3,
            *,
            max_context_chars=None,
        ):
        # 兼容旧字段名 max_context_chars：若调用方传入，则与 max_prompt_chars 保持一致，
        # 两者同时传入且不一致时报错，防止语义歧义。
        if max_context_chars is not None:
            if max_prompt_chars != 4000 and max_prompt_chars != max_context_chars:
                raise ValueError(
                    "max_prompt_chars and max_context_chars must match when both are set"
                )
            max_prompt_chars = max_context_chars

        # fail-fast 校验：预算必须为正，额外文件数不可为负。
        if max_prompt_chars <= 0:
            raise ValueError("max_prompt_chars must be greater than 0")
        if max_extra_context_files < 0:
            raise ValueError("max_extra_context_files must be greater than or equal to 0")

        # 由于 dataclass 为 frozen，常规赋值会触发 FrozenInstanceError，
        # 这里绕过 __setattr__ 直接通过 object.__setattr__ 写入字段，
        # 是在不可变 dataclass 的自定义 __init__ 中初始化字段的标准做法。
        object.__setattr__(self, "max_prompt_chars", max_prompt_chars)
        object.__setattr__(self, "max_extra_context_files", max_extra_context_files)

    @property
    def max_context_chars(self):
        '''Compatibility alias; use ``max_prompt_chars`` for new code.

        中文说明：旧字段名兼容别名，新代码请使用 max_prompt_chars。
        '''
        return self.max_prompt_chars

@dataclass
class LLMReviewResult:
    '''LLM 审查阶段的返回包装结构。

    封装一次 LLM 审查调用的最终结果，既包含解析后的问题列表，也保留原始输出
    与解析过程状态，便于失败诊断、trace 回放与重试逻辑判断。

    设计理由：将 raw_output 与解析后的 issue 一并返回，是为了在 LLM 输出格式
    异常时仍可回溯原始文本；valid / repaired / error 字段让调用方无需捕获异常
    即可判断本次调用是否可用、是否经过修复，简化上层控制流。

    Attributes:
        issue: 从 LLM 输出中解析出的 ReviewIssue 列表（可能为空）。
        raw_output: LLM 返回的原始文本，用于 trace 与调试。
        valid: 解析结果是否可用（True 时 issue 列表可信）。
        repaired: 是否经过修复（如 JSON 损坏后被修复）才得到可用结果。
        error: 解析/调用过程中遇到的错误信息（无错误为空串）。
    '''
    issue: List[ReviewIssue]
    raw_output: str
    valid: bool
    repaired: bool=False
    error: str=""
