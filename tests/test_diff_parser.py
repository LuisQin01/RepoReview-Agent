"""``tests/test_diff_parser.py`` 针对 diff 解析器 ``src/diff_parser.py`` 进行单元测试。

测试体系中的位置
----------------
diff 解析器是整个 Agent 流水线的第二步（``parse_diff``），其输出
(:class:`~src.schemas.ChangedFile` 列表) 会被下游多个模块消费：

* ``src.file_context.collect_file_contexts`` 用来定位文件并读取上下文；
* ``src.reviewers.review_changed_files`` 用来对新增行做规则审查；
* ``src.reporter.render_markdown_report`` 用来渲染报告。

因此解析器必须把 git diff 中的“文件路径、增删行、行号、hunk 范围、
rename 信息”等抽取准确，否则任何下游环节都会出错。本文件即专门
锁定解析器的纯函数行为进行测试，不涉及 HTTP/LLM。

测试策略
--------
1. **内联字符串 diff**：多数测试直接在源码里写一段 diff 文本，便于读者对照
   期望输出，适合小而精的边界用例。
2. **fixtures 文件**：复杂的、多 hunk 的 diff 用 ``tests/fixtures/*.diff``
   文件保存，避免在源码里堆砌超长字符串，也便于复用真实 Git 输出。

覆盖的核心边界
--------------
* 增删行解析与行号、hunk 范围
* 纯重命名（``rename from`` / ``rename to``）
* 带空格的文件路径（引号 / 非引号两种格式）
* ``+++`` 路径优先级（当 ``diff --git`` 头与 ``+++`` 不一致时以 ``+++`` 为准）
* 多 hunk 的新行行号
* rename + 带空格路径不破坏下游消费者
* malformed diff 防御：没有 hunk 的孤立 ``+`` 行应被安全忽略而非崩溃
"""
from pathlib import Path

from src.diff_parser import parse_diff
from src.file_context import collect_file_contexts
from src.schemas import ContextBudget, DiffHunk
from src.reporter import render_markdown_report
from src.reviewers import review_changed_files


def test_parse_added_and_deleted_lines():
    """验证解析器能正确解析新增行、删除行及其在新文件中的行号。

    测试目的
    --------
    这是最基础的一条用例，确认 :func:`src.diff_parser.parse_diff` 能从
    一个标准 git unified diff 中抽取出：

    * 变更文件路径（``path``）；
    * 新增行列表（``added_lines``）及其行号与内容；
    * 删除行列表（``deleted_lines``）及其行号与内容；
    * hunk 范围（``hunks``）的起止行号。

    测试场景
    --------
    diff 把 ``old_debug = True`` 改为 ``print(user.password)``，
    hunk 头 ``@@ -1,3 +1,3 @@`` 表示旧/新文件都从第 1 行开始、各 3 行。
    删除行与新增行都应位于新文件的“第 2 行”（紧随上下文行 ``def login(user):``）。

    预期结果
    --------
    * 仅 1 个 ``ChangedFile``，``path == "app.py"``。
    * 新增行 1 条，行号 2，内容 ``    print(user.password)``。
    * 删除行 1 条，行号 2，内容 ``    old_debug = True``。
    * hunks 为 ``[DiffHunk(start_line=1, end_line=3)]``。
    """
    diff_text = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 def login(user):
-    old_debug = True
+    print(user.password)
     return True
