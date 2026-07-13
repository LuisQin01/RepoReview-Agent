from dataclasses import dataclass, field
from typing import List
from time import perf_counter
from uuid import uuid4

from .schemas import ChangedFile, ContextBudget, FileContext, ReviewIssue

@dataclass
class ReviewState:
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
    task_id:str=field(default_factory=lambda:uuid4().hex[:8])
    started_at_perf:float=field(default_factory=perf_counter)
    trace_path:str=""

