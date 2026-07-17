"""Validated, JSON-safe data boundaries for the two-round review protocol.

This module only defines protocol values.  It does not call a model, collect
evidence, or publish findings.  Callers serialize instances through ``to_dict``
so provider objects and other executable values cannot cross the boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Mapping, cast

from src.model_protocol import JSONValue
from src.schemas import ReviewIssue
from src.trace import sanitize_trace_text


EVIDENCE_STATUSES = frozenset(
    {"found", "not_found", "unavailable", "truncated", "unsupported", "conflicting"}
)
VERDICT_STATUSES = frozenset({"confirmed", "rejected", "inconclusive"})


class VerificationProtocolError(ValueError):
    """Raised when a value cannot safely enter the verification protocol."""


def _json_copy(value: object, *, field_name: str) -> JSONValue:
    """Return a detached JSON value or reject unsupported values and NaN/Inf."""
    try:
        return cast(JSONValue, json.loads(json.dumps(value, allow_nan=False)))
    except (TypeError, ValueError) as exc:
        raise VerificationProtocolError(
            f"{field_name}_must_be_json_serializable"
        ) from exc


def _json_object(value: object, *, field_name: str) -> dict[str, JSONValue]:
    """Return a detached JSON object, preserving the protocol's dict boundary."""
    if not isinstance(value, Mapping):
        raise VerificationProtocolError(f"{field_name}_must_be_an_object")
    copied = _json_copy(dict(value), field_name=field_name)
    if not isinstance(copied, dict):  # Defensive: the JSON round trip preserves objects.
        raise VerificationProtocolError(f"{field_name}_must_be_an_object")
    return copied


