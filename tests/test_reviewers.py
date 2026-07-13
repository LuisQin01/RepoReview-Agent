from src.diff_parser import parse_diff
from src.reviewers import review_changed_files


def test_reviewers_find_hardcoded_secret():
    diff_text = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,2 +1,3 @@
 def login():
+    api_key = "abc123"
     return True
"""

    changed_files = parse_diff(diff_text)
    issues = review_changed_files(changed_files)

    categories = {issue.category for issue in issues}

    assert "secret" in categories
    assert all(issue.source == "rule" for issue in issues)
