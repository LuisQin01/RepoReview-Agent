'''
根据diff里面的变更行，到真实文件当中找上下文
比如说120行变了，找到前后20行，100-140
然后把上下文挂到 changed_file.hunks.context_lines 里面

Python 文件可以使用标准库 ast 定位变更行所属的函数、方法或类。

模块职责：
    本模块是 RepoReview Agent 的「上下文检索核心」。
    给定一次 PR 的 diff（变更文件、变更行、变更 hunk），它会回到真实仓库
    中读取相应文件的源码，按预算截取与变更相关的上下文片段，最终包装成
    FileContext 列表供下游 prompt builder 拼装进 LLM 的输入。

    一个核心例子：某文件第 120 行发生了变更，则在源文件中截取 100-140 行
    作为上下文，并通过 ast 定位该行所属的函数/方法/类，把符号信息一并
    挂到 changed_file.hunks.context_lines 上，便于 LLM 理解代码语义边界。

在整体架构中的位置：
    diff 解析（diff_parser）→ 上下文检索（本模块 file_context）→
    prompt 构建（prompt_builder）→ LLM 调用（reviewer）。
    本模块处于 diff 与 LLM 之间，决定了「LLM 能看到什么」。

设计理由：
    1. 按 hunk 优先截断：保留完整 hunk 才能让 LLM 看到完整的语义块，
       而不是断章取义的若干行；只有在单个 hunk 自身超出预算时才做前缀截断。
    2. 敏感文件黑名单：作为三层敏感防护的 L1 层，确保密钥/证书/配置等
       不会进入 LLM payload，避免泄露。
    3. 跨文件上下文：除变更文件本身外，还通过解析 import 语句、新增调用名
       拉取相关定义文件，扩大 LLM 的视野，提升审查准确性。
    4. 预算控制：通过 ContextBudget 限制总字符数，避免 prompt 超长。
'''
import ast
import re

from pathlib import Path
from .schemas import ContextBudget, DiffHunk, FileContext, PythonSymbol

# 仓库遍历时需要跳过的目录：版本控制、缓存、虚拟环境、依赖、运行时 trace 等。
# 这些目录体积大且与代码审查无关，跳过可显著减少扫描开销（性能优化点）。
IGNORED_DIRS={".git", "__pycache__", ".venv", "venv", "node_modules", "traces"}

