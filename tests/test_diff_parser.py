from src.diff_parser import parse_diff


def test_parse_added_and_deleted_lines():
    diff_text = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 def login(user):
-    old_debug = True
+    print(user.password)
     return True
"""

    changed_files = parse_diff(diff_text)

    assert len(changed_files) == 1

    changed = changed_files[0]
    assert changed.path == "app.py"

    assert len(changed.added_lines) == 1
    assert changed.added_lines[0].line_no == 2
    assert changed.added_lines[0].content == "    print(user.password)"

    assert len(changed.deleted_lines) == 1
    assert changed.deleted_lines[0].line_no == 2
    assert changed.deleted_lines[0].content == "    old_debug = True"