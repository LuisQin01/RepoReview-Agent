"""RepoReview Agent CLI 集成测试套件（最大规模的端到端测试文件）。

被测模块
--------
- ``src/cli.py``：CLI 入口 ``run_review_agent``，串联 diff 解析、静态检查、
  LLM 审查、上下文收集、摘要评论发布与 trace 落盘的完整 pipeline。
- ``src/review_service.py``：渲染摘要评论 ``render_summary_comment``、
  规则审查 ``review_changed_files``、LLM 调用 ``get_call_model`` 等可被
  monkeypatch 替换的协作点。
- ``src/github_provider.py``：GitHub PR 评论的 GET/PATCH/POST 生命周期实现。
- ``src/git_provider.py``：``SUMMARY_COMMENT_MARKER`` 标记与输入校验异常。

测试策略
--------
本文件覆盖四条主线：

1. **CLI 冒烟**：用最小 diff 验证 ``run_review_agent`` 能跑通解析→静态检查→
   输出 trace 的完整链路，断言关键 trace step 存在。
2. **摘要评论生命周期（M5 里程碑核心）**：通过 ``install_summary_provider``
   注入可控的 ``http_open``，模拟 GitHub REST 响应，验证未 opt-in 不构造
   provider、opt-in 后走 GET（翻页查找已有标记评论）→ POST/PATCH 的幂等
   生命周期、外部用户发的标记评论不被越权更新、403 与缺 author 在 HTTP
   调用前后被正确拦截，且 token 不泄露到 trace/stdout/stderr。
3. **LLM 重试与降级**：用 mock provider 复现 timeout 重试耗尽、OpenAI 503
   重试耗尽、timeout_then_success 恢复、unlocatable finding 降级到 summary
   等场景，验证 trace 记录的 attempts/retries/exhausted 不变量。
4. **安全脱敏（P0 回归）**：构造包含 credential 的 finding，断言
   ``[REDACTED]`` 替换发生在 HTTP body / 本地 output / trace 文件之前。

mock 设计要点
--------------
- ``SummaryHttpResponse``：模拟 ``urllib`` 的响应对象，提供 ``read``/``close``
  与 ``headers``，让被测代码无需真实网络即可消费响应体。
- ``install_summary_provider``：用 monkeypatch 替换 ``src.cli.GitHubPRProvider``
  构造入口，并注入自定义 ``http_open``，从而把网络层变成可断言的列表。
- ``install_summary_renderer``：把 ``render_summary_comment`` 替换为返回固定
  body 的 stub，使断言可以精确比对 POST/PATCH 的请求体。
- ``token="test-token-must-not-leak"``：刻意命名的标记 token，所有涉及 token
  的测试都断言该字符串不出现在 trace / stdout / stderr / HTTP body 中。

在整体测试体系中的位置
----------------------
本文件是集成层测试：单元测试（``test_review_service.py`` 等）覆盖各模块内部
逻辑，本文件覆盖 ``run_review_agent`` 把它们组装起来后的端到端行为，重点
守护跨模块协作的不变量（生命周期幂等、token 不泄露、LLM 降级可观测）。
"""

import json
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from src.cli import run_review_agent
from src.git_provider import GitProviderInputError, SUMMARY_COMMENT_MARKER
from src.github_provider import GitHubAuthorizationError, GitHubPRProvider


class SummaryHttpResponse:
    """模拟 ``urllib.request.urlopen`` 返回的 HTTP 响应对象。

    设计理由：``GitHubPRProvider`` 通过注入的 ``http_open`` 调用网络层并按
    ``urllib`` 响应协议（``read()`` 取 body、``close()`` 释放、``headers``
    读取头）消费结果。本类只实现这三者即可让被测代码无感运行，避免引入
    真实 HTTP 客户端或第三方 mock 库。``payload`` 既可以是 JSON 字符串
    （评论列表）也可以是空数组字面量 ``"[]"``，统一在 ``__init__`` 中
    encode 成 bytes 以匹配 ``read()`` 的真实返回类型。
    """

    def __init__(self, payload):
        self._body = payload.encode("utf-8")  # 与 urllib 响应一致：read() 返回 bytes
        self.headers = {}  # 占位 headers，被测代码读取时不会 KeyError

    def read(self):
        return self._body

    def close(self):
        pass  # no-op：测试中无需真正释放资源


def make_summary_publish_args(tmp_path, *, publish=False, trace=False):
    """构造摘要评论场景下 ``run_review_agent`` 所需的 ``args`` 命名空间。

    用途：为摘要评论生命周期测试组提供一份「最小可发布」的输入——一个含
    ``print('debug')`` 调试语句的 ``app.py``，以及对应的单 hunk diff。这样
    规则审查一定能产出 finding，从而保证摘要评论 body 非空、生命周期断言
    有意义。

    参数
    ----
    - ``publish``：是否开启 ``publish_summary_comment`` opt-in 开关，是摘要
      评论链路的入口条件。
    - ``trace``：是否落盘 trace 文件，token 泄露断言依赖 trace 文件存在。
    - 固定 ``pr_url`` 指向 ``acme/reviewed-repo#42``，使 GET/PATCH URL 在
      各测试中可被精确断言。

    返回 ``SimpleNamespace`` 而非 ``argparse.Namespace``，是因为测试直接
    调用 ``run_review_agent(args)`` 而不经 CLI 解析，``SimpleNamespace``
    足以模拟属性访问且更轻量。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def run():\n    print('debug')\n", encoding="utf-8"
    )
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 def run():
+    print('debug')
""",
        encoding="utf-8",
    )
    return SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=False,
        llm_provider="mock",
        trace=trace,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
        publish_summary_comment=publish,
        pr_url="https://github.com/acme/reviewed-repo/pull/42",
    )


def install_summary_provider(monkeypatch, http_open, *, token="test-token", author="reporeview-bot"):
    """用 monkeypatch 替换 ``src.cli.GitHubPRProvider`` 构造入口并注入 ``http_open``。

    设计理由：``run_review_agent`` 内部通过 ``src.cli.GitHubPRProvider`` 这个
    符号引用 provider 类，因此替换 ``src.cli`` 命名空间下的属性即可拦截构造。
    把 ``http_open`` 作为依赖注入到 ``GitHubPRProvider``，使所有 HTTP 调用都
    流经测试提供的闭包——闭包可以把 request 收集到列表里供断言，或按 method
    返回不同 ``SummaryHttpResponse``，从而把不可控的网络层变成确定性的桩。

    - ``token``：默认 ``"test-token"``；token 泄露测试会显式传入
      ``"test-token-must-not-leak"`` 作为可被字符串搜索的标记。
    - ``author``：摘要评论归属的 bot login，用于判断 GET 回来的评论是否
      属于本 agent（外部用户发的标记评论不应被 PATCH 更新）。
    """
    monkeypatch.setattr(
        "src.cli.GitHubPRProvider",
        lambda: GitHubPRProvider(
            token=token,
            summary_comment_author_login=author,
            http_open=http_open,
        ),
    )


