from dataclasses import dataclass, field
from typing import List

from .schemas import ChangedFile, FileContext, ReviewIssue

@dataclass
class ReviewState:
    diff_path:str
    repo_root:str
    output_format:str
    use_llm:bool
    max_context_chars:int

    diff_text:str=""
    changed_files:List[ChangedFile]=field(default_factory=list)
    contexts:List[FileContext]=field(default_factory=list)

    rule_issues:List[ReviewIssue]=field(default_factory=list)
    llm_issues:List[ReviewIssue]=field(default_factory=list)
    issues:List[ReviewIssue]=field(default_factory=list)

    output:str=""
    trace_steps:List[dict]=field(default_factory=list)
    errors:List[str]=field(default_factory=list)