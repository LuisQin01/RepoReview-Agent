"""Three-layer tests proving declared sensitive categories never enter the model payload.

L1 retrieval  — read_file_context returns empty for every declared category
L2 collection — collect_file_contexts never adds sensitive files as extra candidates
L3 outbound   — build_llm_prompt redacts every sensitive category's diff
E2E           — run_review_agent output + trace contain no secret marker

中文说明（实习面试展示用）：
    本文件是 RepoReview Agent 的「安全测试核心」，验证三层敏感泄露防护
    在端到端链路上确实阻断了敏感内容外泄。

    被测模块与分层：
        - L1 检索层：``src/file_context.read_file_context`` —— 文件名 / 后缀 /
          路径黑名单，在读取阶段直接拒读敏感文件。
        - L2 收集层：``src/file_context.collect_file_contexts`` —— import 候选
          只产生 .py 路径、call_name 候选只扫描 *.py，因此敏感文件（非 .py）
          天然不会成为额外上下文候选。
        - L3 外发层：``src/llm_reviewer.build_llm_prompt`` —— 对敏感文件的
          diff（含 rename 场景的 old_path）替换为脱敏占位符。
        - E2E 端到端：``src/cli.run_review_agent`` —— 全链路验证最终 output
          与 trace 文件均不含敏感标记。

    关键设计（重要）：
        测试参数从实现常量 ``SENSITIVE_FILE_NAMES`` / ``SENSITIVE_FILE_SUFFIXES``
        自动枚举，而非硬编码副本。这样：
          1. 实现侧新增敏感项时，测试自动覆盖，无需手工同步；
          2. canary 用例做反向验证——若有人从常量中删除某敏感项，canary
             仍会存活并使测试失败，证明测试在守护「已声明集合」的完整性。

    在整体测试体系中的位置：
        与 ``test_file_context.py`` 互补——后者验证检索 / 截断 / 预算的
        正确语义，本文件验证这些语义在端到端链路上的安全效果。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.cli import run_review_agent
from src.file_context import (
    SENSITIVE_FILE_NAMES,
    SENSITIVE_FILE_SUFFIXES,
    collect_file_contexts,
    read_file_context,
)
from src.llm_reviewer import _REDACTED_DIFF_PLACEHOLDER, build_llm_prompt
from src.schemas import ChangedFile, ContextBudget, DiffLine

SECRET_MARKER = "LEAKED_SECRET_VALUE_42"  # 全局敏感标记：植入所有敏感文件内容中，测试中只要发现该字符串出现即判定为泄露

# ---------------------------------------------------------------------------
# Parameter sources — auto-enumerated from implementation constants
# 参数来源 —— 从实现常量自动枚举（不硬编码副本）
# ---------------------------------------------------------------------------

# Auto-enumerated from SENSITIVE_FILE_NAMES / SENSITIVE_FILE_SUFFIXES.
# New declarations are automatically covered; no hardcoded copy of the sets.
# 从 SENSITIVE_FILE_NAMES / SENSITIVE_FILE_SUFFIXES 自动派生：
# 实现侧新增敏感项时测试自动覆盖，无需在此手工同步副本。
_L1_CONSTANT_NAME_CASES = sorted(SENSITIVE_FILE_NAMES)  # 文件名黑名单派生用例
_L1_CONSTANT_SUFFIX_CASES = [
    f"certificates/service{suffix}" for suffix in sorted(SENSITIVE_FILE_SUFFIXES)
]  # 后缀黑名单派生用例（统一放在 certificates/ 下构造路径）

# Prefix variants — exercise filename.startswith(".env."), a separate code
# path not represented by SENSITIVE_FILE_NAMES entries directly.
# 前缀变体用例：覆盖 ``filename.startswith(".env.")`` 这条独立判定分支，
# 该分支不在 SENSITIVE_FILE_NAMES 集合中直接体现，需单独构造。
_L1_PREFIX_VARIANT_CASES = [".env.local", ".env.production"]

# Canary cases for reverse validation.  These overlap with constant-derived
# cases when the constants are intact (deduplicated away).  If any item is
# removed from the constant, the canary survives and the L1 test FAILS
# because read_file_context no longer rejects the file — proving the test
# guards the declared set.
# Canary（金丝雀）反向验证用例：常量完整时这些项会被去重（与上面派生用例重叠）；
# 一旦有人从常量中删除对应敏感项，canary 不再被去重而存活下来，read_file_context
# 不再拒读该文件，L1 测试随即失败——以此证明测试在守护「已声明集合」的完整性。
_L1_CANARY_CASES = [
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "certificates/service.der",
    "certificates/service.p12",
]

# 合并四类用例并去重（保留首次出现顺序）：dict.fromkeys 既去重又保序。
_L1_ALL_CASES = list(dict.fromkeys(
    _L1_CONSTANT_NAME_CASES
    + _L1_CONSTANT_SUFFIX_CASES
    + _L1_PREFIX_VARIANT_CASES
    + _L1_CANARY_CASES
))

# L3 层参数：同样从常量派生，覆盖所有文件名 + 后缀类目。
_L3_ALL_CASES = sorted(SENSITIVE_FILE_NAMES) + [
    f"certificates/service{suffix}" for suffix in sorted(SENSITIVE_FILE_SUFFIXES)
]


# ============================================================================
# L1: read_file_context rejects every declared sensitive category
# L1 检索层：read_file_context 必须拒读每一个已声明的敏感类目
# ============================================================================

@pytest.mark.parametrize("file_path", _L1_ALL_CASES)
def test_l1_read_file_context_rejects_all_declared_sensitive_categories(
    tmp_path, file_path
):
    """L1 检索层防护：对每一个已声明敏感类目，read_file_context 都拒读且零泄露。

    测试目的：
        验证 L1 层（文件名 / 后缀 / 前缀黑名单）对全部敏感类目的覆盖完整性。
        任一类目未被拒读即视为泄露。

    测试场景（参数化设计理由）：
        参数来自 ``_L1_ALL_CASES``，由实现常量自动枚举 + 前缀变体 + canary
        合并去重而成。每个用例在仓库中创建敏感文件并写入 SECRET_MARKER，
        调用 read_file_context 读取。

    预期结果（核心不变量）：
        - exists 为 False（拒读，非「不存在」语义）；
        - content 为空、chars_read 为 0（字节级零泄露）；
        - error 含 "sensitive"（拒绝原因可识别）。

    特殊逻辑：
        canary 用例提供反向验证——若实现常量被删项，canary 存活使测试失败，
        从而守护「已声明集合」不被无声缩减。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"API_KEY={SECRET_MARKER}\n", encoding="utf-8")  # 写入可追踪的敏感标记

    context = read_file_context(repo, file_path, max_chars=4000)

    assert context.exists is False, (  # L1 层必须拒读，exists=False
        f"L1 检索层泄露: 敏感文件 {file_path} 不应被读取 (exists 应为 False)"
    )
    assert context.content == "", (  # 内容必须为空：零字节泄露
        f"L1 检索层泄露: 敏感文件 {file_path} 的 content 应为空"
    )
    assert context.chars_read == 0, (  # 读取计数为 0，防止长度侧信道
        f"L1 检索层泄露: 敏感文件 {file_path} 的 chars_read 应为 0"
    )
    assert "sensitive" in context.error.lower(), (  # 拒绝原因可识别，便于归类
        f"L1 检索层: 敏感文件 {file_path} 的 error 应包含 'sensitive'，得到 {context.error!r}"
    )