def install_summary_renderer(monkeypatch):
    """替换 ``render_summary_comment`` 为返回固定 body 的桩，并返回该 body。

    设计理由：真实的 ``render_summary_comment`` 会把 findings 拼成 markdown
    表格，body 内容既长又随 finding 变化，不利于精确断言 POST/PATCH 请求体。
    本桩返回一个以 ``SUMMARY_COMMENT_MARKER`` 开头的固定字符串（保证被
    ``GitHubPRProvider`` 识别为「本 agent 的评论」），并在内部断言
    ``issues`` 与 ``changed_files`` 非空，从而顺带守护「空结果不应渲染」的
    约定。返回 body 让调用方可以用 ``==`` 严格比对请求体。
    """
    body = SUMMARY_COMMENT_MARKER + "\n## rendered by CLI integration test"

    def render(issues, changed_files):
        assert issues  # 守护：没有 finding 时不应进入渲染路径
        assert changed_files  # 守护：没有变更文件时不应进入渲染路径
        return body

    monkeypatch.setattr("src.review_service.render_summary_comment", render)
    return body


def test_cli_does_not_construct_summary_provider_without_opt_in(tmp_path, monkeypatch):
    """验证：未开启 ``publish_summary_comment`` opt-in 时绝不构造 provider。

    测试目的
    --------
    守护 opt-in 入口的最强不变量——只要用户没有显式 ``--publish-summary-comment``，
    ``GitHubPRProvider`` 就不应该被实例化。这能避免凭空创建网络客户端、读取
    token 等副作用，也是「默认安全」的体现。

    场景构造
    --------
    - ``make_summary_publish_args(tmp_path)`` 默认 ``publish=False``。
    - 用 monkeypatch 把 ``src.cli.GitHubPRProvider`` 替换为一个一旦被调用就
      ``pytest.fail`` 的 lambda，从而把「构造」这一行为转换成可观测的失败。

    预期结果
    --------
    - 审查正常产出 findings（说明 pipeline 主干未受影响）。
    - trace 中不出现 ``publish_summary_comment`` step（说明发布链路完全跳过）。
    """
    args = make_summary_publish_args(tmp_path)
    monkeypatch.setattr(
        "src.cli.GitHubPRProvider",
        lambda: pytest.fail("summary provider must not be constructed"),
    )

    output, trace_steps = run_review_agent(args)

    assert json.loads(output)["findings"]  # 主干审查照常产出结果
    # 不变量：未 opt-in 时 trace 中绝无 publish_summary_comment step
    assert not any(step["step"] == "publish_summary_comment" for step in trace_steps)


def test_cli_publishes_summary_comment_when_opted_in(tmp_path, monkeypatch):
    """验证：opt-in 后通过 GET（翻页查找）→ POST（创建）完成首次发布。

    测试目的
    --------
    覆盖 M5 摘要评论生命周期的「创建」分支：当 PR 上还没有本 agent 的标记
    评论时，应先 GET 评论列表确认不存在，再 POST 创建新评论，并把
    ``action=created`` 与 ``comment_id`` 记入 trace。同时守护 token 不泄露
    到落盘的 trace 文件。

    场景构造
    --------
    - ``install_summary_renderer`` 注入固定 body，便于精确比对 POST 请求体。
    - ``http_open`` 闭包按 method 分支：GET 返回空数组 ``"[]"``（模拟无既有
      评论）；POST 断言 URL 与 body 后返回 ``{"id": 73}``。
    - GET URL 断言带 ``?per_page=100&page=1`` 翻页参数，验证翻页契约。
    - 注入 ``token="test-token-must-not-leak"`` 标记 token，开启 ``trace=True``
      以落盘 trace 文件供泄露检查。

    预期结果
    --------
    - 请求序列严格为 ``["GET", "POST"]``。
    - trace 的 ``publish_summary_comment`` detail 为
      ``{"action": "created", "comment_id": 73}``。
    - 恰好生成 1 个 trace 文件，且标记 token 不出现在文件内容中。
    """
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    requests = []
    expected_body = install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            # 断言翻页参数契约：首次拉取评论必须带 per_page=100&page=1
            assert request.full_url == comments_url + "?per_page=100&page=1"
            return SummaryHttpResponse("[]")
        assert request.method == "POST"
        assert request.full_url == comments_url
        # 断言请求体严格等于 renderer 返回的 body（无额外字段污染）
        assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
        return SummaryHttpResponse(json.dumps({"id": 73}))

    token = "test-token-must-not-leak"
    install_summary_provider(monkeypatch, http_open, token=token)
    _output, trace_steps = run_review_agent(
        make_summary_publish_args(tmp_path, publish=True, trace=True)
    )

    # 不变量：创建分支的请求序列必须严格为 GET 然后 POST
    assert [request.method for request in requests] == ["GET", "POST"]
    # 不变量：trace 记录 created 动作与返回的 comment_id
    assert next(step for step in trace_steps if step["step"] == "publish_summary_comment")[
        "detail"
    ] == {"action": "created", "comment_id": 73}
    trace_files = list((tmp_path / "traces").glob("*.json"))
    assert len(trace_files) == 1  # 恰好落盘 1 个 trace 文件
    # 安全不变量：token 不得写入 trace 文件
    assert token not in trace_files[0].read_text(encoding="utf-8")


def test_cli_updates_its_existing_summary_comment_when_opted_in(tmp_path, monkeypatch):
    """验证：已存在本 agent 的标记评论时走 PATCH 更新（幂等）。

    测试目的
    --------
    覆盖生命周期的「更新」分支：当 GET 回来的评论列表中已存在 body 以
    ``SUMMARY_COMMENT_MARKER`` 开头且 ``user.login`` 等于配置的 author
    （``reporeview-bot``）的评论时，应通过 PATCH
    ``/issues/comments/{id}`` 更新该评论，而非新建。这是「重跑不重复发评论」
    幂等性的核心。

    场景构造
    --------
    - GET 返回一条 ``id=41``、body 含 ``SUMMARY_COMMENT_MARKER``、author 为
      ``reporeview-bot`` 的评论，模拟「本 agent 上次发的评论还在」。
    - PATCH 分支断言 URL 指向 ``/issues/comments/41`` 且请求体为 renderer
      返回的固定 body。
    - author 默认 ``reporeview-bot``，与 GET 返回的 user.login 一致，从而
      触发「认领为自己的评论」分支。

    预期结果
    --------
    - 请求序列为 ``["GET", "PATCH"]``（无 POST）。
    - trace detail 为 ``{"action": "updated", "comment_id": 41}``。
    """
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    update_url = "https://api.github.com/repos/acme/reviewed-repo/issues/comments/41"
    requests = []
    expected_body = install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            assert request.full_url == comments_url + "?per_page=100&page=1"
            # 模拟：已存在本 agent 上次发布的标记评论（id=41, author=reporeview-bot）
            return SummaryHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 41,
                            "body": SUMMARY_COMMENT_MARKER + "\nold",
                            "user": {"login": "reporeview-bot"},
                        }
                    ]
                )
            )
        assert request.method == "PATCH"
        assert request.full_url == update_url
        assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
        return SummaryHttpResponse(json.dumps({"id": 41}))

    install_summary_provider(monkeypatch, http_open)
    _output, trace_steps = run_review_agent(make_summary_publish_args(tmp_path, publish=True))

    # 不变量：更新分支请求序列为 GET 然后 PATCH（不创建新评论）
    assert [request.method for request in requests] == ["GET", "PATCH"]
    # 不变量：trace 记录 updated 动作与被更新的 comment_id
    assert next(step for step in trace_steps if step["step"] == "publish_summary_comment")[
        "detail"
    ] == {"action": "updated", "comment_id": 41}


