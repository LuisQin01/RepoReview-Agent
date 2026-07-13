import pytest

from src.file_context import collect_file_contexts, locate_python_symbol, read_file_context
from src.schemas import ChangedFile, ContextBudget, DiffHunk, DiffLine


def test_read_file_context_rejects_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("do not read me", encoding="utf-8")

    context = read_file_context(
        repo_root=repo,
        file_path="../secret.txt",
        max_chars=100,
    )

    assert context.exists is False
    assert "outside" in context.error

def test_read_file_context_truncates_large_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    target = repo / "app.py"
    target.write_text("abcdef", encoding="utf-8")

    context = read_file_context(repo, "app.py", max_chars=3)

    assert context.exists is True
    assert context.content == "abc"
    assert context.truncated is True
    assert context.chars_read == 3


def test_read_file_context_prioritizes_changed_lines_when_truncated(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = "changed_marker = True\n"
    lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
    lines[249] = marker
    (repo / "app.py").write_text("".join(lines), encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len(marker),
        changed_line_nos=[250],
    )

    assert context.content == marker
    assert "header_1" not in context.content
    assert context.truncated is True
    assert context.chars_read == len(marker)


def test_read_file_context_preserves_multiple_changed_lines_when_truncated(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    markers = ["first_changed_marker\n", "second_changed_marker\n"]
    lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
    lines[19], lines[249] = markers
    (repo / "app.py").write_text("".join(lines), encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=sum(map(len, markers)),
        changed_line_nos=[20, 250],
    )

    assert context.content == "".join(markers)
    assert "header_1" not in context.content


def test_read_file_context_preserves_unchanged_lines_in_changed_hunk(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
    lines[249] = "if enabled:\n"
    lines[250] = "    return value\n"
    (repo / "app.py").write_text("".join(lines), encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len("if enabled:\n    return value\n"),
        changed_line_nos=[250],
        changed_hunks=[DiffHunk(start_line=250, end_line=251)],
    )

    assert context.content == "if enabled:\n    return value\n"
    assert context.truncated is True


def test_read_file_context_keeps_multiple_hunks_separate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    content = "one\nfirst_change\ntwo\nignored_between_hunks\nthree\nsecond_change\nfour\n"
    (repo / "app.py").write_text(content, encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len("one\nfirst_change\nthree\nsecond_change\n"),
        changed_hunks=[DiffHunk(1, 2), DiffHunk(5, 6)],
    )

    assert context.content == "one\nfirst_change\nthree\nsecond_change\n"
    assert "ignored_between_hunks" not in context.content


def test_read_file_context_only_partially_copies_an_oversized_hunk(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("prefix\nif enabled:\n    return value\n", encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len("if enabled:\n"),
        changed_hunks=[DiffHunk(2, 3)],
    )

    assert context.content == "if enabled:\n"
    assert context.truncated is True


def test_read_file_context_falls_back_to_file_start_for_missing_changed_line(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("first_line\nsecond_line\n", encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=5,
        changed_line_nos=[999],
    )

    assert context.content == "first"
    assert context.truncated is True


def test_collect_file_contexts_enforces_total_budget_for_changed_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    markers = {
        "first.py": "first_changed_marker\n",
        "second.py": "second_changed_marker\n",
    }
    changed_files = []

    for path, marker in markers.items():
        lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
        lines[249] = marker
        (repo / path).write_text("".join(lines), encoding="utf-8")
        changed_files.append(
            ChangedFile(
                path=path,
                added_lines=[DiffLine(path, 250, marker.rstrip())],
                deleted_lines=[],
                patch="",
            )
        )

    budget = ContextBudget(
        max_prompt_chars=sum(map(len, markers.values())),
        max_extra_context_files=0,
    )
    contexts = collect_file_contexts(repo, changed_files, context_budget=budget)

    assert sum(context.chars_read for context in contexts) <= budget.max_prompt_chars
    assert {context.path: context.content for context in contexts} == markers


def test_collect_file_contexts_applies_remaining_budget_to_extra_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("helper()\n", encoding="utf-8")
    (repo / "helper.py").write_text(
        "def helper():\n    return 'long context'\n",
        encoding="utf-8",
    )
    changed_file = ChangedFile(
        path="app.py",
        added_lines=[DiffLine("app.py", 1, "helper()")],
        deleted_lines=[],
        patch="",
    )
    budget = ContextBudget(max_prompt_chars=20, max_extra_context_files=1)

    contexts = collect_file_contexts(repo, [changed_file], context_budget=budget)

    assert [context.path for context in contexts] == ["app.py", "helper.py"]
    assert sum(context.chars_read for context in contexts) <= budget.max_prompt_chars
    assert contexts[1].truncated is True


def test_context_budget_rejects_invalid_limits():
    with pytest.raises(ValueError, match="max_prompt_chars"):
        ContextBudget(max_prompt_chars=0)

    with pytest.raises(ValueError, match="max_extra_context_files"):
        ContextBudget(max_extra_context_files=-1)


def test_locate_python_symbol_returns_containing_function():
    source = """def calculate_total(values):
    total = sum(values)
    return total


class Ignored:
    pass
"""

    symbol = locate_python_symbol(source, 2)

    assert symbol is not None
    assert symbol.name == "calculate_total"
    assert symbol.kind == "function"
    assert symbol.qualified_name == "calculate_total"
    assert symbol.class_name is None
    assert (symbol.start_line, symbol.end_line) == (1, 3)
    assert symbol.source == "def calculate_total(values):\n    total = sum(values)\n    return total\n"


def test_locate_python_symbol_distinguishes_method_and_class():
    source = """class Processor:
    setting = "safe"

    def handle(self, value):
        return value + 1
"""

    method = locate_python_symbol(source, 5)
    containing_class = locate_python_symbol(source, 2)

    assert method is not None
    assert method.name == "handle"
    assert method.kind == "method"
    assert method.qualified_name == "Processor.handle"
    assert method.class_name == "Processor"
    assert (method.start_line, method.end_line) == (4, 5)
    assert method.source == "    def handle(self, value):\n        return value + 1\n"

    assert containing_class is not None
    assert containing_class.name == "Processor"
    assert containing_class.kind == "class"
    assert (containing_class.start_line, containing_class.end_line) == (1, 5)


def test_locate_python_symbol_returns_none_for_unlocatable_source_or_line():
    assert locate_python_symbol("def broken(:\n", 1) is None
    assert locate_python_symbol("value = 1\n", 1) is None
    assert locate_python_symbol("def valid():\n    return 1\n", 0) is None