def _assert_json_safe(value: dict[str, JSONValue], *, field_name: str) -> None:
    """Validate the complete public projection with strict JSON semantics."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise VerificationProtocolError(
            f"{field_name}_must_be_json_serializable"
        ) from exc


def _non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationProtocolError(
            f"{field_name}_must_be_a_non_empty_string"
        )
    return value


def _optional_non_empty_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, field_name=field_name)


def _non_negative_integer(value: object, *, field_name: str) -> int:
    # bool is an int subclass but is never a meaningful line number or size.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VerificationProtocolError(
            f"{field_name}_must_be_a_non_negative_integer"
        )
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerificationProtocolError("confidence_must_be_a_finite_number")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise VerificationProtocolError("confidence_must_be_between_zero_and_one")
    return normalized


def _candidate_finding_to_dict(
    candidate: ReviewIssue | Mapping[str, JSONValue],
) -> dict[str, JSONValue]:
    """Project a ReviewIssue or an equivalent mapping to a JSON-safe object."""
    if isinstance(candidate, ReviewIssue):
        raw_candidate: object = asdict(candidate)
    elif isinstance(candidate, Mapping):
        try:
            # Reuse the shared model's constructor as the authoritative field shape.
            raw_candidate = asdict(ReviewIssue(**dict(candidate)))
        except TypeError as exc:
            raise VerificationProtocolError(
                "candidate_finding_must_match_review_issue"
            ) from exc
    else:
        raise VerificationProtocolError(
            "candidate_finding_must_be_a_review_issue_or_object"
        )
    return _json_object(raw_candidate, field_name="candidate_finding")


@dataclass(frozen=True)
class EvidenceRequest:
    """One deterministic request for an evidence type registered by a later task."""

    evidence_type: str
    params: dict[str, JSONValue]
    dedup_key: str = field(init=False)

    def __post_init__(self) -> None:
        evidence_type = _non_empty_string(
            self.evidence_type, field_name="evidence_type"
        )
        params = _json_object(self.params, field_name="evidence_request_params")
        canonical = json.dumps(
            {"evidence_type": evidence_type, "params": params},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        object.__setattr__(self, "evidence_type", evidence_type)
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "dedup_key", f"{evidence_type}:{digest}")
        _assert_json_safe(self.to_dict(), field_name="evidence_request")

    def to_dict(self) -> dict[str, JSONValue]:
        """Return the public JSON representation, including the derived key."""
        return {
            "evidence_type": self.evidence_type,
            "params": self.params,
            "dedup_key": self.dedup_key,
        }


@dataclass(frozen=True)
class ReviewHypothesis:
    """One stable, locatable review hypothesis awaiting deterministic evidence."""

    hypothesis_id: str
    file_path: str
    line_no: int
    description: str
    evidence_requests: list[EvidenceRequest] = field(default_factory=list)
    confidence: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hypothesis_id",
            _non_empty_string(self.hypothesis_id, field_name="hypothesis_id"),
        )
        if not isinstance(self.file_path, str):
            raise VerificationProtocolError("file_path_must_be_a_string")
        object.__setattr__(
            self, "line_no", _non_negative_integer(self.line_no, field_name="line_no")
        )
        object.__setattr__(
            self,
            "description",
            _non_empty_string(self.description, field_name="description"),
        )
        if not isinstance(self.evidence_requests, list) or not all(
            isinstance(request, EvidenceRequest)
            for request in self.evidence_requests
        ):
            raise VerificationProtocolError(
                "evidence_requests_must_be_a_list_of_evidence_requests"
            )
        object.__setattr__(self, "evidence_requests", list(self.evidence_requests))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        _assert_json_safe(self.to_dict(), field_name="hypothesis")

    def to_dict(self) -> dict[str, JSONValue]:
        """Return the public JSON representation of this hypothesis."""
        return {
            "hypothesis_id": self.hypothesis_id,
            "file_path": self.file_path,
            "line_no": self.line_no,
            "description": self.description,
            "evidence_requests": [
                request.to_dict() for request in self.evidence_requests
            ],
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class EvidenceItem:
    """The bounded result associated with one evidence request."""

    request: EvidenceRequest | str
    status: str
    data: JSONValue
    actual_size: int
    limit: int
    truncated: bool

    def __post_init__(self) -> None:
        if isinstance(self.request, EvidenceRequest):
            request: EvidenceRequest | str = self.request
        else:
            request = _non_empty_string(self.request, field_name="request_dedup_key")
        if not isinstance(self.status, str) or self.status not in EVIDENCE_STATUSES:
            raise VerificationProtocolError("unsupported_evidence_status")
        if not isinstance(self.truncated, bool):
            raise VerificationProtocolError("truncated_must_be_a_boolean")
        object.__setattr__(self, "request", request)
        object.__setattr__(
            self, "data", _json_copy(self.data, field_name="evidence_data")
        )
        object.__setattr__(
            self,
            "actual_size",
            _non_negative_integer(self.actual_size, field_name="actual_size"),
        )
        object.__setattr__(
            self, "limit", _non_negative_integer(self.limit, field_name="limit")
        )
        _assert_json_safe(self.to_dict(), field_name="evidence_item")

    @property
    def request_dedup_key(self) -> str:
        """Return the stable request identifier independent of representation."""
        return (
            self.request.dedup_key
            if isinstance(self.request, EvidenceRequest)
            else self.request
        )

    def to_dict(self) -> dict[str, JSONValue]:
        """Return the public JSON representation with a stable request link."""
        return {
            "request": self.request_dedup_key,
            "status": self.status,
            "data": self.data,
            "actual_size": self.actual_size,
            "limit": self.limit,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class VerificationVerdict:
    """The second-round decision for exactly one hypothesis."""

    hypothesis_id: str
    status: str
    candidate_finding: ReviewIssue | dict[str, JSONValue] | None
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hypothesis_id",
            _non_empty_string(self.hypothesis_id, field_name="hypothesis_id"),
        )
        if not isinstance(self.status, str) or self.status not in VERDICT_STATUSES:
            raise VerificationProtocolError("unsupported_verdict_status")
        if not isinstance(self.reason, str):
            raise VerificationProtocolError("verdict_reason_must_be_a_string")
        if self.status != "confirmed" and self.candidate_finding is not None:
            raise VerificationProtocolError(
                "non_confirmed_verdict_cannot_have_candidate_finding"
            )
        if self.candidate_finding is not None:
            # Snapshot the mutable shared ReviewIssue at the protocol boundary.
            object.__setattr__(
                self,
                "candidate_finding",
                _candidate_finding_to_dict(self.candidate_finding),
            )
        _assert_json_safe(self.to_dict(), field_name="verification_verdict")

    def to_dict(self) -> dict[str, JSONValue]:
        """Return the public JSON representation of this verdict."""
        candidate = (
            None
            if self.candidate_finding is None
            else _candidate_finding_to_dict(self.candidate_finding)
        )
        return {
            "hypothesis_id": self.hypothesis_id,
            "status": self.status,
            "candidate_finding": candidate,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DialogueTraceEntry:
    """A controlled trace summary without reasoning text or source payloads."""

    phase: str
    hypothesis_id: str | None = None
    evidence_request_key: str | None = None
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "phase", _non_empty_string(self.phase, field_name="trace_phase")
        )
        object.__setattr__(
            self,
            "hypothesis_id",
            _optional_non_empty_string(
                self.hypothesis_id, field_name="trace_hypothesis_id"
            ),
        )
        object.__setattr__(
            self,
            "evidence_request_key",
            _optional_non_empty_string(
                self.evidence_request_key,
                field_name="trace_evidence_request_key",
            ),
        )
        if not isinstance(self.summary, str):
            raise VerificationProtocolError("trace_summary_must_be_a_string")
        # Reuse the repository trace sanitizer so summaries are bounded and
        # credential patterns cannot be persisted at this protocol boundary.
        object.__setattr__(self, "summary", sanitize_trace_text(self.summary))
        _assert_json_safe(self.to_dict(), field_name="dialogue_trace_entry")

    def to_dict(self) -> dict[str, JSONValue]:
        """Return the fixed, JSON-safe trace summary fields."""
        return {
            "phase": self.phase,
            "hypothesis_id": self.hypothesis_id,
            "evidence_request_key": self.evidence_request_key,
            "summary": self.summary,
        }