def test_cli_creates_summary_instead_of_updating_external_marker(tmp_path, monkeypatch):
    """验证：外部用户发的标记评论不被 PATCH，而是新建评论（防越权）。

    测试目的
    --------
    守护「归属判断」的安全边界：即使某条评论的 body 同样以
    ``SUMMARY_COMMENT_MARKER`` 开头，但 ``user.login`` 不是配置的 author
    （此处为 ``collaborator`` 而非 ``reporeview-bot``），也不得对其进行
    PATCH——否则 agent 会越权改写他人评论。正确行为是 POST 新建一条自己的
    评论。

    场景构造
    --------
    - GET 返回一条 ``id=41``、body 含 marker、但 author 为 ``collaborator``
      的评论，模拟「别的用户/工具发了同样标记的评论」。
    - 因为 author 不匹配，provider 不应认领它，应走 POST 新建分支。

    预期结果
    --------
    - 请求序列为 ``["GET", "POST"]``。
    - 所有请求的 URL 都不包含 ``/issues/comments/41``（即绝不 PATCH 外部评论）。
    """
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    requests = []
    expected_body = install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            # 模拟：外部用户 collaborator 发了一条带 marker 的评论（非本 agent）
            return SummaryHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 41,
                            "body": SUMMARY_COMMENT_MARKER + "\nexternal",
                            "user": {"login": "collaborator"},
                        }
                    ]
                )
            )
        assert request.method == "POST"
        assert request.full_url == comments_url
        assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
        return SummaryHttpResponse(json.dumps({"id": 73}))

    install_summary_provider(monkeypatch, http_open)
    run_review_agent(make_summary_publish_args(tmp_path, publish=True))

    # 不变量：外部评论不被认领，走新建分支 GET 然后 POST
    assert [request.method for request in requests] == ["GET", "POST"]
    # 安全不变量：绝不 PATCH 外部评论的 URL（/issues/comments/41）
    assert all("/issues/comments/41" not in request.full_url for request in requests)


def test_cli_propagates_summary_permission_failure_without_token_in_trace(
    tmp_path, monkeypatch, capsys
):
    """验证：POST 收到 403 时抛 ``GitHubAuthorizationError`` 且 token 不进 trace/stdout/stderr。

    测试目的
    --------
    覆盖「权限失败」分支的安全守护：当 GitHub 返回 403 Forbidden 时，
    agent 必须把错误包装成 ``GitHubAuthorizationError``（带稳定的
    ``github_access_denied:status=403`` 错误码便于上层处理），且绝不把
    token 泄露到异常消息、stdout、stderr 或落盘的 trace 文件中。

    场景构造
    --------
    - GET 返回空数组（模拟无既有评论），驱动流程进入 POST。
    - POST 分支 ``raise HTTPError(..., 403, "Forbidden", ...)`` 模拟权限被拒。
    - 注入 ``token="test-token-must-not-leak"``，开启 trace。
    - ``capsys`` 捕获 stdout/stderr 以断言其中不含 token。

    预期结果
    --------
    - 抛出 ``GitHubAuthorizationError`` 且消息匹配 ``github_access_denied:status=403``。
    - 请求序列仍为 ``["GET", "POST"]``（确认 403 发生在 POST 阶段）。
    - token 不出现在异常消息、stdout、stderr 中。
    - 不落盘任何 trace 文件（失败路径不应持久化含敏感信息的上下文）。
    """
    token = "test-token-must-not-leak"
    requests = []
    install_summary_renderer(monkeypatch)

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return SummaryHttpResponse("[]")
        assert request.method == "POST"
        # 模拟 GitHub 返回 403 Forbidden：权限不足
        raise HTTPError(request.full_url, 403, "Forbidden", {}, BytesIO())

    install_summary_provider(monkeypatch, http_open, token=token)
    args = make_summary_publish_args(tmp_path, publish=True, trace=True)

    # 断言：错误被包装为带稳定错误码的 GitHubAuthorizationError
    with pytest.raises(GitHubAuthorizationError, match="github_access_denied:status=403") as exc_info:
        run_review_agent(args)

    captured = capsys.readouterr()
    # 不变量：403 发生在 POST 阶段，故请求序列仍是 GET 然后 POST
    assert [request.method for request in requests] == ["GET", "POST"]
    # 安全不变量：token 不出现在异常消息、stdout、stderr 中
    assert token not in str(exc_info.value)
    assert token not in captured.out
    assert token not in captured.err
    # 安全不变量：失败路径不落盘 trace 文件（避免持久化敏感上下文）
    assert not list((tmp_path / "traces").glob("*.json"))


def test_cli_rejects_missing_summary_author_before_http_call(tmp_path, monkeypatch, capsys):
    """验证：缺少 author login 时在发起任何 HTTP 调用前就拒绝。

    测试目的
    --------
    守护「输入校验前置」不变量：当 ``summary_comment_author_login`` 为空时，
    必须在发起任何 HTTP 请求之前就抛 ``GitProviderInputError``（错误码
    ``missing_summary_comment_author_login``）。提前校验能避免在无 author 的
    情况下误判评论归属、触发越权更新，也避免无谓的网络往返。

    场景构造
    --------
    - ``monkeypatch.delenv`` 清掉环境变量 ``GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN``，
      确保没有从环境兜底取到 author。
    - ``install_summary_provider(..., author=None)`` 显式置空 author。
    - ``http_open`` 设计成「一旦被调用就 ``pytest.fail``」的桩，从而把「是否
      发起 HTTP」这一行为转换成可观测的失败信号。

    预期结果
    --------
    - 抛出 ``GitProviderInputError`` 且消息严格匹配
      ``^missing_summary_comment_author_login$``。
    - ``requests`` 列表为空（确认零 HTTP 调用）。
    - token 不出现在异常消息、stdout、stderr 中。
    - 不落盘 trace 文件。
    """
    token = "test-token-must-not-leak"
    requests = []
    # 清掉环境变量，确保 author 没有从环境兜底
    monkeypatch.delenv("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN", raising=False)
    install_summary_renderer(monkeypatch)

    def http_open(*_args, **_kwargs):
        requests.append(True)
        # 一旦走到这里说明校验没有前置，直接失败
        pytest.fail("unexpected HTTP call")

    install_summary_provider(monkeypatch, http_open, token=token, author=None)
    args = make_summary_publish_args(tmp_path, publish=True, trace=True)

    # 断言：抛出带稳定错误码的输入校验异常
    with pytest.raises(
        GitProviderInputError, match="^missing_summary_comment_author_login$"
    ) as exc_info:
        run_review_agent(args)

    captured = capsys.readouterr()
    # 不变量：校验前置——零 HTTP 调用
    assert requests == []
    # 安全不变量：token 不出现在异常消息、stdout、stderr 中
    assert token not in str(exc_info.value)
    assert token not in captured.out
    assert token not in captured.err
    # 安全不变量：校验失败不落盘 trace 文件
    assert not list((tmp_path / "traces").glob("*.json"))


