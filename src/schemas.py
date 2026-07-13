'''
用来定义项目当中的数据结构

让模块之间传递的数据有统一的格式
'''

from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class DiffLine:
    file_path:str
    line_no:int
    content:str

@dataclass(frozen=True)
class DiffHunk:
    """A range of lines in the new version of a file described by one hunk.

    ``end_line`` is inclusive.  A zero-length new-file hunk is represented by
    ``end_line == start_line - 1``.
    """
    start_line:int
    end_line:int

@dataclass
class ChangedFile:
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
    file_path:问题出现在哪个文件
    line_no:问题出现在哪一行
    severity:问题的严重程度，是warning还是error或者info
    category:问题的类别
    message:问题的详细描述
    suggestion:改进建议

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
    source:str=""

@dataclass
class FileContext:
    '''
    用来存储某个文件的上下文信息
    '''
    path:str
    exists: bool
    content: str
    truncated: bool
    chars_read:int
    error:str=""

@dataclass(frozen=True, init=False)
class ContextBudget:
    '''Limits the complete prompt sent to the LLM for one review run.

    File-context retrieval uses this value as an upper bound, but only the
    fully serialized prompt is authoritative.  ``max_context_chars`` is kept
    as a read-only compatibility alias for callers using the former name.
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
        if max_context_chars is not None:
            if max_prompt_chars != 4000 and max_prompt_chars != max_context_chars:
                raise ValueError(
                    "max_prompt_chars and max_context_chars must match when both are set"
                )
            max_prompt_chars = max_context_chars

        if max_prompt_chars <= 0:
            raise ValueError("max_prompt_chars must be greater than 0")
        if max_extra_context_files < 0:
            raise ValueError("max_extra_context_files must be greater than or equal to 0")

        object.__setattr__(self, "max_prompt_chars", max_prompt_chars)
        object.__setattr__(self, "max_extra_context_files", max_extra_context_files)

    @property
    def max_context_chars(self):
        '''Compatibility alias; use ``max_prompt_chars`` for new code.'''
        return self.max_prompt_chars

@dataclass
class LLMReviewResult:
    issue: List[ReviewIssue]
    raw_output: str
    valid: bool
    repaired: bool=False
    error: str=""
