'''
用来把原始的git diff解析成结构化的数据

比如说输入要从

diff --git a/app.py b/app.py
@@ -10,6 +10,8 @@
+print(user.password)

变成

ChangedFile:
  path: app.py
  hunks:
    start_line: 10
    added_lines:
      line 12: print(user.password)

的形式，其中包含了文件路径，修改的开始行号，以及修改的内容。

【模块职责】
作为 pipeline 的"入口解析器"，把任意一段 git diff 文本（通常由 git diff 命令产出）
转换为结构化的 ChangedFile 列表，供后续上下文检索、规则审查、LLM 审查等阶段消费。

【在整体架构中的位置】
位于 pipeline 最前端，依赖 schemas 模块的 ChangedFile / DiffHunk / DiffLine，
被 agent 主流程在拿到 diff_text 后第一时间调用。是连接"纯文本 diff"与"结构化审查
世界"的唯一桥梁，因此其健壮性直接决定后续所有阶段能否正确工作。

【设计理由】
- 采用逐行状态机解析而非正则整体匹配：git diff 行间存在强上下文依赖（如当前
  hunk 的行号计数器需逐行累加），状态机方式更易正确维护新旧行号计数器，
  也能稳健处理 rename、多 hunk、特殊字符路径等边界情况。
- 使用 shlex 处理路径：git 会对含空格/特殊字符的路径加引号转义，shlex 能正确
  还原，比简单 split 更鲁棒。
- 解析失败时尽量保留原始 patch 文本而不抛异常：保证一个文件的畸形 diff 不会
  中断整次审查，提升整体鲁棒性。
'''

import re
import shlex

from .schemas import ChangedFile, DiffHunk, DiffLine


def _parse_git_path(value, prefix=None):
    """Decode one Git path and remove an optional a/ or b/ prefix.

    中文说明：
    解码 git diff 中的单个路径并去除可选的 ``a/`` 或 ``b/`` 前缀。

    git 在 diff 中用 ``a/`` 表示旧版本路径、``b/`` 表示新版本路径作为约定前缀，
    真实文件路径需要去掉该前缀。同时 git 会对含空格/特殊字符的路径用引号包裹
    并做 C 风格转义，因此先用 shlex 还原。

    Args:
        value (str): 待解析的原始路径片段。
        prefix (str, optional): 需要去掉的前缀，如 "a/" 或 "b/"。

    Returns:
        str: 解码并去前缀后的真实文件路径；若 shlex 解析失败且无法还原，
            则返回 strip 后的原始字符串作为兜底。
    """
    value = value.strip()
    # shlex.split 可正确处理被引号包裹、含空格或反斜杠转义的路径；
    # 解析失败（如引号不闭合）时退化为空列表，走后续兜底逻辑。
    try:
        parsed = shlex.split(value)
    except ValueError:
        parsed = []

    # 只接受"单 token"的解析结果；多于一个 token 说明输入异常，保留原始值。
    if len(parsed) == 1:
        value = parsed[0]

    # 去掉约定前缀（a/ 或 b/），得到真实仓库相对路径。
    if prefix and value.startswith(prefix):
        return value[len(prefix):]
    return value


def _parse_diff_git_paths(line):
    """Return old and new paths from a ``diff --git`` header when possible.

    中文说明：
    从 ``diff --git a/old b/new`` 头行中解析出旧路径与新路径。

    优先用 shlex 拆分整行 payload（能处理带空格/特殊字符的路径）；
    若拆分结果不是恰好两个 token，则尝试基于 ``b/`` 分隔符的兜底解析，
    以兼容部分调用方直接拼接的未加引号路径。

    Args:
        line (str): 完整的 ``diff --git ...`` 头行。

    Returns:
        tuple[str | None, str | None]: (old_path, new_path)；无法解析时返回 (None, None)。
    """
    payload = line[len("diff --git "):]
    # 优先按 shlex 拆分，正确还原被引号包裹的含空格路径。
    try:
        paths = shlex.split(payload)
    except ValueError:
        paths = []

    # 标准情况：恰好两个 token，分别是 a/old 与 b/new。
    if len(paths) == 2:
        return _parse_git_path(paths[0], "a/"), _parse_git_path(paths[1], "b/")

    # Git normally quotes whitespace paths. This fallback also accepts the
    # unquoted form used by callers, where " b/" separates old and new paths.
    # 兜底：处理未加引号的含空格路径——以第一个 " b/" 作为新旧路径分隔点。
    separator = payload.find(" b/")
    if payload.startswith("a/") and separator >= 0:
        return payload[2:separator], payload[separator + 3:]

    return None, None


