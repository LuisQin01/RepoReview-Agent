"""``src/validation.py`` 与 ``src/llm_reviewer.py``（prompt/parse 部分）的单元测试集合。

本文件覆盖 RepoReview Agent 的校验层，主要验证以下能力：

1. **issue 定位校验**（``validate_issue_locations``）：判断 LLM 返回的 finding
   是否落在变更 hunk 边界内，决定其 ``placement`` 是 ``inline``（可内联展示）
   还是 ``summary``（降级到摘要）；并对非法 path/line 进行修复；
2. **LLM 响应解析与修复**（``validate_llm_response`` / ``parse_llm_response``）：
   处理坏 JSON、缺失字段、非法 confidence（NaN/Infinity）、非法文本元数据类型，
   并在修复后保证 JSON/Markdown 报告可正常渲染；
3. **source 字段管理**：prompt 中声明 ``source`` 由系统赋值，解析时丢弃 LLM
   自带的 ``source`` 并统一赋为 ``"llm"``；
4. **prompt 预算控制**（``build_llm_prompt``）：在 token 预算内对大 patch、
   diff 行、上下文与 findings 进行截断，并以 ``_TRUNCATION_MARKER`` 收尾。

测试策略
--------
- 直接调用 ``validate_*`` / ``parse_llm_response`` / ``build_llm_prompt`` 函数，
  断言返回值与 ``placement``、``repaired``、``errors`` 等字段；
- 对报告渲染，调用 ``render_json_report`` / ``render_markdown_report`` 验证
  修复后的字段在输出中表现正确；
- 对预算控制，构造超长 patch 与多类内容，断言 prompt 长度不超限且以截断
  标记结尾。

在整体测试体系中的位置
----------------------
本文件是 LLM 调用链上游的护栏：确保即便模型返回不规范内容，系统也能修复
或降级，绝不向用户暴露原始异常或非法数据。与 ``test_llm_reviewer.py`` 共同
覆盖 prompt 构建与响应解析的完整闭环。
"""
import json

from src.validation import validate_issue_locations, validate_llm_response
from src.llm_reviewer import (
    _TRUNCATION_MARKER,
    build_llm_prompt,
    parse_llm_response,
    review_with_llm,
)
from src.reporter import render_json_report, render_markdown_report
from src.schemas import ChangedFile, DiffHunk, DiffLine, FileContext, ReviewIssue


def test_validate_issue_locations_keeps_changed_hunk_boundaries_and_repository_rules():
    """验证 issue 落在 hunk 边界内时保留为 inline，仓库级 issue 归入 summary。

    测试目的
    --------
    ``validate_issue_locations`` 应根据变更文件的 hunk 边界判断 finding 是否
    可内联展示：
    - 行号落在某个 hunk 的 ``[start, end]`` 区间内 → ``inline``；
    - 仓库级 finding（``file_path="(repository)"``）→ ``summary``。

    测试场景
    --------
    构造一个含两个 hunk（``[10,12]`` 与 ``[20,20]``）的文件，提交四条 finding：
    三条落在 hunk 边界（10、12、20），一条为仓库级 ``test_gap``。

    预期输出
    --------
    - 校验后 findings 列表内容不变（顺序与字段保持）；
    - 前三条 ``placement="inline"``，最后一条 ``placement="summary"``。
    """
    changed_files = [
        ChangedFile(
            path="src/app.py",
            added_lines=[],
            deleted_lines=[],
            patch="",
            hunks=[DiffHunk(10, 12), DiffHunk(20, 20)],
        )
    ]
    issues = [
        ReviewIssue("src/app.py", 10, "warning", "llm", "start", "fix", source="llm"),  # hunk1 起始
        ReviewIssue("src/app.py", 12, "warning", "llm", "end", "fix", source="llm"),  # hunk1 结束
        ReviewIssue("src/app.py", 20, "warning", "llm", "second", "fix", source="llm"),  # hunk2 单行
        ReviewIssue("(repository)", 0, "warning", "test_gap", "gap", "fix", source="rule"),  # 仓库级
    ]

    validated = validate_issue_locations(issues, changed_files)

    assert validated == issues  # 内容与顺序不变
    # 前三条可内联，仓库级归入摘要
    assert [issue.placement for issue in validated] == ["inline", "inline", "inline", "summary"]


