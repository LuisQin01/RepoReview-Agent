"""``src/github_provider.py`` 的单元测试集合。

本文件聚焦于 ``GitHubPRProvider``——RepoReview Agent 中负责与 GitHub REST API
交互的适配器层。其核心职责包括：

1. 从 PR URL 或 ``owner/repo/number`` 三元组抓取 PR 元数据、变更文件、patch 与
   提交历史；
2. 将 GitHub 返回的各类 HTTP 错误（限流、鉴权失败、未找到、5xx）映射为领域内
   的异常类型，以便上层按语义处理；
3. 在 PR 上发布/更新摘要评论（带 ``SUMMARY_COMMENT_MARKER`` 标记，区分新建与
   更新，并防止越权改写他人评论）。

测试策略
--------
- **不发起真实网络请求**。所有 HTTP 调用通过注入的 ``http_open`` 可调用对象
  拦截，配合 ``FakeHttpResponse`` 模拟 ``urllib`` 的响应对象；
- 使用 ``github_http_fixture`` 提供一组 PR/files/commits 的 JSON fixture，
  覆盖正常路径与 ``patch`` 缺失等边界；
- 对各类 HTTP 错误，直接构造 ``urllib.error.HTTPError`` 触发分支，验证异常
  类型与 ``retry_after_seconds`` 等关键字段；
- 对评论发布逻辑，断言 HTTP 方法序列、URL 与请求体，确保不越权更新外部评论。

在整体测试体系中的位置
----------------------
本文件属于 ``tests/`` 目录下的 provider 层测试，与 ``test_llm_client.py``、
``test_llm_reviewer.py``、``test_validation.py`` 共同构成对核心模块的端到端
单元覆盖，确保数据获取层在与外部 GitHub API 解耦的前提下行为可预期。
"""
import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from src.git_provider import (
    GitProviderInputError,
    SUMMARY_COMMENT_MARKER,
    parse_pull_request_ref,
)
from src.reporter import render_summary_comment
from src.github_provider import (
    GitHubAPIError,
    GitHubAuthorizationError,
    GitHubNotFoundError,
    GitHubPRProvider,
    GitHubRateLimitError,
)


class FakeHttpResponse:
    """模拟 ``urllib`` HTTP 响应对象的轻量替身。

    将传入的字符串 payload 编码为字节流，提供与真实响应一致的 ``read``/``close``
    接口以及 ``headers`` 字典，便于在不触网的前提下让被测代码按真实流程读取
    响应体与响应头。
    """

    def __init__(self, payload, headers=None):
        self._body = payload.encode("utf-8")  # 真实响应以字节形式返回，这里同步编码
        self.headers = headers or {}  # 默认空响应头，便于未传入时安全访问
        self.closed = False

    def read(self):
        return self._body  # 一次性返回完整响应体，匹配 urllib 的 read 行为

    def close(self):
        self.closed = True  # 记录关闭状态，可用于校验资源是否被正确释放