# ============================================================================
# L2: collect_file_contexts never collects sensitive files as extra candidates
# L2 收集层：collect_file_contexts 绝不把敏感文件作为额外候选收集
# =============================================================================

def test_l2_import_candidates_naturally_avoid_sensitive_paths(tmp_path):
    """Import candidates only produce .py / __init__.py paths, so sensitive
    files (which have non-.py names/suffixes) can never become import
    candidates.  This test documents and guards that conclusion.

    中文说明：
        L2 收集层防护（import 路径）——import 候选只会产生 .py / __init__.py
        路径，而敏感文件名 / 后缀均非 .py，因此天然不会成为 import 候选。
        本用例文档化并守护这一结论。

    测试场景：
        app.py 中 ``import id_rsa``，仓库内同时存在 id_rsa.py（普通模块，
        合法 import 候选）与 id_rsa（无扩展名，敏感私钥文件）。验证只收集
        前者，后者绝不进入上下文。

    预期结果：
        - id_rsa.py 被收集为 import 候选；
        - id_rsa（敏感）不被收集；
        - SECRET_MARKER 不出现在任何上下文内容中。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "import id_rsa\nid_rsa.do_work()\n",
        encoding="utf-8",
    )
    # id_rsa.py exists — would be a valid import candidate
    # id_rsa.py 存在 —— 是合法的 import 候选
    (repo / "id_rsa.py").write_text(
        "def do_work():\n    return True\n",
        encoding="utf-8",
    )
    # id_rsa (no extension) is the sensitive file — must NOT be collected
    # id_rsa（无扩展名）是敏感私钥文件 —— 绝不能被收集
    (repo / "id_rsa").write_text(
        f"PRIVATE_KEY={SECRET_MARKER}\n",
        encoding="utf-8",
    )

    changed_file = ChangedFile(
        path="app.py",
        added_lines=[
            DiffLine("app.py", 1, "import id_rsa"),
            DiffLine("app.py", 2, "id_rsa.do_work()"),
        ],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=5),
    )

    collected_paths = [ctx.path for ctx in contexts]
    all_content = "".join(ctx.content for ctx in contexts)  # 拼接所有上下文内容用于统一排查泄露

    assert "id_rsa.py" in collected_paths, (  # 普通模块应作为 import 候选被收集
        "非敏感文件 id_rsa.py 应作为 import candidate 被收集"
    )
    assert "id_rsa" not in collected_paths, (  # 敏感私钥文件绝不能被收集
        "L2 收集层泄露: 敏感文件 id_rsa (无扩展名) 不应被收集为上下文"
    )
    assert SECRET_MARKER not in all_content, (  # 全局不变量：敏感标记不得出现在任何上下文
        "L2 收集层泄露: 敏感标记不应出现在任何上下文内容中"
    )


def test_l2_call_name_candidates_never_collect_sensitive_files(tmp_path):
    """Call-name candidates only come from _iter_python_files (yields *.py),
    so sensitive files can never become call-name candidates.  This test
    places every declared sensitive name/suffix in the repo and verifies
    none are collected.

    中文说明：
        L2 收集层防护（call_name 路径）——call_name 候选源自
        ``_iter_python_files``，只会扫描 *.py 文件，因此所有敏感文件名 /
        后缀（非 .py）都不可能成为 call_name 候选。本用例把「全部」已声明
        敏感名 / 后缀都放进仓库，逐一验证无一被收集。

    测试场景：
        app.py 调用 do_work()，helper.py 定义 do_work（合法 call_name 候选）。
        同时在仓库中创建所有 SENSITIVE_FILE_NAMES 与 SENSITIVE_FILE_SUFFIXES
        对应的文件（均写入 SECRET_MARKER）。

    预期结果：
        - helper.py 被收集为 call_name 候选；
        - 任何收集到的路径都不命中敏感判定（文件名 / 后缀 / .env. 前缀）；
        - SECRET_MARKER 不出现在任何上下文内容中。

    特殊逻辑：
        同样从实现常量枚举敏感文件，保证新增敏感项自动覆盖。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("do_work()\n", encoding="utf-8")
    (repo / "helper.py").write_text(
        "def do_work():\n    return True\n",
        encoding="utf-8",
    )

    # 把所有已声明敏感文件名 / 后缀都植入仓库，逐一验证无一被收集
    for sensitive_name in SENSITIVE_FILE_NAMES:
        (repo / sensitive_name).write_text(
            f"SECRET={SECRET_MARKER}\n", encoding="utf-8"
        )
    for suffix in SENSITIVE_FILE_SUFFIXES:
        (repo / f"asset{suffix}").write_text(
            f"SECRET={SECRET_MARKER}\n", encoding="utf-8"
        )

    changed_file = ChangedFile(
        path="app.py",
        added_lines=[DiffLine("app.py", 1, "do_work()")],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=10),
    )

    collected_paths = [ctx.path for ctx in contexts]
    all_content = "".join(ctx.content for ctx in contexts)

    assert "helper.py" in collected_paths, (  # 普通模块应作为 call_name 候选被收集
        "非敏感文件 helper.py 应作为 call_name candidate 被收集"
    )

    # 遍历每个被收集路径，逐一断言其不命中任何敏感判定分支
    for path in collected_paths:
        p = Path(path)
        is_sensitive = (
            p.name in SENSITIVE_FILE_NAMES  # 文件名黑名单
            or p.suffix in SENSITIVE_FILE_SUFFIXES  # 后缀黑名单
            or p.name.startswith(".env.")  # .env. 前缀分支
        )
        assert not is_sensitive, (
            f"L2 收集层泄露: 敏感文件 {path} 不应被收集为 call_name candidate"
        )

    assert SECRET_MARKER not in all_content, (  # 全局不变量：敏感标记零出现
        "L2 收集层泄露: 敏感标记不应出现在任何上下文内容中"
    )


