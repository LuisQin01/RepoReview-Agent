'''
根据diff里面的变更行，到真实文件当中找上下文
比如说120行变了，找到前后20行，100-140
然后把上下文挂到 changed_file.hunks.context_lines 里面

Python 文件可以使用标准库 ast 定位变更行所属的函数、方法或类。
'''
import ast
import re

from pathlib import Path
from .schemas import ContextBudget, DiffHunk, FileContext, PythonSymbol

IGNORED_DIRS={".git", "__pycache__", ".venv", "venv", "node_modules", "traces"}

def _error_context(file_path,message):
    return FileContext(
        path=file_path,
        exists=False,
        content="",
        truncated=False,
        chars_read=0,
        error=message
    )

def locate_python_symbol(source, line_no):
    """Return the innermost Python symbol containing ``line_no``.

    Invalid Python and lines outside a symbol deliberately return ``None`` so
    callers can fall back to their existing file-level context.
    """
    if line_no < 1:
        return None

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    source_lines = source.splitlines(keepends=True)
    symbols = []

    def visit(node, parents=(), class_names=()):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            decorator_lines = [decorator.lineno for decorator in node.decorator_list]
            start_line = min([node.lineno, *decorator_lines])
            end_line = node.end_lineno or node.lineno
            parent = parents[-1] if parents else None

            if isinstance(node, ast.ClassDef):
                kind = "class"
                next_class_names = (*class_names, node.name)
            else:
                kind = "method" if isinstance(parent, ast.ClassDef) else "function"
                next_class_names = class_names

            symbol_names = [parent_node.name for parent_node in parents]
            qualified_name = ".".join([*symbol_names, node.name])
            symbols.append(
                PythonSymbol(
                    name=node.name,
                    kind=kind,
                    start_line=start_line,
                    end_line=end_line,
                    source="".join(source_lines[start_line - 1:end_line]),
                    qualified_name=qualified_name,
                    class_name=".".join(class_names) or None,
                )
            )
            parents = (*parents, node)
            class_names = next_class_names

        for child in ast.iter_child_nodes(node):
            visit(child, parents, class_names)

    visit(tree)

    containing_symbols = [
        symbol
        for symbol in symbols
        if symbol.start_line <= line_no <= symbol.end_line
    ]
    if not containing_symbols:
        return None

    return min(
        containing_symbols,
        key=lambda symbol: (symbol.end_line - symbol.start_line, -symbol.start_line),
    )

def _truncate_to_changed_lines(content, changed_line_nos, max_chars):
    if len(content) <= max_chars:
        return content, False

    valid_line_nos = sorted({
        line_no
        for line_no in changed_line_nos or []
        if isinstance(line_no, int) and line_no > 0
    })
    if not valid_line_nos:
        return content[:max_chars], True

    lines = content.splitlines(keepends=True)
    selected = []
    remaining_chars = max_chars

    for line_no in valid_line_nos:
        if line_no > len(lines) or remaining_chars == 0:
            continue
        line = lines[line_no - 1]
        selected.append(line[:remaining_chars])
        remaining_chars -= len(selected[-1])

    if selected:
        return "".join(selected), True

    return content[:max_chars], True


def _normalize_hunks(changed_hunks):
    """Return valid, ordered new-file hunk ranges without merging them."""
    normalized = []
    for hunk in changed_hunks or []:
        if isinstance(hunk, DiffHunk):
            start_line, end_line = hunk.start_line, hunk.end_line
        elif isinstance(hunk, (tuple, list)) and len(hunk) == 2:
            start_line, end_line = hunk
        else:
            continue

        if (
            isinstance(start_line, int)
            and isinstance(end_line, int)
            and start_line > 0
            and end_line >= start_line
        ):
            normalized.append((start_line, end_line))

    return sorted(set(normalized))


