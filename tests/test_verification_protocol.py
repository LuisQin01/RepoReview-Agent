"""Offline behavior tests for the M8 verification protocol models."""

import json
import math

import pytest

from src.schemas import ReviewIssue
from src.verification_protocol import (
    DialogueTraceEntry,
    EvidenceItem,
    EvidenceRequest,
    ReviewHypothesis,
    VerificationProtocolError,
    VerificationVerdict,
)


def _candidate_issue() -> ReviewIssue:
    return ReviewIssue(
        file_path="src/app.py",
        line_no=12,
        severity="warning",
        category="exception_handling",
        message="The error is discarded.",
        suggestion="Preserve a stable failure status.",
        reason="The caller cannot distinguish failure from no result.",
        confidence=0.9,
        evidence="except: return []",
        source="llm",
        placement="inline",
    )


def _request() -> EvidenceRequest:
    return EvidenceRequest(
        evidence_type="caller_exception_handling",
        params={"path": "src/app.py", "line_no": 12},
    )


def test_valid_models_construct_and_expose_json_safe_dicts():
    request = _request()
    hypothesis = ReviewHypothesis(
        hypothesis_id="hyp-1",
        file_path="src/app.py",
        line_no=12,
        description="The caller may hide an operational failure.",
        evidence_requests=[request],
        confidence=0.75,
    )
    evidence = EvidenceItem(
        request=request,
        status="found",
        data={"handlers": ["ValueError"]},
        actual_size=1,
        limit=5,
        truncated=False,
    )
    confirmed = VerificationVerdict(
        hypothesis_id="hyp-1",
        status="confirmed",
        candidate_finding=_candidate_issue(),
        reason="The caller converts failure into an empty result.",
    )
    rejected = VerificationVerdict(
        hypothesis_id="hyp-2",
        status="rejected",
        candidate_finding=None,
        reason="The exception is propagated.",
    )
    inconclusive = VerificationVerdict(
        hypothesis_id="hyp-3",
        status="inconclusive",
        candidate_finding=None,
        reason="Required context was truncated.",
    )
    trace = DialogueTraceEntry(
        phase="verification",
        hypothesis_id="hyp-1",
        evidence_request_key=request.dedup_key,
        summary="confirmed",
    )

    for value in (
        request,
        hypothesis,
        evidence,
        confirmed,
        rejected,
        inconclusive,
        trace,
    ):
        json.dumps(value.to_dict(), allow_nan=False)

    assert evidence.request_dedup_key == request.dedup_key
    assert confirmed.to_dict()["candidate_finding"]["file_path"] == "src/app.py"


def test_confirmed_verdict_accepts_an_equivalent_finding_dict():
    candidate = _candidate_issue().__dict__.copy()

    verdict = VerificationVerdict(
        hypothesis_id="hyp-1",
        status="confirmed",
        candidate_finding=candidate,
        reason="Evidence supports the candidate.",
    )

    assert verdict.to_dict()["candidate_finding"] == candidate


def test_confirmed_verdict_snapshots_a_mutable_review_issue():
    candidate = _candidate_issue()
    verdict = VerificationVerdict(
        hypothesis_id="hyp-1",
        status="confirmed",
        candidate_finding=candidate,
        reason="Evidence supports the candidate.",
    )

    candidate.confidence = math.nan

    assert verdict.to_dict()["candidate_finding"]["confidence"] == 0.9
    json.dumps(verdict.to_dict(), allow_nan=False)


def test_confirmed_verdict_rejects_a_non_equivalent_finding_dict():
    with pytest.raises(
        VerificationProtocolError, match="candidate_finding_must_match_review_issue"
    ):
        VerificationVerdict(
            hypothesis_id="hyp-1",
            status="confirmed",
            candidate_finding={"message": "Missing required location fields."},
            reason="Malformed candidates must not cross the protocol boundary.",
        )


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_confidence_boundaries_are_valid(confidence):
    hypothesis = ReviewHypothesis(
        hypothesis_id="hyp-boundary",
        file_path="src/app.py",
        line_no=0,
        description="A file-level hypothesis.",
        evidence_requests=[],
        confidence=confidence,
    )

    assert hypothesis.confidence == confidence
    assert hypothesis.evidence_requests == []


