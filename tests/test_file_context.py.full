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


@pytest.mark.parametrize(
    "file_path",
    [
        ".env",
        ".env.production",
        ".envrc",
        "keys/id_rsa",
        "certificates/service.pem",
        "certificates/service.key",
        "certificates/service.crt",
        "certificates/service.cer",
        "certificates/service.pfx",
        "config/production.yaml",
        "CONFIG/nested/PROD.JSON",
        "deploy/values.yaml",
        "DEPLOY/nested/VALUES.YML",
        "settings.json",
    ],
)
def test_read_file_context_skips_sensitive_files_without_reading_content(tmp_path, file_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("API_KEY=secret\n", encoding="utf-8")

    context = read_file_context(repo, file_path, max_chars=100)

    assert context.exists is False
    assert context.content == ""
    assert context.chars_read == 0
    assert "sensitive" in context.error


def test_read_file_context_does_not_reject_non_sensitive_filename_containing_key(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "monkey.py").write_text("value = 1\n", encoding="utf-8")

    context = read_file_context(repo, "monkey.py", max_chars=100)

    assert context.exists is True
    assert context.content == "value = 1\n"


def test_read_file_context_keeps_non_sensitive_configuration_readable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "openapi.yaml"
    config.write_text("feature_enabled: true\n", encoding="utf-8")

    context = read_file_context(repo, "openapi.yaml", max_chars=100)

    assert context.exists is True
    assert context.content == "feature_enabled: true\n"


def test_read_file_context_skips_all_config_yaml_under_config_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config" / "development.yaml"
    config.parent.mkdir()
    config.write_text("api_key: secret\n", encoding="utf-8")

    context = read_file_context(repo, "config/development.yaml", max_chars=100)

    assert context.exists is False
    assert context.content == ""
    assert context.chars_read == 0
    assert "sensitive" in context.error


def test_collect_file_contexts_preserves_sensitive_file_provenance_without_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    changed_file = ChangedFile(
        path=".env",
        added_lines=[DiffLine(file_path=".env", line_no=1, content="+API_KEY=secret")],
        deleted_lines=[],
        patch="@@ -0,0 +1,1 @@\n+API_KEY=secret\n",
        hunks=[DiffHunk(start_line=1, end_line=1)],
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=100, max_extra_context_files=0),
    )

    assert len(contexts) == 1
    context = contexts[0]
    assert context.path == ".env"
    assert context.exists is False
    assert context.content == ""
    assert context.chars_read == 0
    assert context.source == "changed_file"
    assert context.selection_reason == "file is changed in the pull request"

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


def test_collect_file_contexts_records_provenance_for_each_selection_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "from helper import imported_value\ncall_target()\n",
        encoding="utf-8",
    )
    (repo / "helper.py").write_text("imported_value = 1\n", encoding="utf-8")
    (repo / "services.py").write_text(
        "def call_target():\n    return True\n",
        encoding="utf-8",
    )
    changed_file = ChangedFile(
        path="app.py",
        added_lines=[
            DiffLine("app.py", 1, "from helper import imported_value"),
            DiffLine("app.py", 2, "call_target()"),
        ],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=2),
    )

    provenance = {
        context.path: (context.source, context.selection_reason)
        for context in contexts
    }
    assert provenance == {
        "app.py": ("changed_file", "file is changed in the pull request"),
        "helper.py": ("import_candidate", "imported by selected context app.py"),
        "services.py": ("call_name_candidate", "defines added call name(s): call_target"),
    }


def test_collect_file_contexts_keeps_the_first_provenance_for_a_duplicate_candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "from helper import helper\nhelper()\n",
        encoding="utf-8",
    )
    (repo / "helper.py").write_text(
        "def helper():\n    return True\n",
        encoding="utf-8",
    )
    changed_file = ChangedFile(
        path="app.py",
        added_lines=[
            DiffLine("app.py", 1, "from helper import helper"),
            DiffLine("app.py", 2, "helper()"),
        ],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=1),
    )

    assert [context.path for context in contexts] == ["app.py", "helper.py"]
    assert contexts[1].source == "import_candidate"
    assert contexts[1].selection_reason == "imported by selected context app.py"


def test_collect_file_contexts_keeps_provenance_when_selected_file_cannot_be_read(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    changed_file = ChangedFile(
        path="missing.py",
        added_lines=[],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=0),
    )

    assert len(contexts) == 1
    context = contexts[0]
    assert context.exists is False
    assert context.source == "changed_file"
    assert context.selection_reason == "file is changed in the pull request"
    assert "does not exist" in context.error


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
