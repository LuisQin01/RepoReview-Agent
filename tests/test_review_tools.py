"""Tests for the provider-independent read-only tool contract."""

import json
from pathlib import Path

import pytest

from src.diff_parser import parse_diff
from src.review_tools import (
    ChangedHunksTool,
    FinishReview,
    INTERNAL_ERROR_CODE,
    ReadFileContextTool,
    ReviewTool,
    SearchPythonSymbolTool,
    ToolDispatcher,
    ToolResult,
)
from src.schemas import ChangedFile, DiffHunk


class FakeTool:
    """A structural fake used only to verify the protocol declaration."""

    name = "fake_read_only"
    description = "Returns a fixed safe result without provider dependencies."
    parameters_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, arguments):
        self.received_arguments = arguments
        return ToolResult(
            success=True,
            model_summary="One safe result is available.",
            error_code=None,
            truncated=False,
            result_size=1,
            result_limit=1,
            data={"path": arguments["path"]},
            usage={"items": 1},
        )


class RaisingTool(FakeTool):
    name = "raising_read_only"

    def run(self, arguments):
        raise RuntimeError("database password: secret-value")


class InvalidResultTool(FakeTool):
    name = "invalid_result"

    def run(self, arguments):
        return {"not": "a ToolResult"}


class SpyTool(FakeTool):
    name = "spy_read_only"

    def __init__(self):
        self.run_calls = 0

    def run(self, arguments):
        self.run_calls += 1
        return super().run(arguments)


class NestedSpyTool:
    """A fake whose nested schema verifies recursive dispatcher validation."""

    name = "nested_spy_read_only"
    description = "Accepts a bounded nested options object."
    parameters_schema = {
        "type": "object",
        "properties": {
            "options": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    "line_numbers": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["mode", "line_numbers"],
                "additionalProperties": False,
            }
        },
        "required": ["options"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.run_calls = 0

    def run(self, arguments):
        self.run_calls += 1
        return ToolResult(
            success=True,
            model_summary="Nested arguments are valid.",
            error_code=None,
            truncated=False,
            result_size=1,
            result_limit=1,
            data={"mode": arguments["options"]["mode"]},
        )


def test_fake_tool_satisfies_the_serializable_read_only_protocol():
    tool = FakeTool()

    assert isinstance(tool, ReviewTool)
    assert json.loads(json.dumps(tool.parameters_schema)) == tool.parameters_schema


def test_tool_results_distinguish_success_expected_failure_and_internal_failure():
    success = ToolResult(
        success=True,
        model_summary="Found one matching file.",
        error_code=None,
        truncated=False,
        result_size=1,
        result_limit=3,
        data={"paths": ["src/example.py"]},
        usage={"items": 1},
    )
    expected_failure = ToolResult(
        success=False,
        model_summary="The requested file was not found.",
        error_code="not_found",
        truncated=False,
        result_size=0,
        result_limit=3,
        usage={"items": 0},
    )
    internal_failure = ToolResult(
        success=False,
        model_summary="The tool is temporarily unavailable.",
        error_code=INTERNAL_ERROR_CODE,
        truncated=False,
        result_size=0,
        result_limit=3,
        usage={"items": 0},
        internal_error_detail="database password: secret-value",
    )

    assert success.success is True
    assert expected_failure.error_code == "not_found"
    assert internal_failure.error_code == INTERNAL_ERROR_CODE
    assert json.loads(json.dumps(success.to_dict())) == success.to_dict()
    assert "secret-value" not in json.dumps(internal_failure.to_model_dict())
    assert "secret-value" not in json.dumps(internal_failure.to_dict())


def test_tool_result_rejects_non_serializable_data_and_inconsistent_errors():
    with pytest.raises(ValueError, match="tool_result_must_be_json_serializable"):
        ToolResult(
            success=True,
            model_summary="Safe summary.",
            error_code=None,
            truncated=False,
            result_size=1,
            result_limit=1,
            data={"unsafe": object()},
        )

    with pytest.raises(ValueError, match="successful_tool_result_cannot_have_error_code"):
        ToolResult(
            success=True,
            model_summary="Safe summary.",
            error_code="not_found",
            truncated=False,
            result_size=0,
            result_limit=1,
        )


def test_dispatcher_passes_arguments_and_result_through_to_registered_fake_tool():
    dispatcher = ToolDispatcher()
    tool = FakeTool()
    dispatcher.register(tool)

    arguments = {"path": "src/example.py"}
    result = dispatcher.dispatch(tool.name, arguments)

    assert dispatcher.get(tool.name) is tool
    assert tool.received_arguments is arguments
    assert result.success is True
    assert result.data == {"path": "src/example.py"}


def test_dispatcher_runs_a_tool_once_for_schema_valid_arguments():
    dispatcher = ToolDispatcher()
    tool = SpyTool()
    dispatcher.register(tool)

    result = dispatcher.dispatch(tool.name, {"path": "src/example.py"})

    assert result.success is True
    assert tool.run_calls == 1


@pytest.mark.parametrize(
    "arguments",
    [
        "not-an-object",
        {},
        {"path": 42},
        {"path": "src/example.py", "token": "secret-value"},
    ],
)
def test_dispatcher_rejects_invalid_schema_arguments_without_calling_the_tool(arguments):
    dispatcher = ToolDispatcher()
    tool = SpyTool()
    dispatcher.register(tool)

    result = dispatcher.dispatch(tool.name, arguments)

    assert result.success is False
    assert result.error_code == "invalid_arguments"
    assert tool.run_calls == 0
    assert "secret-value" not in result.model_summary
    assert "secret-value" not in json.dumps(result.to_dict())


def test_dispatcher_runs_a_tool_once_for_valid_nested_schema_arguments():
    dispatcher = ToolDispatcher()
    tool = NestedSpyTool()
    dispatcher.register(tool)

    result = dispatcher.dispatch(
        tool.name,
        {"options": {"mode": "changed_only", "line_numbers": [3, 8]}},
    )

    assert result.success is True
    assert result.data == {"mode": "changed_only"}
    assert tool.run_calls == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"options": {"line_numbers": [3]}},
        {"options": {"mode": 3, "line_numbers": [3]}},
        {"options": {"mode": "changed_only", "line_numbers": ["3"]}},
        {"options": {"mode": "changed_only", "line_numbers": [3], "token": "secret-value"}},
    ],
)
def test_dispatcher_rejects_invalid_nested_schema_arguments_before_tool_run(arguments):
    dispatcher = ToolDispatcher()
    tool = NestedSpyTool()
    dispatcher.register(tool)

    result = dispatcher.dispatch(tool.name, arguments)

    assert result.success is False
    assert result.error_code == "invalid_arguments"
    assert tool.run_calls == 0
    assert "secret-value" not in json.dumps(result.to_dict())


