"""Provider-independent contracts for read-only review tools.

This module defines provider-independent tool contracts, schema validation, and
deterministic dispatch. Model integration belongs to later milestones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Collection, Protocol, Sequence, TypeAlias, runtime_checkable

from .file_context import _is_sensitive_file_path, read_file_context
from .schemas import ChangedFile, DiffHunk
from .trace import _BARE_SENSITIVE_VALUE_MIN_CHARS, redact_sensitive_values


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
JSONSchema: TypeAlias = dict[str, JSONValue]

EXPECTED_TOOL_ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "forbidden",
        "not_found",
        "unavailable",
        "truncated",
        "unsupported",
    }
)
INTERNAL_ERROR_CODE = "internal_error"


@dataclass(frozen=True)
class ToolResult:
    """A JSON-safe result returned by a read-only review tool.

    ``result_size`` and ``result_limit`` describe the returned payload rather
    than treating a truncated result as complete information.
    """

    success: bool
    model_summary: str
    error_code: str | None
    truncated: bool
    result_size: int
    result_limit: int
    data: JSONValue = None
    usage: dict[str, int] = field(default_factory=dict)
    internal_error_detail: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.result_size < 0 or self.result_limit < 0:
            raise ValueError("tool_result_size_must_be_non_negative")
        if self.success and self.error_code is not None:
            raise ValueError("successful_tool_result_cannot_have_error_code")
        if not self.success and self.error_code is None:
            raise ValueError("failed_tool_result_requires_error_code")
        if self.error_code is not None and self.error_code not in (
            EXPECTED_TOOL_ERROR_CODES | {INTERNAL_ERROR_CODE}
        ):
            raise ValueError("unsupported_tool_error_code")
        if self.internal_error_detail is not None and self.error_code != INTERNAL_ERROR_CODE:
            raise ValueError("internal_error_detail_requires_internal_error")
        try:
            json.dumps(self.to_dict(), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool_result_must_be_json_serializable") from exc

    def to_dict(self) -> dict[str, JSONValue]:
        """Return the public JSON-serializable representation of this result."""
        # Diagnostics remain log-only; tool adapters must never derive model text from them.
        return {
            "success": self.success,
            "model_summary": self.model_summary,
            "error_code": self.error_code,
            "truncated": self.truncated,
            "result_size": self.result_size,
            "result_limit": self.result_limit,
            "data": self.data,
            "usage": self.usage,
        }

    def to_model_dict(self) -> dict[str, JSONValue]:
        """Return the safe structured payload available to a model adapter."""
        return self.to_dict()


@runtime_checkable
class ReviewTool(Protocol):
    """Structural contract for a provider-independent, read-only review tool."""

    name: str
    description: str
    parameters_schema: JSONSchema

    def run(self, arguments: dict[str, JSONValue]) -> ToolResult:
        """Return a structured result for validated tool arguments."""


class ToolDispatcher:
    """Register and invoke read-only tools by their unique stable names.

    The dispatcher validates model-supplied arguments against each registered
    schema before the tool execution boundary.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ReviewTool] = {}

    def register(self, tool: ReviewTool) -> None:
        """Register ``tool`` once, rejecting duplicate or invalid names."""
        if not isinstance(tool, ReviewTool):
            raise TypeError("tool_must_implement_review_tool_protocol")
        if not isinstance(tool.name, str) or not tool.name:
            raise ValueError("tool_name_must_be_non_empty_string")
        if tool.name in self._tools:
            raise ValueError("duplicate_tool_name")
        self._tools[tool.name] = tool

    def get(self, tool_name: str) -> ReviewTool | None:
        """Return the registered tool for ``tool_name``, if one exists."""
        return self._tools.get(tool_name)

    def dispatch(self, tool_name: str, arguments: JSONValue) -> ToolResult:
        """Invoke a registered tool only after its local schema admits ``arguments``."""
        if not isinstance(tool_name, str) or not tool_name:
            # Routing input is untrusted; validate it before using it as a dict key.
            return self._invalid_arguments_result()
        tool = self.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                model_summary="The requested tool is not registered.",
                error_code="not_found",
                truncated=False,
                result_size=0,
                result_limit=0,
            )
        if not self._arguments_match_schema(arguments, tool.parameters_schema):
            # Models cannot expand a tool's capability by adding undeclared arguments.
            return self._invalid_arguments_result(
                "The tool arguments do not match the registered schema."
            )

        try:
            result = tool.run(arguments)
        except Exception:
            # Tool diagnostics are log-only; callers receive a stable safe failure.
            return self._internal_error_result()
        if not isinstance(result, ToolResult):
            return self._internal_error_result()
        return result

    @staticmethod
    def _arguments_match_schema(arguments: JSONValue, schema: JSONSchema) -> bool:
        """Return whether ``arguments`` matches the dispatcher-supported schema subset."""
        return ToolDispatcher._value_matches_schema(arguments, schema)

    @staticmethod
    def _value_matches_schema(value: JSONValue, schema: JSONValue) -> bool:
        """Recursively validate a JSON value against the supported schema subset."""
        if not isinstance(schema, dict):
            return False
        expected_type = schema.get("type")
        if expected_type == "object":
            return ToolDispatcher._object_matches_schema(value, schema)
        if expected_type == "array":
            items_schema = schema.get("items")
            return (
                isinstance(value, list)
                and isinstance(items_schema, dict)
                and all(ToolDispatcher._value_matches_schema(item, items_schema) for item in value)
            )
        return ToolDispatcher._value_matches_type(value, expected_type)

    @staticmethod
    def _object_matches_schema(value: JSONValue, schema: dict[str, JSONValue]) -> bool:
        """Validate every object level so nested keys cannot bypass the whitelist."""
        if not isinstance(value, dict) or schema.get("additionalProperties") is not False:
            return False

        properties = schema.get("properties")
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        if not all(isinstance(name, str) and name in properties for name in required):
            return False
        if not all(isinstance(name, str) and isinstance(rule, dict) for name, rule in properties.items()):
            return False
        if any(name not in value for name in required):
            return False
        if any(name not in properties for name in value):
            return False

        return all(
            ToolDispatcher._value_matches_schema(value[name], rule)
            for name, rule in properties.items()
            if name in value
        )

    @staticmethod
    def _value_matches_type(value: JSONValue, expected_type: JSONValue) -> bool:
        """Validate the JSON primitive types accepted in tool argument schemas."""
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected_type == "boolean":
            return isinstance(value, bool)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "object":
            return isinstance(value, dict)
        if expected_type == "null":
            return value is None
        return False

    @staticmethod
    def _invalid_arguments_result(summary: str = "The tool name must be a non-empty string.") -> ToolResult:
        """Return the safe failure used for an invalid dispatcher request."""
        return ToolResult(
            success=False,
            model_summary=summary,
            error_code="invalid_arguments",
            truncated=False,
            result_size=0,
            result_limit=0,
        )

    @staticmethod
    def _internal_error_result() -> ToolResult:
        """Return the safe failure used when a tool breaks its contract."""
        return ToolResult(
            success=False,
            model_summary="The tool could not complete the request.",
            error_code=INTERNAL_ERROR_CODE,
            truncated=False,
            result_size=0,
            result_limit=0,
        )