@pytest.fixture
def github_http_fixture():
    """提供 PR/files/commits 三类 JSON fixture 的 HTTP 拦截器。

    构造一个 ``open_fixture`` 函数，按请求 URL 与 ``Accept`` 头分发预置的
    响应：
    - PR 元数据（base/head sha、fork 仓库信息）；
    - 变更文件列表（含一个带 ``patch`` 的源码文件与一个无 ``patch`` 的二进制
      资源文件，用于覆盖 patch 缺失分支）；
    - 提交历史（含 author login）；
    - 当请求 ``application/vnd.github.v3.diff`` 时返回原始 diff 文本。

    同时通过 ``calls`` 列表记录每次调用的 URL、请求头与超时值，供测试断言
    请求次数、鉴权头与超时透传。
    """
    base = "https://api.github.com/repos/acme/reviewed-repo/pulls/42"
    responses = {
        base: json.dumps(
            {
                "title": "Fix parser",
                "body": "Keeps quoted paths intact.",
                "base": {"sha": "base-sha", "repo": {"full_name": "acme/reviewed-repo"}},
                "head": {"sha": "head-sha", "repo": {"full_name": "contributor/fork"}},
            }
        ),
        base + "/files?per_page=100&page=1": json.dumps(
            [
                {
                    "filename": "src/parser.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                },
                {
                    "filename": "assets/logo.png",
                    "status": "modified",
                },
            ]
        ),
        base + "/commits?per_page=100&page=1": json.dumps(
            [
                {
                    "sha": "commit-sha",
                    "commit": {"message": "Fix quoted diff paths"},
                    "author": {"login": "contributor"},
                }
            ]
        ),
    }
    calls = []

    def open_fixture(request, timeout):
        # 记录每次请求的 URL、请求头与超时值，便于在测试中验证调用序列
        calls.append((request.full_url, request.headers, timeout))
        if request.headers["Accept"] == "application/vnd.github.v3.diff":
            # 请求 diff 媒体类型时，URL 必须指向 PR 本体
            assert request.full_url == base
            return FakeHttpResponse(
                "diff --git a/src/parser.py b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n"
            )
        # 其余请求按 URL 直接查表返回预置 JSON
        return FakeHttpResponse(responses[request.full_url])

    return open_fixture, calls


def test_fetch_pull_request_from_url_reads_fork_metadata_files_patch_and_commits(
    github_http_fixture,
):
    """验证从 PR URL 抓取数据时的端到端正常路径。

    测试目的
    --------
    验证 ``GitHubPRProvider.fetch_pull_request`` 在仅提供 ``pr_url`` 时，能正确
    解析出 ``owner/repo/number`` 引用，并依次发起 PR 元数据、文件列表、原始
    diff、提交历史四类请求，组装出完整的 ``PullRequest`` 对象。

    测试场景
    --------
    - 注入 ``github_http_fixture`` 提供的拦截器，并显式传入 ``timeout_seconds=7.5``
      与 ``token="test-token"``，用于验证参数透传；
    - fixture 中包含一个无 ``patch`` 的二进制文件 ``assets/logo.png``，应触发
      ``patch_unavailable`` 告警。

    预期输出
    --------
    - 引用三元组、PR 元数据（标题、描述、base/head sha、fork 标记、head 仓库名）
      与 fixture 一致；
    - 变更文件列表保留顺序，且二进制文件的 ``patch`` 为 ``None``；
    - ``patch`` 以 ``diff --git`` 开头；提交历史含 sha/message/author_login；
    - ``warnings`` 含 ``patch_unavailable:assets/logo.png``；
    - 恰好发起 4 次 HTTP 调用，且每次都带上 ``Bearer test-token`` 与 ``7.5`` 超时。
    """
    http_open, calls = github_http_fixture
    provider = GitHubPRProvider(token="test-token", http_open=http_open, timeout_seconds=7.5)

    pull_request = provider.fetch_pull_request(
        pr_url="https://github.com/acme/reviewed-repo/pull/42"
    )

    assert pull_request.reference.owner == "acme"  # URL 解析得到的 owner
    assert pull_request.reference.repo == "reviewed-repo"  # URL 解析得到的 repo
    assert pull_request.reference.number == 42  # URL 解析得到的 PR 编号
    assert pull_request.metadata.title == "Fix parser"  # PR 元数据透传
    assert pull_request.metadata.description == "Keeps quoted paths intact."
    assert pull_request.metadata.base_sha == "base-sha"  # 目标分支 sha
    assert pull_request.metadata.head_sha == "head-sha"  # 源分支 sha
    assert pull_request.metadata.is_fork is True  # head 仓库与 base 不同，识别为 fork
    assert pull_request.metadata.head_repository == "contributor/fork"
    # 变更文件顺序、状态与 patch 完整保留；二进制文件 patch 为 None
    assert [(item.path, item.status, item.patch) for item in pull_request.changed_files] == [
        ("src/parser.py", "modified", "@@ -1 +1 @@\n-old\n+new"),
        ("assets/logo.png", "modified", None),
    ]
    assert pull_request.patch.startswith("diff --git")  # 原始 diff 文本前缀不变
    # 提交历史三元组（sha、message、author_login）保留
    assert [(commit.sha, commit.message, commit.author_login) for commit in pull_request.commits] == [
        ("commit-sha", "Fix quoted diff paths", "contributor")
    ]
    assert pull_request.warnings == ["patch_unavailable:assets/logo.png"]  # 缺 patch 告警
    assert len(calls) == 4  # 共发起 4 次 HTTP 调用：PR/files/diff/commits
    assert all(timeout == 7.5 for _url, _headers, timeout in calls)  # 超时参数全程透传
    # 鉴权头每次请求都正确携带
    assert all(headers["Authorization"] == "Bearer test-token" for _url, headers, _timeout in calls)


def test_fetch_pull_request_accepts_owner_repo_and_number(github_http_fixture):
    """验证 ``owner/repo/number`` 三元组入参的解析路径。

    测试目的
    --------
    确保除 PR URL 外，``fetch_pull_request`` 也接受显式的 ``owner``、``repo``、
    ``number`` 参数，并构造出与 ``parse_pull_request_ref`` 一致的引用对象。

    测试场景
    --------
    使用 ``github_http_fixture`` 提供的拦截器，仅传入三元组（不传 ``pr_url``）。

    预期输出
    --------
    返回的 ``pull_request.reference`` 与 ``parse_pull_request_ref`` 直接构造的
    引用完全相等，说明三元组路径与 URL 路径产出一致。
    """
    http_open, _calls = github_http_fixture
    provider = GitHubPRProvider(http_open=http_open)

    pull_request = provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)

    assert pull_request.reference == parse_pull_request_ref(
        owner="acme", repo="reviewed-repo", number=42
    )