def test_dispatcher_rejects_duplicate_tool_names():
    dispatcher = ToolDispatcher()
    dispatcher.register(FakeTool())

    with pytest.raises(ValueError, match="duplicate_tool_name"):
        dispatcher.register(FakeTool())


def test_dispatcher_returns_structured_failure_for_unknown_or_broken_tools():
    dispatcher = ToolDispatcher()

    unknown = dispatcher.dispatch("missing", {"path": "src/example.py"})
    assert unknown.success is False
    assert unknown.error_code == "not_found"

    dispatcher.register(RaisingTool())
    failed = dispatcher.dispatch("raising_read_only", {"path": "src/example.py"})
    assert failed.success is False
    assert failed.error_code == INTERNAL_ERROR_CODE
    assert "secret-value" not in failed.model_summary

    dispatcher.register(InvalidResultTool())
    invalid_result = dispatcher.dispatch("invalid_result", {"path": "src/example.py"})
    assert invalid_result.success is False
    assert invalid_result.error_code == INTERNAL_ERROR_CODE


@pytest.mark.parametrize("tool_name", ["", 0, None, []])
def test_dispatcher_returns_structured_failure_for_invalid_tool_names(tool_name):
    result = ToolDispatcher().dispatch(tool_name, {})

    assert result.success is False
    assert result.error_code == "invalid_arguments"


def _finish_changed_files():
    return [
        ChangedFile(
            path="src/app.py",
            added_lines=[],
            deleted_lines=[],
            patch="@@ -10 +10 @@\n+value = True",
            hunks=[DiffHunk(start_line=10, end_line=10)],
        )
    ]


def _valid_finish_finding():
    return {
        "file": "src/app.py",
        "line": 10,
        "severity": "high",
        "issue": "Unchecked result",
        "reason": "The call may fail.",
        "suggested_fix": "Handle the failure.",
        "confidence": 0.9,
        "evidence": "The changed call has no check.",
    }