# ============================================================================
# L3: build_llm_prompt redacts every sensitive category's diff
# L3 外发层：build_llm_prompt 对每一个敏感类目的 diff 做脱敏替换
# =============================================================================

@pytest.mark.parametrize("file_path", _L3_ALL_CASES)
def test_l3_build_llm_prompt_redacts_secret_diff_for_each_sensitive_category(
    file_path,
):
    """L3 外发层防护：敏感文件的 diff 必须被替换为脱敏占位符，秘密不得进入 prompt。

    测试目的：
        即使敏感文件的 diff（含 SECRET_MARKER 的 added_lines / patch）被
        传入 ``build_llm_prompt``，最终 prompt 中也必须不含秘密，且出现
        ``_REDACTED_DIFF_PLACEHOLDER`` 证明脱敏已生效。

    测试场景（参数化设计理由）：
        参数来自 ``_L3_ALL_CASES``（从实现常量派生，覆盖所有文件名 + 后缀类目）。
        每个用例构造一个 ChangedFile，其 added_lines 与 patch 都含 SECRET_MARKER。

    预期结果（核心不变量）：
        - SECRET_MARKER 不在 prompt 中（零泄露）；
        - ``_REDACTED_DIFF_PLACEHOLDER`` 在 prompt 中（脱敏动作可观测）。
    """
    changed_file = ChangedFile(
        path=file_path,
        added_lines=[DiffLine(file_path, 1, f"API_KEY={SECRET_MARKER}")],
        deleted_lines=[],
        patch=f"@@ -0,0 +1,1 @@\n+API_KEY={SECRET_MARKER}\n",
    )

    prompt = build_llm_prompt(
        changed_files=[changed_file],
        contexts=[],
        rule_issues=[],
        max_prompt_chars=10000,
    )

    assert SECRET_MARKER not in prompt, (  # 秘密不得进入 LLM prompt
        f"L3 外发层泄露: 敏感文件 {file_path} 的 diff 中的秘密不应出现在 LLM prompt 中"
    )
    assert _REDACTED_DIFF_PLACEHOLDER in prompt, (  # 必须出现脱敏占位符，证明脱敏生效
        f"L3 外发层: 敏感文件 {file_path} 的 diff 应被替换为 redaction placeholder"
    )