def test_changed_files_at_github_limit_are_marked_as_possibly_truncated():
    """验证达到 GitHub 3000 文件上限时给出截断告警。

    测试目的
    --------
    GitHub ``/pulls/{n}/files`` 接口最多返回 3000 个文件。当返回数量恰好达到
    该上限时，``GitHubPRProvider`` 应在 ``warnings`` 中标记
    ``changed_files_may_be_truncated_at_github_limit``，提醒上层结果可能不完整。

    测试场景
    --------
    自定义 ``http_open``：按页（每页 100）生成共 3000 个文件响应，PR 元数据与
    提交历史返回最小有效内容。

    预期输出
    --------
    - ``changed_files`` 长度恰为 3000；
    - ``warnings`` 仅含上限截断告警。
    """
    base = "https://api.github.com/repos/acme/reviewed-repo/pulls/42"

    def http_open(request, timeout):
        if request.headers["Accept"] == "application/vnd.github.v3.diff":
            return FakeHttpResponse("diff --git a/app.py b/app.py\n")
        if request.full_url == base:
            return FakeHttpResponse(
                json.dumps(
                    {
                        "title": "Large change",
                        "body": "",
                        "base": {"sha": "base-sha"},
                        "head": {"sha": "head-sha", "repo": {"full_name": "acme/reviewed-repo"}},
                    }
                )
            )
        if request.full_url.startswith(base + "/files?"):
            # 按分页参数动态生成 100 个文件，直到累计 3000 个为止
            page = int(request.full_url.rsplit("page=", 1)[1])
            start = (page - 1) * 100
            return FakeHttpResponse(
                json.dumps(
                    [
                        {
                            "filename": "src/file_{}.py".format(index),
                            "status": "modified",
                            "patch": "@@ -1 +1 @@\n+value = {}".format(index),
                        }
                        for index in range(start, min(start + 100, 3000))
                    ]
                )
            )
        if request.full_url == base + "/commits?per_page=100&page=1":
            return FakeHttpResponse("[]")
        raise AssertionError("unexpected URL: {}".format(request.full_url))

    pull_request = GitHubPRProvider(http_open=http_open).fetch_pull_request(
        owner="acme", repo="reviewed-repo", number=42
    )

    assert len(pull_request.changed_files) == 3000  # 恰好达到 GitHub 上限
    assert pull_request.warnings == [
        "changed_files_may_be_truncated_at_github_limit"
    ]