def test_finish_review_terminates_with_only_existing_validator_output():
    finish = FinishReview(_finish_changed_files())

    result = finish.finish({"findings": [_valid_finish_finding()]})

    assert result.finished is True
    assert result.status == "finished"
    assert finish.is_finished is True
    assert result.finding_count == 1
    assert result.rejected_count == 0
    assert result.findings[0].source == "llm"
    assert result.findings[0].placement == "inline"


def test_finish_review_safely_terminates_with_an_empty_result():
    finish = FinishReview(_finish_changed_files())

    result = finish.finish({"findings": []})

    assert result.finished is True
    assert result.findings == ()
    assert result.finding_count == 0
    assert result.received_count == 0
    assert result.truncated is False


def test_finish_review_filters_repaired_or_unlocatable_findings_from_terminal_output():
    finish = FinishReview(_finish_changed_files())
    missing_required_field = _valid_finish_finding()
    missing_required_field.pop("issue")
    outside_changed_hunk = _valid_finish_finding()
    outside_changed_hunk["line"] = 11
    string_line = {**_valid_finish_finding(), "line": "10"}
    string_confidence = {**_valid_finish_finding(), "confidence": "0.9"}
    unexpected_field = {**_valid_finish_finding(), "unexpected": True}

    result = finish.finish(
        {
            "findings": [
                _valid_finish_finding(),
                missing_required_field,
                outside_changed_hunk,
                string_line,
                string_confidence,
                unexpected_field,
            ]
        }
    )

    assert result.finished is True
    assert result.finding_count == 1
    assert result.rejected_count == 5
    assert result.findings == (result.findings[0],)
    assert result.findings[0].line_no == 10


@pytest.mark.parametrize(
    "arguments",
    [
        {"findings": "not-a-list"},
        {"findings": [_valid_finish_finding()], "extra": True},
    ],
)
def test_finish_review_invalid_arguments_do_not_terminate_the_review(arguments):
    finish = FinishReview(_finish_changed_files())

    result = finish.finish(arguments)

    assert result.finished is False
    assert result.status == "invalid_arguments"
    assert result.findings == ()
    assert finish.is_finished is False


def test_finish_review_is_first_finish_wins_and_signals_no_more_tool_dispatch():
    finish = FinishReview(_finish_changed_files())
    first = finish.finish({"findings": []})

    repeated = finish.finish({"findings": [_valid_finish_finding()]})

    assert first.status == "finished"
    assert repeated.finished is True
    assert repeated.status == "already_finished"
    assert repeated.findings == ()
    assert repeated.finding_count == 0
    assert finish.is_finished is True


def test_finish_review_marks_over_limit_output_as_incomplete():
    finish = FinishReview(_finish_changed_files(), max_findings=1)

    result = finish.finish({"findings": [_valid_finish_finding(), _valid_finish_finding()]})

    assert result.finished is True
    assert result.finding_count == 1
    assert result.finding_limit == 1
    assert result.received_count == 2
    assert result.truncated is True


def _parse_fixture(name):
    return parse_diff((Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))


def test_changed_hunks_tool_returns_each_hunk_with_its_new_file_range():
    changed_files = _parse_fixture("multiple_hunks_new_line_numbers.diff")
    dispatcher = ToolDispatcher()
    dispatcher.register(ChangedHunksTool(changed_files, {"service.py"}))

    result = dispatcher.dispatch("get_changed_hunks", {"path": "service.py"})

    assert result.success is True
    assert result.truncated is False
    assert result.result_size == 2
    assert result.usage["hunks_returned"] == 2
    assert result.usage["hunk_limit"] == 8
    assert result.usage["character_limit_per_hunk"] == 4_000
    hunks = result.data["hunks"]
    assert [(hunk["new_start"], hunk["new_end"]) for hunk in hunks] == [(2, 4), (21, 23)]
    assert [hunk["patch"] for hunk in hunks] == [
        "@@ -2,2 +2,3 @@\n def start():\n+    first = True\n     return True",
        "@@ -20,2 +21,3 @@\n def finish():\n+    second = True\n     return False",
    ]
    assert all(hunk["hunk_id"].startswith("hunk:") for hunk in hunks)
    assert all(hunk["truncated"] is False for hunk in hunks)
    assert result.usage["characters_returned"] == sum(len(hunk["patch"]) for hunk in hunks)