def test_cli_smoke_runs_simple_diff(tmp_path):
    """CLI 冒烟测试：用最小 diff 验证 ``run_review_agent`` 主干链路通畅。

    测试目的
    --------
    不依赖任何 mock，端到端跑通 diff 解析 → 静态检查 → JSON 输出 → trace
    收集的最小路径，确认 CLI 入口与核心 pipeline 装配正确。这是其它所有
    集成测试的前提：如果本测试失败，后续复杂断言都无意义。

    场景构造
    --------
    - 仓库中放一个 ``app.py``，含 ``print(user.password)`` 这种明显的安全
      规则违例，确保规则审查一定能产出 finding。
    - diff 是该文件新增一行的单 hunk，最简单可控。
    - ``llm=False`` 排除 LLM 路径，``format="json"`` 输出结构化结果。

    预期结果
    --------
    - 输出 JSON 含非空 ``findings``，且全部 ``source == "rule"``（未启用 LLM）。
    - trace 含 ``parse_diff`` 与 ``run_static_checks`` 两个关键 step。
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    app_file = repo / "app.py"
    app_file.write_text(
        """def login(user):
    print(user.password)
    return True
""",
        encoding="utf-8",
    )

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def login(user):
+    print(user.password)
     return True
""",
        encoding="utf-8",
    )

    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=False,
        llm_provider="mock",
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    output, trace_steps = run_review_agent(args)
    data = json.loads(output)

    assert "findings" in data  # 输出结构契约：必须含 findings 字段
    assert data["findings"]  # 规则违例（打印密码）应被检出
    # 不变量：未启用 LLM 时所有 finding 来源都是 rule
    assert all(finding["source"] == "rule" for finding in data["findings"])
    # 链路完整性：parse_diff 与 run_static_checks 两个 step 必须存在
    assert any(step["step"] == "parse_diff" for step in trace_steps)
    assert any(step["step"] == "run_static_checks" for step in trace_steps)


def test_cli_mock_llm_metadata_reaches_json_and_markdown(tmp_path):
    """验证：mock LLM 的 finding 元数据能正确落到 JSON 与 markdown 两种输出格式。

    测试目的
    --------
    守护 LLM finding 的字段映射在两种输出格式下都正确：JSON 输出里 ``reason``
    / ``confidence`` / ``evidence`` / ``source`` 等字段直接来自 LLM 响应；
    markdown 输出里同一 finding 要被正确渲染到表格的对应列。该测试同时锁定了
    markdown 表头的列顺序契约（10 列），防止列顺序被无意改动后破坏下游解析。

    场景构造
    --------
    - ``app.py`` 前面填 9 行 ``existing_N = N`` 把目标 hunk 推到第 10 行，
      使 ``evidence`` 为 ``app.py:10``（验证行号映射而非硬编码 1）。
    - diff 是在原文件第 9 行后新增 ``def run(): return 1`` 两行（``@@ -9,0 +10,2 @@``）。
    - ``llm=True``、``mock_fixture="normal"`` 走 mock LLM 的正常响应路径。
    - 同一份 ``args`` 先跑 ``format="json"`` 再改为 ``format="markdown"`` 跑
      一次，复用输入确保两次结果可比对。

    预期结果
    --------
    - JSON finding 的 ``reason``/``confidence``/``evidence``/``source`` 与 mock
      fixture 的预设一致。
    - markdown 表头恰好 10 列且顺序固定；LLM 那一行的对应列与 JSON 字段一致。
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    app_file = repo / "app.py"
    # 前 9 行占位，把目标 hunk 推到第 10 行，验证行号映射而非硬编码
    app_file.write_text(
        "".join(f"existing_{line_no} = {line_no}\n" for line_no in range(1, 10))
        + "def run():\n    return 1\n",
        encoding="utf-8",
    )

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -9,0 +10,2 @@
+def run():
+    return 1
""",
        encoding="utf-8",
    )

    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,
        llm_provider="mock",
        mock_fixture="normal",
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    # 第一次跑：JSON 输出，校验 LLM finding 的字段映射
    json_output, _ = run_review_agent(args)
    json_findings = json.loads(json_output)["findings"]
    llm_finding = next(finding for finding in json_findings if finding["source"] == "llm")

    # 断言：LLM finding 元数据与 mock fixture 预设一致
    assert llm_finding["reason"] == "新增代码可能执行失败，但没有看到错误处理逻辑"
    assert llm_finding["confidence"] == 0.76
    assert llm_finding["evidence"] == "app.py:10"
    assert llm_finding["source"] == "llm"

    # 第二次跑：markdown 输出，校验同一 finding 被正确渲染到表格
    args.format = "markdown"
    markdown_output, _ = run_review_agent(args)

    # 切出 Findings 区块（在 ## Findings 与 ## JSON Output 之间）
    findings_section = markdown_output.split("## Findings\n\n", 1)[1].split(
        "\n\n## JSON Output", 1
    )[0]
    table_lines = [line for line in findings_section.splitlines() if line.startswith("|")]
    separator_cells = [cell.strip() for cell in table_lines[1].strip("|").split("|")]
    data_rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in table_lines[2:]
    ]
    cells = next(row for row in data_rows if row[3] == "llm")

    # 断言：表头 10 列契约 + 对齐方式（Line/Confidence 右对齐 ---:）
    assert table_lines[0] == "| Severity | File | Line | Category | Issue | Reason | Suggestion | Confidence | Evidence | Source |"
    assert table_lines[1] == "| --- | --- | ---: | --- | --- | --- | --- | ---: | --- | --- |"
    assert len(separator_cells) == 10  # 分隔行恰好 10 列
    assert len(cells) == 10  # 数据行恰好 10 列
    # 断言：markdown 表格中 LLM 行的对应列与 JSON 字段一致
    assert cells[5] == "新增代码可能执行失败，但没有看到错误处理逻辑"
    assert cells[7] == "0.76"
    assert cells[8] == "app.py:10"
    assert cells[9] == "llm"