def _build_changed_file(
    path, added_lines, deleted_lines, patch_lines, old_path, is_rename, hunks
):
    """Assemble a ``ChangedFile`` from the accumulated per-file state.

    中文说明：
    将状态机在解析单个文件过程中累积的各部分结果，组装成一个 ChangedFile 对象。
    抽取为独立函数是为了在"文件切换"与"末尾收尾"两处复用同一段构造逻辑，
    保证二者产出结构完全一致，避免重复代码与不一致风险。

    Args:
        path (str): 变更后的新文件路径。
        added_lines (list[DiffLine]): 新增行列表。
        deleted_lines (list[DiffLine]): 删除行列表。
        patch_lines (list[str]): 原始 patch 行列表（将被 ``\\n`` 拼接为 patch 字段）。
        old_path (str | None): 旧路径（仅 rename 有意义）。
        is_rename (bool): 是否为重命名。
        hunks (list[DiffHunk]): hunk 区间列表。

    Returns:
        ChangedFile: 组装完成的变更文件结构。
    """
    return ChangedFile(
        path=path,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        # patch_lines 为逐行列表，拼回多行字符串便于整体写入 prompt 与日志。
        patch="\n".join(patch_lines),
        # 仅在 rename 时保留 old_path，避免非 rename 场景误带历史路径造成歧义。
        old_path=old_path if is_rename else None,
        is_rename=is_rename,
        hunks=hunks,
    )


