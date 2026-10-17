from carenote.data.seed import DEMO_CLINIC_ID, DEMO_PATIENT_ID, seed_demo_data
from carenote.domain.models import EntryType
from carenote.repositories.memory import InMemoryCareNoteRepository


def test_seed_contains_patient_three_ai_types_and_valid_provenance():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)

    patient = repository.get_patient(DEMO_PATIENT_ID, DEMO_CLINIC_ID)
    entries = repository.list_entries(DEMO_PATIENT_ID, DEMO_CLINIC_ID)
    highlights = repository.list_highlights(DEMO_PATIENT_ID)

    assert patient is not None and patient.synthetic is True
    assert {
        EntryType.AI_DOCTOR_CONSULT_SUMMARY,
        EntryType.AI_NURSE_CONSULT_SUMMARY,
        EntryType.AI_PATIENT_SESSION_SUMMARY,
    }.issubset({entry.type for entry in entries})
    assert len({entry.timestamp.date() for entry in entries}) >= 3
    for highlight in highlights:
        pointer = highlight.provenance_pointer
        source = repository.entries[pointer.entry_id]
        assert source.content[pointer.start : pointer.end] == highlight.text


def test_seed_is_idempotent():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    seed_demo_data(repository)
    assert len(repository.patients) == 1
    assert len(repository.entries) == 6
    assert len(repository.highlights) == 3
    assert len(repository.source_artifacts) == 6