def make_llm_fixture_args(tmp_path, fixture):
    """构造 LLM fixture 测试所需的 ``args``，专门用于跑 mock LLM provider。

    用途：与 ``make_summary_publish_args`` 区分——本函数固定 ``llm=True``、
    ``llm_provider="mock"`` 并传入 ``mock_fixture``，用于驱动 mock LLM 的
    各类预设响应（``bad_json`` / ``empty`` / ``timeout`` / ``timeout_then_success``
    等）。输入是一个最简单的 ``def run(): return 1`` 新增 hunk，确保 LLM
    审查一定会被触发且响应内容完全由 fixture 决定，排除规则审查的干扰。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return 1
""",
        encoding="utf-8",
    )
    return SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,
        llm_provider="mock",
        mock_fixture=fixture,
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )


@pytest.mark.parametrize(
    ("fixture", "expected_valid", "expected_errors"),
    [
        ("bad_json", False, ["llm_json_parse_error"]),
        ("empty", True, []),
    ],
)
def test_cli_mock_llm_fixtures_reach_validation(
    tmp_path, fixture, expected_valid, expected_errors
):
    """参数化验证：LLM 响应进入校验层后的 ``valid`` / ``errors`` 落点正确。

    测试目的
    --------
    守护 LLM 输出校验逻辑：当 LLM 返回的 JSON 无法解析（``bad_json``）时，
    ``valid=False`` 且 ``errors`` 含 ``llm_json_parse_error``；当 LLM 返回空
    findings（``empty``）时，``valid=True`` 且无 errors。两种情况下都不应产出
    任何 LLM finding（解析失败或空结果都不算有效 finding）。

    场景构造
    --------
    - 用 ``@pytest.mark.parametrize`` 复用同一测试体跑两个 fixture。
    - ``make_llm_fixture_args`` 提供最小输入，确保行为完全由 fixture 决定。

    预期结果
    --------
    - trace 的 ``run_llm_review`` step 记录的 ``valid`` / ``errors`` 与参数
      预期一致。
    - ``findings`` 计数为 0；输出中无 ``source == "llm"`` 的 finding。
    """
    output, trace_steps = run_review_agent(make_llm_fixture_args(tmp_path, fixture))

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    # 断言：校验结果 valid/errors 与 fixture 预期一致
    assert llm_step["detail"]["valid"] is expected_valid
    assert llm_step["detail"]["errors"] == expected_errors
    # 不变量：解析失败或空结果都不产出 LLM finding
    assert llm_step["detail"]["findings"] == 0
    assert not any(finding["source"] == "llm" for finding in findings)


def test_cli_mock_llm_timeout_is_recorded_without_an_llm_finding(tmp_path):
    """验证：LLM 调用持续 timeout 时重试耗尽、记入 trace 但不产出 finding。

    测试目的
    --------
    覆盖「重试耗尽」降级路径：mock provider 始终抛 ``mock_timeout``，agent 应
    重试到上限（3 次 attempts / 2 次 retries）后放弃，把完整重试轨迹记入
    trace（``exhausted=True``、``retry_errors`` 列出每次失败），但不产出
    任何 LLM finding——因为从未拿到有效响应。

    场景构造
    --------
    - ``make_llm_fixture_args(tmp_path, "timeout")`` 让 mock provider 每次都
      抛 ``mock_timeout``，触发重试循环。

    预期结果
    --------
    - ``run_llm_review`` step 的 detail 完整匹配：``called=True``、
      ``findings=0``、``error="mock_timeout"``、``attempts=3``、
      ``retries=2``、``retry_errors=["mock_timeout"]*3``、``exhausted=True``。
    - 输出中无 LLM finding。
    """
    output, trace_steps = run_review_agent(make_llm_fixture_args(tmp_path, "timeout"))

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    # 断言：重试耗尽轨迹完整记录（3 次尝试 = 1 次初始 + 2 次重试，每次都失败）
    assert llm_step["detail"] == {
        "called": True,
        "provider": "mock",
        "findings": 0,
        "error": "mock_timeout",
        "attempts": 3,
        "retries": 2,
        "retry_errors": ["mock_timeout", "mock_timeout", "mock_timeout"],
        "exhausted": True,
    }
    # 不变量：从未拿到有效响应，绝不产出 LLM finding
    assert not any(finding["source"] == "llm" for finding in findings)


def test_cli_records_exhausted_openai_http_503_without_llm_finding(
    tmp_path, monkeypatch
):
    """验证：OpenAI 持续返回 503 时重试耗尽、降级记录但不产出 finding。

    测试目的
    --------
    覆盖真实 OpenAI provider 的「服务不可用」降级路径：当 ``openai.OpenAI``
    的 ``responses.create`` 持续抛 503 异常时，agent 应重试 3 次后放弃，
    把错误记为 ``openai_call_failed:service unavailable``，且不产出 LLM
    finding。与 mock timeout 测试对照——这里验证的是真实 SDK 异常的归类。

    场景构造（mock 设计理由）
    --------
    - 定义 ``ProviderUnavailable`` 异常带 ``status_code = 503``，模拟 OpenAI
      SDK 在服务不可用时抛出的异常类型。
    - ``FakeResponses.create`` 每次都 ``raise ProviderUnavailable``，并把
      ``kwargs`` 收集到 ``request_arguments`` 用于断言调用次数。
    - ``FakeOpenAI`` 暴露 ``.responses`` 属性，匹配真实 SDK 的接口形态。
    - ``monkeypatch.setitem(sys.modules, "openai", ...)`` 把整个 ``openai``
      模块替换为桩，避免依赖真实 SDK 安装；``setenv("OPENAI_API_KEY")``
      满足 provider 构造的前置条件。
    - ``args.llm_provider = "openai"`` 切换到真实 provider 路径。

    预期结果
    --------
    - ``request_arguments`` 长度为 3（重试 3 次）。
    - trace ``findings=0``、``error="openai_call_failed:service unavailable"``。
    - 输出中无 LLM finding。
    """
    request_arguments = []

    class ProviderUnavailable(Exception):
        status_code = 503  # 模拟 OpenAI SDK 的 503 服务不可用异常

    class FakeResponses:
        def create(self, **kwargs):
            request_arguments.append(kwargs)  # 记录每次调用参数以断言重试次数
            raise ProviderUnavailable("service unavailable")

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()  # 暴露 .responses 匹配真实 SDK 接口

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # 满足 provider 构造前置条件
    # 把 openai 模块整体替换为桩，避免依赖真实 SDK 安装
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    args = make_llm_fixture_args(tmp_path, "normal")
    args.llm_provider = "openai"  # 切换到真实 OpenAI provider 路径

    output, trace_steps = run_review_agent(args)

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    # 不变量：503 持续失败时重试 3 次（1 次初始 + 2 次重试）
    assert len(request_arguments) == 3
    assert llm_step["detail"]["findings"] == 0
    # 断言：错误被归类为 openai_call_failed 且带原始消息
    assert llm_step["detail"]["error"] == "openai_call_failed:service unavailable"
    # 不变量：服务不可用时不产出 LLM finding
    assert not any(finding["source"] == "llm" for finding in findings)