def test_validate_issue_locations_downgrades_out_of_scope_or_unlocatable_findings_to_summary():
    """验证越界或不可定位的 finding 降级为 summary，并修复非法 path/line。

    测试目的
    --------
    当 finding 的文件不在变更列表、行号超出 hunk 范围、path 类型非法或 line
    类型非法时，应将其 ``placement`` 降级为 ``summary``，并对非法 path/line
    做修复（path → ``"(unlocatable)"``，line → ``0``），避免后续渲染崩溃。

    测试场景
    --------
    - 一条合法 finding（落在 hunk 内）作为对照；
    - 五条非法 finding：错误文件、行号在 hunk 前、行号在 hunk 后、path 为
      列表、line 为布尔值。

    预期输出
    --------
    - 合法 finding 的 ``placement="inline"``；
    - 五条非法 finding 的 ``placement="summary"``；
    - path 为列表的 finding 修复后 ``file_path="(unlocatable)"``；
    - line 为布尔的 finding 修复后 ``line_no=0``。
    """
    changed_files = [
        ChangedFile(
            path="src/app.py",
            added_lines=[],
            deleted_lines=[],
            patch="",
            hunks=[DiffHunk(10, 12)],
        )
    ]
    valid_issue = ReviewIssue("src/app.py", 11, "warning", "llm", "valid", "fix", source="llm")
    invalid_issues = [
        ReviewIssue("src/other.py", 11, "warning", "llm", "wrong file", "fix", source="llm"),  # 文件不匹配
        ReviewIssue("src/app.py", 9, "warning", "llm", "before", "fix", source="llm"),  # 行号在 hunk 之前
        ReviewIssue("src/app.py", 13, "warning", "llm", "after", "fix", source="llm"),  # 行号在 hunk 之后
        ReviewIssue(["src/app.py"], 11, "warning", "llm", "bad path", "fix", source="llm"),  # path 非字符串
        ReviewIssue("src/app.py", True, "warning", "llm", "bad line", "fix", source="llm"),  # line 非数字
    ]

    validated = validate_issue_locations([valid_issue, *invalid_issues], changed_files)

    assert validated == [valid_issue, *invalid_issues]  # 列表保持不变
    assert valid_issue.placement == "inline"  # 合法 finding 内联
    assert [issue.placement for issue in invalid_issues] == ["summary"] * len(invalid_issues)  # 非法一律降级
    assert invalid_issues[3].file_path == "(unlocatable)"  # 非法 path 修复为占位
    assert invalid_issues[4].line_no == 0  # 非法 line 修复为 0


def test_bad_json_returns_error_not_exception():
    """验证坏 JSON 返回错误结果而非抛异常。

    测试目的
    --------
    当 LLM 返回的内容无法解析为 JSON 时，``validate_llm_response`` 不应抛出
    异常，而应返回一个 ``valid=False`` 的结果，并在 ``errors`` 中标记
    ``llm_json_parse_error``，便于上层优雅降级。

    测试场景
    --------
    传入非 JSON 文本 ``"this is not json"``。

    预期输出
    --------
    - ``result.valid is False``；
    - ``result.findings == []``；
    - ``errors`` 含 ``llm_json_parse_error``。
    """
    result = validate_llm_response("this is not json")

    assert result.valid is False  # 解析失败，标记无效
    assert result.findings == []  # 无 findings
    assert "llm_json_parse_error" in result.errors  # 错误码存在