def test_changed_hunks_tool_keeps_renamed_space_path_and_new_line_range():
    changed_files = parse_diff(
        """diff --git a/old name.py b/new name.py
similarity index 90%
rename from old name.py
rename to new name.py
--- a/old name.py
+++ b/new name.py
@@ -1 +1,2 @@
 def run():
+    print(\"new\")
"""
    )
    tool = ChangedHunksTool(changed_files, {"new name.py"})

    result = tool.run({"path": "new name.py"})

    assert result.success is True
    assert result.data["path"] == "new name.py"
    assert result.data["old_path"] == "old name.py"
    assert result.data["is_rename"] is True
    assert result.data["hunks"][0]["new_start"] == 1
    assert result.data["hunks"][0]["new_end"] == 2


def test_changed_hunks_tool_returns_structured_empty_result_for_pure_rename():
    changed_files = _parse_fixture("pure_rename_with_spaces.diff")

    result = ChangedHunksTool(changed_files, {"new name.py"}).run({"path": "new name.py"})

    assert result.success is True
    assert result.truncated is False
    assert result.data["hunks"] == []
    assert result.result_size == 0
    assert "no readable change hunks" in result.model_summary


@pytest.mark.parametrize("path", ["other.py", "../secret.py", "/tmp/secret.py", "C:\\secret.py"])
def test_changed_hunks_tool_rejects_paths_outside_the_current_review_scope(path):
    tool = ChangedHunksTool(_parse_fixture("multiple_hunks_new_line_numbers.diff"), {"service.py"})

    result = tool.run({"path": path})

    assert result.success is False
    assert result.error_code == "forbidden"
    assert result.result_size == 0
    assert result.data is None


def test_changed_hunks_tool_distinguishes_scoped_missing_record_from_scope_escape():
    tool = ChangedHunksTool([], {"missing.py"})

    result = tool.run({"path": "missing.py"})

    assert result.success is False
    assert result.error_code == "not_found"


def test_changed_hunks_tool_reports_unavailable_when_patch_and_parsed_ranges_disagree():
    changed_file = ChangedFile(
        path="app.py",
        added_lines=[],
        deleted_lines=[],
        patch="@@ -1 +1 @@\n+value = True",
        hunks=[DiffHunk(start_line=2, end_line=2)],
    )

    result = ChangedHunksTool([changed_file], {"app.py"}).run({"path": "app.py"})

    assert result.success is False
    assert result.error_code == "unavailable"
    assert result.data is None


def test_changed_hunks_tool_exposes_character_truncation_without_claiming_completeness():
    tool = ChangedHunksTool(
        _parse_fixture("multiple_hunks_new_line_numbers.diff"),
        {"service.py"},
        max_hunks=1,
        max_chars_per_hunk=30,
    )

    result = tool.run({"path": "service.py"})

    assert result.success is True
    assert result.truncated is True
    assert result.result_size == 1
    assert result.result_limit == 1
    assert result.data["hunks"][0]["truncated"] is True
    assert "TRUNCATED" in result.data["hunks"][0]["patch"]
    assert "incomplete" in result.model_summary
    assert "complete" not in result.model_summary.replace("incomplete", "")


def test_changed_hunks_tool_suppresses_sensitive_rename_source_before_returning_patch():
    changed_file = ChangedFile(
        path="renamed.py",
        old_path=".env",
        is_rename=True,
        added_lines=[],
        deleted_lines=[],
        patch="@@ -1 +1 @@\n-secret=value\n+safe=value",
        hunks=[DiffHunk(start_line=1, end_line=1)],
    )

    result = ChangedHunksTool([changed_file], {"renamed.py"}).run({"path": "renamed.py"})

    assert result.success is False
    assert result.error_code == "forbidden"
    assert "secret=value" not in result.model_summary
    assert result.data is None


def test_changed_hunks_tool_redacts_sensitive_values_in_a_normal_scoped_file_before_budgeting():
    secret = "LEAKED_SECRET_VALUE_" + "A" * 100
    changed_file = ChangedFile(
        path="src/app.py",
        added_lines=[],
        deleted_lines=[],
        patch=f"@@ -1 +1 @@\n+api_key={secret}",
        hunks=[DiffHunk(start_line=1, end_line=1)],
    )

    result = ChangedHunksTool(
        [changed_file], {"src/app.py"}, max_chars_per_hunk=60
    ).run({"path": "src/app.py"})

    serialized = json.dumps(result.to_model_dict())
    assert result.success is True
    assert result.truncated is False
    assert "api_key=[REDACTED]" in result.data["hunks"][0]["patch"]
    assert secret not in serialized
    assert result.data["hunks"][0]["returned_size"] == len(
        result.data["hunks"][0]["patch"]
    )


