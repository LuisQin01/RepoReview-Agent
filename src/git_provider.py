"""Provider-neutral models and read-only Git pull-request interface.

本模块在整体架构中处于“Provider 边界层”，负责定义与具体 Git 托管平台无关的
数据模型与只读 PR 接口。设计上遵循依赖倒置原则（DIP）：
- 上层审查流水线只依赖 :class:`GitProvider` 这个 ``Protocol``，而不依赖任何具体平台实现；
- 平台实现（如 :class:`src.github_provider.GitHubPRProvider`）再各自实现该接口，
  便于将来扩展 GitLab、Bitbucket 等托管平台而无需修改上游代码。
- 这里同时承担“输入校验”职责：通过 :class:`PullRequestRef` 在构造时即校验 owner/repo/number，
  把不合法输入挡在业务逻辑之外，体现防御式编程思想。
"""

from dataclasses import dataclass, field
import re
from typing import List, Optional, Protocol
from urllib.parse import urlsplit


# owner/repo 名称允许的字符集：字母、数字、下划线、点、连字符
# 用于在构造 PullRequestRef 时做白名单校验，防止路径注入等攻击
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
# 摘要评论的 HTML 注释标记。本 Agent 发布摘要评论时会写入该标记，
# 后续幂等更新时通过它识别“这是本 bot 发布的评论”，从而决定是 PATCH 更新还是 POST 新建。
SUMMARY_COMMENT_MARKER = "<!-- reporeview-summary -->"


class GitProviderError(RuntimeError):
    """A Git provider request could not be completed safely.

    所有 Provider 层异常的基类，表示一次 Git 请求因安全/可用性原因无法完成。
    上层可统一捕获该异常族进行错误处理与脱敏。
    """


class GitProviderInputError(GitProviderError):
    """The caller supplied an invalid pull-request reference.

    专指“调用方传入的 PR 引用不合法”，与“请求本身失败”区分开，
    便于上层做 4xx 类输入错误的细分处理。
    """


@dataclass(frozen=True)
class PullRequestRef:
    """对一次 Pull Request 的不可变引用（owner/repo/number 三元组）。

    采用 ``frozen=True`` 不可变 dataclass，确保引用在构造后不会被篡改，
    可安全地作为 dict key 或在多线程间传递。构造时通过 :meth:`__post_init__`
    做严格校验，从源头上杜绝非法输入进入后续 API 调用。

    Args:
        owner: 仓库所属用户/组织名，必须匹配白名单字符集。
        repo: 仓库名，必须匹配白名单字符集。
        number: PR 编号，必须为正整数。
    """

    owner: str
    repo: str
    number: int

    def __post_init__(self):
        # 严格校验 owner：必须为字符串且匹配白名单字符集，防止路径穿越/注入
        if not isinstance(self.owner, str) or not _NAME_PATTERN.fullmatch(self.owner):
            raise GitProviderInputError("invalid_pull_request_owner")
        # 严格校验 repo：同上
        if not isinstance(self.repo, str) or not _NAME_PATTERN.fullmatch(self.repo):
            raise GitProviderInputError("invalid_pull_request_repo")
        # 严格校验 number：必须为 int、且不能是 bool（Python 中 bool 是 int 子类，需显式排除）、且大于 0
        # 排除 bool 是因为 True/False 会被当作 1/0，可能导致语义错误
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number <= 0:
            raise GitProviderInputError("invalid_pull_request_number")


def parse_pull_request_ref(
    *,
    pr_url: Optional[str] = None,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    number: Optional[int] = None,
) -> PullRequestRef:
    """Build a validated PR reference from one GitHub URL or three fields.

    支持两种互斥的输入方式，强制“二选一”以避免歧义：
    1. 传入完整的 GitHub PR URL（``pr_url``），内部解析出 owner/repo/number；
    2. 传入 owner/repo/number 三个字段。

    URL 校验较严格（要求 https、host 为 github.com、无 userinfo、path 恰好 4 段
    且第 3 段为 ``pull``），目的是防止 SSRF、凭据泄露与非 GitHub 链接混入。

    Args:
        pr_url: 可选的 GitHub PR 完整 URL。
        owner: 可选的仓库 owner。
        repo: 可选的仓库名。
        number: 可选的 PR 编号。

    Returns:
        构造并校验通过的 :class:`PullRequestRef`。

    Raises:
        GitProviderInputError: 当同时传/都不传 URL 与三元组、或 URL 格式非法时抛出。
    """
    # 判断调用方选择了哪种输入方式
    has_url = pr_url is not None
    has_parts = any(value is not None for value in (owner, repo, number))
    # 强制二选一：同时提供或都不提供均视为非法
    if has_url == has_parts:
        raise GitProviderInputError("provide_exactly_one_pull_request_reference")

    if has_url:
        # —— URL 解析分支 ——
        if not isinstance(pr_url, str):
            raise GitProviderInputError("invalid_pull_request_url")
        parsed = urlsplit(pr_url)
        # 安全约束：必须是 https、host 限定 github.com、不允许携带 userinfo（防止凭据注入/泄露）
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise GitProviderInputError("invalid_github_pull_request_url")
        # 拆分 path 并过滤空段，期望形如 /{owner}/{repo}/pull/{number} 共 4 段
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) != 4 or segments[2] != "pull":
            raise GitProviderInputError("invalid_github_pull_request_url")
        try:
            # 第 4 段必须是可转为 int 的 PR 编号
            pull_number = int(segments[3])
        except ValueError as exc:
            raise GitProviderInputError("invalid_pull_request_number") from exc
        return PullRequestRef(segments[0], segments[1], pull_number)

    # —— 三元组分支：直接交给 PullRequestRef 做严格校验 ——
    return PullRequestRef(owner, repo, number)


