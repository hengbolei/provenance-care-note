from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from carenote.agent.state import (
    MAX_CLAIMS,
    Budget,
    CheckFailure,
    ClaimCheck,
    ClaimDecision,
    DraftReview,
    HighlightDecision,
    ReviewDecisions,
    VerificationReport,
)

START = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def _citation(**overrides):
    return {"entry_id": str(uuid4()), "start": 0, "end": 9, "quote": "metformin"} | overrides


def _claim(**overrides):
    return {
        "text": "Metformin was started at 500 mg.",
        "category": "medication",
        "risk_level": "moderate",
        "citations": [_citation()],
    } | overrides


def test_draft_review_parses_a_well_formed_model_response():
    draft = DraftReview.model_validate(
        {
            "claims": [_claim()],
            "open_questions": ["Is the patient taking it with food?"],
            "candidate_highlights": [
                {
                    "citation": _citation(),
                    "risk_reason": "New medication",
                    "suggested_action": "Confirm tolerance at follow-up",
                }
            ],
        }
    )

    assert draft.claims[0].citations[0].quote == "metformin"
    assert draft.candidate_highlights[0].clinical_entities == []


@pytest.mark.parametrize(
    "claim",
    [
        _claim(citations=[]),
        _claim(category="diagnosis"),
        _claim(risk_level="urgent"),
        _claim(text="x" * 401),
        _claim(citations=[_citation(start=9, end=9)]),
        _claim(citations=[_citation(quote="")]),
        _claim(confidence=0.9),
    ],
    ids=[
        "no-citation",
        "unknown-category",
        "unknown-risk",
        "text-too-long",
        "empty-span",
        "empty-quote",
        "extra-key",
    ],
)
def test_malformed_claims_are_rejected(claim):
    with pytest.raises(ValidationError):
        DraftReview.model_validate({"claims": [claim]})


def test_draft_size_is_capped():
    with pytest.raises(ValidationError):
        DraftReview.model_validate({"claims": [_claim()] * (MAX_CLAIMS + 1)})


def test_candidate_highlights_cannot_set_their_own_risk_or_priority():
    highlight = {
        "citation": _citation(),
        "risk_reason": "Allergy",
        "suggested_action": "Flag",
        "priority": 100,
    }
    with pytest.raises(ValidationError):
        DraftReview.model_validate({"claims": [], "candidate_highlights": [highlight]})


def test_draft_schema_forbids_additional_properties_for_structured_output():
    schema = DraftReview.model_json_schema()

    assert schema["additionalProperties"] is False
    for name in ("Claim", "Citation", "CandidateHighlight"):
        assert schema["$defs"][name]["additionalProperties"] is False


def test_verification_report_passes_only_when_all_claims_verify_and_conflicts_are_covered():
    verified = ClaimCheck(claim_index=0)
    failed = ClaimCheck(claim_index=1, failures=[CheckFailure.QUOTE_MISMATCH])

    assert VerificationReport(checks=[verified]).passed
    assert not VerificationReport(checks=[verified, failed]).passed
    assert VerificationReport(checks=[verified, failed]).unsupported == [failed]
    assert not VerificationReport(checks=[verified], uncovered_conflict_ids=[uuid4()]).passed


def test_budget_reports_the_first_ceiling_exceeded():
    budget = Budget(started_at=START)
    assert budget.exceeded(START) is None

    assert "step" in Budget(started_at=START, steps=13).exceeded(START)
    assert "token" in Budget(started_at=START, input_tokens=50_000, output_tokens=10_001).exceeded(
        START
    )
    assert "cost" in Budget(started_at=START, cost_usd=0.51).exceeded(START)
    assert "time" in budget.exceeded(START + timedelta(seconds=61))


def test_budget_allows_two_revisions_by_default():
    assert Budget(started_at=START, revisions=1).can_revise
    assert not Budget(started_at=START, revisions=2).can_revise


@pytest.mark.parametrize(
    "decision",
    [
        {"claim_index": 0, "action": "edit"},
        {"claim_index": 0, "action": "accept", "text": "Changed"},
        {"claim_index": 0, "action": "reject"},
    ],
    ids=["edit-without-text", "text-without-edit", "reject-without-reason"],
)
def test_claim_decisions_require_matching_details(decision):
    with pytest.raises(ValidationError):
        ClaimDecision.model_validate(decision)


def test_review_decisions_accept_each_item_once():
    decisions = ReviewDecisions(
        claims=[
            ClaimDecision(claim_index=0, action="accept"),
            ClaimDecision(claim_index=1, action="edit", text="Metformin 500 mg daily."),
            ClaimDecision(claim_index=2, action="reject", reason="Not supported by the note"),
        ],
        highlights=[HighlightDecision(highlight_index=0, action="accept")],
    )
    assert len(decisions.claims) == 3

    with pytest.raises(ValidationError):
        ReviewDecisions(
            claims=[
                ClaimDecision(claim_index=0, action="accept"),
                ClaimDecision(claim_index=0, action="reject", reason="Changed my mind"),
            ]
        )


def test_revise_needs_an_instruction_and_only_revise_takes_one():
    assert ReviewDecisions(action="revise", revise_instruction="Shorter.").action == "revise"
    with pytest.raises(ValidationError):
        ReviewDecisions(action="revise")
    with pytest.raises(ValidationError):
        ReviewDecisions(action="commit", revise_instruction="Shorter.")
