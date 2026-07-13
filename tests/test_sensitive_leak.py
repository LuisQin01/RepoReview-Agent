"""Three-layer tests proving declared sensitive categories never enter the model payload.

L1 retrieval  — read_file_context returns empty for every declared category
L2 collection — collect_file_contexts never adds sensitive files as extra candidates
L3 outbound   — build_llm_prompt redacts every sensitive category's diff
E2E           — run_review_agent output + trace contain no secret marker
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

SECRET_MARKER = "LEAKED_SECRET_VALUE_42"

# ---------------------------------------------------------------------------
# Parameter sources — auto-enumerated from implementation constants
# ---------------------------------------------------------------------------

# Auto-enumerated from SENSITIVE_FILE_NAMES / SENSITIVE_FILE_SUFFIXES.
# New declarations are automatically covered; no hardcoded copy of the sets.
_L1_CONSTANT_NAME_CASES = sorted(SENSITIVE_FILE_NAMES)
_L1_CONSTANT_SUFFIX_CASES = [
    f"certificates/service{suffix}" for suffix in sorted(SENSITIVE_FILE_SUFFIXES)
]

# Prefix variants — exercise filename.startswith(".env."), a separate code
# path not represented by SENSITIVE_FILE_NAMES entries directly.
_L1_PREFIX_VARIANT_CASES = [".env.local", ".env.production"]

# Canary cases for reverse validation.  These overlap with constant-derived
# cases when the constants are intact (deduplicated away).  If any item is
# removed from the constant, the canary survives and the L1 test FAILS
# because read_file_context no longer rejects the file — proving the test
# guards the declared set.
_L1_CANARY_CASES = [
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "certificates/service.der",
    "certificates/service.p12",
]

_L1_ALL_CASES = list(dict.fromkeys(
    _L1_CONSTANT_NAME_CASES
    + _L1_CONSTANT_SUFFIX_CASES
    + _L1_PREFIX_VARIANT_CASES
    + _L1_CANARY_CASES
))

_L3_ALL_CASES = sorted(SENSITIVE_FILE_NAMES) + [
    f"certificates/service{suffix}" for suffix in sorted(SENSITIVE_FILE_SUFFIXES)
]


# ============================================================================
# L1: read_file_context rejects every declared sensitive category
# ============================================================================

@pytest.mark.parametrize("file_path", _L1_ALL_CASES)
def test_l1_read_file_context_rejects_all_declared_sensitive_categories(
    tmp_path, file_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"API_KEY={SECRET_MARKER}\n", encoding="utf-8")

    context = read_file_context(repo, file_path, max_chars=4000)

    assert context.exists is False, (
        f"L1 检索层泄露: 敏感文件 {file_path} 不应被读取 (exists 应为 False)"
    )
    assert context.content == "", (
        f"L1 检索层泄露: 敏感文件 {file_path} 的 content 应为空"
    )
    assert context.chars_read == 0, (
        f"L1 检索层泄露: 敏感文件 {file_path} 的 chars_read 应为 0"
    )
    assert "sensitive" in context.error.lower(), (
        f"L1 检索层: 敏感文件 {file_path} 的 error 应包含 'sensitive'，得到 {context.error!r}"
    )


# ============================================================================
# L2: collect_file_contexts never collects sensitive files as extra candidates
# =============================================================================

def test_l2_import_candidates_naturally_avoid_sensitive_paths(tmp_path):
    """Import candidates only produce .py / __init__.py paths, so sensitive
    files (which have non-.py names/suffixes) can never become import
    candidates.  This test documents and guards that conclusion.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "import id_rsa\nid_rsa.do_work()\n",
        encoding="utf-8",
    )
    # id_rsa.py exists — would be a valid import candidate
    (repo / "id_rsa.py").write_text(
        "def do_work():\n    return True\n",
        encoding="utf-8",
    )
    # id_rsa (no extension) is the sensitive file — must NOT be collected
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
    all_content = "".join(ctx.content for ctx in contexts)

    assert "id_rsa.py" in collected_paths, (
        "非敏感文件 id_rsa.py 应作为 import candidate 被收集"
    )
    assert "id_rsa" not in collected_paths, (
        "L2 收集层泄露: 敏感文件 id_rsa (无扩展名) 不应被收集为上下文"
    )
    assert SECRET_MARKER not in all_content, (
        "L2 收集层泄露: 敏感标记不应出现在任何上下文内容中"
    )


