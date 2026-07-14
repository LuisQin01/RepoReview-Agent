"""``tests/test_reporter.py`` 针对报告渲染器 ``src/reporter.py`` 进行单元测试。

测试体系中的位置
----------------
报告渲染器是 Agent 流水线的倒数第二步（``render_report``）：把
:class:`~src.schemas.ReviewIssue` 列表 + :class:`~src.schemas.ChangedFile`
列表渲染成可在 GitHub PR 上发布的 Markdown 评论，或本地的 JSON 报告。
渲染结果会直接暴露给外部（如 PR 评论），因此不仅要“能渲染”，还必须
保证 **格式安全**（表格转义）与 **内容安全**（敏感值脱敏）。

本文件重点覆盖四类不变量：
1. **MARKER 与精简字段**：summary 必须以 ``SUMMARY_COMMENT_MARKER`` 开头，
   且只包含精简后的统计字段，不包含完整 JSON 输出。
2. **按 severity 排序**：``error`` 必须排在 ``warning`` 之前。
3. **表格转义**：finding 文本中的 ``|`` 与换行必须被转义，否则会破坏
   Markdown 表格结构，甚至导致注入。
4. **summary-only 降级**与空 findings 处理。
5. **P0 安全**：任何凭证值（secret / token）在 summary 和 JSON 报告中
   都必须被脱敏为 ``[REDACTED]``，绝不能原样出现。

测试策略
--------
* 直接构造 :class:`ReviewIssue` / :class:`ChangedFile` 实例作为输入，
  跳过解析与审查环节，便于精确控制测试边界。
* 用模块级常量 ``SECRET_MARKER`` / ``GITHUB_TOKEN_MARKER`` /
  ``OPENAI_TOKEN_MARKER`` 模拟三种常见凭证形态，验证脱敏覆盖面。
"""
from src.git_provider import SUMMARY_COMMENT_MARKER
from src.reporter import render_summary_comment
from src.schemas import ChangedFile, ReviewIssue


def test_render_summary_comment_is_marked_compact_and_sorted():
    """验证 summary 评论：带 MARKER、精简字段、按 severity 排序、表格转义。

    测试目的
    --------
    一条用例同时锁定 summary 的四个不变量，避免拆成多个小测试造成上下文割裂：

    1. 必须以 ``SUMMARY_COMMENT_MARKER + "\n"`` 开头（用于在 PR 评论里被识别为本 Agent 的 summary）；
    2. 包含精简统计字段 ``- Changed files:`` 与 ``- Findings:``；
    3. **不**包含 ``## JSON Output``（完整 JSON 只在本地报告里出现，summary 不重复）；
    4. 按 severity 降序排列（``error`` 先于 ``warning``）；
    5. finding 文本中的 ``|`` 与 ``\n`` 必须被转义为 ``\\|`` 与 ``<br>``。

    测试场景
    --------
    构造两个 issue：
    * ``z.py`` 的 ``warning``（todo）—— message 干净；
    * ``a.py`` 的 ``error``（secret）—— message 含 ``|`` 与换行，专门用来
      验证表格转义。
    变更文件两个：``a.py`` 与 ``z.py``，故意乱序输入，验证计数仍正确。

    预期结果
    --------
    * ``summary.startswith(SUMMARY_COMMENT_MARKER + "\n")``；
    * 含 ``- Changed files: 2`` 与 ``- Findings: 2``；
    * 不含 ``## JSON Output``；
    * ``error`` 行在 ``warning`` 行之前出现（用 ``str.index`` 比较下标）；
    * 转义后 ``token\\|with<br>newline`` 出现在 summary 中。
    """
    issues = [
        ReviewIssue(
            file_path="z.py",
            line_no=3,
            severity="warning",
            category="todo",
            message="later",
            suggestion="remove it",
        ),
        ReviewIssue(
            file_path="a.py",
            line_no=2,
            severity="error",
            category="secret",
            message="token|with\nnewline",  # 故意注入表格分隔符与换行，验证转义
            suggestion="rotate it",
        ),
    ]
    changed_files = [
        ChangedFile("a.py", [], [], ""),
        ChangedFile("z.py", [], [], ""),
    ]

    summary = render_summary_comment(issues, changed_files)

    assert summary.startswith(SUMMARY_COMMENT_MARKER + "\n")  # 必须带 PR 识别标记
    assert "- Changed files: 2" in summary  # 精简统计字段：变更文件数
    assert "- Findings: 2" in summary  # 精简统计字段：findings 总数
    assert "## JSON Output" not in summary  # summary 不重复完整 JSON
    # 通过下标比较验证 error 排在 warning 之前（severity 降序）
    assert summary.index("| error | a.py | 2") < summary.index("| warning | z.py | 3")
    # 验证表格转义：| -> \|，换行 -> <br>，防止破坏 Markdown 表格/注入
    assert "token\\|with<br>newline" in summary


