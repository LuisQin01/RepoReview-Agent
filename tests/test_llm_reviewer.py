"""``src/llm_reviewer.py`` 的单元测试集合。

本文件聚焦于 LLM 审查模块的两类核心职责：

1. **Prompt 构建**：``build_llm_prompt`` 将变更文件、上下文与规则发现序列化为
   发往模型的 prompt；
2. **敏感信息脱敏**：对 ``.env`` 等敏感文件的 diff 必须在进入 prompt 前被
   ``REDACTED`` 替换，确保 secret 不会泄露到 LLM 调用链或 trace 文件中。

测试策略
--------
- 用 ``_sensitive_changed_file`` / ``_normal_changed_file`` 两个辅助函数构造
  标准化的 ``ChangedFile``，分别代表敏感与正常变更，便于复用；
- 通过注入 ``fake_call_model`` 捕获实际发给模型的 prompt，断言 secret 不在其中；
- ``test_trace_does_not_contain_secret_when_llm_enabled`` 是一个全链路验证：
  调用 ``run_review_agent`` 走完从 diff 解析、上下文读取、LLM 审查到 trace
  落盘的完整流程，确保任意环节都不泄露 secret。

在整体测试体系中的位置
----------------------
本文件位于 LLM 调用链的中游：上游是 ``test_validation.py``（prompt/parse 校验），
下游是 ``test_llm_client.py``（重试与超时）。本文件确保进入下游的 prompt
本身是脱敏的、合规的。
"""
import json
from types import SimpleNamespace

from src.cli import run_review_agent
from src.llm_reviewer import build_llm_prompt, review_with_llm
from src.schemas import ChangedFile, DiffLine


def _sensitive_changed_file():
    """构造一个包含敏感信息的 ``ChangedFile`` 测试夹具。

    模拟 ``.env`` 文件新增一行 ``OPENAI_API_KEY=review-secret``，用于验证
    ``build_llm_prompt`` 与 ``review_with_llm`` 是否正确脱敏。其中
    ``review-secret`` 是一个易识别的占位 secret，便于在断言中搜索是否泄露。
    """
    return ChangedFile(
        path=".env",
        added_lines=[DiffLine(".env", 1, "+OPENAI_API_KEY=review-secret")],
        deleted_lines=[],
        patch="@@ -0,0 +1,1 @@\n+OPENAI_API_KEY=review-secret\n",
    )


def _normal_changed_file():
    """构造一个普通源码变更的 ``ChangedFile`` 测试夹具。

    模拟 ``app.py`` 新增 ``def run():`` 一行，用于验证正常 diff 不被脱敏、
    原文进入 prompt。作为 ``_sensitive_changed_file`` 的对照样本。
    """
    return ChangedFile(
        path="app.py",
        added_lines=[DiffLine("app.py", 1, "+def run():")],
        deleted_lines=[],
        patch="@@ -0,0 +1,1 @@\n+def run():\n",
    )


def test_build_llm_prompt_redacts_sensitive_diff():
    """验证 ``build_llm_prompt`` 对敏感文件 diff 进行脱敏。

    测试目的
    --------
    当变更文件为 ``.env`` 等敏感文件时，prompt 中不得出现 secret 明文，应被
    ``REDACTED`` 替换；同时文件路径 ``.env`` 仍应保留，以便模型知道存在敏感
    文件变更。

    测试场景
    --------
    仅传入 ``_sensitive_changed_file()``，不附带上下文与规则发现。

    预期输出
    --------
    - prompt 不含 ``review-secret``；
    - prompt 含 ``REDACTED``；
    - prompt 含 ``.env``（路径保留）。
    """
    prompt = build_llm_prompt(
        changed_files=[_sensitive_changed_file()],
        contexts=[],
        rule_issues=[],
    )

    assert "review-secret" not in prompt  # secret 不得出现在 prompt
    assert "REDACTED" in prompt  # 脱敏标记应出现
    assert ".env" in prompt  # 文件路径保留