_HUNK_HEADER_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_HUNK_TRUNCATION_MARKER = "\n[TRUNCATED: hunk character limit reached]\n"
_FILE_CONTEXT_TRUNCATION_MARKER = "\n[TRUNCATED: file context limit reached]\n"


class ChangedHunksTool:
    """Expose only parsed changed hunks from one explicitly scoped review.

    ``review_scope`` is supplied separately from ``changed_files`` so a path
    that is permitted but has no retained diff record is distinguishable from
    a path the model was never authorized to request.  The tool never opens a
    repository file; its only source text is ``ChangedFile.patch``.
    """

    name = "get_changed_hunks"
    description = "Return bounded changed hunks and new-file line ranges for one scoped path."
    parameters_schema: JSONSchema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        changed_files: Sequence[ChangedFile],
        review_scope: Collection[str],
        *,
        max_hunks: int = 8,
        max_chars_per_hunk: int = 4_000,
    ) -> None:
        """Create a bounded adapter for one immutable review scope.

        Args:
            changed_files: Parsed diff records available to this review only.
            review_scope: Canonical relative paths the model may request.
            max_hunks: Maximum hunk records returned by one call.
            max_chars_per_hunk: Maximum characters returned for each hunk patch.
        """
        if max_hunks <= 0 or max_chars_per_hunk <= 0:
            raise ValueError("changed_hunks_limits_must_be_positive")

        self._review_scope = frozenset(_normalize_review_path(path) for path in review_scope)
        self._changed_files: dict[str, ChangedFile] = {}
        for changed_file in changed_files:
            path = _normalize_review_path(changed_file.path)
            if path in self._changed_files:
                raise ValueError("duplicate_changed_file_path")
            self._changed_files[path] = changed_file
        self._max_hunks = max_hunks
        self._max_chars_per_hunk = max_chars_per_hunk

    def run(self, arguments: dict[str, JSONValue]) -> ToolResult:
        """Return bounded hunk summaries for a path admitted by this review scope."""
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if not isinstance(path, str):
            return _changed_hunks_failure("invalid_arguments", "A string path is required.")
        try:
            normalized_path = _normalize_review_path(path)
        except ValueError:
            return _changed_hunks_failure("forbidden", "The requested path is outside the review scope.")

        if normalized_path not in self._review_scope:
            return _changed_hunks_failure("forbidden", "The requested path is outside the review scope.")
        changed_file = self._changed_files.get(normalized_path)
        if changed_file is None:
            return _changed_hunks_failure("not_found", "No retained change record exists for this scoped path.")
        # Diffs bypass file readers, so check both rename endpoints before exposing patch text.
        if _is_sensitive_file_path(changed_file.path) or (
            changed_file.old_path and _is_sensitive_file_path(changed_file.old_path)
        ):
            return _changed_hunks_failure("forbidden", "The requested path is not available to this tool.")

        extracted = _extract_hunks(changed_file)
        if changed_file.hunks and not extracted:
            return _changed_hunks_failure(
                "unavailable", "The retained patch cannot be safely matched to its parsed hunk ranges."
            )
        if not extracted:
            return ToolResult(
                success=True,
                model_summary="The scoped changed file has no readable change hunks.",
                error_code=None,
                truncated=False,
                result_size=0,
                result_limit=self._max_hunks,
                data={
                    "path": normalized_path,
                    "old_path": changed_file.old_path,
                    "is_rename": changed_file.is_rename,
                    "hunks": [],
                },
                usage={
                    "hunks_returned": 0,
                    "hunk_limit": self._max_hunks,
                    "characters_returned": 0,
                    "character_limit_per_hunk": self._max_chars_per_hunk,
                },
            )

        returned_hunks: list[dict[str, JSONValue]] = []
        characters_returned = 0
        content_truncated = len(extracted) > self._max_hunks
        for hunk, patch in extracted[: self._max_hunks]:
            # Patch text crosses the model boundary here, so redact before sizing or truncating it.
            safe_patch = redact_sensitive_values(patch)
            returned_patch, hunk_truncated = _truncate_hunk_patch(
                safe_patch, self._max_chars_per_hunk
            )
            characters_returned += len(returned_patch)
            content_truncated = content_truncated or hunk_truncated
            returned_hunks.append(
                {
                    "hunk_id": _stable_hunk_id(normalized_path, hunk, patch),
                    "new_start": hunk.start_line,
                    "new_end": hunk.end_line,
                    "patch": returned_patch,
                    "truncated": hunk_truncated,
                    "returned_size": len(returned_patch),
                    "result_limit": self._max_chars_per_hunk,
                }
            )

        summary = f"Returned {len(returned_hunks)} scoped change hunk(s)."
        if content_truncated:
            summary += " The returned content is incomplete because a configured limit was reached."
        return ToolResult(
            success=True,
            model_summary=summary,
            error_code=None,
            truncated=content_truncated,
            result_size=len(returned_hunks),
            result_limit=self._max_hunks,
            data={
                "path": normalized_path,
                "old_path": changed_file.old_path,
                "is_rename": changed_file.is_rename,
                "hunks": returned_hunks,
            },
            usage={
                "hunks_returned": len(returned_hunks),
                "hunk_limit": self._max_hunks,
                "characters_returned": characters_returned,
                "character_limit_per_hunk": self._max_chars_per_hunk,
            },
        )