def test_render_summary_comment_marks_downgraded_finding_as_summary_only():
    """验证 ``placement=summary`` 的 finding 在 summary 中以 “summary only” 形式展示。

    测试目的
    --------
    某些 finding（尤其是 LLM 产生的）定位的行号可能落在变更 hunk 之外，
    无法作为行内评论发布，只能“降级”为 summary-only。这种 finding 必须：

    * 在 summary 表格里以 ``summary only`` 作为位置列、``summary`` 作为
      placement 列展示；
    * **不**以原行号（``99``）的形式出现，避免误导用户去定位一个
      hunk 之外的行。

    测试场景
    --------
    构造一个 ``placement="summary"``、``line_no=99`` 的 issue，
    调用 ``render_summary_comment``。

    预期结果
    --------
    * summary 中存在完整的一行表格，位置列是 ``summary only``、
      placement 列是 ``summary``；
    * summary 中 **不**出现 ``| warning | app.py | 99``（即不以原行号展示）。
    """
    issue = ReviewIssue(
        file_path="app.py",
        line_no=99,
        severity="warning",
        category="llm",
        message="line is outside the changed hunk",
        suggestion="fix it",
        source="llm",
        placement="summary",  # 标记为 summary-only，不能行内发布
    )

    summary = render_summary_comment([issue], [ChangedFile("app.py", [], [], "")])

    # 降级展示：位置列写 summary only，placement 列写 summary
    assert "| warning | app.py | summary only | summary | llm | line is outside the changed hunk |" in summary
    # 不应以原始行号 99 出现，避免误导用户去 hunk 之外定位
    assert "| warning | app.py | 99" not in summary


def test_render_summary_comment_handles_no_findings():
    """验证无 findings 时 summary 仍带 MARKER 且显式提示“No findings.”。

    测试目的
    --------
    空 findings 不应导致渲染崩溃或输出空字符串，而应输出一条带 MARKER、
    ``- Findings: 0`` 计数以及 ``No findings.`` 提示的合法 summary，
    保证 PR 评论里用户能清楚看到“已审查、无问题”这一结论。

    测试场景
    --------
    传入空的 issues 与 changed_files 列表。

    预期结果
    --------
    * summary 以 ``SUMMARY_COMMENT_MARKER + "\n"`` 开头；
    * 含 ``- Findings: 0``；
    * 含 ``No findings.`` 文案。
    """
    summary = render_summary_comment([], [])

    assert summary.startswith(SUMMARY_COMMENT_MARKER + "\n")  # 即使无 findings 也要带标记
    assert "- Findings: 0" in summary  # 计数为 0
    assert "No findings." in summary  # 显式给出“无问题”提示，避免空评论误导用户


# 三类典型凭证标记，覆盖 secret 字面量、GitHub token、OpenAI token 三种形态，
# 用于验证脱敏逻辑对不同格式凭证都有覆盖。
SECRET_MARKER = "LEAKED_SECRET_VALUE_42"
GITHUB_TOKEN_MARKER = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
OPENAI_TOKEN_MARKER = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"


