"""``tests/test_api.py`` 对 HTTP 适配层 ``src/api.py`` 进行端到端集成测试。

测试体系中的位置
----------------
本文件是整个测试套件的“顶层入口”测试：它不再单独验证某个纯函数，
而是通过 FastAPI 的 :class:`~fastapi.testclient.TestClient` 直接调用
``POST /reviews`` 端点，覆盖从请求校验、diff 解析、规则审查、报告渲染
到 trace 落盘的完整链路。与下列单元测试形成互补：

* :mod:`tests.test_diff_parser`  —— diff 解析器单元测试
* :mod:`tests.test_reviewers`    —— 规则审查器单元测试
* :mod:`tests/test_reporter`     —— 报告渲染单元测试

测试策略
--------
1. 通过 :func:`make_client` 在 ``tmp_path`` 下创建一个临时 Git 仓库工作目录，
   以避免测试之间互相污染，也避免触碰真实文件系统。
2. 使用 FastAPI 官方提供的 :class:`TestClient`，无需启动真实 HTTP 服务即可
   发起请求并断言响应。
3. 重点覆盖三类场景：
   * **正常路径**：合法 diff 能跑完整流程并返回结构化结果（8 步 trace）。
   * **边界与安全**：空 diff、超大 diff、客户端越权字段（``repo_root`` / ``use_llm``）
     必须返回 422，敏感值绝不能回显。
   * **降级路径**：空 diff 不应产生 findings，但仍返回 200。
"""
from fastapi.testclient import TestClient

from src.api import MAX_DIFF_CHARS, create_app


def make_client(tmp_path):
    """构造一个指向临时仓库的 :class:`TestClient`，供本文件所有测试复用。

    用途
    ----
    * 在 pytest 的 ``tmp_path`` fixture 目录下创建一个 ``repo`` 子目录，并写入
      一个最小可用的 ``app.py``。这样被测的 :func:`src.api.create_app` 收到的
      ``repo_root`` 就指向一个真实存在的工作目录，规则审查器在收集文件上下文
      时不会因为找不到文件而误报。
    * 返回的 :class:`TestClient` 已绑定该仓库，调用方只需 ``client.post(...)``
      即可发起请求，无需再关心仓库初始化细节。

    设计理由
    --------
    抽出为辅助函数是因为每个测试都需要“初始化仓库 + 创建 app”这一相同的
    前置步骤，集中实现可避免重复代码，也方便后续如果仓库结构变化只改一处。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    return TestClient(create_app(repo_root=repo))


def test_post_reviews_runs_service_and_returns_structured_review(tmp_path):
    """验证 ``POST /reviews`` 的正常路径：合法 diff 能跑完整条审查流水线。

    测试目的
    --------
    确认一次成功的审查请求返回 HTTP 200，且响应体包含完整的结构化结果
    （``task_id``、``errors``、``metrics``、``findings``、``steps``），
    由此可证明：请求校验、diff 解析、规则审查、报告渲染、trace 落盘
    这 8 个步骤都被正确触发且顺序正确。

    测试场景
    --------
    构造一个最小 diff：在 ``app.py`` 中新增 ``print("debug")``。这条新增行
    同时满足“规则可识别的调试代码”特征，因此期望被规则审查器命中为
    ``category == "debug"`` 的 finding。

    预期结果
    --------
    * 状态码 200，``errors`` 为空列表。
    * ``metrics.changed_files`` 等于 1（仅 ``app.py`` 一个变更文件）。
    * ``metrics.findings`` 与 ``findings`` 列表长度一致（计数正确）。
    * ``metrics.llm_called is False``：默认不开启 LLM，仅规则审查。
    * findings 中存在一条 ``category == "debug"`` 的项，其 ``source == "rule"``
      且 ``placement == "inline"``（行内 placement，区别于 summary-only）。
    * ``steps`` 列表严格等于 8 个步骤名称，验证流水线顺序。
    """
    response = make_client(tmp_path).post(
        "/reviews",
        json={
            "diff": """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def run():