@pytest.mark.parametrize("confidence", [-0.1, 1.1, math.nan, math.inf, -math.inf])
def test_invalid_confidence_is_rejected(confidence):
    with pytest.raises(VerificationProtocolError):
        ReviewHypothesis(
            hypothesis_id="hyp-invalid",
            file_path="src/app.py",
            line_no=1,
            description="Invalid confidence must fail fast.",
            evidence_requests=[],
            confidence=confidence,
        )


def test_dedup_key_is_independent_of_parameter_insertion_order():
    first = EvidenceRequest(
        evidence_type="caller_exception_handling",
        params={"path": "src/app.py", "options": {"depth": 2, "mode": "strict"}},
    )
    second = EvidenceRequest(
        evidence_type="caller_exception_handling",
        params={"options": {"mode": "strict", "depth": 2}, "path": "src/app.py"},
    )

    assert first.dedup_key == second.dedup_key


def test_evidence_item_allows_none_data_and_a_request_key():
    item = EvidenceItem(
        request="caller_exception_handling:stable-key",
        status="not_found",
        data=None,
        actual_size=0,
        limit=0,
        truncated=False,
    )

    assert item.to_dict()["data"] is None
    json.dumps(item.to_dict(), allow_nan=False)


@pytest.mark.parametrize("status", ["maybe", None, []])
def test_invalid_evidence_status_is_rejected(status):
    with pytest.raises(VerificationProtocolError, match="unsupported_evidence_status"):
        EvidenceItem(
            request="request-key",
            status=status,
            data=None,
            actual_size=0,
            limit=0,
            truncated=False,
        )


@pytest.mark.parametrize("status", ["maybe", None, []])
def test_invalid_verdict_status_is_rejected(status):
    with pytest.raises(VerificationProtocolError, match="unsupported_verdict_status"):
        VerificationVerdict(
            hypothesis_id="hyp-1",
            status=status,
            candidate_finding=None,
            reason="Unknown status.",
        )


@pytest.mark.parametrize("status", ["rejected", "inconclusive"])
def test_non_confirmed_verdict_cannot_carry_a_finding(status):
    with pytest.raises(
        VerificationProtocolError,
        match="non_confirmed_verdict_cannot_have_candidate_finding",
    ):
        VerificationVerdict(
            hypothesis_id="hyp-1",
            status=status,
            candidate_finding=_candidate_issue(),
            reason="A non-confirmed verdict cannot publish a candidate.",
        )


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (
            lambda: EvidenceRequest(
                evidence_type="caller_exception_handling",
                params={"score": math.nan},
            ),
            "evidence_request_params_must_be_json_serializable",
        ),
        (
            lambda: EvidenceItem(
                request="request-key",
                status="found",
                data={"score": math.inf},
                actual_size=1,
                limit=1,
                truncated=False,
            ),
            "evidence_data_must_be_json_serializable",
        ),
    ],
)
def test_non_json_data_is_rejected(factory, error):
    with pytest.raises(VerificationProtocolError, match=error):
        factory()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"line_no": -1},
        {"line_no": True},
    ],
)
def test_hypothesis_rejects_invalid_line_numbers(kwargs):
    with pytest.raises(VerificationProtocolError, match="line_no"):
        ReviewHypothesis(
            hypothesis_id="hyp-1",
            file_path="src/app.py",
            description="Line numbers must be bounded integers.",
            evidence_requests=[],
            confidence=0.5,
            **kwargs,
        )


@pytest.mark.parametrize(
    ("actual_size", "limit"),
    [(-1, 0), (0, -1), (True, 1), (1, False)],
)
def test_evidence_item_rejects_invalid_size_bounds(actual_size, limit):
    with pytest.raises(VerificationProtocolError):
        EvidenceItem(
            request="request-key",
            status="found",
            data=None,
            actual_size=actual_size,
            limit=limit,
            truncated=False,
        )


def test_trace_has_only_controlled_summary_and_link_fields():
    trace = DialogueTraceEntry(
        phase="evidence_collection",
        hypothesis_id="hyp-1",
        evidence_request_key="request-key",
        summary="not_found",
    )

    assert set(trace.to_dict()) == {
        "phase",
        "hypothesis_id",
        "evidence_request_key",
        "summary",
    }
    assert not hasattr(trace, "reasoning")
    assert not hasattr(trace, "source_code")


def test_trace_summary_is_redacted_and_bounded():
    trace = DialogueTraceEntry(
        phase="verification",
        summary="api_key=sk-secret " + ("x" * 400),
    )

    assert "sk-secret" not in trace.summary
    assert "[REDACTED]" in trace.summary
    assert len(trace.summary) <= 303