"""

    changed_files = parse_diff(diff_text)

    assert len(changed_files) == 1  # 仅解析到 app.py 一个变更文件

    changed = changed_files[0]
    assert changed.path == "app.py"  # 新文件路径

    assert len(changed.added_lines) == 1  # 仅一条新增行
    assert changed.added_lines[0].line_no == 2  # 新文件中的行号（紧跟上下文行）
    assert changed.added_lines[0].content == "    print(user.password)"  # 保留原始缩进

    assert len(changed.deleted_lines) == 1  # 仅一条删除行
    assert changed.deleted_lines[0].line_no == 2  # 旧文件中的行号
    assert changed.deleted_lines[0].content == "    old_debug = True"  # 保留原始缩进
    assert changed.hunks == [DiffHunk(start_line=1, end_line=3)]  # hunk 在新文件中的起止行


def test_parse_pure_rename_records_old_and_new_path():
    """验证纯重命名 diff（无内容变更）能正确记录新旧路径与 ``is_rename``。

    测试目的
    --------
    git 在“仅改名、内容无变化”时会输出 ``rename from`` / ``rename to`` 且
    不带任何 hunk。解析器必须把这种 diff 识别为 rename，且不能凭空
    造出增删行。

    测试场景
    --------
    使用 fixtures 中的 ``pure_rename_with_spaces.diff``：将 ``old name.py``
    重命名为 ``new name.py``，路径含空格。读取文件后调用 ``parse_diff``。

    预期结果
    --------
    * 仅 1 个 ``ChangedFile``；
    * ``path == "new name.py"``（新路径），``old_path == "old name.py"``（旧路径）；
    * ``is_rename is True``；
    * ``added_lines`` 与 ``deleted_lines`` 均为空（纯改名无内容变更）。

    特殊逻辑解释
    ------------
    之所以用 fixtures 文件而非内联字符串，是因为 rename + 带空格路径
    的 diff 较长且容易在源码里转义出错；用独立文件保留原始字节最稳。
    """
    fixture_path = Path(__file__).parent / "fixtures" / "pure_rename_with_spaces.diff"
    changed_files = parse_diff(fixture_path.read_text(encoding="utf-8"))

    assert len(changed_files) == 1  # 仅一个文件被重命名
    changed = changed_files[0]
    assert changed.path == "new name.py"  # 新路径（带空格也必须正确解析）
    assert changed.old_path == "old name.py"  # 旧路径
    assert changed.is_rename is True  # 必须识别为 rename
    assert changed.added_lines == []  # 纯改名不应有任何新增行
    assert changed.deleted_lines == []  # 纯改名不应有任何删除行


def test_parse_unquoted_diff_git_header_with_spaces_without_hunks():
    """验证只有 ``diff --git`` 头、且路径含空格但未加引号时仍能取到正确路径。

    测试目的
    --------
    git 在某些场景下会输出未加引号的 ``diff --git a/my file.py b/my file.py``，
    而且后面可能完全没有 hunk（例如纯 mode 变更）。解析器需要能从这种
    “只有 header”的最小 diff 中稳健地推断出文件路径，而不是把
    ``my`` 当作路径、把 ``file.py`` 当成下一个东西。

    测试场景
    --------
    输入只有一行 ``diff --git a/my file.py b/my file.py\n``，无任何 hunk。

    预期结果
    --------
    第一个 ChangedFile 的 ``path == "my file.py"``（含空格）。
    """
    changed_files = parse_diff("diff --git a/my file.py b/my file.py\n")

    assert changed_files[0].path == "my file.py"  # 未加引号的带空格路径也必须正确还原


def test_parse_renamed_diff_with_spaces_and_content_change():
    """验证 rename + 内容变更的 diff：同时记录新旧路径、rename 标志与新增行。

    测试目的
    --------
    比“纯改名”更复杂的情况是：文件改了名，同时内容也有改动。解析器
    既要识别 ``rename from/to`` 标志，又要正确解析 hunk 中的新增行，并且
    新增行的 ``file_path`` 必须是“新路径”。

    测试场景
    --------
    ``old name.py`` 改名为 ``new name.py``，且新增 ``print("new")`` 一行。

    预期结果
    --------
    * ``path == "new name.py"``，``old_path == "old name.py"``，``is_rename is True``；
    * 新增行的 ``(file_path, line_no, content)`` 等于
      ``("new name.py", 2, '    print("new")')``，即新增行归属新路径、行号 2。

    特殊逻辑解释
    ------------
    显式断言三元组而非单个字段，是为了同时锁定 ``file_path``、``line_no``、
    ``content`` 三者的组合关系，防止路径正确而行号错位这类隐蔽 bug。
    """
    changed_files = parse_diff(
        """diff --git a/old name.py b/new name.py
similarity index 90%
rename from old name.py
rename to new name.py
--- a/old name.py
+++ b/new name.py
@@ -1 +1,2 @@
 def run():