def test_l2_call_name_candidates_never_collect_sensitive_files(tmp_path):
    """Call-name candidates only come from _iter_python_files (yields *.py),
    so sensitive files can never become call-name candidates.  This test
    places every declared sensitive name/suffix in the repo and verifies
    none are collected.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("do_work()\n", encoding="utf-8")
    (repo / "helper.py").write_text(
        "def do_work():\n    return True\n",
        encoding="utf-8",
    )

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

    assert "helper.py" in collected_paths, (
        "非敏感文件 helper.py 应作为 call_name candidate 被收集"
    )

    for path in collected_paths:
        p = Path(path)
        is_sensitive = (
            p.name in SENSITIVE_FILE_NAMES
            or p.suffix in SENSITIVE_FILE_SUFFIXES
            or p.name.startswith(".env.")
        )
        assert not is_sensitive, (
            f"L2 收集层泄露: 敏感文件 {path} 不应被收集为 call_name candidate"
        )

    assert SECRET_MARKER not in all_content, (
        "L2 收集层泄露: 敏感标记不应出现在任何上下文内容中"
    )


# ============================================================================
# L3: build_llm_prompt redacts every sensitive category's diff
# =============================================================================

@pytest.mark.parametrize("file_path", _L3_ALL_CASES)
def test_l3_build_llm_prompt_redacts_secret_diff_for_each_sensitive_category(
    file_path,
):
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

    assert SECRET_MARKER not in prompt, (
        f"L3 外发层泄露: 敏感文件 {file_path} 的 diff 中的秘密不应出现在 LLM prompt 中"
    )
    assert _REDACTED_DIFF_PLACEHOLDER in prompt, (
        f"L3 外发层: 敏感文件 {file_path} 的 diff 应被替换为 redaction placeholder"
    )


@pytest.mark.parametrize("old_path", _L3_ALL_CASES)
def test_l3_build_llm_prompt_redacts_secret_diff_when_sensitive_file_renamed(
    old_path,
):
    """A sensitive file renamed to a non-sensitive name still carries secret
    content in deleted_lines/patch.  The sanitizer must check old_path too.
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

    assert SECRET_MARKER not in prompt, (
        f"L3 外发层泄露: 敏感文件 {old_path} 重命名为 config.txt 后，"
        f"旧文件内容中的秘密不应出现在 LLM prompt 中"
    )
    assert _REDACTED_DIFF_PLACEHOLDER in prompt, (
        f"L3 外发层: 敏感文件 {old_path} 重命名为 config.txt 后，"
        f"diff 应被替换为 redaction placeholder"
    )


# ============================================================================
# E2E: run_review_agent output + trace contain no secret marker
# =============================================================================

@pytest.mark.parametrize("output_format", ["json", "markdown"])
def test_e2e_run_review_agent_does_not_leak_secret_through_output_or_trace(
    tmp_path, output_format
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / ".env").write_text(f"API_KEY={SECRET_MARKER}\n", encoding="utf-8")

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
    )

    trace_dir = tmp_path / "traces"

    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format=output_format,
        output=None,
        llm=True,
        llm_provider="mock",
        mock_fixture="normal",
        trace=True,
        trace_dir=str(trace_dir),
        max_extra_context_files=0,
    )

    output, trace_steps = run_review_agent(args)

    combined = output + json.dumps(trace_steps, ensure_ascii=False)

    assert SECRET_MARKER not in combined, (
        f"E2E 泄露 ({output_format}): 敏感标记不应出现在 output 或 trace_steps 中"
    )

    trace_files = list(trace_dir.glob("*.json"))
    assert len(trace_files) >= 1, "应至少生成 1 个 trace 文件"
    for trace_file in trace_files:
        trace_content = trace_file.read_text(encoding="utf-8")
        assert SECRET_MARKER not in trace_content, (
            f"E2E 泄露 ({output_format}): 敏感标记不应出现在 trace 文件 {trace_file.name} 中"
        )
