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
    def __init__(self, payload, headers=None):
        self._body = payload.encode("utf-8")
        self.headers = headers or {}
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


@pytest.fixture
def github_http_fixture():
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
        calls.append((request.full_url, request.headers, timeout))
        if request.headers["Accept"] == "application/vnd.github.v3.diff":
            assert request.full_url == base
            return FakeHttpResponse(
                "diff --git a/src/parser.py b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n"
            )
        return FakeHttpResponse(responses[request.full_url])

    return open_fixture, calls


def test_fetch_pull_request_from_url_reads_fork_metadata_files_patch_and_commits(
    github_http_fixture,
):
    http_open, calls = github_http_fixture
    provider = GitHubPRProvider(token="test-token", http_open=http_open, timeout_seconds=7.5)

    pull_request = provider.fetch_pull_request(
        pr_url="https://github.com/acme/reviewed-repo/pull/42"
    )

    assert pull_request.reference.owner == "acme"
    assert pull_request.reference.repo == "reviewed-repo"
    assert pull_request.reference.number == 42
    assert pull_request.metadata.title == "Fix parser"
    assert pull_request.metadata.description == "Keeps quoted paths intact."
    assert pull_request.metadata.base_sha == "base-sha"
    assert pull_request.metadata.head_sha == "head-sha"
    assert pull_request.metadata.is_fork is True
    assert pull_request.metadata.head_repository == "contributor/fork"
    assert [(item.path, item.status, item.patch) for item in pull_request.changed_files] == [
        ("src/parser.py", "modified", "@@ -1 +1 @@\n-old\n+new"),
        ("assets/logo.png", "modified", None),
    ]
    assert pull_request.patch.startswith("diff --git")
    assert [(commit.sha, commit.message, commit.author_login) for commit in pull_request.commits] == [
        ("commit-sha", "Fix quoted diff paths", "contributor")
    ]
    assert pull_request.warnings == ["patch_unavailable:assets/logo.png"]
    assert len(calls) == 4
    assert all(timeout == 7.5 for _url, _headers, timeout in calls)
    assert all(headers["Authorization"] == "Bearer test-token" for _url, headers, _timeout in calls)


def test_fetch_pull_request_accepts_owner_repo_and_number(github_http_fixture):
    http_open, _calls = github_http_fixture
    provider = GitHubPRProvider(http_open=http_open)

    pull_request = provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)

    assert pull_request.reference == parse_pull_request_ref(
        owner="acme", repo="reviewed-repo", number=42
    )


def test_changed_files_at_github_limit_are_marked_as_possibly_truncated():
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

    assert len(pull_request.changed_files) == 3000
    assert pull_request.warnings == [
        "changed_files_may_be_truncated_at_github_limit"
    ]