def test_read_file_context_tool_returns_sanitized_bounded_context_for_a_repo_relative_path(tmp_path):
    source = tmp_path / "src" / "my file.py"
    source.parent.mkdir()
    source.write_text("api_key=LEAKED_SECRET_VALUE_42\nprint('safe')\n", encoding="utf-8")
    tool = ReadFileContextTool(tmp_path, max_chars=100)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)

    result = dispatcher.dispatch("read_file_context", {"path": "src/my file.py"})

    assert result.success is True
    assert result.truncated is False
    assert result.data["path"] == "src/my file.py"
    assert "api_key=[REDACTED]" in result.data["content"]
    assert "LEAKED_SECRET_VALUE_42" not in json.dumps(result.to_model_dict())
    assert result.result_size == len(result.data["content"])
    assert result.result_limit == 100


@pytest.mark.parametrize(
    ("token", "max_chars"),
    [
        ("ghp_" + "A" * 20, 9),
        ("github_pat_" + "A" * 20, 9),
        ("sk-" + "A" * 20, 8),
    ],
)
def test_read_file_context_tool_redacts_bare_token_crossing_reader_budget(
    tmp_path, token, max_chars
):
    (tmp_path / "source.py").write_text(token, encoding="utf-8")

    result = ReadFileContextTool(tmp_path, max_chars=max_chars).run(
        {"path": "source.py"}
    )

    serialized = json.dumps(result.to_model_dict())
    assert result.success is True
    assert result.truncated is True
    assert len(result.data["content"]) <= max_chars
    assert token not in serialized
    assert token[:max_chars] not in serialized


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt", "C:outside.txt"])
def test_read_file_context_tool_rejects_non_repository_relative_paths(tmp_path, path):
    result = ReadFileContextTool(tmp_path).run({"path": path})

    assert result.success is False
    assert result.error_code == "forbidden"
    assert result.data is None
    assert str(tmp_path) not in result.model_summary


def test_read_file_context_tool_rejects_resolved_symlink_escaping_repository_root(
    tmp_path, monkeypatch
):
    outside = tmp_path.parent / "outside-context.txt"
    outside.write_text("outside secret", encoding="utf-8")
    link = tmp_path / "linked.txt"

    # Windows CI may deny symlink creation; the security decision is based on
    # Path.resolve(), so model its escaped resolution deterministically.
    original_resolve = type(link).resolve

    def resolve_with_escape(path, *args, **kwargs):
        if path == link:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(link), "resolve", resolve_with_escape)

    result = ReadFileContextTool(tmp_path).run({"path": "linked.txt"})

    assert result.success is False
    assert result.error_code == "forbidden"
    assert "outside secret" not in json.dumps(result.to_model_dict())


def test_read_file_context_tool_rejects_sensitive_files_without_returning_content(tmp_path):
    secret = "FILE_CONTEXT_SECRET_MARKER"
    (tmp_path / ".env").write_text(f"TOKEN={secret}", encoding="utf-8")

    result = ReadFileContextTool(tmp_path).run({"path": ".env"})

    assert result.success is False
    assert result.error_code == "forbidden"
    assert secret not in json.dumps(result.to_model_dict())


@pytest.mark.parametrize("sensitive_path", [".env", "config/settings.json"])
def test_read_file_context_tool_rejects_safe_alias_resolved_to_sensitive_target(
    tmp_path, monkeypatch, sensitive_path
):
    sensitive_target = tmp_path / sensitive_path
    sensitive_target.parent.mkdir(parents=True, exist_ok=True)
    sensitive_target.write_text("TOKEN=DO_NOT_EXPOSE", encoding="utf-8")
    alias = tmp_path / "safe.txt"

    # Model an in-repository symlink without requiring symlink privileges on Windows CI.
    original_resolve = type(alias).resolve

    def resolve_to_sensitive_target(path, *args, **kwargs):
        if path == alias:
            return sensitive_target
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(alias), "resolve", resolve_to_sensitive_target)

    result = ReadFileContextTool(tmp_path).run({"path": "safe.txt"})

    assert result.success is False
    assert result.error_code == "forbidden"
    assert result.data is None
    assert "DO_NOT_EXPOSE" not in json.dumps(result.to_model_dict())


def test_read_file_context_tool_marks_large_file_context_as_incomplete(tmp_path):
    (tmp_path / "large.py").write_text("x" * 200, encoding="utf-8")

    result = ReadFileContextTool(tmp_path, max_chars=40).run({"path": "large.py"})

    assert result.success is True
    assert result.truncated is True
    assert len(result.data["content"]) <= 40
    assert result.result_size == len(result.data["content"])
    assert result.result_limit == 40
    assert "incomplete" in result.model_summary
    assert "complete" not in result.model_summary.replace("incomplete", "")


