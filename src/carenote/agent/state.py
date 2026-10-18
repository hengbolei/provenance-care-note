"""Schemas for the Clinical Review Agent (design §6–§8, §11).

Two kinds of type live here:

- **Model output** (`Citation`, `Claim`, `CandidateHighlight`, `DraftReview`): what the model must
  return through structured output. Every model forbids extra keys and bounds its lengths, so a
  response that does not fit is rejected before anything reads it.
- **Run state** (`ClaimCheck`, `VerificationReport`, `Budget`, `ReviewDecisions`, `ReviewState`):
  what the graph carries between nodes. Only deterministic code writes these.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from carenote.domain.models import AgentActor, Conflict, EntryRef, RiskLevel, Span

MAX_CLAIMS = 20
MAX_CITATIONS_PER_CLAIM = 5
MAX_CANDIDATE_HIGHLIGHTS = 10
MAX_OPEN_QUESTIONS = 10


class _ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(_ModelOutput):
    """A span the model claims supports a claim, with the exact text it expects to find there."""

    entry_id: UUID
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_span(self) -> Citation:
        if self.end <= self.start:
            raise ValueError("citation end must be greater than start")
        return self


ClaimCategory = Literal["medication", "allergy", "symptom", "plan", "follow_up", "other"]


class Claim(_ModelOutput):
    text: str = Field(min_length=1, max_length=400)
    category: ClaimCategory
    risk_level: RiskLevel
    citations: list[Citation] = Field(min_length=1, max_length=MAX_CITATIONS_PER_CLAIM)


class CandidateHighlight(_ModelOutput):
    """A highlight the model proposes. Priority and risk come from the deterministic scorer."""

    citation: Citation
    risk_reason: str = Field(min_length=1, max_length=500)
    suggested_action: str = Field(min_length=1, max_length=240)
    clinical_entities: list[str] = Field(default_factory=list, max_length=20)


class DraftReview(_ModelOutput):
    claims: list[Claim] = Field(max_length=MAX_CLAIMS)
    open_questions: list[str] = Field(default_factory=list, max_length=MAX_OPEN_QUESTIONS)
    candidate_highlights: list[CandidateHighlight] = Field(
        default_factory=list, max_length=MAX_CANDIDATE_HIGHLIGHTS
    )


class CheckFailure(StrEnum):
    """Why a citation or claim failed verification (design §8, checks 1–5)."""

    UNKNOWN_ENTRY = "unknown_entry"
    OUT_OF_BOUNDS = "out_of_bounds"
    QUOTE_MISMATCH = "quote_mismatch"
    UNSUPPORTED_ENTITIES = "unsupported_entities"
    PATIENT_SOURCE_RISK = "patient_source_risk"


class ClaimCheck(BaseModel):
    claim_index: int = Field(ge=0)
    failures: list[CheckFailure] = Field(default_factory=list)
    detail: list[str] = Field(default_factory=list)
    # Citations after quote relocation; offsets here are the ones to show and to commit.
    citations: list[Citation] = Field(default_factory=list)

    @property
    def verified(self) -> bool:
        return not self.failures


class VerificationReport(BaseModel):
    checks: list[ClaimCheck] = Field(default_factory=list)
    # Deterministic conflicts no verified claim mentions (design §8, check 6).
    uncovered_conflict_ids: list[UUID] = Field(default_factory=list)

    @property
    def unsupported(self) -> list[ClaimCheck]:
        return [check for check in self.checks if not check.verified]

    @property
    def passed(self) -> bool:
        return not self.unsupported and not self.uncovered_conflict_ids


class Budget(BaseModel):
    """Per-run ceilings (design §10) and what the run has used so far."""

    max_steps: int = Field(default=12, ge=1)
    max_revisions: int = Field(default=2, ge=0)
    max_tokens: int = Field(default=60_000, ge=1)
    max_cost_usd: float = Field(default=0.50, gt=0)
    max_seconds: float = Field(default=60, gt=0)

    started_at: datetime
    steps: int = Field(default=0, ge=0)
    revisions: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)

    def exceeded(self, now: datetime) -> str | None:
        """Return why the run must stop, or None while it is within every ceiling."""
        if self.steps > self.max_steps:
            return f"step limit of {self.max_steps} exceeded"
        if self.revisions > self.max_revisions:
            return f"revision limit of {self.max_revisions} exceeded"
        if self.input_tokens + self.output_tokens > self.max_tokens:
            return f"token limit of {self.max_tokens} exceeded"
        if self.cost_usd > self.max_cost_usd:
            return f"cost limit of ${self.max_cost_usd:.2f} exceeded"
        if now - self.started_at > timedelta(seconds=self.max_seconds):
            return f"time limit of {self.max_seconds:g} s exceeded"
        return None

    @property
    def can_revise(self) -> bool:
        return self.revisions < self.max_revisions


class ClaimDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_index: int = Field(ge=0)
    action: Literal["accept", "edit", "reject"]
    text: str | None = Field(default=None, min_length=1, max_length=400)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_action(self) -> ClaimDecision:
        if (self.action == "edit") != (self.text is not None):
            raise ValueError("edited text is required for edit and only allowed for edit")
        if self.action == "reject" and self.reason is None:
            raise ValueError("a reason is required to reject a claim")
        return self


class HighlightDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    highlight_index: int = Field(ge=0)
    action: Literal["accept", "reject"]


class ReviewDecisions(BaseModel):
    """A clinician's response at `human_review`: per-item decisions, or a request to revise."""

    model_config = ConfigDict(extra="forbid")

    claims: list[ClaimDecision] = Field(default_factory=list)
    highlights: list[HighlightDecision] = Field(default_factory=list)
    revise_instruction: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_unique_items(self) -> ReviewDecisions:
        claim_indexes = [item.claim_index for item in self.claims]
        highlight_indexes = [item.highlight_index for item in self.highlights]
        if len(set(claim_indexes)) != len(claim_indexes):
            raise ValueError("each claim can be decided only once")
        if len(set(highlight_indexes)) != len(highlight_indexes):
            raise ValueError("each highlight can be decided only once")
        return self


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMMITTED = "committed"
    DISCARDED = "discarded"
    BUDGET_EXCEEDED = "budget_exceeded"
    FAILED = "failed"


class ReviewState(TypedDict):
    """Graph state for one run (design §6)."""

    run_id: UUID
    actor: AgentActor
    patient_id: UUID
    status: RunStatus
    stop_reason: str | None
    context: list[EntryRef]
    conflicts: list[Conflict]
    questions: list[str]
    evidence: dict[str, list[Span]]
    draft: DraftReview | None
    verification: VerificationReport | None
    budget: Budget
    decisions: ReviewDecisions | None