def test_cli_retries_mock_timeout_then_publishes_recovered_llm_finding(tmp_path):
    """验证：前两次 timeout、第三次成功的恢复路径——重试有效且产出 finding。

    测试目的
    --------
    覆盖「重试后恢复」正向路径：与 ``test_cli_mock_llm_timeout_is_recorded``
    的「始终失败」对照，本测试验证当第三次尝试成功时，``exhausted=False``、
    ``valid=True``、``retry_errors`` 只记录前两次失败，且最终产出 LLM finding。
    这证明重试机制确实能在间歇性故障下抢救出有效结果，而非无脑放弃。

    场景构造
    --------
    - ``mock_fixture="timeout_then_success"`` 让 mock provider 前两次抛
      ``mock_timeout``、第三次返回正常响应。
    - 输入与 ``test_cli_mock_llm_metadata_reaches_json_and_markdown`` 一致
      （9 行占位 + 第 10 行 hunk），保证 fixture 的 normal 响应能被正确映射。

    预期结果
    --------
    - ``valid=True``、``attempts=3``、``retries=2``、
      ``retry_errors=["mock_timeout", "mock_timeout"]``、``exhausted=False``。
    - 输出含 ``source == "llm"`` 的 finding（恢复成功）。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "".join(f"existing_{line_no} = {line_no}\n" for line_no in range(1, 10))
        + "def run():\n    return 1\n",
        encoding="utf-8",
    )
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -9,0 +10,2 @@
+def run():
+    return 1
""",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,
        llm_provider="mock",
        mock_fixture="timeout_then_success",
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    output, trace_steps = run_review_agent(args)

    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")
    findings = json.loads(output)["findings"]

    # 断言：第三次成功——valid=True 且 exhausted=False
    assert llm_step["detail"]["valid"] is True
    # 不变量：共 3 次尝试（1 初始 + 2 重试），前两次失败被记录
    assert llm_step["detail"]["attempts"] == 3
    assert llm_step["detail"]["retries"] == 2
    # retry_errors 只记前两次失败，第三次成功不入此列表
    assert llm_step["detail"]["retry_errors"] == ["mock_timeout", "mock_timeout"]
    assert llm_step["detail"]["exhausted"] is False
    # 断言：恢复成功后产出 LLM finding
    assert any(finding["source"] == "llm" for finding in findings)


def test_cli_downgrades_unlocatable_llm_findings_and_publishes_them_in_summary(tmp_path, monkeypatch):
    """验证：无法定位到 hunk 内的 LLM finding 被降级到 summary，而非丢弃或越界 inline。

    测试目的
    --------
    守护「定位降级」策略：LLM 可能返回行号不在 diff hunk 内、甚至文件不在
    变更范围内的 finding。Agent 不应丢弃它们，也不应越界 inline 评论，而应
    将其 ``placement`` 标记为 ``summary`` 并写入摘要评论 body。本测试同时
    验证摘要 body 中 inline / summary 两类 finding 的渲染格式正确。

    场景构造
    --------
    - diff 只新增 ``app.py`` 第 1 行 ``new_value = 1``。
    - 通过 ``monkeypatch.setattr`` 替换 ``get_call_model``，让 LLM 返回 3 条
      finding：① app.py:1（hunk 内，应 inline）；② app.py:2（文件对但行号
      越出 hunk，应 summary）；③ other.py:1（文件不在变更范围，应 summary）。
    - 注入 ``install_summary_provider`` 收集 HTTP 请求，在 POST 分支断言
      摘要 body 含三类 finding 的对应表格行。

    预期结果
    --------
    - 输出 findings 含 3 条 LLM finding，``issue`` 顺序与输入一致，``placement``
      为 ``["inline", "summary", "summary"]``。
    - 请求序列为 ``["GET", "POST"]``。
    - POST body 中 inline 行带真实行号、summary 行带 ``summary only`` 占位。
    - ``validate_output`` step 的 findings 计数等于实际输出 findings 数。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("new_value = 1\n", encoding="utf-8")
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,1 @@
+new_value = 1
""",
        encoding="utf-8",
    )
    # 构造 3 条 finding：①hunk 内 ②行号越出 hunk ③文件不在变更范围
    response = {
        "findings": [
            {
                "severity": "low",
                "file": "app.py",
                "line": 1,
                "issue": "valid",
                "reason": "inside hunk",
                "suggested_fix": "fix it",
                "confidence": 0.9,
                "evidence": "app.py:1",
            },
            {
                "severity": "low",
                "file": "app.py",
                "line": 2,
                "issue": "wrong line",
                "reason": "outside hunk",
                "suggested_fix": "fix it",
                "confidence": 0.9,
                "evidence": "app.py:2",
            },
            {
                "severity": "low",
                "file": "other.py",
                "line": 1,
                "issue": "wrong file",
                "reason": "outside scope",
                "suggested_fix": "fix it",
                "confidence": 0.9,
                "evidence": "other.py:1",
            },
        ]
    }
    # 替换 get_call_model 为返回固定 response 的桩，完全控制 LLM 输出
    monkeypatch.setattr(
        "src.review_service.get_call_model",
        lambda *_args, **_kwargs: lambda _prompt: json.dumps(response),
    )
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    requests = []

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return SummaryHttpResponse("[]")

        assert request.method == "POST"
        assert request.full_url == comments_url
        summary_body = json.loads(request.data.decode("utf-8"))["body"]
        # 断言摘要 body 渲染：inline 行带真实行号 1
        assert "| info | app.py | 1 | inline | llm | valid |" in summary_body
        # 断言：越出 hunk 的 finding 降级为 summary only（行号占位）
        assert "| info | app.py | summary only | summary | llm | wrong line |" in summary_body
        # 断言：不在变更范围的文件同样降级为 summary only
        assert "| info | other.py | summary only | summary | llm | wrong file |" in summary_body
        return SummaryHttpResponse(json.dumps({"id": 73}))

    install_summary_provider(monkeypatch, http_open)
    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=True,
        llm_provider="mock",
        trace=False,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
        publish_summary_comment=True,
        pr_url="https://github.com/acme/reviewed-repo/pull/42",
    )

    output, trace_steps = run_review_agent(args)

    findings = json.loads(output)["findings"]
    llm_findings = [finding for finding in findings if finding["source"] == "llm"]
    # 不变量：3 条 finding 顺序保留，issue 字段与输入一致
    assert [finding["issue"] for finding in llm_findings] == ["valid", "wrong line", "wrong file"]
    # 不变量：placement 严格为 inline / summary / summary（定位降级生效）
    assert [finding["placement"] for finding in llm_findings] == ["inline", "summary", "summary"]
    # 不变量：发布序列为 GET 然后 POST
    assert [request.method for request in requests] == ["GET", "POST"]
    validate_step = next(step for step in trace_steps if step["step"] == "validate_output")
    # 不变量：validate_output step 记录的 findings 数等于实际输出数
    assert validate_step["detail"]["findings"] == len(findings)