def test_github_rate_limit_is_a_distinct_error():
    """验证 403 主限流（``X-RateLimit-Reset``）映射为 ``GitHubRateLimitError``。

    测试目的
    --------
    当返回 403 且 ``X-RateLimit-Remaining=0`` 时，应识别为限流而非鉴权失败，
    并根据 ``X-RateLimit-Reset`` 与当前时间差计算 ``retry_after_seconds``。

    测试场景
    --------
    通过注入 ``now=lambda: 1000`` 固定当前时间戳，``X-RateLimit-Reset=1060``，
    因此预期退避 60 秒。

    预期输出
    --------
    抛出 ``GitHubRateLimitError``，错误信息中包含 ``retry_after_seconds=60``。
    """
    def rate_limited(_request, timeout):
        raise HTTPError(
            "https://api.github.com/repos/acme/reviewed-repo/pulls/42",
            403,
            "Forbidden",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"},
            BytesIO(),
        )

    provider = GitHubPRProvider(http_open=rate_limited, now=lambda: 1000)  # 固定当前时间便于计算退避

    with pytest.raises(GitHubRateLimitError, match="retry_after_seconds=60"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_http_429_rate_limit_prefers_retry_after():
    """验证 429 限流优先采用 ``Retry-After`` 头。

    测试目的
    --------
    当返回 429 且带 ``Retry-After`` 头时，应直接采用该值作为 ``retry_after_seconds``。

    测试场景
    --------
    构造 429 响应，``Retry-After=30``。

    预期输出
    --------
    抛出 ``GitHubRateLimitError``，错误信息含 ``retry_after_seconds=30``。
    """
    def rate_limited(_request, timeout):
        raise HTTPError(
            "https://api.github.com/repos/acme/reviewed-repo/pulls/42",
            429,
            "Too Many Requests",
            {"Retry-After": "30"},
            BytesIO(),
        )

    provider = GitHubPRProvider(http_open=rate_limited)

    with pytest.raises(GitHubRateLimitError, match="retry_after_seconds=30"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_http_429_rate_limit_without_retry_headers_waits_one_minute():
    """验证 429 限流在无 ``Retry-After`` 头时退避 60 秒。

    测试目的
    --------
    当 429 响应未携带任何退避提示头时，应使用保守的默认值 60 秒，避免对上游
    造成二次压力。

    测试场景
    --------
    构造 429 响应，响应头为空字典。

    预期输出
    --------
    抛出 ``GitHubRateLimitError``，错误信息含 ``retry_after_seconds=60``。
    """
    def rate_limited(_request, timeout):
        raise HTTPError(
            "https://api.github.com/repos/acme/reviewed-repo/pulls/42",
            429,
            "Too Many Requests",
            {},
            BytesIO(),
        )

    provider = GitHubPRProvider(http_open=rate_limited)

    with pytest.raises(GitHubRateLimitError, match="retry_after_seconds=60"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_secondary_rate_limit_prefers_retry_after():
    """验证 403 次级限流（带 ``Retry-After``）优先采用 ``Retry-After``。

    测试目的
    --------
    GitHub 的次级限流可能以 403 + ``Retry-After`` 形式返回。当同时存在
    ``Retry-After`` 与 ``X-RateLimit-Remaining`` 时，应优先采用 ``Retry-After``。

    测试场景
    --------
    构造 403 响应，``Retry-After=45``、``X-RateLimit-Remaining=12``（非 0），
    体现次级限流场景。

    预期输出
    --------
    抛出 ``GitHubRateLimitError``，错误信息含 ``retry_after_seconds=45``。
    """
    def rate_limited(_request, timeout):
        raise HTTPError(
            "https://api.github.com/repos/acme/reviewed-repo/pulls/42",
            403,
            "Forbidden",
            {"Retry-After": "45", "X-RateLimit-Remaining": "12"},
            BytesIO(),
        )

    provider = GitHubPRProvider(http_open=rate_limited)

    with pytest.raises(GitHubRateLimitError, match="retry_after_seconds=45"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_permission_failure_is_a_distinct_error():
    """验证 403 权限拒绝映射为 ``GitHubAuthorizationError``。

    测试目的
    --------
    当 403 响应既无 ``Retry-After`` 也无限流特征时，应视为 token 权限不足，
    抛出 ``GitHubAuthorizationError`` 而非限流异常，避免上层错误重试。

    测试场景
    --------
    构造 403 响应，响应头为空，区分于限流路径。

    预期输出
    --------
    抛出 ``GitHubAuthorizationError``，错误信息含
    ``github_access_denied:status=403``。
    """
    def forbidden(_request, timeout):
        raise HTTPError("https://api.github.com/test", 403, "Forbidden", {}, BytesIO())

    provider = GitHubPRProvider(http_open=forbidden)

    with pytest.raises(GitHubAuthorizationError, match="github_access_denied:status=403"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_404_is_not_found_or_inaccessible():
    """验证 404 映射为 ``GitHubNotFoundError``。

    测试目的
    --------
    404 通常意味着 PR 不存在或当前 token 无权访问。应抛出
    ``GitHubNotFoundError`` 以便上层给出明确提示，而非笼统的 API 错误。

    测试场景
    --------
    构造 404 响应。

    预期输出
    --------
    抛出 ``GitHubNotFoundError``，错误信息含
    ``github_pull_request_not_found_or_inaccessible:status=404``。
    """
    def unavailable(_request, timeout):
        raise HTTPError("https://api.github.com/test", 404, "Not Found", {}, BytesIO())

    provider = GitHubPRProvider(http_open=unavailable)

    with pytest.raises(
        GitHubNotFoundError,
        match="github_pull_request_not_found_or_inaccessible:status=404",
    ):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_api_failure_is_a_distinct_error():
    """验证 5xx（502）映射为 ``GitHubAPIError``。

    测试目的
    --------
    上游网关错误等 5xx 应抛出 ``GitHubAPIError``，与限流、鉴权、未找到等
    语义化异常区分开，便于上层决定是否重试。

    测试场景
    --------
    构造 502 Bad Gateway 响应。

    预期输出
    --------
    抛出 ``GitHubAPIError``，错误信息含 ``github_api_error:status=502``。
    """
    def unavailable(_request, timeout):
        raise HTTPError("https://api.github.com/test", 502, "Bad Gateway", {}, BytesIO())

    provider = GitHubPRProvider(http_open=unavailable)

    with pytest.raises(GitHubAPIError, match="github_api_error:status=502"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_publish_summary_comment_creates_when_no_marked_comment_exists():
    """验证无 bot 标记评论时走 POST 新建路径。

    测试目的
    --------
    当 PR 评论列表中不存在带 ``SUMMARY_COMMENT_MARKER`` 且由本 bot 发表的评论
    时，``publish_summary_comment`` 应通过 POST 创建一条新评论，并返回
    ``action="created"``。

    测试场景
    --------
    - ``http_open`` 拦截 GET 返回一条普通人类评论；
    - 拦截 POST 校验 URL、请求体（与 ``render_summary_comment`` 输出一致）及
      ``Content-type`` 头；
    - 显式提供 ``summary_comment_author_login`` 与 ``token``，验证鉴权头透传。

    预期输出
    --------
    - 返回评论 id 73 与 ``action="created"``；
    - HTTP 方法序列为 ``["GET", "POST"]``；
    - 每次请求都携带 ``Bearer test-token``。
    """
    base = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    requests = []
    body = render_summary_comment([], [])

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            assert request.full_url == base + "?per_page=100&page=1"
            return FakeHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "body": "human comment",
                            "user": {"login": "collaborator"},
                        }
                    ]
                )
            )
        if request.method == "POST":
            assert request.full_url == base  # POST 目标为评论集合根 URL
            assert json.loads(request.data.decode("utf-8")) == {"body": body}  # 请求体仅含 body 字段
            assert request.headers["Content-type"] == "application/json; charset=utf-8"
            return FakeHttpResponse(json.dumps({"id": 73}))
        raise AssertionError("unexpected method: {}".format(request.method))

    result = GitHubPRProvider(
        token="test-token",
        summary_comment_author_login="reporeview-bot",
        http_open=http_open,
    ).publish_summary_comment(
        parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42), body
    )

    assert result.comment_id == 73  # 新建评论 id 来自 POST 响应
    assert result.action == "created"  # 标记本次为新建动作
    assert [request.method for request in requests] == ["GET", "POST"]  # 方法序列正确
    # 鉴权头每次请求都正确携带
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)