def test_missing_fields_are_repaired():
    """验证缺失或非法字段被修复为默认值。

    测试目的
    --------
    LLM 返回的 finding 可能缺字段或给出非法值（如未知 severity）。校验层应
    补全默认值，使后续渲染不依赖完整字段：
    - 非法 severity → ``medium``；
    - 缺 file → ``(unknown)``；
    - 缺 line → ``0``；
    - 缺 confidence → ``0.5``；
    - 缺 reason/suggested_fix/evidence → 空字符串。

    测试场景
    --------
    传入一个仅含 severity（且为非法值）与 issue 的 finding。

    预期输出
    --------
    - ``valid=True``、``repaired=True``；
    - 各字段被修复为上述默认值。
    """
    response_text = """
    {
      "findings": [
        {
          "severity": "strange",
          "issue": "Something is wrong"
        }
      ]
    }
    """

    result = validate_llm_response(response_text)

    assert result.valid is True  # 修复后视为有效
    assert result.repaired is True  # 标记发生过修复

    finding = result.findings[0]
    assert finding["severity"] == "medium"  # 非法 severity 修复为 medium
    assert finding["file"] == "(unknown)"  # 缺 file 修复为占位
    assert finding["line"] == 0  # 缺 line 修复为 0
    assert finding["confidence"] == 0.5  # 缺 confidence 修复为 0.5
    assert finding["reason"] == ""  # 缺 reason 修复为空串
    assert finding["suggested_fix"] == ""
    assert finding["evidence"] == ""


def test_metadata_fields_are_preserved():
    """验证合法 finding 的元数据字段被原样保留。

    测试目的
    --------
    当 finding 提供了完整且合法的字段时，校验层不应做任何修改，``repaired``
    应为 ``False``。同时 ``source`` 字段由系统管理，不应出现在 findings 中。

    测试场景
    --------
    传入一个字段齐全、值合法的 finding。

    预期输出
    --------
    - ``valid=True``、``repaired=False``；
    - issue/reason/suggested_fix/confidence/evidence 原样保留；
    - findings 中不含 ``source`` 键。
    """
    response_text = """
    {
      "findings": [
        {
          "severity": "high",
          "file": "src/app.py",
          "line": 12,
          "issue": "Unhandled error",
          "reason": "The operation can fail.",
          "suggested_fix": "Handle the error.",
          "confidence": 0.82,
          "evidence": "src/app.py:12"
        }
      ]
    }
    """

    result = validate_llm_response(response_text)

    assert result.valid is True  # 合法输入视为有效
    assert result.repaired is False  # 无需修复
    assert result.findings[0]["issue"] == "Unhandled error"  # 原样保留
    assert result.findings[0]["reason"] == "The operation can fail."
    assert result.findings[0]["suggested_fix"] == "Handle the error."
    assert result.findings[0]["confidence"] == 0.82
    assert result.findings[0]["evidence"] == "src/app.py:12"
    assert "source" not in result.findings[0]  # source 由系统管理，不进入 findings


def test_llm_prompt_declares_source_system_managed():
    """验证 prompt 中声明 source 由系统管理。

    测试目的
    --------
    为防止 LLM 自行输出 ``source`` 字段，prompt 中应明确告知模型 ``source``
    由系统赋值，且不应在示例中出现 ``"source": "llm"`` 的字面量（避免模型
    模仿输出）。

    测试场景
    --------
    以空输入调用 ``build_llm_prompt``，仅检查 prompt 文本。

    预期输出
    --------
    - prompt 不含 ``"source": "llm"`` 字面量；
    - prompt 含中文声明 ``source 字段由系统根据调用路径赋值；不要在 JSON 中输出 source。``。
    """
    prompt = build_llm_prompt([], [], [])

    assert '"source": "llm"' not in prompt  # 不暴露 source 字面量，防模型模仿
    # 明确声明 source 由系统管理
    assert "source 字段由系统根据调用路径赋值；不要在 JSON 中输出 source。" in prompt


