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

from .schemas import ChangedFile, DiffLine


def parse_diff(diff_text:str):
    changed_files = []
    current_file = None
    current_added_lines = []
    current_new_line = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            if current_file is not None:
                changed_files.append(
                    ChangedFile(
                        path=current_file,
                        added_lines=current_added_lines,
                    )
                )
            parts = line.split()
            current_file = parts[3]
            if current_file.startswith("b/"):
                current_file = current_file[2:]
            current_added_lines = []

        elif line.startswith("@@"):
            # 拿到 +10, -10 这样的信息，解析出当前hunk的起始行号
            match = re.search(r"\+(\d+)", line)
            if match:
                current_new_line = int(match.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            # 新增行
            content = line[1:]

            current_added_lines.append(
                DiffLine
                (
                    file_path=current_file,
                    line_no=current_new_line,
                    content=content
                )
            )
            current_new_line += 1

        elif line.startswith("-") and not line.startswith("---"):
            # 删除行
            pass
        else:
            if current_new_line is not None:
                current_new_line += 1
    
    if current_file is not None:
        changed_files.append(
            ChangedFile(
                path=current_file,
                added_lines=current_added_lines,
            )
        )

    return changed_files