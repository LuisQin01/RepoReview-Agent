"""Read GitHub pull-request data and publish an owned summary comment."""

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


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_PER_PAGE = 100
GITHUB_PULL_REQUEST_FILES_MAX = 3000
_GITHUB_LOGIN_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9-]{1,39}(?<!-)(?:\[bot\])?$")


class GitHubAPIError(GitProviderError):
    """GitHub returned an API response that cannot be used."""


class GitHubAuthorizationError(GitHubAPIError):
    """The supplied token cannot read the requested pull request."""


class GitHubRateLimitError(GitHubAPIError):
    """GitHub refused the request because its rate limit is exhausted."""


class GitHubNotFoundError(GitHubAPIError):
    """The PR does not exist or cannot be accessed with supplied credentials."""


class GitHubNetworkError(GitHubAPIError):
    """The GitHub API could not be reached."""


def _header_value(headers, name: str) -> Optional[str]:
    if headers is None:
        return None
    value = headers.get(name)
    if value is not None:
        return value
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
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if not api_url.startswith("https://"):
            raise ValueError("api_url must use https")
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.summary_comment_author_login = (
            summary_comment_author_login
            if summary_comment_author_login is not None
            else os.getenv("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN")
        )
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_open = http_open
        self._now = now

    def get_pull_request(self, reference: PullRequestRef) -> PullRequestMetadata:
        payload = self._get_json(self._pulls_url(reference))
        if not isinstance(payload, dict):
            raise GitHubAPIError("github_invalid_pull_request_payload")
        base_sha = self._nested_string(payload, "base", "sha")
        head_sha = self._nested_string(payload, "head", "sha")
        title = payload.get("title")
        body = payload.get("body")
        if not isinstance(title, str) or body is not None and not isinstance(body, str):
            raise GitHubAPIError("github_invalid_pull_request_payload")

        head_repo = payload.get("head", {}).get("repo")
        head_repository = None
        if isinstance(head_repo, dict):
            value = head_repo.get("full_name")
            if not isinstance(value, str):
                raise GitHubAPIError("github_invalid_pull_request_payload")
            head_repository = value
        elif head_repo is not None:
            raise GitHubAPIError("github_invalid_pull_request_payload")

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
        files = []
        for payload in self._get_paginated_json(self._pulls_url(reference) + "/files"):
            if not isinstance(payload, dict):
                raise GitHubAPIError("github_invalid_changed_file_payload")
            path = payload.get("filename")
            status = payload.get("status")
            patch = payload.get("patch")
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
        body, _headers = self._request(
            self._pulls_url(reference), accept="application/vnd.github.v3.diff"
        )
        return body

    def get_pull_request_commits(self, reference: PullRequestRef) -> List[PullRequestCommit]:
        commits = []
        for payload in self._get_paginated_json(self._pulls_url(reference) + "/commits"):
            if not isinstance(payload, dict):
                raise GitHubAPIError("github_invalid_commit_payload")
            sha = payload.get("sha")
            commit = payload.get("commit")
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
        """Create or update the marked summary comment owned by this bot."""
        if not isinstance(body, str) or not body.strip():
            raise GitProviderInputError("invalid_summary_comment_body")
        if SUMMARY_COMMENT_MARKER not in body:
            raise GitProviderInputError("summary_comment_marker_missing")
        author_login = self._validated_summary_comment_author_login()

        existing_comment_id = None
        for comment in self._get_paginated_json(self._issue_comments_url(reference)):
            if not isinstance(comment, dict):
                continue
            comment_body = comment.get("body")
            comment_user = comment.get("user")
            comment_author_login = (
                comment_user.get("login") if isinstance(comment_user, dict) else None
            )
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

        comment_id = response.get("id") if isinstance(response, dict) else None
        if (
            not isinstance(comment_id, int)
            or isinstance(comment_id, bool)
            or comment_id <= 0
        ):
            raise GitHubAPIError("github_invalid_issue_comment_response")
        return SummaryCommentResult(comment_id=comment_id, action=action)

    def _validated_summary_comment_author_login(self) -> str:
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
        """Fetch all read-only PR inputs needed by later review stages."""
        reference = parse_pull_request_ref(
            pr_url=pr_url, owner=owner, repo=repo, number=number
        )
        metadata = self.get_pull_request(reference)
        files = self.get_changed_files(reference)
        patch = self.get_pull_request_patch(reference)
        commits = self.get_pull_request_commits(reference)

        warnings = [
            "patch_unavailable:{}".format(changed_file.path)
            for changed_file in files
            if changed_file.patch is None
        ]
        if len(files) >= GITHUB_PULL_REQUEST_FILES_MAX:
            warnings.append("changed_files_may_be_truncated_at_github_limit")
        if not patch:
            warnings.append("pull_request_patch_empty")
        if metadata.is_fork and metadata.head_repository is None:
            warnings.append("fork_source_repository_unavailable")

        return PullRequestData(reference, metadata, files, patch, commits, warnings)

    def _pulls_url(self, reference: PullRequestRef) -> str:
        return "{}/repos/{}/{}/pulls/{}".format(
            self.api_url,
            quote(reference.owner, safe=""),
            quote(reference.repo, safe=""),
            reference.number,
        )

    def _issue_comments_url(self, reference: PullRequestRef) -> str:
        return "{}/repos/{}/{}/issues/{}/comments".format(
            self.api_url,
            quote(reference.owner, safe=""),
            quote(reference.repo, safe=""),
            reference.number,
        )

    def _issue_comment_url(self, reference: PullRequestRef, comment_id: int) -> str:
        return "{}/repos/{}/{}/issues/comments/{}".format(
            self.api_url,
            quote(reference.owner, safe=""),
            quote(reference.repo, safe=""),
            comment_id,
        )

    def _get_paginated_json(self, url: str) -> List[Dict]:
        values = []
        page = 1
        while True:
            query = urlencode({"per_page": DEFAULT_PER_PAGE, "page": page})
            separator = "&" if "?" in url else "?"
            payload = self._get_json(url + separator + query)
            if not isinstance(payload, list):
                raise GitHubAPIError("github_invalid_list_response")
            values.extend(payload)
            if len(payload) < DEFAULT_PER_PAGE:
                return values
            page += 1

    def _get_json(self, url: str):
        body, _headers = self._request(url, accept="application/vnd.github+json")
        return self._parse_json(body)

    def _send_json(self, url: str, *, method: str, payload: Dict):
        body, _headers = self._request(
            url,
            accept="application/vnd.github+json",
            method=method,
            json_payload=payload,
        )
        return self._parse_json(body)

    @staticmethod
    def _parse_json(body: str):
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
        headers = {
            "Accept": accept,
            "User-Agent": "RepoReview-Agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        data = None
        if json_payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = self._http_open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            self._raise_http_error(exc.code, exc.headers)
        except (URLError, TimeoutError, OSError) as exc:
            raise GitHubNetworkError("github_network_error") from exc

        try:
            body = response.read().decode("utf-8")
            return body, response.headers
        finally:
            response.close()

    def _raise_http_error(self, status_code: int, headers) -> None:
        if status_code == 401:
            raise GitHubAuthorizationError("github_authentication_failed:status=401")
        if status_code in (403, 429):
            retry_after_header = _header_value(headers, "Retry-After")
            remaining = _header_value(headers, "X-RateLimit-Remaining")
            if status_code == 429 or retry_after_header is not None or remaining == "0":
                if retry_after_header is not None:
                    retry_after = self._retry_after_header_seconds(retry_after_header)
                elif remaining == "0":
                    reset = _header_value(headers, "X-RateLimit-Reset")
                    retry_after = self._retry_after_seconds(reset)
                else:
                    # GitHub recommends waiting at least one minute for a
                    # secondary rate limit without retry guidance headers.
                    retry_after = "60"
                raise GitHubRateLimitError(
                    "github_rate_limited:retry_after_seconds={}".format(retry_after)
                )
        if status_code == 403:
            raise GitHubAuthorizationError("github_access_denied:status=403")
        if status_code == 404:
            raise GitHubNotFoundError(
                "github_pull_request_not_found_or_inaccessible:status=404"
            )
        raise GitHubAPIError("github_api_error:status={}".format(status_code))

    def _retry_after_seconds(self, reset: Optional[str]) -> str:
        try:
            return str(max(0, int(reset) - int(self._now())))
        except (TypeError, ValueError):
            return "60"

    @staticmethod
    def _retry_after_header_seconds(retry_after: str) -> str:
        try:
            return str(max(0, int(retry_after)))
        except (TypeError, ValueError):
            return "60"

    @staticmethod
    def _nested_string(payload, outer_key: str, inner_key: str) -> str:
        outer = payload.get(outer_key)
        value = outer.get(inner_key) if isinstance(outer, dict) else None
        if not isinstance(value, str):
            raise GitHubAPIError("github_invalid_pull_request_payload")
        return value