def test_llm_source_is_assigned_by_the_parser():
    """验证解析器丢弃 LLM 自带的 source 并统一赋 ``"llm"``。

    测试目的
    --------
    即便 LLM 在 finding 中输出了 ``"source": "rule"``，解析器也应：
    - 丢弃该字段（不进入 findings）；
    - 在 ``errors`` 中标记 ``llm_finding_0_ignored_source``；
    - 将最终 ``ReviewIssue.source`` 强制赋为 ``"llm"``。

    测试场景
    --------
    传入一个 source 为 ``"rule"`` 的 finding。

    预期输出
    --------
    - ``valid=True``、``repaired=True``；
    - ``errors`` 含 ``llm_finding_0_ignored_source``；
    - findings 中不含 ``source``；
    - ``issues[0].source == "llm"``。
    """
    issues, validation = parse_llm_response(
        """{
          "findings": [{
            "severity": "low",
            "file": "src/app.py",
            "line": 1,
            "issue": "Example",
            "reason": "Example reason",
            "suggested_fix": "Example fix",
            "confidence": 0.6,
            "evidence": "src/app.py:1",
            "source": "rule"
          }]
        }"""
    )

    assert validation.valid is True
    assert validation.repaired is True  # 发生过修复（source 被覆盖）
    assert "llm_finding_0_ignored_source" in validation.errors  # 标记忽略了自带 source
    assert "source" not in validation.findings[0]  # findings 不含 source 键
    assert issues[0].source == "llm"  # 最终 source 由系统赋为 "llm"


def test_non_finite_confidence_is_repaired_before_json_rendering():
    """验证非有限 confidence（NaN/Infinity）被修复后再渲染。

    测试目的
    --------
    LLM 可能返回字符串形式的 ``NaN`` / ``Infinity`` / ``-Infinity``，这些值
    无法被标准 JSON 序列化（``json.dumps`` 默认会输出非法 token）。校验层应：
    - 将其修复为默认值 ``0.5``；
    - 在 ``errors`` 中标记 ``non_finite_confidence``；
    - 确保渲染后的 JSON 报告中不含 ``NaN`` 字面量。

    测试场景
    --------
    对 ``NaN``、``Infinity``、``-Infinity`` 三种值分别构造 finding 并解析。

    预期输出
    --------
    对每种 confidence 值：
    - ``repaired=True``；
    - ``errors`` 含 ``llm_finding_0_non_finite_confidence``；
    - ``issues[0].confidence == 0.5``；
    - JSON 报告中不含 ``NaN``，且 confidence 字段为 ``0.5``。
    """
    for confidence in ("NaN", "Infinity", "-Infinity"):
        issues, validation = parse_llm_response(
            f"""{{
              "findings": [{{
                "severity": "low",
                "file": "src/app.py",
                "line": 1,
                "issue": "Example",
                "reason": "Example reason",
                "suggested_fix": "Example fix",
                "confidence": "{confidence}",
                "evidence": "src/app.py:1"
              }}]
            }}"""
        )

        report = render_json_report(issues)

        assert validation.repaired is True
        assert "llm_finding_0_non_finite_confidence" in validation.errors  # 标记非有限
        assert issues[0].confidence == 0.5  # 修复为默认值
        assert "NaN" not in report  # 渲染输出不含 NaN 字面量
        assert json.loads(report)["findings"][0]["confidence"] == 0.5  # JSON 中为 0.5