+    print(\"debug\")
     return True
"""
        },
    )

    assert response.status_code == 200  # 正常路径必须返回成功状态码
    body = response.json()
    assert body["task_id"]  # 每次审查都应分配一个唯一 task_id，用于追溯
    assert body["errors"] == []  # 无任何执行错误
    assert body["metrics"]["changed_files"] == 1  # 仅 app.py 一个文件变更
    assert body["metrics"]["findings"] == len(body["findings"])  # 计数与实际 findings 数量一致
    assert body["metrics"]["llm_called"] is False  # 默认配置不调用 LLM，仅规则审查
    assert any(finding["category"] == "debug" for finding in body["findings"])  # 规则审查命中调试代码
    debug_finding = next(
        finding for finding in body["findings"] if finding["category"] == "debug"
    )
    assert debug_finding["source"] == "rule"  # 来源是规则审查器而非 LLM
    assert debug_finding["placement"] == "inline"  # 该 finding 可作为行内评论发布
    # 严格校验 8 步流水线顺序，任一步缺失或乱序都视为回归
    assert [step["step"] for step in body["steps"]] == [
        "receive_task",
        "parse_diff",
        "collect_context",
        "run_static_checks",
        "run_llm_review",
        "validate_output",
        "render_report",
        "save_trace",
    ]


def test_post_reviews_accepts_a_valid_diff_with_no_findings(tmp_path):
    """验证合法但无问题的 diff 应返回 200 且 ``findings`` 为空（降级路径）。

    测试目的
    --------
    “能正常审查”与“一定要发现问题”是两件事。本测试确认：当 diff 本身合法、
    却没有触发任何规则时，服务应返回 200 而不是报错；同时
    ``metrics.changed_files`` 仍能正确反映变更文件数量。

    测试场景
    --------
    构造一个从空文件新增 ``hello`` 文本的 diff（``@@ -0,0 +1 @@``），
    内容本身不包含任何敏感信息或调试代码，因此规则审查器不会命中。

    预期结果
    --------
    * 状态码 200。
    * ``findings`` 为空列表。
    * ``metrics.changed_files`` 仍为 1（解析器应正确识别出 notes.txt 这一个变更文件）。
    """
    response = make_client(tmp_path).post(
        "/reviews",
        json={
            "diff": """diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -0,0 +1 @@
+hello\n
"""
        },
    )

    assert response.status_code == 200  # 合法 diff 即使无问题也应成功
    assert response.json()["findings"] == []  # 无规则命中，findings 必须为空
    assert response.json()["metrics"]["changed_files"] == 1  # 解析器仍正确识别到变更文件


def test_post_reviews_rejects_empty_or_oversized_diff_and_unsupported_controls(tmp_path):
    """验证安全边界：非法/越权输入必须被 Pydantic 拒绝并返回 422。

    测试目的
    --------
    这是本文件最重要的安全测试。它确认服务端对客户端的输入做了严格约束
    （``extra=forbid``、字段只读、长度上限），防止恶意或被误用的客户端
    通过 API 字段越权：

    * 选任意仓库路径（``repo_root``），可能读盘越权；
    * 强行开启 LLM 调用（``use_llm``），可能造成费用/数据外泄；
    * 发送超大 diff，可能造成内存耗尽型 DoS。

    测试场景（参数化思路）
    --------------------
    通过一个 ``for`` 循环遍历 5 种“应被拒绝”的请求体：

    1. ``{"diff": ""}``               —— 空字符串，不是合法 diff。
    2. ``{"diff": "not a git diff"}`` —— 非空但格式不是 git diff。
    3. ``{"diff": "x" * (MAX_DIFF_CHARS + 1)}`` —— 超过最大字符数上限一个字节。
    4. ``{"diff": ..., "repo_root": "C:/"}`` —— 客户端试图覆盖服务端仓库路径。
    5. ``{"diff": ..., "use_llm": True}`` —— 客户端试图自行开启 LLM 审查。

    预期结果
    --------
    每一种请求都必须返回 422 Unprocessable Entity。之所以是 422 而非 400，
    是因为 FastAPI/Pydantic 在请求体校验失败时默认返回 422。
    """
    client = make_client(tmp_path)

    for body in (
        {"diff": ""},  # 空 diff
        {"diff": "not a git diff"},  # 非 git diff 格式
        {"diff": "x" * (MAX_DIFF_CHARS + 1)},  # 超过最大允许字符数
        {"diff": "diff --git a/a b/a", "repo_root": "C:/"},  # 越权字段：客户端不能指定仓库路径
        {"diff": "diff --git a/a b/a", "use_llm": True},  # 越权字段：客户端不能开启 LLM
    ):
        response = client.post("/reviews", json=body)
        assert response.status_code == 422  # 所有非法/越权输入一律拒绝，不进入业务逻辑


def test_post_reviews_does_not_echo_sensitive_diff_values(tmp_path):
    """P0 安全测试：敏感值绝不能在响应中回显。

    测试目的
    --------
    防止“反射型信息泄露”：客户端在 diff 里塞入一个伪造的 secret 字符串，
    服务端在返回结果中（包括错误信息、日志字段、原始 diff 回显等）
    不应原样回显该 secret，否则可能通过响应体或日志被二次泄露。

    测试场景
    --------
    构造一个 diff：在 ``app.py`` 中新增 ``api_key = "LEAKED_SECRET_VALUE_42"``。
    该行会被规则审查器识别为硬编码 secret 并产生 finding。关键在于：

    * 整个响应文本（``response.text``）中不能再出现该 secret 字符串；
    * 同时仍要确认审查器确实识别到了该 secret（``category == "secret"``），
      否则“不回显”可能只是因为根本没解析到，那样测试就失去了意义。

    特殊逻辑解释
    ------------
    之所以用 ``response.text`` 而非 ``response.json()``，是因为我们要检查的是
    “整个响应体（含可能的错误信息/调试输出）”都不包含该字符串，JSON 字段
    检查无法覆盖非结构化的回显路径。
    """
    secret = "LEAKED_SECRET_VALUE_42"
    response = make_client(tmp_path).post(
        "/reviews",
        json={
            "diff": f'''diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def run():
+    api_key = "{secret}"
     return True
'''
        },
    )

    assert response.status_code == 200  # 含 secret 的合法 diff 仍应正常处理
    assert secret not in response.text  # P0 不变量：整个响应文本不得回显原始 secret
    # 同时确认规则审查器确实识别到了该 secret，否则上面的“未回显”毫无意义
    assert any(finding["category"] == "secret" for finding in response.json()["findings"])
