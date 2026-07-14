"""Read GitHub pull-request data and publish an owned summary comment.

本模块是 :class:`src.git_provider.GitProvider` 接口针对 GitHub.com 的具体实现，
属于架构中的“Provider 实现层”。设计要点：
- 零第三方依赖：HTTP 调用全部使用标准库 ``urllib``，降低安装与维护成本；
- 分层异常：将 HTTP 错误映射为语义清晰的异常族（鉴权/限流/未找到/网络），
  便于上层做差异化处理；
- 可测试性：``http_open`` 与 ``now`` 通过构造函数注入，测试时可用 fixture 替换，
  无需真实网络即可覆盖各类响应分支；
- 幂等发布：摘要评论通过 :data:`SUMMARY_COMMENT_MARKER` + 作者 login 唯一识别，
  避免重复发布。
"""

import json
import os
import re
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .git_provider import (
    GitProvider,
    GitProviderError,
    GitProviderInputError,
    PullRequestCommit,
    PullRequestData,
    PullRequestFile,
    PullRequestMetadata,
    PullRequestRef,
    SUMMARY_COMMENT_MARKER,
    SummaryCommentResult,
    parse_pull_request_ref,
)


DEFAULT_API_URL = "https://api.github.com"  # GitHub REST API 默认入口
DEFAULT_TIMEOUT_SECONDS = 10.0  # 单次 HTTP 请求默认超时（秒），避免长时间阻塞
DEFAULT_PER_PAGE = 100  # 分页每页条数，GitHub 单页最大即为 100，减少请求次数
# GitHub 对 PR 变更文件数的硬上限（3000）。超过该值时 GitHub 会截断响应，
# 因此当文件数达到该阈值时需产生告警，提示审查结果可能不完整。
GITHUB_PULL_REQUEST_FILES_MAX = 3000
# 摘要评论作者 login 的白名单正则：1~39 位字母/数字/连字符，首尾不能是连字符，可后缀 [bot]。
_GITHUB_LOGIN_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9-]{1,39}(?<!-)(?:\[bot\])?$")


class GitHubAPIError(GitProviderError):
    """GitHub returned an API response that cannot be used.

    GitHub API 层异常的基类，表示收到了响应但无法安全使用（如格式异常、状态码异常）。
    """


class GitHubAuthorizationError(GitHubAPIError):
    """The supplied token cannot read the requested pull request.

    鉴权失败：token 缺失、无效（401）或权限不足（403 且非限流）。
    """


class GitHubRateLimitError(GitHubAPIError):
    """GitHub refused the request because its rate limit is exhausted.

    触发限流（429 或 403+限流头）。异常信息中会带 ``retry_after_seconds``，
    便于上层决定退避策略。
    """


class GitHubNotFoundError(GitHubAPIError):
    """The PR does not exist or cannot be accessed with supplied credentials.

    404：PR 不存在或当前凭据无权访问。
    """


class GitHubNetworkError(GitHubAPIError):
    """The GitHub API could not be reached.

    网络层错误：DNS 失败、连接超时、URLError 等。
    """


def _header_value(headers, name: str) -> Optional[str]:
    """大小写不敏感地从 headers 中取值。

    标准库 ``http.client.HTTPMessage`` 的 ``get`` 默认大小写不敏感，
    但为了兼容自定义 mapping（如测试 fixture 的普通 dict），这里再做一次
    小写匹配兜底。

    Args:
        headers: 响应头对象或 dict，可为 None。
        name: 目标头名（原始大小写）。

    Returns:
        命中的头值；未命中返回 None。
    """
    if headers is None:
        return None
    # 优先用原生 get（标准库 HTTPMessage 本身大小写不敏感）
    value = headers.get(name)
    if value is not None:
        return value
    # 兜底：遍历做小写比较，兼容普通 dict
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


