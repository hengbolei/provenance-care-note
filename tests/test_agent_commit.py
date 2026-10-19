from datetime import UTC, datetime

import pytest
from agent_fakes import ScriptedGateway
from fastapi.testclient import TestClient

from carenote.agent.commit import AGENT_REVIEW_SECTION, commit_review
from carenote.agent.gateway import ModelResult
from carenote.agent.runs import AgentCommitRequest, AgentRunService
from carenote.agent.state import (
    CandidateHighlight,
    Citation,
    Claim,
    ClaimDecision,
    ReviewPlan,
    RunStatus,
)
from carenote.api import routes
from carenote.core.identity import session_service
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID, seed_demo_data
from carenote.domain.models import Actor, ReviewStatus, RiskLevel, Role
from carenote.main import app
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
PATIENT = Actor(id=DEMO_PATIENT_ID, role=Role.PATIENT, clinic_id=DEMO_CLINIC_ID)


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


class GatewayWithUnsupportedItems(ScriptedGateway):
    """Adds a misquoted claim and a highlight whose quote is not in the record."""

    def structured(self, request, schema):
        result = super().structured(request, schema)
        if schema is ReviewPlan:
            return result
        draft = result.output
        good = draft.claims[0].citations[0]
        bad = Citation(entry_id=good.entry_id, start=0, end=10, quote="stop lisinopril tonight")
        draft = draft.model_copy(
            update={
                "claims": [
                    *draft.claims,
                    Claim(
                        text="Lisinopril was stopped tonight.",
                        category="medication",
                        risk_level=RiskLevel.MODERATE,
                        citations=[bad],
                    ),
                ],
                "candidate_highlights": [
                    *draft.candidate_highlights,
                    CandidateHighlight(
                        citation=bad, risk_reason="Unverified", suggested_action="Check"
                    ),
                ],
            }
        )
        return ModelResult(output=draft, model=result.model, usage=result.usage)


@pytest.fixture
def service():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    return CareNoteService(repository)


def _runs(service, gateway=None):
    return AgentRunService(
        service,
        gateway or GatewayWithUnsupportedItems(),
        committer=commit_review(service),
        executor=InlineExecutor(),
        now=lambda: NOW,
    )


def _commit(runs, run_id, claims=(), highlights=()):
    return runs.commit(
        CLINICIAN,
        run_id,
        AgentCommitRequest.model_validate({"claims": list(claims), "highlights": list(highlights)}),
    )


def test_accepted_and_edited_claims_become_an_agent_review_revision(service):
    runs = _runs(service)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    view = _commit(
        runs,
        run_id,
        claims=[
            {"claim_index": 0, "action": "accept"},
            {"claim_index": 1, "action": "edit", "text": "Lisinopril 10 mg started last week."},
            {"claim_index": 2, "action": "reject", "reason": "Not in the record"},
        ],
    )

    assert view.status is RunStatus.COMMITTED
    section = service.repository.get_section(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION)
    assert section.owner_role is Role.CLINICIAN
    assert section.version == 1
    assert "- Morning lisinopril is on hold pending review." in section.content
    assert "- Lisinopril 10 mg started last week." in section.content
    assert "stopped tonight" not in section.content
    assert '"Hold morning lisinopril"' in section.content
    [revision] = service.repository.list_revisions(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION)
    assert revision.changed_by == CLINICIAN.id


def test_each_commit_adds_a_revision_to_the_same_section(service):
    runs = _runs(service)
    for _ in range(2):
        run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id
        _commit(runs, run_id, claims=[{"claim_index": 0, "action": "accept"}])

    assert service.repository.get_section(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION).version == 2