def test_render_summary_comment_redacts_credential_values_in_finding_message():
    """P0: secret values in finding messages must not appear in the published summary.

    本测试的 docstring 保留英文原意作为 P0 安全契约说明；以下为中文补充说明。

    测试目的
    --------
    P0 安全不变量：finding 的任何文本字段（``file_path`` / ``category`` /
    ``message`` / ``suggestion`` / ``reason`` / ``evidence``）中若包含凭证值，
    在渲染为对外发布的 summary 评论时必须被脱敏为 ``[REDACTED]``，
    绝不能原样出现在 PR 评论中。

    测试场景
    --------
    故意把三类凭证标记塞进 issue 的几乎所有字段：
    * ``file_path`` 含 ``API_KEY={SECRET_MARKER}``；
    * ``category`` 含 ``token={SECRET_MARKER}``；
    * ``message`` 含 GitHub token；
    * ``suggestion`` 含 OpenAI token；
    * ``reason`` / ``evidence`` 含 SECRET_MARKER。

    预期结果
    --------
    * SECRET_MARKER 不出现在 summary 中；
    * summary 含 ``[REDACTED]``，且 ``API_KEY=[REDACTED]`` 形式存在
      （说明脱敏保留了“键名”只替换了“值”）；
    * GitHub token 与 OpenAI token 也不出现。
    """
    issues = [
        ReviewIssue(
            file_path="configs/API_KEY={}.py".format(SECRET_MARKER),
            line_no=1,
            severity="error",
            category="token={}".format(SECRET_MARKER),
            message="Hardcoded credential: {}".format(GITHUB_TOKEN_MARKER),
            suggestion="Replace {} with an environment variable".format(
                OPENAI_TOKEN_MARKER
            ),
            reason=f"token={SECRET_MARKER} is exposed",
            evidence=f"app.py:1 API_KEY={SECRET_MARKER}",
        ),
    ]
    changed_files = [ChangedFile("app.py", [], [], "")]

    summary = render_summary_comment(issues, changed_files)

    assert SECRET_MARKER not in summary, (
        "P0 泄露: secret marker 不应出现在 summary comment 中"
    )
    assert "[REDACTED]" in summary  # 脱敏占位符必须出现
    assert "API_KEY=[REDACTED]" in summary  # 脱敏保留键名、仅替换值
    assert GITHUB_TOKEN_MARKER not in summary  # GitHub token 也必须被脱敏
    assert OPENAI_TOKEN_MARKER not in summary  # OpenAI token 也必须被脱敏


def test_render_json_report_redacts_credential_values():
    """P0: secret values must also be redacted in the local JSON report.

    本测试的 docstring 保留英文原意作为 P0 安全契约说明；以下为中文补充说明。

    测试目的
    --------
    P0 安全不变量（本地报告侧）：除了对外发布的 summary 评论，本地保存的
    JSON 报告也必须对凭证值脱敏。否则凭证会通过落盘的 trace/报告文件被
    二次泄露（例如 trace 文件被上传到日志系统时）。

    测试场景
    --------
    构造一个 issue，在 ``message`` / ``suggestion`` / ``reason`` / ``evidence``
    中分别塞入 GitHub token、OpenAI token、SECRET_MARKER。

    预期结果
    --------
    * SECRET_MARKER 不在 JSON 报告中；
    * 报告含 ``[REDACTED]``；
    * GitHub token 与 OpenAI token 也不在报告中。

    特殊逻辑解释
    ------------
    ``render_json_report`` 在函数内部 import 而非模块顶部，是为了与文件其他
    测试（只测 summary）保持导入最小化；这里需要 JSON 渲染能力才按需引入。
    """
    from src.reporter import render_json_report

    issues = [
        ReviewIssue(
            file_path="app.py",
            line_no=1,
            severity="error",
            category="secret",
            message="credential {}".format(GITHUB_TOKEN_MARKER),
            suggestion="replace {}".format(OPENAI_TOKEN_MARKER),
            reason=f"token={SECRET_MARKER}",
            evidence=f"app.py:1 password={SECRET_MARKER}",
        ),
    ]

    report = render_json_report(issues)

    assert SECRET_MARKER not in report  # P0：本地报告也不得包含原始凭证值
    assert "[REDACTED]" in report  # 脱敏占位符必须出现
    assert GITHUB_TOKEN_MARKER not in report  # GitHub token 必须被脱敏
    assert OPENAI_TOKEN_MARKER not in report  # OpenAI token 必须被脱敏