class GitHubPRProvider(GitProvider):
    """A minimal GitHub.com pull-request reader and summary-comment writer.

    ``summary_comment_author_login`` is the non-sensitive GitHub login that
    owns summary comments.  It can also be configured with
    ``GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN``.  Publishing requires a valid
    value and only updates marked comments authored by that login.

    ``http_open`` is injectable so tests can exercise HTTP fixtures without a
    network connection.  It must have the same call shape as ``urlopen``.
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        summary_comment_author_login: Optional[str] = None,
        api_url: str = DEFAULT_API_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_open: Callable = urlopen,
        now: Callable[[], float] = time.time,
    ):
        """构造一个 GitHub PR Provider。

        全部参数均通过关键字传入，便于测试时按需替换。``http_open`` 与 ``now``
        是可注入的“接缝（seam）”，测试中可分别替换为返回固定响应的 fake 与
        固定时间，从而无网络、无时间依赖地覆盖各类分支。

        Args:
            token: GitHub Personal Access Token；为 None 时回退到环境变量 ``GITHUB_TOKEN``。
            summary_comment_author_login: 摘要评论归属的 GitHub login；
                为 None 时回退到 ``GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN``。
            api_url: GitHub API 入口，必须以 ``https://`` 开头。
            timeout_seconds: 单次请求超时秒数，必须大于 0。
            http_open: 执行 HTTP 请求的可调用对象，签名需与 ``urllib.request.urlopen`` 一致。
            now: 返回当前时间戳的可调用对象，用于计算限流重试等待秒数。

        Raises:
            ValueError: 当 ``timeout_seconds`` 非正或 ``api_url`` 非 https 时抛出。
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        # 强制 https，防止 token 在明文链路中泄露
        if not api_url.startswith("https://"):
            raise ValueError("api_url must use https")
        # token 优先用显式参数，其次回退到环境变量，便于在容器/CI 中通过 env 注入
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.summary_comment_author_login = (
            summary_comment_author_login
            if summary_comment_author_login is not None
            else os.getenv("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN")
        )
        # 去掉末尾斜杠，避免后续拼 URL 时出现重复斜杠
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_open = http_open
        self._now = now

    def get_pull_request(self, reference: PullRequestRef) -> PullRequestMetadata:
        """读取 PR 元数据。

        对 GitHub ``GET /repos/{owner}/{repo}/pulls/{number}`` 响应做严格校验后，
        映射为 :class:`PullRequestMetadata`。校验失败一律抛 :class:`GitHubAPIError`，
        避免下游拿到结构异常的数据。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            PR 元数据。

        Raises:
            GitHubAPIError: 当响应非 dict 或字段缺失/类型错误时抛出。
        """
        payload = self._get_json(self._pulls_url(reference))
        if not isinstance(payload, dict):
            raise GitHubAPIError("github_invalid_pull_request_payload")
        base_sha = self._nested_string(payload, "base", "sha")
        head_sha = self._nested_string(payload, "head", "sha")
        title = payload.get("title")
        body = payload.get("body")
        # title 必须是字符串；body 可为 None（PR 没写描述），但若存在则必须是字符串
        if not isinstance(title, str) or body is not None and not isinstance(body, str):
            raise GitHubAPIError("github_invalid_pull_request_payload")

        # head.repo 仅在 fork 或跨仓库 PR 时存在；同仓库 PR 该字段为 None
        head_repo = payload.get("head", {}).get("repo")
        head_repository = None
        if isinstance(head_repo, dict):
            value = head_repo.get("full_name")
            if not isinstance(value, str):
                raise GitHubAPIError("github_invalid_pull_request_payload")
            head_repository = value
        elif head_repo is not None:
            raise GitHubAPIError("github_invalid_pull_request_payload")

        # is_fork 判定：head 仓库存在且其 full_name 与目标仓库不同（大小写不敏感比较）
        return PullRequestMetadata(
            title=title,
            description=body or "",
            base_sha=base_sha,
            head_sha=head_sha,
            is_fork=head_repository is not None and head_repository.lower()
            != "{}/{}".format(reference.owner, reference.repo).lower(),
            head_repository=head_repository,
        )

    def get_changed_files(self, reference: PullRequestRef) -> List[PullRequestFile]:
        """读取 PR 的全部变更文件（自动翻页）。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            :class:`PullRequestFile` 列表。注意：当文件数达到 GitHub 上限时，
            响应会被截断，此处不会感知截断，截断告警由 :meth:`fetch_pull_request` 统一处理。

        Raises:
            GitHubAPIError: 当某条文件记录字段类型不合规时抛出。
        """
        files = []
        for payload in self._get_paginated_json(self._pulls_url(reference) + "/files"):
            if not isinstance(payload, dict):
                raise GitHubAPIError("github_invalid_changed_file_payload")
            path = payload.get("filename")
            status = payload.get("status")
            # patch 可能为 None（二进制文件或 diff 过大被 GitHub 裁剪）
            patch = payload.get("patch")
            # previous_filename 仅在 renamed 时出现
            old_path = payload.get("previous_filename")
            if not isinstance(path, str) or not isinstance(status, str):
                raise GitHubAPIError("github_invalid_changed_file_payload")
            if patch is not None and not isinstance(patch, str):
                raise GitHubAPIError("github_invalid_changed_file_payload")
            if old_path is not None and not isinstance(old_path, str):
                raise GitHubAPIError("github_invalid_changed_file_payload")
            files.append(PullRequestFile(path, status, patch, old_path))
        return files

    def get_pull_request_patch(self, reference: PullRequestRef) -> str:
        """读取整个 PR 的 patch 文本（统一 diff）。

        通过 ``Accept: application/vnd.github.v3.diff`` 让 GitHub 直接返回 diff 文本，
        而非 JSON。这样可一次性拿到完整 patch，便于后续按文件解析。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            PR 的 patch 字符串（可能为空字符串）。
        """
        body, _headers = self._request(
            self._pulls_url(reference), accept="application/vnd.github.v3.diff"
        )
        return body

    def get_pull_request_commits(self, reference: PullRequestRef) -> List[PullRequestCommit]:
        """读取 PR 中的所有 commit（自动翻页）。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            :class:`PullRequestCommit` 列表。

        Raises:
            GitHubAPIError: 当某条 commit 记录字段类型不合规时抛出。
        """
        commits = []
        for payload in self._get_paginated_json(self._pulls_url(reference) + "/commits"):
            if not isinstance(payload, dict):
                raise GitHubAPIError("github_invalid_commit_payload")
            sha = payload.get("sha")
            commit = payload.get("commit")
            # commit.message 来自内层 commit 对象；author.login 来自外层 author 对象
            message = commit.get("message") if isinstance(commit, dict) else None
            author = payload.get("author")
            author_login = author.get("login") if isinstance(author, dict) else None
            if not isinstance(sha, str) or not isinstance(message, str):
                raise GitHubAPIError("github_invalid_commit_payload")
            if author_login is not None and not isinstance(author_login, str):
                raise GitHubAPIError("github_invalid_commit_payload")
            commits.append(PullRequestCommit(sha, message, author_login))
        return commits

    def publish_summary_comment(
        self, reference: PullRequestRef, body: str
    ) -> SummaryCommentResult:
        """Create or update the marked summary comment owned by this bot.

        实现幂等更新策略，保证同一 PR 不会因为重复运行而刷屏：
        1. 先校验 body 非空且包含 :data:`SUMMARY_COMMENT_MARKER`；
        2. 翻页遍历该 PR 的所有 issue 评论，查找“body 含 MARKER 且作者 login
           匹配本 bot”的已有评论；
        3. 找到则 ``PATCH`` 更新该评论，否则 ``POST`` 新建评论。

        通过“MARKER + 作者 login”双重条件唯一识别本 bot 的摘要评论，
        避免误更新他人评论。

        Args:
            reference: 已校验的 PR 引用。
            body: 评论正文，必须包含 :data:`SUMMARY_COMMENT_MARKER`。

        Returns:
            :class:`SummaryCommentResult`，``action`` 为 ``created`` 或 ``updated``。

        Raises:
            GitProviderInputError: body 非法或缺少 MARKER、未配置作者 login 时抛出。
            GitHubAPIError: 已有评论或响应 id 不合规时抛出。
        """
        if not isinstance(body, str) or not body.strip():
            raise GitProviderInputError("invalid_summary_comment_body")
        if SUMMARY_COMMENT_MARKER not in body:
            raise GitProviderInputError("summary_comment_marker_missing")
        author_login = self._validated_summary_comment_author_login()

        # 第一步：翻页查找本 bot 已发布的带 MARKER 的评论
        existing_comment_id = None
        for comment in self._get_paginated_json(self._issue_comments_url(reference)):
            if not isinstance(comment, dict):
                continue
            comment_body = comment.get("body")
            comment_user = comment.get("user")
            comment_author_login = (
                comment_user.get("login") if isinstance(comment_user, dict) else None
            )
            # 双重命中条件：body 含 MARKER 且作者 login 与本 bot 一致
            if (
                isinstance(comment_body, str)
                and isinstance(comment_author_login, str)
                and SUMMARY_COMMENT_MARKER in comment_body
                and comment_author_login == author_login
            ):
                comment_id = comment.get("id")
                if (
                    not isinstance(comment_id, int)
                    or isinstance(comment_id, bool)
                    or comment_id <= 0
                ):
                    raise GitHubAPIError("github_invalid_issue_comment_payload")
                existing_comment_id = comment_id

        # 第二步：根据是否找到已有评论，决定 POST 新建或 PATCH 更新
        if existing_comment_id is None:
            response = self._send_json(
                self._issue_comments_url(reference), method="POST", payload={"body": body}
            )
            action = "created"
        else:
            response = self._send_json(
                self._issue_comment_url(reference, existing_comment_id),
                method="PATCH",
                payload={"body": body},
            )
            action = "updated"

        # 第三步：校验响应中的评论 id 合法性
        comment_id = response.get("id") if isinstance(response, dict) else None
        if (
            not isinstance(comment_id, int)
            or isinstance(comment_id, bool)
            or comment_id <= 0
        ):
            raise GitHubAPIError("github_invalid_issue_comment_response")
        return SummaryCommentResult(comment_id=comment_id, action=action)

    def _validated_summary_comment_author_login(self) -> str:
        """校验并返回摘要评论作者 login。

        校验规则：非空、非纯空白、且匹配 :data:`_GITHUB_LOGIN_PATTERN`。
        通过白名单正则防止注入异常 login 导致后续匹配失效。

        Returns:
            合法的作者 login 字符串。

        Raises:
            GitProviderInputError: 未配置或格式非法时抛出。
        """
        login = self.summary_comment_author_login
        if login is None or (isinstance(login, str) and not login.strip()):
            raise GitProviderInputError("missing_summary_comment_author_login")
        if not isinstance(login, str) or not _GITHUB_LOGIN_PATTERN.fullmatch(login):
            raise GitProviderInputError("invalid_summary_comment_author_login")
        return login

    def fetch_pull_request(
        self,
        *,
        pr_url: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        number: Optional[int] = None,
    ) -> PullRequestData:
        """Fetch all read-only PR inputs needed by later review stages.

        聚合一次 PR 审查所需的全部只读输入（元数据/文件/patch/commits），
        并在抓取过程中收集告警信息，便于上层在审查结果中向用户提示数据完整性问题。

        产生的告警包括：
        - ``patch_unavailable:{path}``：某文件 patch 缺失（二进制或被 GitHub 裁剪）；
        - ``changed_files_may_be_truncated_at_github_limit``：文件数达到 3000 上限，
          响应可能被 GitHub 截断；
        - ``pull_request_patch_empty``：整体 patch 为空；
        - ``fork_source_repository_unavailable``：fork PR 的源仓库不可访问。

        Args:
            pr_url: 可选的 GitHub PR URL。
            owner: 可选的 owner（与 pr_url 二选一）。
            repo: 可选的 repo。
            number: 可选的 PR 编号。

        Returns:
            :class:`PullRequestData`，含全部只读输入与告警列表。
        """
        reference = parse_pull_request_ref(
            pr_url=pr_url, owner=owner, repo=repo, number=number
        )
        # 依次拉取四类只读数据，任一步失败都会抛出对应异常
        metadata = self.get_pull_request(reference)
        files = self.get_changed_files(reference)
        patch = self.get_pull_request_patch(reference)
        commits = self.get_pull_request_commits(reference)

        # —— 收集数据完整性告警 ——
        # 告警 1：逐文件检查 patch 是否缺失
        warnings = [
            "patch_unavailable:{}".format(changed_file.path)
            for changed_file in files
            if changed_file.patch is None
        ]
        # 告警 2：文件数达到 GitHub 上限，响应可能被截断
        if len(files) >= GITHUB_PULL_REQUEST_FILES_MAX:
            warnings.append("changed_files_may_be_truncated_at_github_limit")
        # 告警 3：整体 patch 为空
        if not patch:
            warnings.append("pull_request_patch_empty")
        # 告警 4：fork PR 但源仓库不可访问（已删除/无权限）
        if metadata.is_fork and metadata.head_repository is None:
            warnings.append("fork_source_repository_unavailable")

        return PullRequestData(reference, metadata, files, patch, commits, warnings)

    def _pulls_url(self, reference: PullRequestRef) -> str:
        """构造 ``/repos/{owner}/{repo}/pulls/{number}`` URL。

        owner/repo 经 :func:`urllib.parse.quote` 做 URL 编码（``safe=""``
        表示所有特殊字符都编码），配合 :class:`PullRequestRef` 的白名单校验，
        双重防止 URL 注入。
        """
        return "{}/repos/{}/{}/pulls/{}".format(
            self.api_url,
            quote(reference.owner, safe=""),
            quote(reference.repo, safe=""),
            reference.number,
        )

    def _issue_comments_url(self, reference: PullRequestRef) -> str:
        """构造列出 PR issue 评论的 URL（``/repos/.../issues/{number}/comments``）。"""
        return "{}/repos/{}/{}/issues/{}/comments".format(
            self.api_url,
            quote(reference.owner, safe=""),
            quote(reference.repo, safe=""),
            reference.number,
        )

    def _issue_comment_url(self, reference: PullRequestRef, comment_id: int) -> str:
        """构造单条 issue 评论的 URL（用于 PATCH 更新）。"""
        return "{}/repos/{}/{}/issues/comments/{}".format(
            self.api_url,
            quote(reference.owner, safe=""),
            quote(reference.repo, safe=""),
            comment_id,
        )

    def _get_paginated_json(self, url: str) -> List[Dict]:
        """通用分页拉取，返回所有页聚合后的列表。

        采用“按页数翻页”策略：每页 ``per_page=100``，当某页返回条数不足一页时
        认为已到末页并停止。性能上以“最少请求次数”为目标（每页取最大 100 条）。

        Args:
            url: 不含分页查询串的基础 URL。

        Returns:
            聚合后的 dict 列表。

        Raises:
            GitHubAPIError: 某页响应不是 list 时抛出。
        """
        values = []
        page = 1
        while True:
            query = urlencode({"per_page": DEFAULT_PER_PAGE, "page": page})
            # 兼容 url 中已带查询串的情况
            separator = "&" if "?" in url else "?"
            payload = self._get_json(url + separator + query)
            if not isinstance(payload, list):
                raise GitHubAPIError("github_invalid_list_response")
            values.extend(payload)
            # 性能优化点：不足一页即说明已是最后一页，提前终止，避免多余请求
            if len(payload) < DEFAULT_PER_PAGE:
                return values
            page += 1

    def _get_json(self, url: str):
        """发起 GET 请求并解析为 JSON。"""
        body, _headers = self._request(url, accept="application/vnd.github+json")
        return self._parse_json(body)

    def _send_json(self, url: str, *, method: str, payload: Dict):
        """发起带 JSON body 的请求（POST/PATCH 等）并解析响应为 JSON。

        Args:
            url: 目标 URL。
            method: HTTP 方法（如 ``POST``/``PATCH``）。
            payload: 将被序列化为 JSON 的请求体 dict。
        """
        body, _headers = self._request(
            url,
            accept="application/vnd.github+json",
            method=method,
            json_payload=payload,
        )
        return self._parse_json(body)

    @staticmethod
    def _parse_json(body: str):
        """将响应体解析为 JSON，失败时统一抛 :class:`GitHubAPIError`。

        将 ``json.JSONDecodeError`` 转换为业务异常，避免解析细节泄露给上层。
        """
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError("github_invalid_json_response") from exc

    def _request(
        self,
        url: str,
        *,
        accept: str,
        method: str = "GET",
        json_payload: Optional[Dict] = None,
    ) -> Tuple[str, object]:
        """统一的 HTTP 请求入口。

        封装请求头组装、token 注入、请求发送、异常映射与响应读取。
        使用 ``with`` 风格的 ``try/finally`` 确保响应对象被关闭，避免连接泄漏。

        Args:
            url: 目标 URL。
            accept: ``Accept`` 头值（如 ``application/vnd.github+json`` 或 diff 类型）。
            method: HTTP 方法，默认 ``GET``。
            json_payload: 若提供，则序列化为 JSON 作为请求体。

        Returns:
            ``(body, headers)`` 元组，body 为解码后的字符串，headers 为响应头对象。

        Raises:
            GitHubAuthorizationError/GitHubRateLimitError/GitHubNotFoundError/
            GitHubAPIError: 由 :meth:`_raise_http_error` 按 HTTP 状态码映射。
            GitHubNetworkError: 网络层错误。
        """
        headers = {
            "Accept": accept,
            "User-Agent": "RepoReview-Agent",  # GitHub API 要求携带 User-Agent
            "X-GitHub-Api-Version": "2022-11-28",  # 锁定 API 版本，避免行为漂移
        }
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        data = None
        if json_payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            # ensure_ascii=False 保留中文等 Unicode 字符，减小 body 体积
            data = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = self._http_open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            # HTTPError 表示收到了非 2xx 响应，交给状态码映射逻辑
            self._raise_http_error(exc.code, exc.headers)
        except (URLError, TimeoutError, OSError) as exc:
            # 网络层错误统一映射为 GitHubNetworkError
            raise GitHubNetworkError("github_network_error") from exc

        try:
            body = response.read().decode("utf-8")
            return body, response.headers
        finally:
            # 确保响应被关闭，释放底层连接
            response.close()

    def _raise_http_error(self, status_code: int, headers) -> None:
        """按 HTTP 状态码映射为对应的业务异常。

        重点处理限流场景（403/429）：
        - 优先用 ``Retry-After`` 头计算重试等待秒数；
        - 其次用 ``X-RateLimit-Remaining==0`` + ``X-RateLimit-Reset`` 头
          （reset 是绝对时间戳，需减去当前 ``now()`` 得到相对等待秒数）；
        - 都没有时（如 GitHub 的二级限流），按官方建议至少等待 60 秒。

        Args:
            status_code: HTTP 状态码。
            headers: 响应头对象。

        Raises:
            始终抛出对应异常（无返回）。
        """
        if status_code == 401:
            raise GitHubAuthorizationError("github_authentication_failed:status=401")
        if status_code in (403, 429):
            retry_after_header = _header_value(headers, "Retry-After")
            remaining = _header_value(headers, "X-RateLimit-Remaining")
            # 命中限流的判定：429、或带 Retry-After、或剩余配额为 0
            if status_code == 429 or retry_after_header is not None or remaining == "0":
                if retry_after_header is not None:
                    # 情况 1：有 Retry-After 头，直接用其值
                    retry_after = self._retry_after_header_seconds(retry_after_header)
                elif remaining == "0":
                    # 情况 2：配额耗尽，用 X-RateLimit-Reset（绝对时间戳）减当前时间
                    reset = _header_value(headers, "X-RateLimit-Reset")
                    retry_after = self._retry_after_seconds(reset)
                else:
                    # GitHub recommends waiting at least one minute for a
                    # secondary rate limit without retry guidance headers.
                    # 情况 3：二级限流无明确重试头，按官方建议至少等待 60 秒
                    retry_after = "60"
                raise GitHubRateLimitError(
                    "github_rate_limited:retry_after_seconds={}".format(retry_after)
                )
        if status_code == 403:
            # 排除限流后的 403 视为权限不足
            raise GitHubAuthorizationError("github_access_denied:status=403")
        if status_code == 404:
            raise GitHubNotFoundError(
                "github_pull_request_not_found_or_inaccessible:status=404"
            )
        raise GitHubAPIError("github_api_error:status={}".format(status_code))

    def _retry_after_seconds(self, reset: Optional[str]) -> str:
        """根据 ``X-RateLimit-Reset``（绝对时间戳）计算相对等待秒数。

        Args:
            reset: ``X-RateLimit-Reset`` 头值（Unix 时间戳字符串），可能为 None。

        Returns:
            等待秒数字符串；计算失败时回退到 ``"60"``。
        """
        try:
            # reset 是绝对时间戳，减去当前时间得到相对等待秒数；max(0, ...) 防止负值
            return str(max(0, int(reset) - int(self._now())))
        except (TypeError, ValueError):
            return "60"

    @staticmethod
    def _retry_after_header_seconds(retry_after: str) -> str:
        """解析 ``Retry-After`` 头（秒数形式）。

        Args:
            retry_after: ``Retry-After`` 头值（秒数字符串）。

        Returns:
            等待秒数字符串；解析失败回退到 ``"60"``。
        """
        try:
            return str(max(0, int(retry_after)))
        except (TypeError, ValueError):
            return "60"

    @staticmethod
    def _nested_string(payload, outer_key: str, inner_key: str) -> str:
        """安全取嵌套字段 ``payload[outer_key][inner_key]`` 并要求为字符串。

        Args:
            payload: 顶层 dict。
            outer_key: 外层键。
            inner_key: 内层键。

        Returns:
            取到的字符串值。

        Raises:
            GitHubAPIError: 字段缺失或类型非 str 时抛出。
        """
        outer = payload.get(outer_key)
        value = outer.get(inner_key) if isinstance(outer, dict) else None
        if not isinstance(value, str):
            raise GitHubAPIError("github_invalid_pull_request_payload")
        return value