def test_an_unverified_claim_cannot_be_accepted_as_written(service):
    runs = _runs(service)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    with pytest.raises(ValueError, match="did not pass verification"):
        _commit(runs, run_id, claims=[{"claim_index": 2, "action": "accept"}])
    assert runs.get_run(CLINICIAN, run_id).status is RunStatus.AWAITING_REVIEW

    _commit(runs, run_id, claims=[{"claim_index": 2, "action": "edit", "text": "Check dosing."}])
    section = service.repository.get_section(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION)
    assert "- Check dosing." in section.content


def test_accepted_highlights_are_created_at_the_verified_span(service):
    runs = _runs(service)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id
    before = set(service.repository.highlights)

    _commit(runs, run_id, highlights=[{"highlight_index": 0, "action": "accept"}])

    [new_id] = set(service.repository.highlights) - before
    highlight = service.repository.highlights[new_id]
    entry = service.repository.get_entry(highlight.provenance_pointer.entry_id)
    pointer = highlight.provenance_pointer
    assert entry.content[pointer.start : pointer.end] == "Hold morning lisinopril"
    assert highlight.text == "Hold morning lisinopril"
    assert highlight.status is ReviewStatus.NEEDS_REVIEW
    assert highlight.risk_level is RiskLevel.MODERATE
    assert highlight.priority > 0
    assert highlight.updated_by == CLINICIAN.id


def test_a_highlight_without_a_verified_span_cannot_be_accepted(service):
    runs = _runs(service)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    with pytest.raises(ValueError, match="no verified source span"):
        _commit(runs, run_id, highlights=[{"highlight_index": 1, "action": "accept"}])


def test_committing_nothing_accepted_writes_nothing(service):
    runs = _runs(service)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id
    highlights_before = len(service.repository.highlights)

    view = _commit(runs, run_id, claims=[{"claim_index": 0, "action": "reject", "reason": "No"}])

    assert view.status is RunStatus.COMMITTED
    assert service.repository.get_section(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION) is None
    assert len(service.repository.highlights) == highlights_before


def test_the_committed_review_stays_internal(service):
    runs = _runs(service)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id
    _commit(runs, run_id, claims=[{"claim_index": 0, "action": "accept"}])

    patient_view = service.get_patient_view(PATIENT, DEMO_PATIENT_ID)
    clinician_view = service.get_patient_view(CLINICIAN, DEMO_PATIENT_ID)

    assert patient_view.sections == []
    assert AGENT_REVIEW_SECTION in {section.section for section in clinician_view.sections}


def test_claim_decision_models_are_accepted_directly(service):
    runs = _runs(service)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    view = runs.commit(
        CLINICIAN,
        run_id,
        AgentCommitRequest(claims=[ClaimDecision(claim_index=0, action="accept")]),
    )

    assert view.status is RunStatus.COMMITTED


def test_api_commit_writes_the_review(monkeypatch, service):
    monkeypatch.setattr(routes, "repository", service.repository)
    monkeypatch.setattr(routes, "service", service)
    monkeypatch.setattr(routes, "agent_runs", _runs(service))
    token, _ = session_service.issue(CLINICIAN)
    headers = {"authorization": f"Bearer {token}"}
    client = TestClient(app)

    run_id = client.post(f"/api/patients/{DEMO_PATIENT_ID}/agent-runs", headers=headers).json()[
        "run_id"
    ]
    rejected = client.post(
        f"/api/agent-runs/{run_id}/commit",
        headers=headers,
        json={"claims": [{"claim_index": 2, "action": "accept"}]},
    )
    assert rejected.status_code == 422

    committed = client.post(
        f"/api/agent-runs/{run_id}/commit",
        headers=headers,
        json={
            "claims": [{"claim_index": 0, "action": "accept"}],
            "highlights": [{"highlight_index": 0, "action": "accept"}],
        },
    )
    assert committed.status_code == 200
    assert committed.json()["status"] == "committed"
    sections = client.get(f"/api/patients/{DEMO_PATIENT_ID}", headers=headers).json()["sections"]
    assert AGENT_REVIEW_SECTION in {section["section"] for section in sections}