@pytest.mark.parametrize("old_path", _L3_ALL_CASES)
def test_l3_build_llm_prompt_redacts_secret_diff_when_sensitive_file_renamed(
    old_path,
):
    """A sensitive file renamed to a non-sensitive name still carries secret
    content in deleted_lines/patch.  The sanitizer must check old_path too.

    中文说明：
        L3 外发层防护（rename 场景）——敏感文件被重命名为普通文件名后，
        其旧内容仍存在于 deleted_lines / patch 中。脱敏器必须同时检查
        ``old_path``，否则秘密会借 rename 之机混入 prompt。

    测试场景：
        old_path 为敏感路径，新路径为 config.txt（非敏感），is_rename=True。
        deleted_lines 与 patch 的删除侧均含 SECRET_MARKER。

    预期结果：
        - SECRET_MARKER 不在 prompt 中；
        - ``_REDACTED_DIFF_PLACEHOLDER`` 在 prompt 中。

    设计理由：
        rename 是常见的脱敏绕过尝试——仅检查新路径会漏判。本用例覆盖
        所有敏感类目作为 old_path，确保脱敏器对 old_path 同样生效。
    """
    changed_file = ChangedFile(
        path="config.txt",
        old_path=old_path,
        is_rename=True,
        added_lines=[DiffLine("config.txt", 1, "+safe_value=ok")],
        deleted_lines=[DiffLine(old_path, 1, f"-API_KEY={SECRET_MARKER}")],
        patch=f"--- a/{old_path}\n+++ b/config.txt\n@@ -1,1 +1,1 @@\n-API_KEY={SECRET_MARKER}\n+safe_value=ok\n",
    )

    prompt = build_llm_prompt(
        changed_files=[changed_file],
        contexts=[],
        rule_issues=[],
        max_prompt_chars=10000,
    )

    assert SECRET_MARKER not in prompt, (  # rename 后旧内容秘密也不得外泄
        f"L3 外发层泄露: 敏感文件 {old_path} 重命名为 config.txt 后，"
        f"旧文件内容中的秘密不应出现在 LLM prompt 中"
    )
    assert _REDACTED_DIFF_PLACEHOLDER in prompt, (  # rename 场景同样需出现脱敏占位符
        f"L3 外发层: 敏感文件 {old_path} 重命名为 config.txt 后，"
        f"diff 应被替换为 redaction placeholder"
    )


