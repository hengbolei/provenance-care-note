"""The offline demo: the committed seed cassette must replay and verify with no API key.

If this fails after changing the draft prompt, the seed data or how requests are framed,
re-record with `python scripts/record_demo_cassette.py`.
"""

from datetime import UTC, datetime

from carenote.agent.commit import AGENT_REVIEW_SECTION, commit_review
from carenote.agent.gateway import gateway_from_settings
from carenote.agent.runs import AgentCommitRequest, AgentRunService
from carenote.agent.state import RunStatus
from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    seed_demo_data,
    seed_demo_knowledge,
)
from carenote.domain.models import Actor, Role
from carenote.knowledge.service import KnowledgeService
from carenote.knowledge.store import InMemoryKnowledgeStore
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


def _runs():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    service = CareNoteService(repository)
    knowledge = KnowledgeService(InMemoryKnowledgeStore(), repository)
    seed_demo_knowledge(knowledge.store)
    runs = AgentRunService(
        service,
        gateway_from_settings("recorded"),
        committer=commit_review(service),
        executor=InlineExecutor(),
        now=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        knowledge=knowledge,
    )
    return runs, service


def test_seed_cassette_replays_to_a_fully_verified_draft():
    runs, _ = _runs()

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    assert view.status is RunStatus.AWAITING_REVIEW, view.error
    assert view.verification.passed
    assert view.model.startswith("curated-demo")
    assert len(view.draft.claims) == 6
    assert len(view.draft.open_questions) == 2
    assert len(view.draft.check_points) == 2
    assert all(check.verified for check in view.verification.check_points)
    assert {label.title for label in view.references} == {
        "Lisinopril tablets (synthetic label)",
        "Follow-up after antihypertensive dose changes",
    }
    assert all(citation is not None for citation in view.highlight_citations)


def test_offline_demo_commits_end_to_end():
    runs, service = _runs()
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)
    before = len(service.repository.highlights)

    committed = runs.commit(
        CLINICIAN,
        view.run_id,
        AgentCommitRequest.model_validate(
            {
                "claims": [
                    {"claim_index": index, "action": "accept"}
                    for index in range(len(view.draft.claims))
                ],
                "highlights": [{"highlight_index": 0, "action": "accept"}],
            }
        ),
    )

    assert committed.status is RunStatus.COMMITTED
    section = service.repository.get_section(DEMO_PATIENT_ID, AGENT_REVIEW_SECTION)
    assert section.content.count("\n- ") == 6
    assert len(service.repository.highlights) == before + 1