class ReadFileContextTool:
    """Expose bounded, safe file context rooted at one review repository.

    The adapter delegates all content reads to ``read_file_context``.  Its
    preflight only establishes stable, non-leaking tool failure semantics for
    untrusted model paths.
    """

    name = "read_file_context"
    description = "Read a bounded, sanitized text context from one repository-relative path."
    parameters_schema: JSONSchema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, repo_root: str | Path, *, max_chars: int = 4_000) -> None:
        """Create a file-context adapter with a fixed per-call character limit."""
        if max_chars <= 0:
            raise ValueError("file_context_max_chars_must_be_positive")
        self._repo_root = Path(repo_root).resolve()
        self._max_chars = max_chars

    def run(self, arguments: dict[str, JSONValue]) -> ToolResult:
        """Return a bounded context without exposing local reader diagnostics."""
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if not isinstance(path, str):
            return _file_context_failure("invalid_arguments", "A string path is required.")
        try:
            normalized_path = _normalize_file_context_path(path)
            target_path = (self._repo_root / normalized_path).resolve()
            target_path.relative_to(self._repo_root)
        except ValueError:
            return _file_context_failure("forbidden", "The requested path is not available to this tool.")

        resolved_relative_path = target_path.relative_to(self._repo_root).as_posix()
        if _is_sensitive_file_path(normalized_path) or _is_sensitive_file_path(resolved_relative_path):
            return _file_context_failure("forbidden", "The requested path is not available to this tool.")
        if not target_path.exists():
            return _file_context_failure("not_found", "The requested file was not found.")
        if target_path.is_dir():
            return _file_context_failure("unavailable", "The requested path cannot be read as a text file.")

        # Read a bounded lookahead so bare credential rules can match tokens
        # crossing the model-output boundary before that boundary is applied.
        read_limit = self._max_chars + _BARE_SENSITIVE_VALUE_MIN_CHARS
        context = read_file_context(
            self._repo_root, resolved_relative_path, max_chars=read_limit
        )
        if not context.exists:
            # The reader owns I/O failures, whose diagnostics can contain host paths.
            return _file_context_failure("unavailable", "The requested file could not be read.")

        # Redaction can expand short values, so reapply the model budget after sanitizing.
        safe_content = redact_sensitive_values(context.content)
        returned_content, output_truncated = _truncate_file_context_content(
            safe_content, self._max_chars
        )
        truncated = context.truncated or output_truncated
        summary = "Returned bounded file context."
        if truncated:
            summary += " The returned content is incomplete because a configured limit was reached."
        return ToolResult(
            success=True,
            model_summary=summary,
            error_code=None,
            truncated=truncated,
            result_size=len(returned_content),
            result_limit=self._max_chars,
            data={
                "path": resolved_relative_path,
                "content": returned_content,
                "truncated": truncated,
                "returned_size": len(returned_content),
                "result_limit": self._max_chars,
            },
            usage={
                "characters_returned": len(returned_content),
                "character_limit": self._max_chars,
            },
        )


