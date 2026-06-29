'''
根据diff里面的变更行，到真实文件当中找上下文
比如说120行变了，找到前后20行，100-140
然后把上下文挂到 changed_file.hunks.context_lines 里面

后面可以实现函数边界和class边界，使用tree-sitter来解析python文件，找到函数和类的边界，然后把上下文挂到 changed_file.hunks.context_lines 里面
'''

from pathlib import Path

from .schemas import FileContext

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
        content = target_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _error_context(file_path, f"File {file_path} is not a valid UTF-8 text file")
    except OSError as e:
        return _error_context(file_path, f"Error reading file {file_path}: {str(e)}")
    
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]

    return FileContext(
        path=file_path,
        exists=True,
        content=content,
        truncated=truncated,
        chars_read=len(content),
        error="",
    )

def collect_file_contexts(repo_root, changed_files, max_chars=4000):
    contexts = []

    for changed_file in changed_files:
        context = read_file_context(
            repo_root=repo_root,
            file_path=changed_file.path,
            max_chars=max_chars
        )
        contexts.append(context)
    return contexts