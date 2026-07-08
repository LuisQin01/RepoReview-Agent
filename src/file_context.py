'''
根据diff里面的变更行，到真实文件当中找上下文
比如说120行变了，找到前后20行，100-140
然后把上下文挂到 changed_file.hunks.context_lines 里面

后面可以实现函数边界和class边界，使用tree-sitter来解析python文件，找到函数和类的边界，然后把上下文挂到 changed_file.hunks.context_lines 里面
'''
import re

from pathlib import Path
from .schemas import FileContext

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

def read_file_context(repo_root, file_path, max_chars=4000):
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
            content = f.read(max_chars + 1)

        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
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
        max_chars=4000,
        max_extra_files=3
    ):
    repo_root_path=Path(repo_root).resolve()

    contexts = []
    seen_paths=set()

    def add_context(file_path):
        if file_path in seen_paths:
            return
        seen_paths.add(file_path)
        contexts.append(
            read_file_context(
                repo_root=repo_root,
                file_path=file_path,
                max_chars=max_chars,
            )
        )

    for changed_file in changed_files:
        add_context(changed_file.path)

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
                content=f.read(max_chars)
        except OSError:
            continue
            
        if any(re.search(rf"\bdef\s+{re.escape(name)}\b", content) for name in call_names):
            extra_candidates.append(rel_path)

    for file_path in extra_candidates:
        if len(seen_paths)>=len(changed_files) + max_extra_files:
            break
        add_context(file_path)

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