def _truncate_to_changed_hunks(content, changed_hunks, max_chars):
    """Keep complete new-file hunks when possible, then a single oversized hunk.

    Hunks are considered in source order and remain separate selections.  If a
    hunk fits within the total limit but not the remaining budget, it is
    skipped rather than partially copied.  Partial copying is only used when
    one hunk itself exceeds the complete budget, in which case its contiguous
    prefix is returned.  This preserves predictable hunk-level semantics.
    """
    if len(content) <= max_chars:
        return content, False

    hunk_ranges = _normalize_hunks(changed_hunks)
    if not hunk_ranges:
        return None

    lines = content.splitlines(keepends=True)
    selected = []
    remaining_chars = max_chars

    for start_line, end_line in hunk_ranges:
        if start_line > len(lines):
            continue

        hunk_content = "".join(lines[start_line - 1:min(end_line, len(lines))])
        if not hunk_content:
            continue

        if len(hunk_content) <= remaining_chars:
            selected.append(hunk_content)
            remaining_chars -= len(hunk_content)
            continue

        if len(hunk_content) > max_chars and not selected:
            return hunk_content[:max_chars], True

    if selected:
        return "".join(selected), True

    return None


def read_file_context(
        repo_root,
        file_path,
        max_chars=4000,
        changed_line_nos=None,
        changed_hunks=None,
    ):
    """Read one file context, preferring changed hunks within the limit.

    ``changed_line_nos`` remains supported for callers that have not yet
    supplied parsed hunk ranges.
    """
    repo_root_path = Path(repo_root).resolve()
    target_path = (repo_root_path / file_path).resolve()

    # 检查文件是否在仓库根目录下，如果不在，返回错误
    try:
        target_path.relative_to(repo_root_path)
    except ValueError:
        return _error_context(
            file_path,
            f"File {file_path} is outside of the repository root {repo_root}"
        )

    # 如果文件不存在，返回错误
    if not target_path.exists():
        return _error_context(file_path, f"File {file_path} does not exist")

    # 如果是目录，返回错误
    if target_path.is_dir():
        return _error_context(file_path, f"File {file_path} is a directory")

    # 读取文件内容，如果文件过大，只读取前max_chars个字符
    try:
        with target_path.open("r", encoding="utf-8") as f:
            if (changed_line_nos or changed_hunks) and max_chars > 0:
                full_content = f.read()
            else:
                full_content = f.read(max_chars + 1)

        hunk_result = _truncate_to_changed_hunks(
            full_content,
            changed_hunks,
            max_chars,
        )
        if hunk_result is not None:
            content, truncated = hunk_result
        else:
            content, truncated = _truncate_to_changed_lines(
                full_content,
                changed_line_nos,
                max_chars,
            )
    except UnicodeDecodeError:
        return _error_context(file_path, f"File {file_path} is not a valid UTF-8 text file")
    except OSError as e:
        return _error_context(file_path, f"Error reading file {file_path}: {str(e)}")

    return FileContext(
        path=file_path,
        exists=True,
        content=content,
        truncated=truncated,
        chars_read=len(content),
        error="",
    )

