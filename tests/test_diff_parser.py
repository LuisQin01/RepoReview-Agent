from pathlib import Path

from src.diff_parser import parse_diff
from src.file_context import collect_file_contexts
from src.schemas import ContextBudget, DiffHunk
from src.reporter import render_markdown_report
from src.reviewers import review_changed_files


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
    assert changed.hunks == [DiffHunk(start_line=1, end_line=3)]


def test_parse_pure_rename_records_old_and_new_path():
    fixture_path = Path(__file__).parent / "fixtures" / "pure_rename_with_spaces.diff"
    changed_files = parse_diff(fixture_path.read_text(encoding="utf-8"))

    assert len(changed_files) == 1
    changed = changed_files[0]
    assert changed.path == "new name.py"
    assert changed.old_path == "old name.py"
    assert changed.is_rename is True
    assert changed.added_lines == []
    assert changed.deleted_lines == []


def test_parse_unquoted_diff_git_header_with_spaces_without_hunks():
    changed_files = parse_diff("diff --git a/my file.py b/my file.py\n")

    assert changed_files[0].path == "my file.py"


def test_parse_renamed_diff_with_spaces_and_content_change():
    changed_files = parse_diff(
        """diff --git a/old name.py b/new name.py
similarity index 90%
rename from old name.py
rename to new name.py
--- a/old name.py
+++ b/new name.py
@@ -1 +1,2 @@
 def run():
+    print("new")
"""
    )

    changed = changed_files[0]
    assert changed.path == "new name.py"
    assert changed.old_path == "old name.py"
    assert changed.is_rename is True
    assert [(line.file_path, line.line_no, line.content) for line in changed.added_lines] == [
        ("new name.py", 2, '    print("new")')
    ]


def test_parse_quoted_header_and_plus_plus_plus_path_with_spaces():
    changed_files = parse_diff(
        """diff --git "a/my file.py" "b/my file.py"
--- "a/my file.py"
+++ "b/my file.py"
@@ -1 +1,2 @@
 value = 1
+value = 2
"""
    )

    changed = changed_files[0]
    assert changed.path == "my file.py"
    assert changed.old_path is None
    assert changed.is_rename is False
    assert changed.added_lines[0].file_path == "my file.py"


def test_parse_uses_plus_plus_plus_path_as_new_path_source():
    # This synthetic diff verifies path-source precedence; real Git output
    # normally keeps the diff header and +++ path consistent.
    changed_files = parse_diff(
        """diff --git a/old.py b/header.py
--- a/old.py
+++ b/new file.py
@@ -1 +1,2 @@
 value = 1
+value = 2
"""
    )

    changed = changed_files[0]
    assert changed.path == "new file.py"
    assert [(line.file_path, line.line_no, line.content) for line in changed.added_lines] == [
        ("new file.py", 2, "value = 2")
    ]


def test_parse_new_line_numbers_across_multiple_hunks():
    fixture_path = (
        Path(__file__).parent / "fixtures" / "multiple_hunks_new_line_numbers.diff"
    )
    changed_files = parse_diff(fixture_path.read_text(encoding="utf-8"))

    assert len(changed_files) == 1
    changed = changed_files[0]
    assert [(line.file_path, line.line_no, line.content) for line in changed.added_lines] == [
        ("service.py", 3, "    first = True"),
        ("service.py", 22, "    second = True"),
    ]
    assert changed.hunks == [
        DiffHunk(start_line=2, end_line=4),
        DiffHunk(start_line=21, end_line=23),
    ]


def test_rename_and_space_path_do_not_break_downstream_consumers(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "new name.py").write_text("def run():\n    print('new')\n", encoding="utf-8")
    changed_files = parse_diff(
        """diff --git a/old name.py b/new name.py
similarity index 90%
rename from old name.py
rename to new name.py
--- a/old name.py
+++ b/new name.py
@@ -1 +1,2 @@
 def run():
+    print('new')
"""
    )

    contexts = collect_file_contexts(
        repo,
        changed_files,
        context_budget=ContextBudget(max_extra_context_files=0),
    )
    issues = review_changed_files(changed_files)
    report = render_markdown_report(issues, changed_files, contexts)

    assert contexts[0].exists is True
    assert contexts[0].path == "new name.py"
    assert "new name.py" in report


def test_added_line_without_hunk_is_ignored_without_crashing():
    changed_files = parse_diff(
        """diff --git a/broken.py b/broken.py
+++ b/broken.py
+not associated with a hunk
"""
    )

    assert changed_files[0].path == "broken.py"
    assert changed_files[0].added_lines == []
