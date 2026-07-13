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
'''

import re
import shlex

from .schemas import ChangedFile, DiffLine


def _parse_git_path(value, prefix=None):
    """Decode one Git path and remove an optional a/ or b/ prefix."""
    value = value.strip()
    try:
        parsed = shlex.split(value)
    except ValueError:
        parsed = []

    if len(parsed) == 1:
        value = parsed[0]

    if prefix and value.startswith(prefix):
        return value[len(prefix):]
    return value


def _parse_diff_git_paths(line):
    """Return old and new paths from a ``diff --git`` header when possible."""
    payload = line[len("diff --git "):]
    try:
        paths = shlex.split(payload)
    except ValueError:
        paths = []

    if len(paths) == 2:
        return _parse_git_path(paths[0], "a/"), _parse_git_path(paths[1], "b/")

    # Git normally quotes whitespace paths. This fallback also accepts the
    # unquoted form used by callers, where " b/" separates old and new paths.
    separator = payload.find(" b/")
    if payload.startswith("a/") and separator >= 0:
        return payload[2:separator], payload[separator + 3:]

    return None, None


def _build_changed_file(
    path, added_lines, deleted_lines, patch_lines, old_path, is_rename
):
    return ChangedFile(
        path=path,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        patch="\n".join(patch_lines),
        old_path=old_path if is_rename else None,
        is_rename=is_rename,
    )


def parse_diff(diff_text: str):
    changed_files = []
    current_file = None
    current_old_path = None
    current_is_rename = False
    current_added_lines = []
    current_deleted_lines = []
    current_patch_lines = []
    current_old_line = None
    current_new_line = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_file is not None:
                changed_files.append(
                    _build_changed_file(
                        current_file,
                        current_added_lines,
                        current_deleted_lines,
                        current_patch_lines,
                        current_old_path,
                        current_is_rename,
                    )
                )

            current_old_path, current_file = _parse_diff_git_paths(line)
            current_is_rename = False
            current_added_lines = []
            current_deleted_lines = []
            current_patch_lines = [line]
            current_old_line = None
            current_new_line = None
            continue

        if current_file is not None:
            current_patch_lines.append(line)

        if line.startswith("rename from "):
            current_old_path = _parse_git_path(line[len("rename from "):])
            current_is_rename = True
        elif line.startswith("rename to "):
            current_file = _parse_git_path(line[len("rename to "):])
            current_is_rename = True
        elif line.startswith("+++ "):
            new_path = _parse_git_path(line[len("+++ "):].split("\t", 1)[0], "b/")
            if new_path != "/dev/null":
                current_file = new_path
        elif line.startswith("@@"):
            match = re.search(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                current_old_line = int(match.group(1))
                current_new_line = int(match.group(2))
        elif line.startswith("+") and not line.startswith("+++"):
            # A malformed diff can contain a line outside a hunk. Keep its raw
            # patch text but do not invent a line number for review findings.
            if current_file is not None and current_new_line is not None:
                current_added_lines.append(
                    DiffLine(
                        file_path=current_file,
                        line_no=current_new_line,
                        content=line[1:],
                    )
                )
                current_new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            if current_file is not None and current_old_line is not None:
                current_deleted_lines.append(
                    DiffLine(
                        file_path=current_file,
                        line_no=current_old_line,
                        content=line[1:],
                    )
                )
                current_old_line += 1
        else:
            if current_new_line is not None:
                current_new_line += 1
            if current_old_line is not None:
                current_old_line += 1

    if current_file is not None:
        changed_files.append(
            _build_changed_file(
                current_file,
                current_added_lines,
                current_deleted_lines,
                current_patch_lines,
                current_old_path,
                current_is_rename,
            )
        )

    return changed_files