def test_build_llm_prompt_keeps_normal_diff():
    """验证 ``build_llm_prompt`` 保留正常 diff 原文。

    测试目的
    --------
    对非敏感的普通源码变更，prompt 应原样保留 diff 内容，不应误脱敏，也不应
    出现 ``REDACTED`` 标记。

    测试场景
    --------
    仅传入 ``_normal_changed_file()``。

    预期输出
    --------
    - prompt 含 ``def run():`` 原文；
    - prompt 不含 ``review-secret``（本就不该出现）；
    - prompt 不含 ``REDACTED``（未触发脱敏）。
    """
    prompt = build_llm_prompt(
        changed_files=[_normal_changed_file()],
        contexts=[],
        rule_issues=[],
    )

    assert "def run():" in prompt  # 正常 diff 原文保留
    assert "review-secret" not in prompt  # 无 secret
    assert "REDACTED" not in prompt  # 未触发脱敏


def test_review_with_llm_does_not_leak_secret_to_call_model():
    """验证 ``review_with_llm`` 不向 ``call_model`` 泄露 secret。

    测试目的
    --------
    ``review_with_llm`` 内部会调用 ``build_llm_prompt`` 构造 prompt 并传给
    ``call_model``。需确保传入 ``call_model`` 的 prompt 已脱敏，secret 不会
    经由模型调用链外泄。

    测试场景
    --------
    - 同时传入敏感文件与正常文件，模拟混合场景；
    - 注入 ``fake_call_model`` 捕获实际收到的 prompt。

    预期输出
    --------
    - 捕获的 prompt 不含 ``review-secret``；
    - prompt 含 ``.env``、``app.py`` 路径与 ``REDACTED`` 标记。
    """
    captured = {}

    def fake_call_model(prompt):
        captured["prompt"] = prompt  # 捕获发给模型的 prompt
        return '{"findings": []}'

    review_with_llm(
        changed_files=[_sensitive_changed_file(), _normal_changed_file()],
        contexts=[],
        rule_issues=[],
        call_model=fake_call_model,
    )

    assert "review-secret" not in captured["prompt"]  # 模型收到的 prompt 不含 secret
    assert ".env" in captured["prompt"]  # 敏感文件路径保留
    assert "app.py" in captured["prompt"]  # 正常文件路径保留
    assert "REDACTED" in captured["prompt"]  # 敏感内容被脱敏标记替换


def test_trace_does_not_contain_secret_when_llm_enabled(tmp_path):
    """验证开启 LLM 与 trace 时，secret 不会出现在任何 trace 环节。

    测试目的
    --------
    这是一次全链路验证：从 diff 解析、上下文读取、prompt 构建、LLM 调用到
    trace 落盘，确保 ``review-secret`` 不会泄露到：
    - 内存中的 ``trace_steps`` 整体序列化结果；
    - ``run_llm_review`` 这一步的 detail；
    - 落盘的 trace JSON 文件。

    测试场景
    --------
    - 在 ``tmp_path`` 下构造仓库与 ``.env`` 文件，secret 写入文件；
    - 构造 ``input.diff`` 包含对 ``.env`` 的变更；
    - 通过 ``SimpleNamespace`` 组装 CLI 参数，启用 ``llm=True``、``trace=True``，
      provider 选 ``mock`` 避免真实调用。

    预期输出
    --------
    - 整体 trace_steps 序列化后不含 secret；
    - ``run_llm_review`` 步骤的 detail 不含 secret；
    - 落盘的 trace 文件不含 secret。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # 将 secret 写入仓库 .env，模拟真实仓库中存在敏感文件
    (repo / ".env").write_text("OPENAI_API_KEY=review-secret\n", encoding="utf-8")

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -0,0 +1,1 @@
+OPENAI_API_KEY=review-secret
""",
        encoding="utf-8",
    )

    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,  # 启用 LLM 审查，触发 prompt 构建与脱敏路径
        llm_provider="mock",  # 使用 mock provider 避免真实网络调用
        mock_fixture="normal",
        trace=True,  # 启用 trace 收集
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    _, trace_steps = run_review_agent(args)

    # 整体 trace 序列化后不得含 secret
    blob = json.dumps(trace_steps, ensure_ascii=False)
    assert "review-secret" not in blob

    # 单独检查 LLM 审查步骤的 detail
    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    assert "review-secret" not in json.dumps(llm_step["detail"], ensure_ascii=False)

    # 落盘的 trace 文件同样不得含 secret
    saved = list((tmp_path / "traces").glob("*.json"))[0].read_text(encoding="utf-8")
    assert "review-secret" not in saved