def test_github_rate_limit_is_a_distinct_error():
    def rate_limited(_request, timeout):
        raise HTTPError(
            "https://api.github.com/repos/acme/reviewed-repo/pulls/42",
            403,
            "Forbidden",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"},
            BytesIO(),
        )

    provider = GitHubPRProvider(http_open=rate_limited, now=lambda: 1000)

    with pytest.raises(GitHubRateLimitError, match="retry_after_seconds=60"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_http_429_rate_limit_prefers_retry_after():
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
    def forbidden(_request, timeout):
        raise HTTPError("https://api.github.com/test", 403, "Forbidden", {}, BytesIO())

    provider = GitHubPRProvider(http_open=forbidden)

    with pytest.raises(GitHubAuthorizationError, match="github_access_denied:status=403"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_404_is_not_found_or_inaccessible():
    def unavailable(_request, timeout):
        raise HTTPError("https://api.github.com/test", 404, "Not Found", {}, BytesIO())

    provider = GitHubPRProvider(http_open=unavailable)

    with pytest.raises(
        GitHubNotFoundError,
        match="github_pull_request_not_found_or_inaccessible:status=404",
    ):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_github_api_failure_is_a_distinct_error():
    def unavailable(_request, timeout):
        raise HTTPError("https://api.github.com/test", 502, "Bad Gateway", {}, BytesIO())

    provider = GitHubPRProvider(http_open=unavailable)

    with pytest.raises(GitHubAPIError, match="github_api_error:status=502"):
        provider.fetch_pull_request(owner="acme", repo="reviewed-repo", number=42)


def test_publish_summary_comment_creates_when_no_marked_comment_exists():
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
            assert request.full_url == base
            assert json.loads(request.data.decode("utf-8")) == {"body": body}
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

    assert result.comment_id == 73
    assert result.action == "created"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)


def test_publish_summary_comment_updates_existing_marked_comment():
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
                        {"id": 7, "body": "ordinary comment"},
                        {
                            "id": 41,
                            "body": SUMMARY_COMMENT_MARKER + "\nold",
                            "user": {"login": "reporeview-bot"},
                        },
                    ]
                )
            )
        assert request.method == "PATCH"
        assert request.full_url == update_url
        assert json.loads(request.data.decode("utf-8")) == {"body": body}
        return FakeHttpResponse(json.dumps({"id": 41}))

    result = GitHubPRProvider(
        summary_comment_author_login="reporeview-bot", http_open=http_open
    ).publish_summary_comment(
        parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42), body
    )

    assert result.comment_id == 41
    assert result.action == "updated"
    assert [request.method for request in requests] == ["GET", "PATCH"]


def test_publish_summary_comment_creates_when_marked_comment_is_external():
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
                            "body": SUMMARY_COMMENT_MARKER + "\nexternal",
                            "user": {"login": "collaborator"},
                        },
                        {"id": 42, "body": SUMMARY_COMMENT_MARKER + "\nno user"},
                        {
                            "id": 43,
                            "body": SUMMARY_COMMENT_MARKER + "\nbad user",
                            "user": {"login": ["not-a-login"]},
                        },
                    ]
                )
            )
        assert request.method == "POST"
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
    assert all("/issues/comments/41" not in request.full_url for request in requests)


def test_publish_summary_comment_rejects_body_without_marker_before_http_call():
    provider = GitHubPRProvider(http_open=lambda *_args, **_kwargs: pytest.fail("unexpected HTTP call"))

    with pytest.raises(GitProviderInputError, match="summary_comment_marker_missing"):
        provider.publish_summary_comment(
            parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42),
            "## RepoReview summary",
        )


def test_publish_summary_comment_requires_author_login_before_http_call(capsys, monkeypatch):
    token = "test-token-must-not-leak"
    calls = []
    monkeypatch.delenv("GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN", raising=False)

    def http_open(*_args, **_kwargs):
        calls.append(True)
        pytest.fail("unexpected HTTP call")

    provider = GitHubPRProvider(token=token, http_open=http_open)

    with pytest.raises(
        GitProviderInputError, match="^missing_summary_comment_author_login$"
    ) as exc_info:
        provider.publish_summary_comment(
            parse_pull_request_ref(owner="acme", repo="reviewed-repo", number=42),
            SUMMARY_COMMENT_MARKER + "\n## RepoReview summary",
        )

    captured = capsys.readouterr()
    assert calls == []
    assert token not in str(exc_info.value)
    assert token not in captured.out
    assert token not in captured.err


def test_publish_summary_comment_surfaces_create_permission_failure():
    comments_url = "https://api.github.com/repos/acme/reviewed-repo/issues/42/comments"
    body = SUMMARY_COMMENT_MARKER + "\n## RepoReview summary\n"

    def http_open(request, timeout):
        if request.method == "GET":
            return FakeHttpResponse("[]")
        assert request.method == "POST"
        assert request.full_url == comments_url
        assert json.loads(request.data.decode("utf-8")) == {"body": body}
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
    with pytest.raises(GitProviderInputError):
        parse_pull_request_ref(**kwargs)
