from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID, seed_demo_data
from carenote.domain.models import (
    Actor,
    EntryOrigin,
    EntryType,
    Highlight,
    ProvenanceSource,
    ReviewStatus,
    Role,
    SourceSpan,
    TimelineEntry,
)
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService


def test_highlight_pointer_resolves_to_timeline_span(repository, patient_id, clinic_id):
    entry = repository.add_entry(
        TimelineEntry(
            patient_id=patient_id,
            clinic_id=clinic_id,
            author_role=Role.SYSTEM,
            author_id=None,
            type=EntryType.AI_NURSE_CONSULT_SUMMARY,
            title="Synthetic nurse consultation",
            content="Patient reports dizziness after medication.",
            origin=EntryOrigin(
                source=ProvenanceSource.NURSE_CONSULT,
                source_id="test-nurse-consult",
                source_label="Synthetic nurse consultation",
            ),
        )
    )
    highlight = repository.add_highlight(
        Highlight(
            patient_id=patient_id,
            text="dizziness",
            risk_reason="New symptom after medication",
            suggested_action="Review medication timing",
            priority=90,
            status=ReviewStatus.AI_SUGGESTED,
            provenance_pointer=SourceSpan(entry_id=entry.id, start=16, end=25),
        )
    )
    source = repository.entries[highlight.provenance_pointer.entry_id]
    span = highlight.provenance_pointer
    assert source.content[span.start : span.end] == highlight.text


def test_highlight_resolves_through_summary_to_original_source_span():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    service = CareNoteService(repository)
    actor = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
    highlight = repository.list_highlights(DEMO_PATIENT_ID)[1]
    summary = repository.get_entry(highlight.provenance_pointer.entry_id)

    assert summary is not None
    evidence = service.get_source_evidence(actor, DEMO_PATIENT_ID, summary.id)

    pointer = summary.origin.source_pointer
    assert pointer is not None
    assert evidence.pointer == pointer
    assert evidence.artifact.content[pointer.start : pointer.end] == evidence.excerpt
    assert evidence.excerpt
