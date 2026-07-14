import pytest

from src.git_provider import PullRequestRef, SummaryCommentResult
from src.review_service import ReviewRequest, ReviewService
from src.schemas import ContextBudget


def make_request(tmp_path, **overrides):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    diff_path = tmp_path / "input.diff"
    diff_path.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return True
""",
        encoding="utf-8",
    )
    values = {
        "diff_path": str(diff_path),
        "repo_root": str(repo),
        "output_format": "json",
        "context_budget": ContextBudget(max_prompt_chars=100, max_extra_context_files=0),
    }
    values.update(overrides)
    return ReviewRequest(**values)


def test_review_service_returns_structured_state_and_full_review_trace(tmp_path):
    result = ReviewService().review(make_request(tmp_path))

    assert result.output == result.state.output
    assert result.summary_comment is None
    assert [changed_file.path for changed_file in result.state.changed_files] == ["app.py"]
    assert result.state.contexts[0].path == "app.py"
    assert result.state.errors == []
    assert isinstance(result.state.issues, list)
    assert [step["step"] for step in result.trace_steps] == [
        "receive_task",
        "parse_diff",
        "collect_context",
        "run_static_checks",
        "run_llm_review",
        "validate_output",
        "render_report",
        "save_trace",
    ]


def test_review_service_accepts_inline_diff_text_without_a_temporary_file(tmp_path):
    request = make_request(
        tmp_path,
        diff_path=None,
        diff_text="""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1 @@
+print(\"debug\")
""",
    )

    result = ReviewService().review(request)

    assert result.state.diff_path == "(inline diff)"
    assert any(issue.category == "debug" for issue in result.state.issues)


@pytest.mark.parametrize(
    "overrides",
    [
        {"diff_path": None, "diff_text": None},
        {"diff_text": "diff --git a/app.py b/app.py"},
    ],
)
def test_review_service_requires_exactly_one_diff_source(tmp_path, overrides):
    with pytest.raises(ValueError, match="exactly_one_diff_source_required"):
        ReviewService().review(make_request(tmp_path, **overrides))


def test_review_service_rejects_summary_publication_without_structured_pr_reference(tmp_path):
    constructed = []

    def provider_factory():
        constructed.append(True)
        return object()

    request = make_request(tmp_path, publish_summary_comment=True)

    with pytest.raises(ValueError, match="pull_request_required_for_summary_comment"):
        ReviewService(git_provider_factory=provider_factory).review(request)

    assert constructed == []


def test_review_service_requires_an_injected_provider_for_summary_publication(tmp_path):
    request = make_request(
        tmp_path,
        publish_summary_comment=True,
        pull_request=PullRequestRef("acme", "reviewed-repo", 42),
    )

    with pytest.raises(ValueError, match="git_provider_required_for_summary_comment"):
        ReviewService().review(request)


def test_review_service_publishes_optional_summary_with_structured_reference(tmp_path):
    published = {}

    class FakeProvider:
        def publish_summary_comment(self, reference, body):
            published["reference"] = reference
            published["body"] = body
            return SummaryCommentResult(comment_id=73, action="created")

    reference = PullRequestRef("acme", "reviewed-repo", 42)
    request = make_request(
        tmp_path,
        publish_summary_comment=True,
        pull_request=reference,
    )

    result = ReviewService(git_provider_factory=FakeProvider).review(request)

    assert published["reference"] == reference
    assert "## RepoReview summary" in published["body"]
    assert result.summary_comment == SummaryCommentResult(comment_id=73, action="created")
    assert result.trace_steps[-2]["step"] == "publish_summary_comment"