+    print("new")
"""
    )

    changed = changed_files[0]
    assert changed.path == "new name.py"  # 新路径
    assert changed.old_path == "old name.py"  # 旧路径
    assert changed.is_rename is True  # 识别为 rename
    # 同时锁定新增行的路径、行号、内容三元组，避免“路径对但行号错”这类隐蔽 bug
    assert [(line.file_path, line.line_no, line.content) for line in changed.added_lines] == [
        ("new name.py", 2, '    print("new")')
    ]


def test_parse_quoted_header_and_plus_plus_plus_path_with_spaces():
    """验证 git 对带空格路径加引号时（``"a/my file.py"``）的解析。

    测试目的
    --------
    git 在路径含特殊字符时会自动给 ``diff --git`` / ``---`` / ``+++`` 三处
    都加双引号。解析器必须能剥掉引号，且这种情况下 **不是** rename
    （新旧路径相同），所以 ``old_path`` 应为 ``None``。

    测试场景
    --------
    ``my file.py`` 的新增 ``value = 2``。三处路径都被加了双引号。

    预期结果
    --------
    * ``path == "my file.py"``（去引号）；
    * ``old_path is None``（同名非 rename）；
    * ``is_rename is False``；
    * 新增行的 ``file_path == "my file.py"``。
    """
    changed_files = parse_diff(
        """diff --git "a/my file.py" "b/my file.py"
--- "a/my file.py"
+++ "b/my file.py"
@@ -1 +1,2 @@
 value = 1
+value = 2
"""
    )

    changed = changed_files[0]
    assert changed.path == "my file.py"  # 去掉外层引号后的路径
    assert changed.old_path is None  # 同名变更不算 rename，无旧路径
    assert changed.is_rename is False  # 非重命名
    assert changed.added_lines[0].file_path == "my file.py"  # 新增行路径同样去引号


def test_parse_uses_plus_plus_plus_path_as_new_path_source():
    """验证当 ``diff --git`` 头与 ``+++`` 行不一致时，以 ``+++`` 为准。

    测试目的
    --------
    正常 Git 输出里 ``diff --git`` 头的 ``b/xxx`` 与 ``+++ b/xxx`` 是一致的。
    但本测试故意构造一个“二者不一致”的合成 diff，验证解析器在两者冲突时
    以 ``+++`` 行作为新路径的权威来源——因为 ``+++`` 才是真正描述新文件内容
    的那一行，``diff --git`` 头仅用于元信息。

    测试场景
    --------
    头部 ``b/header.py``，``+++`` 行却是 ``b/new file.py``。

    预期结果
    --------
    * ``path == "new file.py"``（取自 ``+++``）；
    * 新增行 ``(file_path, line_no, content) == ("new file.py", 2, "value = 2")``。

    特殊逻辑解释
    ------------
    注释中保留了原代码里关于“这是合成 diff，真实 Git 输出不会这样”的说明，
    用来强调本用例是“契约测试”而非真实场景回归。
    """
    # This synthetic diff verifies path-source precedence; real Git output
    # normally keeps the diff header and +++ path consistent.
    changed_files = parse_diff(
        """diff --git a/old.py b/header.py
--- a/old.py
+++ b/new file.py
@@ -1 +1,2 @@
 value = 1
