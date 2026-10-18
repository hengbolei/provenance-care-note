"""Commit path: turn a clinician's review decisions into record changes (design §11).

Runs inside the graph's `commit` node, after a clinician has answered from their own session.
Every write goes through `CareNoteService` as that reviewing clinician, so the existing section
ownership, span, scope, revision and audit rules apply unchanged:

- accepted and edited claims become one new revision of the internal `agent_review` section;
  rejected and undecided claims are left out;
- accepted candidate highlights are created with `create_highlight`, at the citation offsets the
  verifier resolved in the original entry, with priority from the deterministic scorer.

Sections are care-team documents that patients never see, so the committed review stays
internal; patient-facing guidance still goes through the existing highlight instruction flow.
"""

from __future__ import annotations

from carenote.agent.graph import Committer
from carenote.agent.state import ReviewDecisions, ReviewState, VerificationReport
from carenote.agent.tools import ReviewTools
from carenote.domain.models import HighlightCreateRequest, ReviewStatus, RiskLevel
from carenote.services.care_notes import CareNoteService

AGENT_REVIEW_SECTION = "agent_review"


def check_commit_decisions(
    decisions: ReviewDecisions,
    verification: VerificationReport | None,
    highlight_citations: list,
) -> None:
    """Reject decisions the commit could not honour, before the run resumes.

    A claim that failed verification can be edited or rejected but not accepted as written, and
    a highlight whose citation did not resolve cannot be created.
    """
    checks = verification.checks if verification is not None else []
    for decision in decisions.claims:
        if decision.action == "accept" and not checks[decision.claim_index].verified:
            raise ValueError(
                f"claim {decision.claim_index} did not pass verification; edit or reject it"
            )
    for decision in decisions.highlights:
        if decision.action == "accept" and highlight_citations[decision.highlight_index] is None:
            raise ValueError(
                f"highlight {decision.highlight_index} has no verified source span to attach to"
            )


def commit_review(service: CareNoteService) -> Committer:
    def commit(state: ReviewState, tools: ReviewTools) -> None:
        reviewer = state["reviewer"]
        decisions = state["decisions"]
        draft = state["draft"]
        verification = state["verification"]
        if reviewer is None or decisions is None or draft is None or verification is None:
            raise RuntimeError("commit needs a reviewer, decisions, a draft and its verification")
        check_commit_decisions(decisions, verification, state["highlight_citations"])
        patient_id = state["patient_id"]

        lines: list[str] = []
        for decision in sorted(decisions.claims, key=lambda item: item.claim_index):
            if decision.action == "reject":
                continue
            claim = draft.claims[decision.claim_index]
            text = decision.text if decision.action == "edit" else claim.text
            lines.append(f"- {text}")
            for citation in verification.checks[decision.claim_index].citations:
                lines.append(
                    f"  Source: entry {citation.entry_id} [{citation.start}:{citation.end}] "
                    f'"{citation.quote}"'
                )
        if lines:
            header = (
                f"Agent review run {state['run_id']} (prompt {state['prompt_version']}), "
                "accepted by the reviewing clinician."
            )
            current = service.repository.get_section(patient_id, AGENT_REVIEW_SECTION)
            service.update_section(
                reviewer,
                patient_id,
                AGENT_REVIEW_SECTION,
                "\n".join([header, "", *lines]),
                expected_version=current.version if current else 0,
            )

        for decision in decisions.highlights:
            if decision.action != "accept":
                continue
            candidate = draft.candidate_highlights[decision.highlight_index]
            citation = state["highlight_citations"][decision.highlight_index]
            service.create_highlight(
                reviewer,
                patient_id,
                HighlightCreateRequest(
                    entry_id=citation.entry_id,
                    start=citation.start,
                    end=citation.end,
                    risk_reason=candidate.risk_reason,
                    suggested_action=candidate.suggested_action,
                    clinical_entities=candidate.clinical_entities,
                    # Model-proposed highlights land as moderate and needing review; the
                    # clinician sets the final risk and status in the highlight workflow.
                    risk_level=RiskLevel.MODERATE,
                    status=ReviewStatus.NEEDS_REVIEW,
                ),
            )

    return commit