def test_publish_summary_comment_updates_existing_marked_comment():
    """验证已有本 bot 标记评论时走 PATCH 更新路径。

    测试目的
    --------
    当评论列表中存在由本 bot（``summary_comment_author_login`` 匹配）发表且带
    ``SUMMARY_COMMENT_MARKER`` 的评论时，应通过 PATCH 更新该评论，避免重复
    发表，并返回 ``action="updated"``。

    测试场景
    --------
    - GET 返回两条评论：一条普通评论、一条带 marker 且 user 为本 bot 的评论；
    - PATCH 校验目标 URL（``/issues/comments/{id}``）与请求体。

    预期输出
    --------
    - 返回评论 id 41 与 ``action="updated"``；
    - HTTP 方法序列为 ``["GET", "PATCH"]``。
    """
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    update_url = "https://api.github.com/repos/acme/reviewed-repo/issues/comments/41"
    body = SUMMARY_COMMENT_MARKER + "\n## RepoReview summary\nupdated"
    requests = []

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            assert request.full_url == comments_url + "?per_page=100&page=1"
            return FakeHttpResponse(
                json.dumps(
                    [
                        {"id": 7, "body": "ordinary comment"},  # 普通评论，应被忽略
                        {
                            "id": 41,
                            "body": SUMMARY_COMMENT_MARKER + "\nold",  # 本 bot 旧评论，应被更新
                            "user": {"login": "reporeview-bot"},
                        },
                    ]
                )
            )
        assert request.method == "PATCH"  # 命中已有评论，应走更新而非新建
        assert request.full_url == update_url  # PATCH 目标为单条评论 URL
        assert json.loads(request.data.decode("utf-8")) == {"body": body}
        return FakeHttpResponse(json.dumps({"id": 41}))

    result = GitHubPRProvider(
        summary_comment_author_login="reporeview-bot", http_open=http_open
    ).publish_summary_comment(
        parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42), body
    )

    assert result.comment_id == 41  # 返回被更新评论的 id
    assert result.action == "updated"  # 标记本次为更新动作
    assert [request.method for request in requests] == ["GET", "PATCH"]