# ============================================================================
# E2E: run_review_agent output + trace contain no secret marker
# E2E 端到端：run_review_agent 的 output 与 trace 文件均不含敏感标记
# =============================================================================

@pytest.mark.parametrize("output_format", ["json", "markdown"])
def test_e2e_run_review_agent_does_not_leak_secret_through_output_or_trace(
    tmp_path, output_format
):
    """E2E 端到端防护：全链路 output 与 trace 文件均不得泄露敏感标记。

    测试目的：
        验证 L1+L2+L3 三层防护在真实 ``run_review_agent`` 链路中协同生效——
        PR 同时含敏感文件（.env）与普通文件（app.py），最终用户可见的
        output 与落盘的 trace 文件都不应出现 SECRET_MARKER。

    测试场景（参数化设计理由）：
        参数化 ``output_format`` 为 json / markdown 两种输出格式，覆盖
        CLI 的两条主要输出路径。用 ``SimpleNamespace`` 构造 args，使用
        ``llm_provider="mock"`` + ``mock_fixture="normal"`` 避免真实 LLM 调用，
        保证测试稳定可复现。trace 开启以验证 trace 文件也不泄露。

    预期结果（核心不变量）：
        - output + trace_steps 拼接字符串不含 SECRET_MARKER；
        - 至少生成 1 个 trace 文件；
        - 每个 trace 文件内容都不含 SECRET_MARKER。

    设计理由：
        单元测试只能验证单层防护，E2E 验证「三层叠加后是否仍有缝隙」
        （如某层遗漏导致秘密流入 trace）。这是安全测试的最终守门。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / ".env").write_text(f"API_KEY={SECRET_MARKER}\n", encoding="utf-8")  # 仓库内的敏感文件（不应被读取）

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        f"""diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -0,0 +1,1 @@
+API_KEY={SECRET_MARKER}
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return 1
""",
        encoding="utf-8",
    )  # diff 同时含敏感文件 .env 与普通文件 app.py

    trace_dir = tmp_path / "traces"

    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format=output_format,
        output=None,
        llm=True,
        llm_provider="mock",  # 使用 mock provider，避免真实 LLM 调用，保证测试稳定
        mock_fixture="normal",
        trace=True,
        trace_dir=str(trace_dir),
        max_extra_context_files=0,
    )

    output, trace_steps = run_review_agent(args)

    combined = output + json.dumps(trace_steps, ensure_ascii=False)  # 合并 output 与 trace_steps 统一排查

    assert SECRET_MARKER not in combined, (  # 用户可见输出 + 内存 trace 都不得泄露
        f"E2E 泄露 ({output_format}): 敏感标记不应出现在 output 或 trace_steps 中"
    )

    trace_files = list(trace_dir.glob("*.json"))
    assert len(trace_files) >= 1, "应至少生成 1 个 trace 文件"  # 确认 trace 落盘
    for trace_file in trace_files:
        trace_content = trace_file.read_text(encoding="utf-8")
        assert SECRET_MARKER not in trace_content, (  # 落盘 trace 文件也不得泄露
            f"E2E 泄露 ({output_format}): 敏感标记不应出现在 trace 文件 {trace_file.name} 中"
        )