def parse_diff(diff_text: str):
    """Parse raw git diff text into a list of ``ChangedFile`` objects.

    中文说明：
    本模块的核心入口。采用"逐行状态机"解析整段 git diff 文本，逐行判定其类型
    （diff 头 / rename / +++ / @@ hunk 头 / +变更行 / -变更行 / 上下文行），
    并维护当前文件的新旧行号计数器，最终产出结构化的 ChangedFile 列表。

    算法思路（逐行状态机）：
    1. 遇到 ``diff --git `` 行 → 切换到新文件：先把上一个文件累积的状态收尾
       存入结果列表，再用新头行重置所有 current_* 状态变量。
    2. 遇到 ``rename from`` / ``rename to`` → 标记为重命名，记录新旧路径。
    3. 遇到 ``+++ `` → 提取新文件路径（``/dev/null`` 表示纯删除，跳过）。
    4. 遇到 ``@@`` hunk 头 → 正则解析出新旧行号起点与行数，初始化行号计数器，
       并记录该 hunk 的行号区间到 current_hunks。
    5. 遇到 ``+``（非 ``+++``）→ 新增行：用 current_new_line 计数器作为行号，
       存入 added_lines，随后计数器 +1。
    6. 遇到 ``-``（非 ``---``）→ 删除行：用 current_old_line 计数器作为行号，
       存入 deleted_lines，随后计数器 +1。
    7. 其它行（上下文行 `` `` 或无前缀行）→ 两个计数器都 +1，保持行号对齐。
    8. 循环结束后，对最后一个文件做收尾存入。

    Args:
        diff_text (str): 完整的 git diff 文本（多文件可合并）。

    Returns:
        list[ChangedFile]: 解析得到的变更文件列表；空 diff 返回空列表。

    性能考虑：
    - 使用 ``splitlines()`` 一次性切分后逐行遍历，单次扫描 O(n) 完成，避免多次
      全文扫描；对于大型 diff 仍可在内存可接受范围内完成。
    - 行号计数器随扫描递增，无需回查，保持线性复杂度。
    """
    changed_files = []
    # 以下 current_* 变量均为"当前正在解析的文件"的累积状态，遇到新 diff 头时重置。
    current_file = None            # 当前文件的新路径
    current_old_path = None        # 当前文件的旧路径（仅 rename 有值）
    current_is_rename = False       # 当前文件是否为重命名
    current_added_lines = []       # 当前文件累积的新增行
    current_deleted_lines = []     # 当前文件累积的删除行
    current_hunks = []             # 当前文件累积的 hunk 区间
    current_patch_lines = []       # 当前文件的原始 patch 行（保留原始文本）
    current_old_line = None        # 旧文件行号计数器（在 hunk 内逐行递增）
    current_new_line = None        # 新文件行号计数器（在 hunk 内逐行递增）

    for line in diff_text.splitlines():
        # ── 分支1：diff 头行，表示开始一个新文件 ──
        if line.startswith("diff --git "):
            # 先把上一个文件累积的状态收尾存入结果（若存在）。
            if current_file is not None:
                changed_files.append(
                    _build_changed_file(
                        current_file,
                        current_added_lines,
                        current_deleted_lines,
                        current_patch_lines,
                        current_old_path,
                        current_is_rename,
                        current_hunks,
                    )
                )

            # 解析新文件的旧/新路径，并重置所有累积状态。
            current_old_path, current_file = _parse_diff_git_paths(line)
            current_is_rename = False
            current_added_lines = []
            current_deleted_lines = []
            current_hunks = []
            # patch_lines 从当前头行开始记录，保证 patch 文本完整。
            current_patch_lines = [line]
            current_old_line = None
            current_new_line = None
            continue

        # 非 diff 头行：若已进入某文件，则把该行追加到 patch 文本中（保留原始内容）。
        if current_file is not None:
            current_patch_lines.append(line)

        # ── 分支2：rename 标记 ──
        if line.startswith("rename from "):
            # 记录重命名前的旧路径。
            current_old_path = _parse_git_path(line[len("rename from "):])
            current_is_rename = True
        elif line.startswith("rename to "):
            # 更新当前文件路径为重命名后的新路径。
            current_file = _parse_git_path(line[len("rename to "):])
            current_is_rename = True
        # ── 分支3：+++ 行，给出新文件路径 ──
        elif line.startswith("+++ "):
            # 取制表符前的部分作为路径（git 会在路径后附加 \t 时间戳等元信息）。
            new_path = _parse_git_path(line[len("+++ "):].split("\t", 1)[0], "b/")
            # /dev/null 表示该文件被完全删除，不作为有效新路径。
            if new_path != "/dev/null":
                current_file = new_path
        # ── 分支4：@@ hunk 头，重置行号计数器并记录 hunk 区间 ──
        elif line.startswith("@@"):
            # 正则解析 ``@@ -旧起[,旧数] +新起[,新数] @@``，捕获新旧行号起点与行数。
            match = re.search(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line
            )
            if match:
                # 初始化旧/新行号计数器为 hunk 的起始行号。
                current_old_line = int(match.group(1))
                current_new_line = int(match.group(3))
                # 新文件行数缺失时按 1 处理（如纯删除 hunk 可能写作 +x,0）。
                new_line_count = int(match.group(4) or 1)
                # 记录该 hunk 在新文件中的行号区间（含两端），供上下文检索使用。
                current_hunks.append(
                    DiffHunk(
                        start_line=current_new_line,
                        end_line=current_new_line + new_line_count - 1,
                    )
                )
        # ── 分支5：新增行（+开头但非+++头）──
        elif line.startswith("+") and not line.startswith("+++"):
            # A malformed diff can contain a line outside a hunk. Keep its raw
            # patch text but do not invent a line number for review findings.
            # 防御性处理：若不在任何 hunk 内（计数器为 None），只保留 patch 文本，
            # 不为这类孤立行编造行号，避免后续审查定位到错误行号。
            if current_file is not None and current_new_line is not None:
                current_added_lines.append(
                    DiffLine(
                        file_path=current_file,
                        line_no=current_new_line,
                        content=line[1:],   # 去掉前导 '+' 得到真实行内容
                    )
                )
                # 新文件行号计数器前进一步。
                current_new_line += 1
        # ── 分支6：删除行（-开头但非---头）──
        elif line.startswith("-") and not line.startswith("---"):
            if current_file is not None and current_old_line is not None:
                current_deleted_lines.append(
                    DiffLine(
                        file_path=current_file,
                        line_no=current_old_line,
                        content=line[1:],   # 去掉前导 '-' 得到真实行内容
                    )
                )
                # 旧文件行号计数器前进一步。
                current_old_line += 1
        # ── 分支7：上下文行（空格开头或无前缀行）──
        else:
            # 上下文行同时存在于新旧文件，两个计数器都前进一步以保持对齐。
            if current_new_line is not None:
                current_new_line += 1
            if current_old_line is not None:
                current_old_line += 1

    # ── 收尾：循环结束后把最后一个文件累积的状态存入结果 ──
    if current_file is not None:
        changed_files.append(
            _build_changed_file(
                current_file,
                current_added_lines,
                current_deleted_lines,
                current_patch_lines,
                current_old_path,
                current_is_rename,
                current_hunks,
            )
        )

    return changed_files