def test_publish_summary_comment_creates_when_marked_comment_is_external():
    """验证带 marker 的外部评论不被越权更新，而是新建。

    测试目的
    --------
    即便某条评论带有 ``SUMMARY_COMMENT_MARKER``，只要其 ``user.login`` 不属于
    本 bot（或 login 字段缺失/异常），就不能 PATCH 它，以防越权改写他人评论。
    此时应回退到 POST 新建路径。

    测试场景
    --------
    GET 返回三条带 marker 的评论，分别覆盖：
    - login 为其他协作者；
    - 缺失 user 字段；
    - user.login 为非字符串（列表），视为异常 login。

    预期输出
    --------
    - 走 POST 新建，返回 id 73 与 ``action="created"``；
    - 方法序列为 ``["GET", "POST"]``；
    - 任何请求 URL 都不包含 ``/issues/comments/41``，证明未对 id=41 发起越权 PATCH。
    """
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    body = SUMMARY_COMMENT_MARKER + "\n## RepoReview summary\nupdated"
    requests = []

    def http_open(request, timeout):
        requests.append(request)
        if request.method == "GET":
            assert request.full_url == comments_url + "?per_page=100&page=1"
            return FakeHttpResponse(
                json.dumps(
                    [
                        {
                            "id": 41,
                            "body": SUMMARY_COMMENT_MARKER + "\nexternal",  # 外部协作者评论
                            "user": {"login": "collaborator"},
                        },
                        {"id": 42, "body": SUMMARY_COMMENT_MARKER + "\nno user"},  # 缺失 user 字段
                        {
                            "id": 43,
                            "body": SUMMARY_COMMENT_MARKER + "\nbad user",  # login 类型异常
                            "user": {"login": ["not-a-login"]},
                        },
                    ]
                )
            )
        assert request.method == "POST"  # 外部评论一律不更新，回退到新建
        assert request.full_url == comments_url
        assert json.loads(request.data.decode("utf-8")) == {"body": body}
        return FakeHttpResponse(json.dumps({"id": 73}))

    result = GitHubPRProvider(
        summary_comment_author_login="reporeview-bot", http_open=http_open
    ).publish_summary_comment(
        parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42), body
    )

    assert result.comment_id == 73
    assert result.action == "created"
    assert [request.method for request in requests] == ["GET", "POST"]
    # 关键不变量：任何请求都未触碰外部评论 41 的更新 URL，防止越权
    assert all("/issues/comments/41" not in request.full_url for request in requests)