def test_invalid_text_metadata_is_repaired_in_json_and_markdown_reports():
    """验证非法文本元数据类型被修复，并在 JSON/Markdown 报告中正确呈现。

    测试目的
    --------
    LLM 可能返回非字符串的文本字段（如 issue 为 null、reason 为 dict、
    suggested_fix 为 list、evidence 为 null）。校验层应：
    - 将这些字段修复为空字符串；
    - 在 ``errors`` 中为每个字段标记对应的 ``invalid_*_type``；
    - 确保 JSON 与 Markdown 报告中不出现 ``None`` 等非法表示。

    测试场景
    --------
    传入一个 issue=null、reason=dict、suggested_fix=list、evidence=null 的
    finding。

    预期输出
    --------
    - ``valid=True``、``repaired=True``；
    - ``errors`` 含四个 ``invalid_*_type`` 标记；
    - JSON 报告中各文本字段为空串，``category="llm"``；
    - Markdown 报告含对应行，且不含 ``None``。
    """
    issues, validation = parse_llm_response(
        """{
          "findings": [{
            "severity": "low",
            "file": "src/app.py",
            "line": 1,
            "issue": null,
            "reason": {"detail": "Example reason"},
            "suggested_fix": ["Example fix"],
            "confidence": 0.5,
            "evidence": null
          }]
        }"""
    )

    json_report = json.loads(render_json_report(issues))
    markdown_report = render_markdown_report(issues, [], [])

    assert validation.valid is True
    assert validation.repaired is True
    # 四个字段均被标记为非法类型
    assert {
        "llm_finding_0_invalid_issue_type",
        "llm_finding_0_invalid_reason_type",
        "llm_finding_0_invalid_suggested_fix_type",
        "llm_finding_0_invalid_evidence_type",
    }.issubset(validation.errors)
    assert json_report["findings"][0]["category"] == "llm"  # category 由系统赋值
    assert json_report["findings"][0]["issue"] == ""  # 修复为空串
    assert json_report["findings"][0]["reason"] == ""
    assert json_report["findings"][0]["suggested_fix"] == ""
    assert json_report["findings"][0]["evidence"] == ""
    # Markdown 报告中对应行存在，且字段为空
    assert "| info | src/app.py | 1 | llm |  |  |  | 0.5 |  | llm |" in markdown_report
    assert "None" not in markdown_report  # 不出现 None 字面量


def test_llm_prompt_budget_includes_large_patch_without_contexts():
    """验证 prompt 预算对大 patch（无上下文）进行截断。

    测试目的
    --------
    当变更文件的 patch 超过 ``max_prompt_chars`` 预算时，``build_llm_prompt``
    应将 prompt 截断到预算以内，并以 ``_TRUNCATION_MARKER`` 结尾，确保 prompt
    不会超过模型上下文限制。

    测试场景
    --------
    构造一个 patch 长达 5000 字符的文件，``max_prompt_chars=4000``，不附上下文。

    预期输出
    --------
    - ``len(prompt) <= 4000``；
    - prompt 含 ``"patch"`` 字段；
    - prompt 以 ``_TRUNCATION_MARKER`` 结尾。
    """
    changed_files = [
        ChangedFile(
            path="src/large.py",
            patch="+" + "x" * 5000,  # 远超预算的 patch
            added_lines=[],
            deleted_lines=[],
        )
    ]

    prompt = build_llm_prompt(
        changed_files,
        contexts=[],
        rule_issues=[],
        max_prompt_chars=4000,
    )

    assert len(prompt) <= 4000  # 不超预算
    assert '"patch"' in prompt  # patch 字段仍在
    assert prompt.endswith(_TRUNCATION_MARKER)  # 以截断标记结尾


def test_llm_prompt_budget_covers_serialized_diff_lines_contexts_and_findings():
    """验证 prompt 预算覆盖 diff 行、上下文与 findings 的综合截断。

    测试目的
    --------
    当 patch、added/deleted 行、上下文内容与规则发现同时存在且总量超预算时，
    ``build_llm_prompt`` 应在预算内进行综合截断，保留 ``changed_files`` 结构
    并以 ``_TRUNCATION_MARKER`` 结尾。

    测试场景
    --------
    构造一个含大 patch、长 added/deleted 行的文件，附一个大上下文与一条长
    finding，``max_prompt_chars=2200``。

    预期输出
    --------
    - ``len(prompt) <= 2200``；
    - prompt 含 ``"changed_files"`` 字段；
    - prompt 以 ``_TRUNCATION_MARKER`` 结尾。
    """
    changed_files = [
        ChangedFile(
            path="src/app.py",
            patch="patch-" + "p" * 1200,
            added_lines=[DiffLine("src/app.py", 4, "added-" + "a" * 600)],
            deleted_lines=[DiffLine("src/app.py", 3, "deleted-" + "d" * 600)],
        )
    ]
    contexts = [
        FileContext(
            path="src/app.py",
            exists=True,
            content="context-" + "c" * 1200,  # 超长上下文
            truncated=False,
            chars_read=1208,
        )
    ]
    findings = [
        ReviewIssue(
            file_path="src/app.py",
            line_no=4,
            severity="warning",
            category="rule",
            message="finding-" + "f" * 600,  # 超长 finding
            suggestion="fix",
        )
    ]

    prompt = build_llm_prompt(
        changed_files,
        contexts,
        findings,
        max_prompt_chars=2200,
    )

    assert len(prompt) <= 2200  # 综合截断后不超预算
    assert '"changed_files"' in prompt  # 结构字段保留
    assert prompt.endswith(_TRUNCATION_MARKER)  # 以截断标记结尾