# 敏感文件名黑名单：环境变量文件、shell 启动钩子、SSH 私钥。
# 这是三层敏感防护的 L1 层（文件名精确匹配），防止密钥直接进入 LLM payload。
SENSITIVE_FILE_NAMES = {".env", ".envrc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}

# 敏感文件后缀黑名单：各类证书/私钥文件（PEM/KEY/CRT 等）。
# L1 层防护的「后缀匹配」部分，覆盖常见密钥格式。
SENSITIVE_FILE_SUFFIXES = {".pem", ".key", ".crt", ".cer", ".der", ".p12", ".pfx"}

# 敏感配置文件后缀：YAML/JSON 配置文件本身并非全部敏感，
# 需要配合文件名（settings.json）、目录（config/）、或部署变量（values.yaml）
# 进一步判定，详见 _is_sensitive_file_path。
SENSITIVE_CONFIG_SUFFIXES = {".yaml", ".yml", ".json"}


def locate_python_symbol(source, line_no):
    """定位包含指定行号的最内层 Python 符号（类/函数/方法）。

    给定源码和一行行号，利用标准库 ast 解析源码并递归遍历 AST，
    找到所有包含该行的符号，再返回跨度最小（最内层）的那个。
    例如第 120 行位于 ``class A`` 的 ``def m`` 中，则返回方法 ``m``，
    而不是外层的 ``A``。

    非法 Python 代码、或行号落在任何符号之外时，刻意返回 ``None``，
    以便调用方回退到已有的文件级上下文，而不是抛异常打断流程。

    Args:
        source (str): 完整的 Python 源码文本。
        line_no (int): 待定位的 1-based 行号。

    Returns:
        PythonSymbol | None: 包含该行的最内层符号；若解析失败或行号
            不在任何符号内，则返回 ``None``。

    设计理由：
        选择「最内层」而非「最外层」，是因为审查一行代码时最关心的
        是它所在的函数/方法的语义边界；外层类信息通过 qualified_name
        和 class_name 字段保留，信息不丢失。
    """
    # 行号非法直接返回 None，避免 ast.parse 后做无意义的过滤
    if line_no < 1:
        return None

    # 解析 AST；源码有语法错误（如 PR 中途状态）时安静失败，返回 None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    # 保留换行符以便后面按行号切片得到原始 source 文本
    source_lines = source.splitlines(keepends=True)
    symbols = []

    def visit(node, parents=(), class_names=()):
        """递归遍历 AST 节点，收集所有类/函数/方法符号。

        Args:
            node: 当前 AST 节点。
            parents: 从根到当前节点路径上的所有祖先符号节点，
                     用于计算 qualified_name。
            class_names: 当前嵌套层级的类名链，用于填充 class_name 字段。
        """
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            # 关键：装饰器的行号通常早于 def/class 行，需要把装饰器纳入
            # 符号起始行，否则带装饰器的符号起始位置会被算错。
            decorator_lines = [decorator.lineno for decorator in node.decorator_list]
            start_line = min([node.lineno, *decorator_lines])
            end_line = node.end_lineno or node.lineno
            parent = parents[-1] if parents else None

            # 区分符号类型：class / method（直接父节点是 ClassDef）/ function
            if isinstance(node, ast.ClassDef):
                kind = "class"
                # 进入类体时把当前类名加入 class_names，供子方法使用
                next_class_names = (*class_names, node.name)
            else:
                kind = "method" if isinstance(parent, ast.ClassDef) else "function"
                next_class_names = class_names

            # 拼接 qualified_name：A.B.c 表示 A 类的 B 内部类的方法 c
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
            # 把当前节点入栈，继续向子节点递归（保留嵌套关系）
            parents = (*parents, node)
            class_names = next_class_names

        # 深度优先遍历所有子节点，确保嵌套符号都被收集
        for child in ast.iter_child_nodes(node):
            visit(child, parents, class_names)

    visit(tree)

    # 过滤出所有覆盖 line_no 的符号（可能有嵌套：类 + 内部方法）
    containing_symbols = [
        symbol
        for symbol in symbols
        if symbol.start_line <= line_no <= symbol.end_line
    ]
    if not containing_symbols:
        return None

    # 选跨度最小的符号：行数最少 = 最内层。
    # 次序键 -start_line 用于在跨度相同时优先选较晚开始的（更靠内）。
    return min(
        containing_symbols,
        key=lambda symbol: (symbol.end_line - symbol.start_line, -symbol.start_line),
    )

def _error_context(file_path, message, source="", selection_reason=""):
    """构造一个表示「读取失败」的 FileContext。

    统一的错误返回格式：exists=False、content 为空、chars_read=0，
    并把失败原因写入 error 字段。保留 source / selection_reason 便于
    上游追溯该上下文是被哪个策略选中的、为什么失败。

    Args:
        file_path (str): 失败的文件相对路径。
        message (str): 失败原因描述。
        source (str): 选择来源（如 changed_file / import_candidate）。
        selection_reason (str): 选择该文件的语义原因说明。

    Returns:
        FileContext: 标记为不存在、内容为空的错误上下文对象。
    """
    return FileContext(
        path=file_path,
        exists=False,
        content="",
        truncated=False,
        chars_read=0,
        error=message,
        source=source,
        selection_reason=selection_reason,
    )


def _is_sensitive_file_path(file_path):
    """判断文件路径是否属于敏感文件，需拦截不进入 LLM payload。

    综合文件名、后缀、路径结构三层信号判定：
      1. 文件名命中 SENSITIVE_FILE_NAMES（如 .env、id_rsa）；
      2. 文件名以 ``.env.`` 开头（覆盖 .env.production 等变体）；
      3. 后缀命中 SENSITIVE_FILE_SUFFIXES（如 .pem、.key）；
      4. 配置文件（yaml/yml/json）且满足以下任一条件视为敏感：
         - 文件名为 settings.json；
         - 路径中包含 config 目录；
         - 路径中包含 deploy 目录且文件名为 values（典型 Helm values.yaml）。

    这是三层敏感防护的 L1 层，目的是把密钥/证书/部署配置挡在 LLM 之外。

    Args:
        file_path (str): 相对仓库根的文件路径。

    Returns:
        bool: True 表示敏感，应跳过读取；False 表示可读取。
    """
    path = Path(file_path)
    filename = path.name.lower()
    suffix = path.suffix.lower()
    stem = path.stem.lower()
    # 路径中除文件名外的所有目录段，全部转小写后存为集合，便于 ``in`` 判断
    parent_parts = {part.lower() for part in path.parts[:-1]}
    # 配置类文件需配合文件名/目录结构二次判定，避免误伤普通 yaml/json
    is_sensitive_config = (
        suffix in SENSITIVE_CONFIG_SUFFIXES
        and (
            filename == "settings.json"
            or "config" in parent_parts
            or ("deploy" in parent_parts and stem == "values")
        )
    )
    return (
        filename in SENSITIVE_FILE_NAMES
        or filename.startswith(".env.")
        or suffix in SENSITIVE_FILE_SUFFIXES
        or is_sensitive_config
    )

def _truncate_to_changed_lines(content, changed_line_nos, max_chars):
    """按变更行号截断内容到 ``max_chars`` 预算内（行级回退方案）。

    当没有可用 hunk 信息时使用此函数：仅保留发生变更的具体行，
    按行号顺序逐行纳入预算，直到预算耗尽。返回 (截断后内容, 是否截断)。

    与 hunk 优先策略相比，此方案保留的是「孤立行」，缺少上下文连续性，
    因此只在 _truncate_to_changed_hunks 返回 None 时作为兜底使用。

    Args:
        content (str): 文件完整内容。
        changed_line_nos (list[int] | None): 变更行号列表（1-based）。
        max_chars (int): 允许保留的最大字符数。

    Returns:
        tuple[str, bool]: (截断后的内容, 是否发生了截断)。
            内容未超预算时直接原样返回且 truncated=False。
    """
    # 内容本身就在预算内，无需任何截断
    if len(content) <= max_chars:
        return content, False

    # 过滤出合法的（int 且 >0）行号并去重排序，便于按顺序遍历
    valid_line_nos = sorted({
        line_no
        for line_no in changed_line_nos or []
        if isinstance(line_no, int) and line_no > 0
    })
    # 没有任何有效行号时退化为「前缀截断」，保证至少有内容可看
    if not valid_line_nos:
        return content[:max_chars], True

    lines = content.splitlines(keepends=True)
    selected = []
    remaining_chars = max_chars

    # 按变更行号顺序逐行纳入：超出行范围或预算耗尽则跳过
    for line_no in valid_line_nos:
        if line_no > len(lines) or remaining_chars == 0:
            continue
        line = lines[line_no - 1]
        # 当前行可能超出剩余预算，只取前 remaining_chars 个字符
        selected.append(line[:remaining_chars])
        remaining_chars -= len(selected[-1])

    if selected:
        return "".join(selected), True

    # 兜底：所有变更行号都越界，则回退到前缀截断
    return content[:max_chars], True


def _normalize_hunks(changed_hunks):
    """规范化 hunk 列表为有序、去重的新文件行范围元组列表。

    支持两种输入格式：
      - DiffHunk dataclass（使用其 start_line / end_line）；
      - 长度为 2 的 tuple/list（视为 (start_line, end_line)）。

    过滤掉非法范围（非 int、start_line<=0、end_line<start_line），
    用 set 去重后再按起始行升序排序。注意：本函数故意不合并相邻
    或重叠的范围，保留原始 hunk 边界，便于 _truncate_to_changed_hunks
    按 hunk 级别独立决策。

    Args:
        changed_hunks (list | None): 原始 hunk 列表。

    Returns:
        list[tuple[int, int]]: 排序去重后的 (start_line, end_line) 列表，
            可能为空。
    """
    normalized = []
    for hunk in changed_hunks or []:
        # 兼容两种数据格式：DiffHunk 对象或二元组
        if isinstance(hunk, DiffHunk):
            start_line, end_line = hunk.start_line, hunk.end_line
        elif isinstance(hunk, (tuple, list)) and len(hunk) == 2:
            start_line, end_line = hunk
        else:
            continue

        # 仅保留合法范围：两端均为 int，start>0，end>=start
        if (
            isinstance(start_line, int)
            and isinstance(end_line, int)
            and start_line > 0
            and end_line >= start_line
        ):
            normalized.append((start_line, end_line))

    # set 去重 + 排序，保证后续按源码顺序处理且无重复
    return sorted(set(normalized))


def _truncate_to_changed_hunks(content, changed_hunks, max_chars):
    """按 hunk 优先策略在预算内截断内容，返回 (内容, 是否截断) 或 None。

    与 _truncate_to_changed_lines 的行级回退不同，本函数以「完整 hunk」
    为最小取舍单位：

      - 内容未超预算：原样返回，truncated=False；
      - 有 hunk 信息时按源码顺序逐个判断：
        * hunk 能塞进剩余预算 → 完整保留；
        * hunk 塞不进剩余预算但未超总预算 → 整个跳过（不做半截截断，
          保持 hunk 级语义的可预测性）；
        * 单个 hunk 自身就超过总预算且尚未保留任何内容 → 取其前缀
          ``max_chars`` 字符，truncated=True；
      - 没有 hunk 信息或所有 hunk 都被跳过 → 返回 None，由调用方回退
        到 _truncate_to_changed_lines。

    设计理由：保留完整 hunk 才能让 LLM 看到完整语义块，断章取义的半截
    hunk 反而会误导模型；只有当单个 hunk 必然要截断时才使用前缀截断。

    Args:
        content (str): 文件完整内容。
        changed_hunks (list | None): hunk 列表（DiffHunk 或二元组）。
        max_chars (int): 允许保留的最大字符数。

    Returns:
        tuple[str, bool] | None: (截断后的内容, 是否截断)；
            若没有可用 hunk 信息则返回 None，提示调用方走兜底逻辑。
    """
    # 内容未超预算，直接全部返回
    if len(content) <= max_chars:
        return content, False

    hunk_ranges = _normalize_hunks(changed_hunks)
    # 没有 hunk 信息：返回 None 让调用方回退到行级截断
    if not hunk_ranges:
        return None

    lines = content.splitlines(keepends=True)
    selected = []
    remaining_chars = max_chars

    # 按源码顺序处理每个 hunk，独立决定保留/跳过/前缀截断
    for start_line, end_line in hunk_ranges:
        # 起始行超出文件实际行数，该 hunk 完全无效，跳过
        if start_line > len(lines):
            continue

        # 切出 hunk 对应的源码片段，end_line 用 min 防止越界
        hunk_content = "".join(lines[start_line - 1:min(end_line, len(lines))])
        if not hunk_content:
            continue

        # 情况 1：hunk 能完整塞进剩余预算 → 保留
        if len(hunk_content) <= remaining_chars:
            selected.append(hunk_content)
            remaining_chars -= len(hunk_content)
            continue

        # 情况 2：hunk 超过总预算，且当前还没保留任何内容 → 取前缀截断
        # 要求 not selected 是为了避免「前面已经塞了一些小 hunk，又遇到
        # 大 hunk」时半截截断造成语义混乱。
        if len(hunk_content) > max_chars and not selected:
            return hunk_content[:max_chars], True

        # 情况 3：塞不进剩余预算但未超总预算 → 整个跳过，继续看下一个 hunk

    if selected:
        return "".join(selected), True

    # 所有 hunk 都被跳过（如都超出剩余预算但有部分小于总预算），
    # 返回 None 让调用方回退到行级截断
    return None


def read_file_context(
        repo_root,
        file_path,
        max_chars=4000,
        changed_line_nos=None,
        changed_hunks=None,
        source="",
        selection_reason="",
    ):
    """读取单个文件的上下文，优先按 hunk 截断以保留完整语义块。

    本函数是单文件上下文读取的核心入口，按以下顺序进行多重检查与处理：
      1. 路径越界检查：防止 ``../`` 等构造的路径逃出仓库根目录；
      2. 敏感文件拦截：调用 _is_sensitive_file_path，命中则直接返回错误；
      3. 文件存在性检查：不存在则返回错误；
      4. 目录检查：路径指向目录则返回错误；
      5. UTF-8 解码读取：解码失败或操作系统错误均转成错误上下文；
      6. 截断策略：优先 _truncate_to_changed_hunks（按完整 hunk），
         返回 None 时回退到 _truncate_to_changed_lines（按行）。

    性能考虑：当没有 changed_line_nos / changed_hunks 且 max_chars>0 时，
    只读取前 ``max_chars+1`` 个字符（多读 1 字符用于判断是否需要截断），
    避免把超大文件整体读入内存。有变更信息时才整体读取以便按 hunk 截断。

    Args:
        repo_root (str | Path): 仓库根目录绝对路径。
        file_path (str): 相对仓库根的文件路径。
        max_chars (int): 允许读取的最大字符数，默认 4000。
        changed_line_nos (list[int] | None): 变更行号（兜底方案用）。
            保留参数是为了兼容尚未提供 hunk 范围的调用方。
        changed_hunks (list | None): hunk 范围列表，优先使用。
        source (str): 选择来源标记（changed_file / import_candidate 等）。
        selection_reason (str): 该文件被选中的语义原因。

    Returns:
        FileContext: 读取成功的上下文（exists=True）或错误上下文
            （exists=False，error 字段说明原因）。
    """
    repo_root_path = Path(repo_root).resolve()
    # resolve() 会展开符号链接和 ``..``，relative_to 用于检测路径越界
    target_path = (repo_root_path / file_path).resolve()

    # 检查文件是否在仓库根目录下，如果不在，返回错误
    # 防止 file_path 含 ``../`` 等构造逃逸仓库，造成任意文件读取
    try:
        target_path.relative_to(repo_root_path)
    except ValueError:
        return _error_context(
            file_path,
            f"File {file_path} is outside of the repository root {repo_root}",
            source=source,
            selection_reason=selection_reason,
        )

    # 敏感文件拦截：L1 层防护，密钥/证书/部署配置不进入 LLM payload
    if _is_sensitive_file_path(file_path):
        return _error_context(
            file_path,
            f"File {file_path} is sensitive and was not read",
            source=source,
            selection_reason=selection_reason,
        )

    # 如果文件不存在，返回错误
    if not target_path.exists():
        return _error_context(
            file_path,
            f"File {file_path} does not exist",
            source=source,
            selection_reason=selection_reason,
        )

    # 如果是目录，返回错误
    if target_path.is_dir():
        return _error_context(
            file_path,
            f"File {file_path} is a directory",
            source=source,
            selection_reason=selection_reason,
        )

    # 读取文件内容，如果文件过大，只读取前max_chars个字符
    # 性能优化点：无变更信息时只读 max_chars+1 字符；有变更信息时
    # 才整体读取以便按 hunk/行号精确截断
    try:
        with target_path.open("r", encoding="utf-8") as f:
            if (changed_line_nos or changed_hunks) and max_chars > 0:
                full_content = f.read()
            else:
                full_content = f.read(max_chars + 1)

        # 截断优先级：hunk 优先 → 行级兜底
        hunk_result = _truncate_to_changed_hunks(
            full_content,
            changed_hunks,
            max_chars,
        )
        if hunk_result is not None:
            content, truncated = hunk_result
        else:
            # hunk 不可用或全部被跳过，回退到按变更行号截断
            content, truncated = _truncate_to_changed_lines(
                full_content,
                changed_line_nos,
                max_chars,
            )
    except UnicodeDecodeError:
        # 二进制文件或非 UTF-8 文本，安静失败转错误上下文
        return _error_context(
            file_path,
            f"File {file_path} is not a valid UTF-8 text file",
            source=source,
            selection_reason=selection_reason,
        )
    except OSError as e:
        # 权限不足、文件被删等其他 OS 错误
        return _error_context(
            file_path,
            f"Error reading file {file_path}: {str(e)}",
            source=source,
            selection_reason=selection_reason,
        )

    return FileContext(
        path=file_path,
        exists=True,
        content=content,
        truncated=truncated,
        chars_read=len(content),
        error="",
        source=source,
        selection_reason=selection_reason,
    )

def collect_file_contexts(
        repo_root,
        changed_files,
        context_budget,
    ):
    """收集本次 PR 审查所需的全部文件上下文（编排函数）。

    按以下顺序编排上下文收集：

    阶段 1：变更文件本身
        对每个 changed_file 调用 read_file_context，传入其 added_lines 行号
        和 hunks，作为最核心的上下文。

    阶段 2：import 候选
        遍历阶段 1 已成功读取的上下文，调用 _extract_import_file_candidates
        解析其中的 from...import / import 语句，把模块名映射为候选文件路径，
        仅保留仓库内确实存在的文件。

    阶段 3：调用名候选
        从所有 changed_file 的新增行中调用 _extract_added_call_names 提取
        函数调用名（排除关键字），然后遍历整个仓库的 Python 文件，
        用正则匹配 ``def <name>`` 找到定义这些调用的文件。

    阶段 4：把候选依次加入
        按顺序调用 add_context 加入候选，直到达到 max_extra_context_files
        上限或 remaining_chars 用尽。seen_paths 全程去重，保证同一文件
        不会被重复读取。

    Args:
        repo_root (str | Path): 仓库根目录绝对路径。
        changed_files (list[ChangedFile]): 本次 PR 的变更文件列表。
        context_budget (ContextBudget): 预算限制，包含 max_prompt_chars
            和 max_extra_context_files。

    Returns:
        list[FileContext]: 所有收集到的上下文（含错误上下文），
            顺序为：变更文件 → 额外候选文件。
    """
    repo_root_path=Path(repo_root).resolve()

    contexts = []
    # seen_paths 用于跨阶段去重：变更文件、import 候选、调用名候选
    # 三种来源可能指向同一文件，必须保证只读取一次
    seen_paths=set()
    # The prompt builder enforces the authoritative total budget.  This cap
    # prevents context retrieval alone from exceeding that limit first.
    # remaining_chars 是「剩余可用预算」，每读一个文件就扣减其 chars_read
    remaining_chars=context_budget.max_prompt_chars

    def add_context(
            file_path,
            source,
            selection_reason,
            changed_line_nos=None,
            changed_hunks=None,
        ):
        """把单个文件加入 contexts 列表，扣减剩余预算。

        闭包访问外层的 contexts / seen_paths / remaining_chars。
        已见过的路径直接返回 False，不再重复读取。

        Returns:
            bool: True 表示新增成功，False 表示因重复跳过。
        """
        nonlocal remaining_chars
        if file_path in seen_paths:
            return False
        seen_paths.add(file_path)
        # 把当前剩余预算作为 max_chars 传给 read_file_context，
        # 单文件读取不会超过剩余预算
        context = read_file_context(
            repo_root=repo_root,
            file_path=file_path,
            max_chars=remaining_chars,
            changed_line_nos=changed_line_nos,
            changed_hunks=changed_hunks,
            source=source,
            selection_reason=selection_reason,
        )
        contexts.append(context)
        # 注意：即使读取失败（chars_read=0）也会扣减 0，不影响后续预算
        remaining_chars -= context.chars_read
        return True

    # 阶段 1：收集所有变更文件的上下文（最核心，必读）
    for changed_file in changed_files:
        add_context(
            changed_file.path,
            source="changed_file",
            selection_reason="file is changed in the pull request",
            changed_line_nos=[line.line_no for line in changed_file.added_lines],
            changed_hunks=changed_file.hunks,
        )

    # extra_candidates 收集阶段 2/3 的候选，统一在阶段 4 加入
    # 每个元素是 (rel_path, source, selection_reason) 三元组
    extra_candidates=[]

    # 阶段 2：解析已读上下文中的 import 语句，找出被引用的源文件
    for context in contexts:
        # 只对读取成功的上下文解析 import；错误上下文没有内容可解析
        if not context.exists:
            continue

        for candidate in _extract_import_file_candidates(context.content, context.path):
            candidate_path=(repo_root_path/candidate).resolve()

            # 路径越界检查：候选文件必须在仓库内
            try:
                candidate_path.relative_to(repo_root_path)
            except ValueError:
                continue

            # 仅保留仓库内实际存在的文件，避免无效候选
            if candidate_path.exists():
                # 统一为正斜杠相对路径，便于跨平台比较与 seen_paths 去重
                rel_path=str(candidate_path.relative_to(repo_root_path)).replace("\\","/")
                extra_candidates.append((
                    rel_path,
                    "import_candidate",
                    f"imported by selected context {context.path}",
                ))

    # 阶段 3：从新增行提取调用名，再扫描全仓库找定义这些调用的文件
    call_names=set()
    # changed_paths 用于在扫描时跳过已经是变更文件的路径（避免重复读）
    changed_paths={changed_file.path for changed_file in changed_files}

    # 汇总所有变更文件新增行中的调用名
    for changed_file in changed_files:
        call_names.update(_extract_added_call_names(changed_file))

    # 遍历整个仓库的 Python 文件，查找哪些文件定义了上述调用名
    for py_file in _iter_python_files(repo_root_path):
        rel_path=str(py_file.relative_to(repo_root_path)).replace("\\", "/")

        # 跳过已是变更文件的，避免重复
        if rel_path in changed_paths:
            continue

        try:
            # 性能考虑：只读最多 max_prompt_chars 字符，避免大文件全读
            # errors="ignore" 是为了跳过偶发的非 UTF-8 字符
            with py_file.open("r", encoding="utf-8", errors="ignore") as f:
                content=f.read(context_budget.max_prompt_chars)
        except OSError:
            continue

        # 用正则匹配 ``def <name>``，找出该文件定义了哪些调用名
        # sorted 是为了让 selection_reason 中的列出顺序稳定可预测
        matching_call_names = sorted(
            name
            for name in call_names
            if re.search(rf"\bdef\s+{re.escape(name)}\b", content)
        )
        if matching_call_names:
            extra_candidates.append((
                rel_path,
                "call_name_candidate",
                "defines added call name(s): " + ", ".join(matching_call_names),
            ))

    # 阶段 4：按顺序把候选加入 contexts，受双重限制：
    #   - max_extra_context_files：额外文件数量上限
    #   - remaining_chars：剩余字符预算（耗尽则停止）
    extra_context_count=0
    for file_path, source, selection_reason in extra_candidates:
        if (
            extra_context_count >= context_budget.max_extra_context_files
            or remaining_chars == 0
        ):
            break
        if add_context(file_path, source, selection_reason):
            extra_context_count += 1

    return contexts

def _iter_python_files(repo_root_path):
    """递归遍历仓库内所有 Python 文件（.py），跳过 IGNORED_DIRS 中的目录。

    使用生成器避免一次性把所有路径加载到内存，对大仓库更友好
    （性能优化点）。rglob 会自动递归子目录。

    Args:
        repo_root_path (Path): 仓库根目录绝对路径。

    Yields:
        Path: 仓库内每个 .py 文件的绝对路径。
    """
    for path in repo_root_path.rglob("*.py"):
        # 路径中任意一段命中 IGNORED_DIRS（如 .git/node_modules）就跳过
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def _module_to_candidate_paths(module_path):
    """把一个模块路径映射为可能的源文件路径列表。

    Python 模块 ``a.b`` 可能对应两种文件：
      - ``a/b.py``：普通模块文件；
      - ``a/b/__init__.py``：包的初始化文件。
    两者都返回，由调用方用 exists() 判定哪个真实存在。

    Args:
        module_path (Path): 模块对应的目录/文件路径。

    Returns:
        list[Path]: 两个候选路径（.py 文件 和 __init__.py）。
    """
    return [
        module_path.with_suffix(".py"),
        module_path / "__init__.py",
    ]


def _resolve_relative_base(current_file_path, dot_count):
    """根据相对导入的点数计算基准目录。

    Python 相对导入的点数语义：
      - ``.``（1 个点）：当前文件所在目录；
      - ``..``（2 个点）：上一级目录；
      - 以此类推，每多一个点多向上一层。

    本函数从当前文件所在目录出发，向上回溯 ``dot_count - 1`` 层。

    Args:
        current_file_path (str | Path): 发起 import 的当前文件路径。
        dot_count (int): 相对导入前缀的点数（>=1）。

    Returns:
        Path: 相对导入的基准目录绝对路径。
    """
    base_dir = Path(current_file_path).parent

    # dot_count=1 时循环 0 次，base_dir 即当前目录
    # dot_count=2 时循环 1 次，base_dir 上移一层
    for _ in range(dot_count - 1):
        base_dir = base_dir.parent

    return base_dir


def _split_import_items(import_part, allow_dots=True):
    """拆分 import 语句中被导入的部分为名称列表。

    处理 ``a, b as c, d`` 这类多名称 + 别名的语法，去掉 ``as`` 别名，
    只保留原始名称。allow_dots 控制名称中是否允许点号：
      - from...import 后的名称本身不带点（如 ``from . import a, b``）；
      - import 后的模块名可能带点（如 ``import a.b, c.d``）。

    Args:
        import_part (str): import 语句中被导入部分的原始文本。
        allow_dots (bool): 名称中是否允许点号。

    Returns:
        list[str]: 合法的名称列表（去掉别名）。
    """
    names = []
    pattern = r"^[a-zA-Z_][\w\.]*$" if allow_dots else r"^[a-zA-Z_][\w]*$"

    for item in import_part.split(","):
        # 去掉 ``as alias`` 部分，只保留原始名称
        name = item.strip().split(" as ")[0].strip()
        # 用正则过滤掉非法名称（如空字符串、含特殊字符的）
        if re.match(pattern, name):
            names.append(name)

    return names


def _extract_import_file_candidates(content, current_file_path):
    """从源码内容中解析 import 语句，提取候选文件路径列表。

    用正则（而非 ast）解析，原因是这里只需要模块名到文件路径的映射，
    且要兼容 PR 中可能存在的语法不完整片段。支持两种语句：

    1. ``from [.]+[module] import name1, name2``
       - 相对导入（点号前缀）：用 _resolve_relative_base 计算基准目录；
       - 绝对导入：把模块名 ``a.b`` 映射为 ``a/b.py`` 或 ``a/b/__init__.py``；
       - 若 from 后无 module（``from . import x``），则对每个导入名
         单独映射到基准目录下的文件。

    2. ``import a.b, c.d``
       - 每个模块名都映射为 .py 文件或 __init__.py。

    Args:
        content (str): 源码文本。
        current_file_path (str): 当前文件路径，用于相对导入基准计算。

    Returns:
        list[str]: 候选文件相对路径列表（正斜杠分隔），可能含不存在的路径，
            由调用方用 exists() 过滤。
    """
    candidates = []

    for line in content.splitlines():
        line = line.strip()

        # 匹配 from...import 语句：捕获三组
        #   group(1) dots: 开头的点号（相对导入前缀），可为空
        #   group(2) module_name: from 后的模块名，可为空（from . import x）
        #   group(3) import_part: import 后的内容
        match = re.match(
            r"^from\s+(\.*)([a-zA-Z_][\w\.]*)?\s+import\s+(.+)$",
            line,
        )
        if match:
            dots, module_name, import_part = match.groups()

            if dots:
                # 相对导入：根据点数计算基准目录
                base_dir = _resolve_relative_base(
                    current_file_path=current_file_path,
                    dot_count=len(dots),
                )

                if module_name:
                    # from .pkg import x → 基准目录 / pkg
                    module_path = base_dir / Path(*module_name.split("."))
                    candidates.extend(_module_to_candidate_paths(module_path))
                else:
                    # from . import x, y → 每个名字单独映射到基准目录
                    # 注意：此处名字不允许带点（allow_dots=False）
                    for name in _split_import_items(import_part, allow_dots=False):
                        candidates.extend(_module_to_candidate_paths(base_dir / name))
            else:
                # 绝对导入：from pkg.mod import x → 模块路径直接从仓库根算起
                if module_name:
                    module_path = Path(*module_name.split("."))
                    candidates.extend(_module_to_candidate_paths(module_path))

            continue

        # 匹配 import 语句：import a.b, c.d
        match = re.match(r"^import\s+(.+)$", line)
        if match:
            # 每个模块名（允许带点）都映射为候选路径
            for module_name in _split_import_items(match.group(1), allow_dots=True):
                module_path = Path(*module_name.split("."))
                candidates.extend(_module_to_candidate_paths(module_path))

    # 统一为正斜杠字符串，便于跨平台路径比较
    return [
        str(candidate).replace("\\", "/")
        for candidate in candidates
    ]


def _extract_added_call_names(changed_file):
    r"""从变更文件的新增行中提取函数调用名，用于跨文件查找定义。

    用正则 ``\b([a-zA-Z_][\w]*)\s*\(`` 匹配「标识符后跟左括号」的模式，
    即函数/方法调用。排除常见关键字（if/for/while/return/def/class/print）
    以减少误报。

    Args:
        changed_file (ChangedFile): 变更文件对象，使用其 added_lines 字段。

    Returns:
        set[str]: 调用名集合（去重）。
    """
    names=set()

    # 关键字与内置函数黑名单，匹配到的不算作「调用名」
    ignored={"if", "for", "while", "return", "def", "class", "print"}

    for line in changed_file.added_lines:
        # 找出所有「标识符 + 左括号」的模式，即函数/方法调用
        for name in re.findall(r"\b([a-zA-Z_][\w]*)\s*\(",line.content):
            if name not in ignored:
                names.add(name)

    return names