def collect_file_contexts(
        repo_root,
        changed_files,
        context_budget,
    ):
    repo_root_path=Path(repo_root).resolve()

    contexts = []
    seen_paths=set()
    # The prompt builder enforces the authoritative total budget.  This cap
    # prevents context retrieval alone from exceeding that limit first.
    remaining_chars=context_budget.max_prompt_chars

    def add_context(
            file_path,
            changed_line_nos=None,
            changed_hunks=None,
        ):
        nonlocal remaining_chars
        if file_path in seen_paths:
            return False
        seen_paths.add(file_path)
        context = read_file_context(
            repo_root=repo_root,
            file_path=file_path,
            max_chars=remaining_chars,
            changed_line_nos=changed_line_nos,
            changed_hunks=changed_hunks,
        )
        contexts.append(context)
        remaining_chars -= context.chars_read
        return True

    for changed_file in changed_files:
        add_context(
            changed_file.path,
            changed_line_nos=[line.line_no for line in changed_file.added_lines],
            changed_hunks=changed_file.hunks,
        )

    extra_candidates=[]

    for context in contexts:
        if not context.exists:
            continue

        for candidate in _extract_import_file_candidates(context.content, context.path):
            candidate_path=(repo_root_path/candidate).resolve()

            try:
                candidate_path.relative_to(repo_root_path)
            except ValueError:
                continue

            if candidate_path.exists():
                rel_path=str(candidate_path.relative_to(repo_root_path)).replace("\\","/")
                extra_candidates.append(rel_path)

    call_names=set()
    changed_paths={changed_file.path for changed_file in changed_files}

    for changed_file in changed_files:
        call_names.update(_extract_added_call_names(changed_file))

    for py_file in _iter_python_files(repo_root_path):
        rel_path=str(py_file.relative_to(repo_root_path)).replace("\\", "/")

        if rel_path in changed_paths:
            continue

        try:
            with py_file.open("r", encoding="utf-8", errors="ignore") as f:
                content=f.read(context_budget.max_prompt_chars)
        except OSError:
            continue

        if any(re.search(rf"\bdef\s+{re.escape(name)}\b", content) for name in call_names):
            extra_candidates.append(rel_path)

    extra_context_count=0
    for file_path in extra_candidates:
        if (
            extra_context_count >= context_budget.max_extra_context_files
            or remaining_chars == 0
        ):
            break
        if add_context(file_path):
            extra_context_count += 1

    return contexts

def _iter_python_files(repo_root_path):
    for path in repo_root_path.rglob("*.py"):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def _module_to_candidate_paths(module_path):
    return [
        module_path.with_suffix(".py"),
        module_path / "__init__.py",
    ]


def _resolve_relative_base(current_file_path, dot_count):
    base_dir = Path(current_file_path).parent

    for _ in range(dot_count - 1):
        base_dir = base_dir.parent

    return base_dir


def _split_import_items(import_part, allow_dots=True):
    names = []
    pattern = r"^[a-zA-Z_][\w\.]*$" if allow_dots else r"^[a-zA-Z_][\w]*$"

    for item in import_part.split(","):
        name = item.strip().split(" as ")[0].strip()
        if re.match(pattern, name):
            names.append(name)

    return names


def _extract_import_file_candidates(content, current_file_path):
    candidates = []

    for line in content.splitlines():
        line = line.strip()

        match = re.match(
            r"^from\s+(\.*)([a-zA-Z_][\w\.]*)?\s+import\s+(.+)$",
            line,
        )
        if match:
            dots, module_name, import_part = match.groups()

            if dots:
                base_dir = _resolve_relative_base(
                    current_file_path=current_file_path,
                    dot_count=len(dots),
                )

                if module_name:
                    module_path = base_dir / Path(*module_name.split("."))
                    candidates.extend(_module_to_candidate_paths(module_path))
                else:
                    for name in _split_import_items(import_part, allow_dots=False):
                        candidates.extend(_module_to_candidate_paths(base_dir / name))
            else:
                if module_name:
                    module_path = Path(*module_name.split("."))
                    candidates.extend(_module_to_candidate_paths(module_path))

            continue

        match = re.match(r"^import\s+(.+)$", line)
        if match:
            for module_name in _split_import_items(match.group(1), allow_dots=True):
                module_path = Path(*module_name.split("."))
                candidates.extend(_module_to_candidate_paths(module_path))

    return [
        str(candidate).replace("\\", "/")
        for candidate in candidates
    ]


def _extract_added_call_names(changed_file):
    names=set()

    ignored={"if", "for", "while", "return", "def", "class", "print"}

    for line in changed_file.added_lines:
        for name in re.findall(r"\b([a-zA-Z_][\w]*)\s*\(",line.content):
            if name not in ignored:
                names.add(name)

    return names
