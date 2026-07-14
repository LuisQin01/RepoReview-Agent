from fastapi.testclient import TestClient

from src.api import MAX_DIFF_CHARS, create_app


def make_client(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    return TestClient(create_app(repo_root=repo))


def test_post_reviews_runs_service_and_returns_structured_review(tmp_path):
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

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"]
    assert body["errors"] == []
    assert body["metrics"]["changed_files"] == 1
    assert body["metrics"]["findings"] == len(body["findings"])
    assert body["metrics"]["llm_called"] is False
    assert any(finding["category"] == "debug" for finding in body["findings"])
    debug_finding = next(
        finding for finding in body["findings"] if finding["category"] == "debug"
    )
    assert debug_finding["source"] == "rule"
    assert debug_finding["placement"] == "inline"
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

    assert response.status_code == 200
    assert response.json()["findings"] == []
    assert response.json()["metrics"]["changed_files"] == 1


def test_post_reviews_rejects_empty_or_oversized_diff_and_unsupported_controls(tmp_path):
    client = make_client(tmp_path)

    for body in (
        {"diff": ""},
        {"diff": "not a git diff"},
        {"diff": "x" * (MAX_DIFF_CHARS + 1)},
        {"diff": "diff --git a/a b/a", "repo_root": "C:/"},
        {"diff": "diff --git a/a b/a", "use_llm": True},
    ):
        response = client.post("/reviews", json=body)
        assert response.status_code == 422


def test_post_reviews_does_not_echo_sensitive_diff_values(tmp_path):
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

    assert response.status_code == 200
    assert secret not in response.text
    assert any(finding["category"] == "secret" for finding in response.json()["findings"])