def test_publish_summary_comment_rejects_body_without_marker_before_http_call():
    """验证缺少 marker 的 body 在发起 HTTP 前即被拒绝。

    测试目的
    --------
    为保证后续能识别本 bot 评论，发布的 body 必须包含
    ``SUMMARY_COMMENT_MARKER``。若缺失，应在发起任何 HTTP 请求前抛出
    ``GitProviderInputError``，避免发出无法被后续识别的评论。

    测试场景
    --------
    - ``http_open`` 设为 ``pytest.fail``，确保一旦发起 HTTP 即测试失败；
    - body 不含 marker。

    预期输出
    --------
    抛出 ``GitProviderInputError``，错误信息含 ``summary_comment_marker_missing``。
    """
    # http_open 设为 fail，确保前置校验在 HTTP 之前完成
    provider = GitHubPRProvider(http_open=lambda *_args, **_kwargs: pytest.fail("unexpected HTTP call"))

    with pytest.raises(GitProviderInputError, match="summary_comment_marker_missing"):
        provider.publish_summary_comment(
            parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42),
            "## RepoReview summary",
        )


def test_publish_summary_comment_requires_author_login_before_http_call(capsys, monkeypatch):
    """验证缺少 author login 时在 HTTP 前拒绝，且不泄露 token。

    测试目的
    --------
    摘要评论的归属判断依赖 ``summary_comment_author_login``。若未配置，应在
    发起 HTTP 前抛出 ``GitProviderInputError``。同时需确保异常信息与标准输出
    /错误输出中均不泄露 token，避免敏感信息暴露。

    测试场景
    --------
    - 通过 ``monkeypatch.delenv`` 清除环境变量 ``GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN``；
    - 显式传入 token，并通过 ``capsys`` 捕获 stdout/stderr；
    - ``http_open`` 设为 ``pytest.fail`` 确保不发起请求。

    预期输出
    --------
    - 抛出 ``GitProviderInputError``，匹配 ``missing_summary_comment_author_login``；
    - ``calls`` 为空（未发起 HTTP）；
    - 异常信息、stdout、stderr 中均不含 token 字符串。
    """
    token = "test-token-must-not-leak"
    calls = []
    monkeypatch.delenv("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN", raising=False)  # 清除可能的环境变量配置

    def http_open(*_args, **_kwargs):
        calls.append(True)
        pytest.fail("unexpected HTTP call")  # 不应到达此处

    provider = GitHubPRProvider(token=token, http_open=http_open)

    with pytest.raises(
        GitProviderInputError, match="^missing_summary_comment_author_login$"
    ) as exc_info:
        provider.publish_summary_comment(
            parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42),
            SUMMARY_COMMENT_MARKER + "\n## RepoReview summary",
        )

    captured = capsys.readouterr()
    assert calls == []  # 确认未发起任何 HTTP 调用
    assert token not in str(exc_info.value)  # 异常信息不泄露 token
    assert token not in captured.out  # 标准输出不泄露 token
    assert token not in captured.err  # 标准错误不泄露 token