def _normalize_review_path(path: str) -> str:
    """Accept only a canonical relative POSIX path without traversal components."""
    if not isinstance(path, str) or not path or path != path.strip():
        raise ValueError("review_path_must_be_non_empty")
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
        raise ValueError("review_path_must_be_relative")
    parts = PurePosixPath(path).parts
    if not parts or any(part in {".", ".."} for part in parts) or "\\" in path:
        raise ValueError("review_path_must_be_canonical")
    return str(PurePosixPath(path))


def _normalize_file_context_path(path: str) -> str:
    """Accept only canonical repository-relative paths for filesystem access."""
    if not isinstance(path, str) or not path or path != path.strip():
        raise ValueError("file_context_path_must_be_non_empty")
    windows_path = PureWindowsPath(path)
    if windows_path.is_absolute() or windows_path.drive or PurePosixPath(path).is_absolute():
        raise ValueError("file_context_path_must_be_relative")
    parts = PurePosixPath(path).parts
    if not parts or any(part in {".", ".."} for part in parts) or "\\" in path:
        raise ValueError("file_context_path_must_be_canonical")
    return str(PurePosixPath(path))


def _extract_hunks(changed_file: ChangedFile) -> list[tuple[DiffHunk, str]]:
    """Split retained patch text into hunk records without consulting the worktree."""
    patch_lines = changed_file.patch.splitlines()
    header_indexes = [index for index, line in enumerate(patch_lines) if line.startswith("@@")]
    if len(header_indexes) != len(changed_file.hunks):
        # A malformed patch cannot be safely associated with parsed line ranges.
        return []

    extracted: list[tuple[DiffHunk, str]] = []
    for index, hunk in enumerate(changed_file.hunks):
        start = header_indexes[index]
        end = header_indexes[index + 1] if index + 1 < len(header_indexes) else len(patch_lines)
        header = patch_lines[start]
        match = _HUNK_HEADER_RE.search(header)
        if match is None:
            return []
        parsed_start = int(match.group(1))
        parsed_end = parsed_start + int(match.group(2) or 1) - 1
        if (parsed_start, parsed_end) != (hunk.start_line, hunk.end_line):
            return []
        extracted.append((hunk, "\n".join(patch_lines[start:end])))
    return extracted


