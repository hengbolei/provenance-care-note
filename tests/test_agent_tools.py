from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from carenote.agent.state import Citation
from carenote.agent.tools import ReviewTools
from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    seed_demo_data,
)
from carenote.domain.models import (
    Actor,
    AgentActor,
    EntryOrigin,
    EntryType,
    ProvenanceSource,
    Role,
    TimelineEntry,
)
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import AccessDeniedError, CareNoteService

NAMED_NOTE = "Maya Chen (Synthetic) asked for a call on 91234567 about lisinopril."


@pytest.fixture
def repository():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    return repository


@pytest.fixture
def named_entry(repository):
    return repository.add_entry(
        TimelineEntry(
            patient_id=DEMO_PATIENT_ID,
            clinic_id=DEMO_CLINIC_ID,
            author_role=Role.STAFF,
            author_id=uuid4(),
            timestamp=datetime(2026, 10, 18, tzinfo=UTC),
            type=EntryType.STAFF_NOTE,
            title="Call request",
            content=NAMED_NOTE,
            origin=EntryOrigin(
                source=ProvenanceSource.MANUAL_ENTRY, source_id="call", source_label="Call"
            ),
        )
    )


@pytest.fixture
def tools(repository):
    clinician = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
    return ReviewTools(
        CareNoteService(repository),
        AgentActor.for_run(clinician, uuid4()),
        DEMO_PATIENT_ID,
        today=date(2026, 10, 18),
    )


def test_overview_omits_identifiers(tools):
    overview = tools.get_patient_overview()

    assert overview.age_years == 41
    assert overview.entry_count == 6
    assert overview.open_highlights
    dumped = overview.model_dump_json()
    for identifier in ("Maya Chen", "SYN-000184", "1985"):
        assert identifier not in dumped


def test_list_entries_returns_refs_newest_first_with_filters(tools):
    refs = tools.list_entries()
    assert len(refs) == 6
    assert [ref.timestamp for ref in refs] == sorted((ref.timestamp for ref in refs), reverse=True)

    patient_notes = tools.list_entries(types=[EntryType.PATIENT_NOTE])
    assert [ref.type for ref in patient_notes] == [EntryType.PATIENT_NOTE]
    assert len(tools.list_entries(limit=2)) == 2
    with pytest.raises(ValueError):
        tools.list_entries(limit=0)


def test_read_entry_redacts_and_keeps_the_offset_map(tools, named_entry):
    entry_text = tools.read_entry(named_entry.id)

    assert "Maya Chen" not in entry_text.text
    assert "91234567" not in entry_text.text
    assert entry_text.text.startswith("[REDACTED_NAME] asked for a call on [REDACTED_PHONE]")
    redaction = tools.redactions[named_entry.id]
    start = entry_text.text.index("lisinopril")
    original = redaction.to_original(start, start + len("lisinopril"))
    assert NAMED_NOTE[original[0] : original[1]] == "lisinopril"


def test_search_hits_are_spans_of_the_redacted_text(tools, named_entry):
    hits = tools.search_entries("call lisinopril")

    top = hits[0]
    assert top.entry_id == named_entry.id
    assert "Maya Chen" not in top.text
    assert tools.read_entry(named_entry.id).text[top.start : top.end] == top.text


def test_source_evidence_is_redacted(tools):
    ai_entry = tools.list_entries(types=[EntryType.AI_NURSE_CONSULT_SUMMARY])[0]

    excerpt = tools.get_source_evidence(ai_entry.entry_id)

    assert excerpt.excerpt
    assert "Maya Chen" not in excerpt.excerpt


def test_detect_conflicts_returns_the_deterministic_findings(tools):
    conflicts = tools.detect_conflicts()

    assert {conflict.entity for conflict in conflicts} == {"lisinopril"}


def test_entries_outside_the_patient_record_are_not_readable(tools):
    with pytest.raises(LookupError):
        tools.read_entry(uuid4())


def test_service_search_is_care_team_only_and_denies_the_agent_identity(repository):
    staff = Actor(id=uuid4(), role=Role.STAFF, clinic_id=DEMO_CLINIC_ID)
    service = CareNoteService(repository)

    with pytest.raises(AccessDeniedError):
        service.search_entries(
            Actor(id=DEMO_PATIENT_ID, role=Role.PATIENT, clinic_id=DEMO_CLINIC_ID),
            DEMO_PATIENT_ID,
            "lisinopril",
        )
    assert service.search_entries(staff, DEMO_PATIENT_ID, "lisinopril")
    with pytest.raises(AccessDeniedError):
        service.search_entries(
            AgentActor.for_run(
                Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID), uuid4()
            ),
            DEMO_PATIENT_ID,
            "lisinopril",
        )


def test_a_run_cannot_reach_another_clinic(repository):
    other_clinic = Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=uuid4())
    tools = ReviewTools(
        CareNoteService(repository), AgentActor.for_run(other_clinic, uuid4()), DEMO_PATIENT_ID
    )

    with pytest.raises(LookupError):
        tools.list_entries()


def test_propose_highlight_holds_candidates_in_run_state_only(tools, repository, named_entry):
    highlights_before = len(repository.highlights)
    text = tools.read_entry(named_entry.id).text
    start = text.index("lisinopril")
    citation = Citation(
        entry_id=named_entry.id, start=start, end=start + len("lisinopril"), quote="lisinopril"
    )

    index = tools.propose_highlight(citation, "Dizziness on lisinopril", "Review dose")

    assert index == 0
    assert tools.proposed[0].citation == citation
    assert len(repository.highlights) == highlights_before


def test_propose_highlight_rejects_spans_outside_the_entry(tools, named_entry):
    citation = Citation(entry_id=named_entry.id, start=500, end=510, quote="lisinopril")

    with pytest.raises(ValueError):
        tools.propose_highlight(citation, "Reason", "Action")
    assert tools.proposed == []


def test_tool_calls_are_recorded_as_metadata_only(tools, named_entry):
    tools.get_patient_overview()
    tools.read_entry(named_entry.id)
    tools.search_entries("lisinopril", k=3)

    assert [call.name for call in tools.calls] == [
        "get_patient_overview",
        "read_entry",
        "search_entries",
    ]
    assert tools.calls[1].entry_id == named_entry.id
    assert "lisinopril" not in "".join(call.model_dump_json() for call in tools.calls)


def test_context_entries_is_the_principals_view(tools, named_entry):
    context = tools.context_entries()

    assert named_entry.id in context
    assert all(entry.patient_id == DEMO_PATIENT_ID for entry in context.values())