def test_cli_trace_records_context_provenance(tmp_path):
    """验证：trace 记录上下文来源 provenance，且落盘文件与内存 trace 一致。

    测试目的
    --------
    守护「上下文溯源」可观测性：``collect_context`` step 必须记录每个被选中
    上下文的 ``source``（来源类型，如 ``changed_file``）与 ``selection_reason``
    （为何选中），让调试者能还原「为什么 LLM 看到了这段上下文」。同时验证
    落盘的 trace 文件与内存中的 trace_steps 内容一致（落盘不丢字段）。

    场景构造
    --------
    - ``app.py`` 新增 ``def run(): return True``，diff 只改这一个文件。
    - ``trace=True`` 开启落盘，``llm=False`` 排除 LLM 路径聚焦上下文收集。
    - 因为只改了 ``app.py``，``selected_contexts[0]`` 必然是它，source 为
      ``changed_file``，reason 固定为「file is changed in the pull request」。

    预期结果
    --------
    - 内存 trace 的 ``collect_context`` step 含一条 selected_context，source
      与 selection_reason 符合预期。
    - 恰好落盘 1 个 trace 文件，其 ``context_files[0]`` 的 source / reason
      与内存 trace 一致。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return True
""",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=False,
        llm_provider="mock",
        trace=True,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )

    _, trace_steps = run_review_agent(args)

    # 内存 trace：collect_context step 记录被选中上下文的来源与原因
    collect_step = next(step for step in trace_steps if step["step"] == "collect_context")
    selected_context = collect_step["detail"]["selected_contexts"][0]
    # 不变量：来源为 changed_file（因为 app.py 在 diff 中被修改）
    assert selected_context["source"] == "changed_file"
    # 不变量：选中原因固定文案
    assert selected_context["selection_reason"] == "file is changed in the pull request"

    # 落盘 trace：恰好 1 个文件，且 context_files 字段与内存 trace 一致
    trace_paths = list((tmp_path / "traces").glob("*.json"))
    assert len(trace_paths) == 1
    saved_context = json.loads(trace_paths[0].read_text(encoding="utf-8"))["context_files"][0]
    # 不变量：落盘不丢字段，source / reason 与内存 trace 一致
    assert saved_context["source"] == "changed_file"
    assert saved_context["selection_reason"] == "file is changed in the pull request"


# ---------------------------------------------------------------------------
# P0 regression: secret values in finding messages must not reach the PR body
# P0 回归：finding 消息中的密钥值在进入 PR body 前必须被脱敏
# ---------------------------------------------------------------------------

# 三个刻意命名的标记串，用于在输出中搜索是否泄露。覆盖：通用密钥值、
# GitHub token 前缀格式（ghp_）、OpenAI token 前缀格式（sk-proj-）。
P0_SECRET_MARKER = "LEAKED_SECRET_VALUE_42"
P0_GITHUB_TOKEN_MARKER = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
P0_OPENAI_TOKEN_MARKER = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"


def test_cli_redacts_secret_values_in_published_summary_body(tmp_path, monkeypatch):
    """P0 安全回归：finding 中出现的 credential 必须在进入 HTTP body / 本地输出 / trace 前被脱敏。

    测试目的
    --------
    守护最高优先级（P0）安全不变量：当 finding 的 ``category`` / ``message``
    / ``suggestion`` / ``reason`` / ``evidence`` 字段里出现密钥值或 token 时，
    agent 必须用 ``[REDACTED]`` 替换后才允许写入 PR 评论 body、本地输出、
    trace 内存对象与落盘文件。任何一处泄露都会把用户的密钥发到 GitHub 或
    持久化到磁盘，属于不可接受的安全事故。

    场景构造
    --------
    - 文件路径本身就含密钥（``configs/API_KEY={secret}.py``），验证路径中的
      密钥也被脱敏（不仅限于 finding 字段）。
    - ``fake_review`` 注入一条 ReviewIssue，其每个文本字段都塞入不同的标记
      串：``category``/``reason``/``evidence`` 含 ``P0_SECRET_MARKER``，
      ``message`` 含 GitHub token 格式，``suggestion`` 含 OpenAI token 格式。
    - 通过 ``monkeypatch`` 替换 ``review_changed_files``，完全控制 finding 内容。
    - 开启 ``publish=True`` 与 ``trace=True``，使 body / output / trace 文件
      三个泄露面都存在，便于全量断言。

    预期结果
    --------
    - 三个标记串都不出现在任何 HTTP body、本地 output、trace_steps 序列化
      字符串、落盘 trace 文件中。
    - POST body 中确实出现 ``[REDACTED]`` 与 ``API_KEY=[REDACTED]``，且
      ``error`` severity 行存在——证明脱敏发生了而非 finding 被静默丢弃。
    """
    from src.schemas import ReviewIssue

    args = make_summary_publish_args(tmp_path, publish=True, trace=True)

    # 路径中嵌入密钥，验证路径维度的脱敏（不仅限于 finding 文本字段）
    secret_path = "configs/API_KEY={}.py".format(P0_SECRET_MARKER)
    source_file = Path(args.repo) / secret_path
    source_file.parent.mkdir()
    source_file.write_text("value = 1\n", encoding="utf-8")
    Path(args.diff).write_text(
        """diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -0,0 +1,1 @@
+value = 1
""".format(path=secret_path),
        encoding="utf-8",
    )

    # 注入一条 finding，每个文本字段都塞入不同的标记串以全覆盖泄露面
    def fake_review(changed_files):
        return [
            ReviewIssue(
                file_path=secret_path,
                line_no=1,
                severity="error",
                category="token={}".format(P0_SECRET_MARKER),  # 通用密钥值
                message="Hardcoded credential: {}".format(P0_GITHUB_TOKEN_MARKER),  # GitHub token 格式
                suggestion="Replace {} with an environment variable".format(
                    P0_OPENAI_TOKEN_MARKER
                ),  # OpenAI token 格式
                reason=f"token={P0_SECRET_MARKER} is exposed",
                evidence=f"app.py:1 API_KEY={P0_SECRET_MARKER}",
                source="rule",
            ),
        ]

    monkeypatch.setattr("src.review_service.review_changed_files", fake_review)

    requests = []

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return SummaryHttpResponse("[]")
        assert request.method == "POST"
        return SummaryHttpResponse(json.dumps({"id": 73}))

    install_summary_provider(monkeypatch, http_open)
    output, trace_steps = run_review_agent(args)

    # Secret must not appear in any HTTP request body (POST or PATCH).
    for request in requests:
        if request.data:
            body = request.data.decode("utf-8")
            assert P0_SECRET_MARKER not in body, (
                "P0 泄露: secret marker 出现在 HTTP body 中"
            )

    # Secret must not appear in local output.
    assert P0_SECRET_MARKER not in output, (
        "P0 泄露: secret marker 出现在本地输出中"
    )

    assert P0_GITHUB_TOKEN_MARKER not in output  # GitHub token 格式不进 output
    assert P0_OPENAI_TOKEN_MARKER not in output  # OpenAI token 格式不进 output

    # Secret must not appear in trace steps.
    assert P0_SECRET_MARKER not in json.dumps(trace_steps, ensure_ascii=False), (
        "P0 泄露: secret marker 出现在 trace_steps 中"
    )

    # Secret must not appear in saved trace files.
    trace_files = list((tmp_path / "traces").glob("*.json"))
    assert len(trace_files) == 1
    assert P0_SECRET_MARKER not in trace_files[0].read_text(encoding="utf-8"), (
        "P0 泄露: secret marker 出现在 trace 文件中"
    )

    trace_content = trace_files[0].read_text(encoding="utf-8")
    assert P0_GITHUB_TOKEN_MARKER not in trace_content  # GitHub token 不进 trace 文件
    assert P0_OPENAI_TOKEN_MARKER not in trace_content  # OpenAI token 不进 trace 文件

    # Verify redaction actually happened (finding was not silently dropped).
    # 验证脱敏确实发生了（而非把 finding 静默丢弃）：检查 POST body 含 [REDACTED]
    post_request = next(r for r in requests if r.method == "POST")
    post_body = json.loads(post_request.data.decode("utf-8"))["body"]
    assert P0_SECRET_MARKER not in post_body  # 三个标记串在 POST body 中均不存在
    assert P0_GITHUB_TOKEN_MARKER not in post_body
    assert P0_OPENAI_TOKEN_MARKER not in post_body
    assert "[REDACTED]" in post_body  # 脱敏占位符确实出现
    assert "API_KEY=[REDACTED]" in post_body  # 路径中的密钥也被脱敏
    assert "error" in post_body  # severity 行存在，证明 finding 未被丢弃