def test_publish_summary_comment_surfaces_create_permission_failure():
    """验证 POST 创建评论遇到 403 时抛出鉴权异常。

    测试目的
    --------
    当 GET 成功（无已有标记评论）但 POST 创建评论返回 403 时，应将该错误
    透传为 ``GitHubAuthorizationError``，便于上层提示权限不足。

    测试场景
    --------
    - GET 返回空评论列表；
    - POST 抛出 403 HTTPError。

    预期输出
    --------
    抛出 ``GitHubAuthorizationError``，错误信息含
    ``github_access_denied:status=403``。
    """
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    body = SUMMARY_COMMENT_MARKER + "\n## RepoReview summary\n"

    def http_open(request, timeout):
        if request.method == "GET":
            return FakeHttpResponse("[]")  # 无已有评论，应走 POST 新建
        assert request.method == "POST"
        assert request.full_url == comments_url
        assert json.loads(request.data.decode("utf-8")) == {"body": body}
        # 创建评论时遭遇 403，模拟 token 无写权限
        raise HTTPError(request.full_url, 403, "Forbidden", {}, BytesIO())

    provider = GitHubPRProvider(
        summary_comment_author_login="reporeview-bot", http_open=http_open
    )

    with pytest.raises(GitHubAuthorizationError, match="github_access_denied:status=403"):
        provider.publish_summary_comment(
            parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42), body
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pr_url": "https://example.com/acme/reviewed-repo/pull/42"},
        {"pr_url": "https://github.com/acme/reviewed-repo/pull/not-a-number"},
        {"pr_url": "https://github.com/acme/reviewed-repo/pull/42", "owner": "acme"},
        {"owner": "acme"},
    ],
)
def test_pull_request_reference_rejects_untrusted_or_ambiguous_inputs(kwargs):
    """验证 ``parse_pull_request_ref`` 拒绝非法与歧义输入（参数化）。

    测试目的
    --------
    PR 引用解析必须严格：只信任 ``github.com`` 域名、PR 编号必须为数字、且
    ``pr_url`` 与 ``owner/repo/number`` 不能混用。任何不合规输入都应抛出
    ``GitProviderInputError``。

    参数化设计理由
    --------------
    通过 ``pytest.mark.parametrize`` 一次覆盖四类典型非法输入：
    - 非 github.com 域名（防钓鱼/不可信来源）；
    - PR 编号非数字；
    - URL 与 owner 同时提供（歧义）；
    - 仅提供 owner 而缺 repo/number（不完整）。

    预期输出
    --------
    每组参数均抛出 ``GitProviderInputError``。
    """
    with pytest.raises(GitProviderInputError):
        parse_pull_request_ref(**kwargs)
