'''
用来定义项目当中的数据结构

让模块之间传递的数据有统一的格式
'''

from dataclasses import dataclass
from typing import List, Optional

@dataclass
class DiffLine:
    file_path:str
    line_no:int
    content:str

@dataclass
class ChangedFile:
    path:str
    added_lines:List[DiffLine]
    deleted_lines:List[DiffLine]
    patch:str
    old_path:Optional[str]=None
    is_rename:bool=False

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

@dataclass
class LLMReviewResult:
    issue: List[ReviewIssue]
    raw_output: str
    valid: bool
    repaired: bool=False
    error: str=""
