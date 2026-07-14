"""Provider-neutral models and read-only Git pull-request interface."""

from dataclasses import dataclass, field
import re
from typing import List, Optional, Protocol
from urllib.parse import urlsplit


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
SUMMARY_COMMENT_MARKER = "<!-- reporeview-summary -->"


class GitProviderError(RuntimeError):
    """A Git provider request could not be completed safely."""


class GitProviderInputError(GitProviderError):
    """The caller supplied an invalid pull-request reference."""


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    def __post_init__(self):
        if not isinstance(self.owner, str) or not _NAME_PATTERN.fullmatch(self.owner):
            raise GitProviderInputError("invalid_pull_request_owner")
        if not isinstance(self.repo, str) or not _NAME_PATTERN.fullmatch(self.repo):
            raise GitProviderInputError("invalid_pull_request_repo")
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number <= 0:
            raise GitProviderInputError("invalid_pull_request_number")


def parse_pull_request_ref(
    *,
    pr_url: Optional[str] = None,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    number: Optional[int] = None,
) -> PullRequestRef:
    """Build a validated PR reference from one GitHub URL or three fields."""
    has_url = pr_url is not None
    has_parts = any(value is not None for value in (owner, repo, number))
    if has_url == has_parts:
        raise GitProviderInputError("provide_exactly_one_pull_request_reference")

    if has_url:
        if not isinstance(pr_url, str):
            raise GitProviderInputError("invalid_pull_request_url")
        parsed = urlsplit(pr_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise GitProviderInputError("invalid_github_pull_request_url")
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) != 4 or segments[2] != "pull":
            raise GitProviderInputError("invalid_github_pull_request_url")
        try:
            pull_number = int(segments[3])
        except ValueError as exc:
            raise GitProviderInputError("invalid_pull_request_number") from exc
        return PullRequestRef(segments[0], segments[1], pull_number)

    return PullRequestRef(owner, repo, number)


@dataclass(frozen=True)
class PullRequestMetadata:
    title: str
    description: str
    base_sha: str
    head_sha: str
    is_fork: bool
    head_repository: Optional[str]


@dataclass(frozen=True)
class PullRequestFile:
    path: str
    status: str
    patch: Optional[str]
    old_path: Optional[str] = None


@dataclass(frozen=True)
class PullRequestCommit:
    sha: str
    message: str
    author_login: Optional[str]


@dataclass
class PullRequestData:
    reference: PullRequestRef
    metadata: PullRequestMetadata
    changed_files: List[PullRequestFile]
    patch: str
    commits: List[PullRequestCommit]
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SummaryCommentResult:
    comment_id: int
    action: str


class GitProvider(Protocol):
    """Git operations required by the review pipeline's provider boundary."""

    def get_pull_request(self, reference: PullRequestRef) -> PullRequestMetadata:
        ...

    def get_changed_files(self, reference: PullRequestRef) -> List[PullRequestFile]:
        ...

    def get_pull_request_patch(self, reference: PullRequestRef) -> str:
        ...

    def get_pull_request_commits(self, reference: PullRequestRef) -> List[PullRequestCommit]:
        ...

    def publish_summary_comment(
        self, reference: PullRequestRef, body: str
    ) -> SummaryCommentResult:
        ...
