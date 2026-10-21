"""Committing check points: written with their reference version, audited, failures refused."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agent_fakes import CheckPointGateway

from carenote.agent.commit import AGENT_REVIEW_SECTION, commit_review
from carenote.agent.runs import AgentCommitRequest, AgentRunService
from carenote.agent.state import RunStatus
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID, seed_demo_data
from carenote.domain.models import Actor, Role
from carenote.knowledge.models import KnowledgeDocumentCreate
from carenote.knowledge.service import KnowledgeService
from carenote.knowledge.store import InMemoryKnowledgeStore
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
ADMIN = Actor(id=uuid4(), role=Role.ADMIN, clinic_id=DEMO_CLINIC_ID)
LABEL = (
    "Lisinopril tablets.\n"
    "Symptomatic hypotension may occur after the first dose or a dose increase.\n"
    "Check blood pressure and kidney function after a dose change."
)
GOOD = "Check blood pressure after the lisinopril dose change."


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


def _runs(texts):
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    service = CareNoteService(repository)
    knowledge = KnowledgeService(InMemoryKnowledgeStore(), repository, now=lambda: NOW)
    document, _ = knowledge.create_document(
        ADMIN,
        KnowledgeDocumentCreate(
            kind="drug_label",
            title="Lisinopril label",
            source="Synthetic",
            version_label="2025 edition",
            effective_date="2025-01-01",
            content=LABEL,
        ),
    )
    runs = AgentRunService(
        service,
        CheckPointGateway(texts),
        committer=commit_review(service),
        executor=InlineExecutor(),
        now=lambda: NOW,
        knowledge=knowledge,
    )
    return runs, service, document


def _commit(runs, run_id, check_points, claims=()):
    return runs.commit(
        CLINICIAN,
        run_id,
        AgentCommitRequest.model_validate(
            {"claims": list(claims), "check_points": list(check_points)}
        ),
    )


def test_the_run_view_names_the_cited_reference_version():
    runs, _, document = _runs([GOOD])

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    [label] = view.references
    assert (label.document_id, label.version, label.title, label.kind, label.label) == (
        document.id,
        1,
        "Lisinopril label",
        "drug_label",
        "2025 edition",
    )
    assert view.verification.check_points[0].verified


def test_accepted_check_points_are_written_with_their_reference():
    runs, care, _ = _runs([GOOD])
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    view = _commit(
        runs,
        run_id,
        [{"check_point_index": 0, "action": "accept"}],
        claims=[{"claim_index": 0, "action": "accept"}],
    )

    assert view.status is RunStatus.COMMITTED
    content = care.repository.get_section(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION).content
    assert "Points to check (from the reference library):" in content
    assert f"- {GOOD}" in content
    assert "Reference: Lisinopril label, v1 (2025 edition)" in content
    operations = [event.operation for event in care.repository.audit_events]
    assert "agent_check_point_accepted" in operations
    assert care.repository.verify_audit_chain()


def test_rejected_check_points_are_left_out_but_audited():
    runs, care, _ = _runs([GOOD])
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    _commit(
        runs,
        run_id,
        [{"check_point_index": 0, "action": "reject"}],
        claims=[{"claim_index": 0, "action": "accept"}],
    )

    content = care.repository.get_section(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION).content
    assert "Points to check" not in content
    assert "agent_check_point_rejected" in [e.operation for e in care.repository.audit_events]


def test_an_unverified_check_point_cannot_be_accepted():
    # Every draft, including both automatic revisions, gives treatment advice.
    runs, _, _ = _runs(["Increase lisinopril to 20 mg."])
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    with pytest.raises(ValueError, match="check point 0 did not pass verification"):
        _commit(runs, run_id, [{"check_point_index": 0, "action": "accept"}])


def test_a_decision_for_a_missing_check_point_is_refused():
    runs, _, _ = _runs([GOOD])
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id

    with pytest.raises(ValueError, match="check point the draft does not have"):
        _commit(runs, run_id, [{"check_point_index": 3, "action": "reject"}])