def test_llm_prompt_keeps_complete_normal_input_when_within_budget():
    """验证预算内正常输入不被截断。

    测试目的
    --------
    当所有内容总长在 ``max_prompt_chars`` 预算内时，prompt 应完整保留所有
    内容，不出现 ``_TRUNCATION_MARKER``，确保正常场景下信息无丢失。

    测试场景
    --------
    构造一个小 patch 与简短上下文，``max_prompt_chars=10000``（远大于内容）。

    预期输出
    --------
    - ``len(prompt) <= 10000``；
    - prompt 不含 ``_TRUNCATION_MARKER``；
    - prompt 含 patch 原文 ``+safe_change()`` 与上下文 ``def safe_change``。
    """
    changed_files = [
        ChangedFile(
            path="src/app.py",
            patch="+safe_change()",
            added_lines=[DiffLine("src/app.py", 1, "safe_change()")],
            deleted_lines=[],
        )
    ]
    contexts = [
        FileContext("src/app.py", True, "def safe_change():\n    return True\n", False, 35)
    ]

    prompt = build_llm_prompt(
        changed_files,
        contexts,
        rule_issues=[],
        max_prompt_chars=10000,
    )

    assert len(prompt) <= 10000  # 预算内
    assert _TRUNCATION_MARKER not in prompt  # 未触发截断
    assert "+safe_change()" in prompt  # patch 原文保留
    assert "def safe_change" in prompt  # 上下文原文保留


def test_review_with_llm_sends_the_budgeted_prompt_to_the_model():
    """验证 ``review_with_llm`` 将预算控制后的 prompt 发送给模型。

    测试目的
    --------
    ``review_with_llm`` 内部应先调用 ``build_llm_prompt`` 进行预算截断，再将
    截断后的 prompt 传给 ``call_model``。需确保：
    - 仅发送一次请求；
    - 发送的 prompt 不超过 ``max_prompt_chars``；
    - prompt 以 ``_TRUNCATION_MARKER`` 结尾（证明经过了截断）。

    测试场景
    --------
    构造一个超长 patch（5000 字符），``max_prompt_chars=4000``，注入
    ``call_model`` 捕获实际收到的 prompt。

    预期输出
    --------
    - ``issues == []``、``validation.valid is True``；
    - 捕获到 1 个 prompt；
    - 该 prompt 长度 ``<= 4000``；
    - 该 prompt 以 ``_TRUNCATION_MARKER`` 结尾。
    """
    captured_prompts = []
    changed_files = [
        ChangedFile("src/app.py", [], [], "+" + "x" * 5000)  # 超长 patch
    ]

    issues, validation = review_with_llm(
        changed_files,
        contexts=[],
        rule_issues=[],
        # 捕获 prompt 并返回空 findings
        call_model=lambda prompt: captured_prompts.append(prompt) or '{"findings": []}',
        max_prompt_chars=4000,
    )

    assert issues == []  # 模型返回空 findings
    assert validation.valid is True
    assert len(captured_prompts) == 1  # 仅发送一次请求
    assert len(captured_prompts[0]) <= 4000  # 发送的 prompt 在预算内
    assert captured_prompts[0].endswith(_TRUNCATION_MARKER)  # 经过了截断