# ---------------------------------------------------------------------------
# P1 lifecycle: re-running the agent updates the same comment, not duplicate
# P1 生命周期：二次运行 agent 应更新同一条评论，而非重复创建
# ---------------------------------------------------------------------------

def test_cli_create_then_update_summary_uses_same_comment_id(tmp_path, monkeypatch):
    """P1 生命周期回归：二次运行必须 PATCH 首次创建的评论，证明「重跑不重复」。

    测试目的
    --------
    守护「重跑幂等」的端到端退出条件：第一次运行应 POST 创建评论（id=73），
    第二次运行应 GET 发现该评论已存在且归属本 agent，从而 PATCH 更新它而非
    再次 POST。这是 M5 里程碑「重跑不产生重复评论」验收点的直接回归守护——
    若该测试失败，意味着每次重跑都会在 PR 上多一条评论，严重破坏体验。

    场景构造
    --------
    - ``get_count`` 是闭包共享的可变计数器，让同一个 ``http_open`` 桩能区分
      第几次 GET：第一次返回空数组（模拟无既有评论），第二次返回 id=73 的
      本 agent 评论（模拟首次运行创建的评论已存在）。
    - POST 分支返回 ``{"id": 73}``，PATCH 分支断言 URL 指向
      ``/issues/comments/73`` 且 body 为固定值，二者共用 expected_body。
    - ``install_summary_provider`` 注入该 ``http_open``，复用 ``make_summary_publish_args``
      的输入连续跑两次 ``run_review_agent``。

    预期结果
    --------
    - 第一次 trace 的 publish step 为 ``{"action": "created", "comment_id": 73}``。
    - 第二次 trace 的 publish step 为 ``{"action": "updated", "comment_id": 73}``。
    - 整体请求序列严格为 ``["GET", "POST", "GET", "PATCH"]``——第二次不再 POST，
      证明没有重复创建。``get_count`` 恰好为 2（两次 GET）。
    """
    args = make_summary_publish_args(tmp_path, publish=True)
    expected_body = install_summary_renderer(monkeypatch)

    get_count = [0]  # 闭包共享计数器：让桩区分第几次 GET
    all_requests = []

    def http_open(request, timeout):
        all_requests.append(request)
        if request.method == "GET":
            get_count[0] += 1
            if get_count[0] == 1:
                # First run: no existing marked comment.
                # 第一次运行：PR 上还没有本 agent 的标记评论
                return SummaryHttpResponse("[]")
            # Second run: the comment created by the first run now exists.
            # 第二次运行：首次创建的评论（id=73, author=reporeview-bot）已存在
            return SummaryHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 73,
                            "body": SUMMARY_COMMENT_MARKER + "\n## RepoReview summary",
                            "user": {"login": "reporeview-bot"},
                        }
                    ]
                )
            )
        if request.method == "POST":
            assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
            return SummaryHttpResponse(json.dumps({"id": 73}))
        if request.method == "PATCH":
            # 断言：PATCH 指向首次创建的 comment_id=73
            assert request.full_url == (
                "https://api.github.com/repos/acme/reviewed-repo/issues/comments/73"
            )
            assert json.loads(request.data.decode("utf-8")) == {"body": expected_body}
            return SummaryHttpResponse(json.dumps({"id": 73}))
        raise AssertionError("unexpected method: {}".format(request.method))

    install_summary_provider(monkeypatch, http_open)

    # First run: creates comment with id 73.
    # 第一次运行：创建评论，trace 记录 created
    _output1, trace_steps1 = run_review_agent(args)
    publish1 = next(s for s in trace_steps1 if s["step"] == "publish_summary_comment")
    assert publish1["detail"] == {"action": "created", "comment_id": 73}

    # Second run: updates the same comment (PATCH, not POST).
    # 第二次运行：更新同一评论，trace 记录 updated（而非再次 created）
    _output2, trace_steps2 = run_review_agent(args)
    publish2 = next(s for s in trace_steps2 if s["step"] == "publish_summary_comment")
    assert publish2["detail"] == {"action": "updated", "comment_id": 73}

    # Sequence: GET (1st run), POST (1st run), GET (2nd run), PATCH (2nd run).
    # No duplicate POST — the second run updates the same comment.
    # 不变量：请求序列严格为 GET→POST→GET→PATCH，第二次不 POST（无重复创建）
    assert [r.method for r in all_requests] == ["GET", "POST", "GET", "PATCH"]
    # 不变量：恰好 2 次 GET（两次运行各拉取一次评论列表）
    assert get_count[0] == 2