def test_read_file_context_tool_returns_recoverable_not_found_for_missing_file(tmp_path):
    result = ReadFileContextTool(tmp_path).run({"path": "missing.py"})

    assert result.success is False
    assert result.error_code == "not_found"
    assert result.data is None


def test_search_python_symbol_tool_returns_function_class_and_method_locations(tmp_path):
    source = tmp_path / "src" / "symbols.py"
    source.parent.mkdir()
    source.write_text(
        "def top_level():\n"
        "    return 1\n"
        "\n"
        "class Processor:\n"
        "    setting = 1\n"
        "\n"
        "    def handle(self):\n"
        "        return self.setting\n",
        encoding="utf-8",
    )
    tool = SearchPythonSymbolTool(tmp_path, {"src/symbols.py"})
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)

    function = dispatcher.dispatch("search_python_symbol", {"path": "src/symbols.py", "line_no": 2})
    containing_class = dispatcher.dispatch("search_python_symbol", {"path": "src/symbols.py", "line_no": 5})
    method = dispatcher.dispatch("search_python_symbol", {"path": "src/symbols.py", "line_no": 8})

    assert function.data == {
        "path": "src/symbols.py",
        "name": "top_level",
        "kind": "function",
        "qualified_name": "top_level",
        "class_name": None,
        "start_line": 1,
        "end_line": 2,
    }
    assert containing_class.data["kind"] == "class"
    assert containing_class.data["qualified_name"] == "Processor"
    assert method.data["kind"] == "method"
    assert method.data["qualified_name"] == "Processor.handle"
    assert method.data["start_line"] == 7
    assert method.data["end_line"] == 8
    assert all(result.success and result.result_size == result.result_limit == 1 for result in (function, containing_class, method))
    assert "source" not in method.data


def test_search_python_symbol_tool_distinguishes_not_found_parse_failure_and_truncation(tmp_path):
    source = tmp_path / "symbols.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tool = SearchPythonSymbolTool(tmp_path, {"symbols.py"}, max_source_chars=20)

    not_found = tool.run({"path": "symbols.py", "line_no": 1})
    source.write_text("def broken(:\n", encoding="utf-8")
    parse_failure = tool.run({"path": "symbols.py", "line_no": 1})
    source.write_text("def very_long_name():\n    return 1\n", encoding="utf-8")
    truncated = tool.run({"path": "symbols.py", "line_no": 2})

    assert (not_found.success, not_found.error_code, not_found.truncated) == (False, "not_found", False)
    assert (parse_failure.success, parse_failure.error_code, parse_failure.truncated) == (False, "unavailable", False)
    assert (truncated.success, truncated.error_code, truncated.truncated) == (False, "unavailable", True)
    assert truncated.usage["source_char_limit"] == 20
    assert truncated.usage["source_chars_read"] == 21


def test_search_python_symbol_tool_rejects_non_python_and_out_of_scope_paths(tmp_path):
    (tmp_path / "symbols.py").write_text("def visible():\n    return 1\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not python\n", encoding="utf-8")
    tool = SearchPythonSymbolTool(tmp_path, {"symbols.py", "notes.txt"})

    non_python = tool.run({"path": "notes.txt", "line_no": 1})
    traversal = tool.run({"path": "../symbols.py", "line_no": 1})
    out_of_scope = tool.run({"path": "other.py", "line_no": 1})

    assert (non_python.success, non_python.error_code) == (False, "unsupported")
    assert (traversal.success, traversal.error_code) == (False, "forbidden")
    assert (out_of_scope.success, out_of_scope.error_code) == (False, "forbidden")


def test_search_python_symbol_tool_rejects_symlink_escape_from_repository_and_scope(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-symbols.py"
    outside.write_text("def secret():\n    return 1\n", encoding="utf-8")
    alias = tmp_path / "alias.py"
    tool = SearchPythonSymbolTool(tmp_path, {"alias.py"})

    original_resolve = type(alias).resolve

    def resolve_with_escape(path, *args, **kwargs):
        if path == alias:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(alias), "resolve", resolve_with_escape)

    result = tool.run({"path": "alias.py", "line_no": 1})

    assert (result.success, result.error_code) == (False, "forbidden")
    assert "secret" not in json.dumps(result.to_model_dict())
