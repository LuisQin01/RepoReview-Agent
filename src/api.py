"""Minimal HTTP adapter for one stateless review run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .review_service import ReviewRequest, ReviewService
from .trace import redact_sensitive_structure


MAX_DIFF_CHARS = 1_000_000


class CreateReviewRequest(BaseModel):
    """The only client-controlled input for a review run."""

    model_config = ConfigDict(extra="forbid")

    diff: str = Field(min_length=1, max_length=MAX_DIFF_CHARS)


class ReviewMetrics(BaseModel):
    changed_files: int
    findings: int
    duration_ms: int
    llm_called: bool


class ReviewStep(BaseModel):
    step: str
    duration_ms: int


class ReviewResponse(BaseModel):
    task_id: str
    findings: list[dict[str, Any]]
    errors: list[str]
    metrics: ReviewMetrics
    steps: list[ReviewStep]


def create_app(
    repo_root: str | Path = ".",
    review_service: Optional[ReviewService] = None,
) -> FastAPI:
    """Create an app bound to a server-controlled repository root.

    The client cannot select a filesystem path, enable LLM calls, or publish
    GitHub comments through this endpoint.
    """

    service = review_service or ReviewService()
    configured_repo_root = str(Path(repo_root).resolve())
    app = FastAPI(title="RepoReview API", version="0.1.0")

    @app.post("/reviews", response_model=ReviewResponse, status_code=200)
    def create_review(payload: CreateReviewRequest) -> ReviewResponse:
        request = ReviewRequest(
            diff_text=payload.diff,
            repo_root=configured_repo_root,
            output_format="json",
            use_llm=False,
        )
        try:
            result = service.review(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_review_request") from exc

        if not result.state.changed_files:
            raise HTTPException(status_code=422, detail="diff_contains_no_changed_files")

        rendered = json.loads(result.output)
        duration_ms = sum(step["duration_ms"] for step in result.trace_steps)
        steps = [
            ReviewStep(step=step["step"], duration_ms=step["duration_ms"])
            for step in result.trace_steps
        ]
        llm_called = any(
            step["step"] == "run_llm_review"
            and step.get("detail", {}).get("called") is True
            for step in result.trace_steps
        )
        return ReviewResponse(
            task_id=result.state.task_id,
            findings=rendered["findings"],
            errors=redact_sensitive_structure(result.state.errors),
            metrics=ReviewMetrics(
                changed_files=len(result.state.changed_files),
                findings=len(result.state.issues),
                duration_ms=duration_ms,
                llm_called=llm_called,
            ),
            steps=steps,
        )

    return app


app = create_app()