@dataclass(frozen=True)
class PullRequestMetadata:
    """PR 的元数据（标题、描述、base/head SHA、是否 fork 等）。

    不可变，便于在流水线各阶段安全共享。
    """

    title: str
    description: str
    base_sha: str
    head_sha: str
    is_fork: bool
    head_repository: Optional[str]


@dataclass(frozen=True)
class PullRequestFile:
    """PR 中单个变更文件的描述。

    Args:
        path: 变更后的文件路径。
        status: 变更类型（added/removed/modified/renamed 等）。
        patch: 该文件的 diff 文本；可能为 None（如二进制文件或 diff 被截断）。
        old_path: 仅在 renamed 时存在，表示重命名前的路径。
    """

    path: str
    status: str
    patch: Optional[str]
    old_path: Optional[str] = None


@dataclass(frozen=True)
class PullRequestCommit:
    """PR 中单个 commit 的描述。

    Args:
        sha: commit 的 Git SHA。
        message: commit 信息。
        author_login: commit 作者的 GitHub login；可能为 None（如用户已删除账号）。
    """

    sha: str
    message: str
    author_login: Optional[str]


@dataclass
class PullRequestData:
    """聚合一次 PR 审查所需的全部只读输入。

    与上面几个 frozen dataclass 不同，这里使用可变 dataclass，因为
    :attr:`warnings` 需要在 :meth:`GitHubPRProvider.fetch_pull_request` 中逐步累积。

    Args:
        reference: PR 引用。
        metadata: PR 元数据。
        changed_files: 变更文件列表。
        patch: 整个 PR 的 patch 文本（统一 diff）。
        commits: commit 列表。
        warnings: 在抓取过程中产生的告警信息（如 patch 缺失、文件数超限等）。
    """

    reference: PullRequestRef
    metadata: PullRequestMetadata
    changed_files: List[PullRequestFile]
    patch: str
    commits: List[PullRequestCommit]
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SummaryCommentResult:
    """发布摘要评论后的结果。

    Args:
        comment_id: 评论 ID。
        action: 执行的动作，``created`` 表示新建，``updated`` 表示更新已有评论。
    """

    comment_id: int
    action: str


class GitProvider(Protocol):
    """Git operations required by the review pipeline's provider boundary.

    这是一个 ``Protocol``（结构化类型/鸭子类型接口），定义了审查流水线所需的
    五个 Git 操作。采用 Protocol 而非抽象基类的好处：
    - 上层依赖“接口”而非“实现”，符合依赖倒置原则（DIP）；
    - 任何具备同名方法的类即被视为实现了该接口，无需显式继承，便于测试时
      注入 fake/stub，也便于将来新增 GitLab、Bitbucket 等实现。

    这同时是“策略模式”的体现：不同平台实现可互换，上游流水线无需改动。
    """

    def get_pull_request(self, reference: PullRequestRef) -> PullRequestMetadata:
        """读取 PR 元数据（标题、描述、base/head SHA 等）。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            该 PR 的 :class:`PullRequestMetadata`。
        """
        ...

    def get_changed_files(self, reference: PullRequestRef) -> List[PullRequestFile]:
        """读取 PR 中所有变更文件。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            :class:`PullRequestFile` 列表。
        """
        ...

    def get_pull_request_patch(self, reference: PullRequestRef) -> str:
        """读取整个 PR 的 patch 文本（统一 diff）。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            PR 的 patch 字符串。
        """
        ...

    def get_pull_request_commits(self, reference: PullRequestRef) -> List[PullRequestCommit]:
        """读取 PR 中的所有 commit。

        Args:
            reference: 已校验的 PR 引用。

        Returns:
            :class:`PullRequestCommit` 列表。
        """
        ...

    def publish_summary_comment(
        self, reference: PullRequestRef, body: str
    ) -> SummaryCommentResult:
        """发布（或幂等更新）本 bot 拥有的摘要评论。

        实现应保证幂等：若已存在由本 bot 发布、带 :data:`SUMMARY_COMMENT_MARKER`
        的评论，则更新它；否则新建。

        Args:
            reference: 已校验的 PR 引用。
            body: 评论正文，必须包含 :data:`SUMMARY_COMMENT_MARKER`。

        Returns:
            :class:`SummaryCommentResult`，含评论 ID 与执行动作。
        """
        ...