+value = 2
"""
    )

    changed = changed_files[0]
    assert changed.path == "new file.py"  # 以 +++ 行为准
    # 锁定新增行三元组，确保路径来源正确传递到了每一行
    assert [(line.file_path, line.line_no, line.content) for line in changed.added_lines] == [
        ("new file.py", 2, "value = 2")
    ]


def test_parse_new_line_numbers_across_multiple_hunks():
    """验证多 hunk diff 中新行行号按各自 hunk 的 ``+起始行`` 正确累加。

    测试目的
    --------
    一个文件可能包含多个 hunk，每个 hunk 的 ``@@ -a,b +c,d @@`` 中 ``c``
    才是“该 hunk 在新文件中的起始行号”。解析器必须为每个 hunk 独立维护
    行号，不能把第二个 hunk 的行号接着第一个 hunk 末尾继续累加。

    测试场景
    --------
    使用 fixtures 中的 ``multiple_hunks_new_line_numbers.diff``：两个 hunk
    分别位于新文件的 ``@@ -2,3 +2,3 @@`` 与 ``@@ -21,3 +21,3 @@``，
    各新增一行。

    预期结果
    --------
    * 新增行依次为 ``("service.py", 3, "    first = True")`` 和
      ``("service.py", 22, "    second = True")``，行号跨 hunk 不连续；
    * ``hunks`` 严格等于 ``[DiffHunk(2,4), DiffHunk(21,23)]``。

    特殊逻辑解释
    ------------
    用 fixtures 文件保存这段较长 diff，既避免源码内堆砌大量字符串，
    也便于将来用真实 Git 输出替换而不破坏断言结构。
    """
    fixture_path = (
        Path(__file__).parent / "fixtures" / "multiple_hunks_new_line_numbers.diff"
    )
    changed_files = parse_diff(fixture_path.read_text(encoding="utf-8"))

    assert len(changed_files) == 1  # 仍是单个文件，只是含两个 hunk
    changed = changed_files[0]
    # 行号跨 hunk 不连续：第一个 hunk 在第 3 行，第二个在第 22 行
    assert [(line.file_path, line.line_no, line.content) for line in changed.added_lines] == [
        ("service.py", 3, "    first = True"),
        ("service.py", 22, "    second = True"),
    ]
    # 每个 hunk 的起止行号独立计算，分别覆盖 [2,4] 与 [21,23]
    assert changed.hunks == [
        DiffHunk(start_line=2, end_line=4),
        DiffHunk(start_line=21, end_line=23),
    ]


def test_rename_and_space_path_do_not_break_downstream_consumers(tmp_path):
    """验证 rename + 带空格路径在下游整条流水线中不被破坏（集成回归）。

    测试目的
    --------
    这是本文件唯一一条“跨模块”测试：除了 ``parse_diff``，还串联了
    ``collect_file_contexts``、``review_changed_files``、
    ``render_markdown_report``，确认 rename + 带空格路径这种边界场景
    不会在下游任一环节报错或丢失信息。

    测试场景
    --------
    在临时仓库中真实写一个 ``new name.py`` 文件，再喂给解析器一段 rename diff。
    之后把解析结果依次喂给上下文收集器、规则审查器、报告渲染器。

    预期结果
    --------
    * ``contexts[0].exists is True``：文件被正确定位（路径含空格也成立）；
    * ``contexts[0].path == "new name.py"``：上下文路径与新路径一致；
    * 渲染出的报告中应包含 ``new name.py`` 字样。

    特殊逻辑解释
    ------------
    之所以临时写一个真实文件，是因为 ``collect_file_contexts`` 会去文件系统
    读文件，必须让 ``new name.py`` 真实存在；否则该集成测试无法覆盖
    “下游消费者”这一层。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # 真实创建带空格的文件，确保下游 collect_file_contexts 能定位到
    (repo / "new name.py").write_text("def run():\n    print('new')\n", encoding="utf-8")
    changed_files = parse_diff(
        """diff --git a/old name.py b/new name.py
similarity index 90%
rename from old name.py
rename to new name.py
--- a/old name.py
+++ b/new name.py
@@ -1 +1,2 @@
 def run():
+    print('new')
"""
    )

    contexts = collect_file_contexts(
        repo,
        changed_files,
        context_budget=ContextBudget(max_extra_context_files=0),
    )
    issues = review_changed_files(changed_files)
    report = render_markdown_report(issues, changed_files, contexts)

    assert contexts[0].exists is True  # 下游能定位到带空格的真实文件
    assert contexts[0].path == "new name.py"  # 上下文路径与解析的新路径一致
    assert "new name.py" in report  # 报告中保留了新路径信息，未丢失


def test_added_line_without_hunk_is_ignored_without_crashing():
    """验证 malformed diff 防御：没有 hunk 的孤立 ``+`` 行应被安全忽略。

    测试目的
    --------
    malformed/截断的 diff 中可能出现“只有 ``+`` 行但没有 ``@@`` hunk 头”的
    情况。解析器必须 **容错** 而非崩溃：这种孤立 ``+`` 行因为没有 hunk
    上下文，无法确定它属于哪一行，应被丢弃；同时文件路径仍应被解析。

    测试场景
    --------
    diff 只有 ``diff --git`` 头、``+++`` 头和一行 ``+not associated with a hunk``，
    缺少 ``@@`` hunk 头。

    预期结果
    --------
    * ``path == "broken.py"``：文件路径仍能从 ``+++`` 头解析出；
    * ``added_lines == []``：孤立的 ``+`` 行因无 hunk 归属而被忽略。

    特殊逻辑解释
    ------------
    这是一条“防御性”测试：它不验证正确性，而是验证“不会因为脏输入崩溃”，
    这对从真实 Git/CI 系统接收任意 diff 的解析器至关重要。
    """
    changed_files = parse_diff(
        """diff --git a/broken.py b/broken.py
+++ b/broken.py
+not associated with a hunk
"""
    )

    assert changed_files[0].path == "broken.py"  # 路径仍能从 +++ 头解析出
    assert changed_files[0].added_lines == []  # 没有 hunk 归属的孤立 + 行被安全忽略