def _truncate_hunk_patch(patch: str, limit: int) -> tuple[str, bool]:
    """Return a character-bounded patch whose marker makes incomplete text explicit."""
    if len(patch) <= limit:
        return patch, False
    if limit <= len(_HUNK_TRUNCATION_MARKER):
        return _HUNK_TRUNCATION_MARKER[:limit], True
    return patch[: limit - len(_HUNK_TRUNCATION_MARKER)] + _HUNK_TRUNCATION_MARKER, True


def _truncate_file_context_content(content: str, limit: int) -> tuple[str, bool]:
    """Apply the adapter's model-output budget after redaction."""
    if len(content) <= limit:
        return content, False
    if limit <= len(_FILE_CONTEXT_TRUNCATION_MARKER):
        return _FILE_CONTEXT_TRUNCATION_MARKER[:limit], True
    return (
        content[: limit - len(_FILE_CONTEXT_TRUNCATION_MARKER)]
        + _FILE_CONTEXT_TRUNCATION_MARKER,
        True,
    )


def _stable_hunk_id(path: str, hunk: DiffHunk, patch: str) -> str:
    """Derive a content-stable identifier rather than relying on hunk list position."""
    digest = sha256(f"{path}\0{hunk.start_line}\0{hunk.end_line}\0{patch}".encode()).hexdigest()
    return f"hunk:{digest[:16]}"


def _changed_hunks_failure(error_code: str, summary: str) -> ToolResult:
    """Create the non-leaking structured failures used by ``ChangedHunksTool``."""
    return ToolResult(
        success=False,
        model_summary=summary,
        error_code=error_code,
        truncated=False,
        result_size=0,
        result_limit=0,
        usage={"hunks_returned": 0},
    )


def _file_context_failure(error_code: str, summary: str) -> ToolResult:
    """Create safe structured failures without returning reader diagnostics."""
    return ToolResult(
        success=False,
        model_summary=summary,
        error_code=error_code,
        truncated=False,
        result_size=0,
        result_limit=0,
        usage={"characters_returned": 0},
    )
