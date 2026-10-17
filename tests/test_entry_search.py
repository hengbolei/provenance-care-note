from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from carenote.domain.models import (
    EntryOrigin,
    EntryType,
    ProvenanceSource,
    Role,
    Span,
    TimelineEntry,
)
from carenote.repositories.memory import InMemoryCareNoteRepository

CLINIC = uuid4()
OTHER_CLINIC = uuid4()
PATIENT = uuid4()
BASE = datetime(2026, 10, 1, tzinfo=UTC)


def _entry(content, *, days=0, clinic=CLINIC, patient=PATIENT, internal=True):
    return TimelineEntry(
        patient_id=patient,
        clinic_id=clinic,
        author_role=Role.CLINICIAN,
        author_id=uuid4(),
        timestamp=BASE + timedelta(days=days),
        type=EntryType.CLINICIAN_NOTE,
        title="Note",
        content=content,
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY, source_id="manual", source_label="Manual"
        ),
        internal=internal,
    )


@pytest.fixture
def repo():
    return InMemoryCareNoteRepository()


def test_hits_are_exact_citable_spans(repo):
    entry = repo.add_entry(_entry("Slept well.  Started metformin 500mg with breakfast. No rash."))

    [hit] = repo.search_entries(PATIENT, CLINIC, "metformin dose 500 mg")

    assert hit.entry_id == entry.id
    assert hit.text == "Started metformin 500mg with breakfast."
    assert entry.content[hit.start : hit.end] == hit.text


def test_more_matching_terms_rank_first_then_more_recent_entries(repo):
    repo.add_entry(_entry("Dizziness reported.", days=1))
    recent = repo.add_entry(_entry("Dizziness again today.", days=5))
    best = repo.add_entry(_entry("Dizziness after lisinopril dose.", days=0))

    hits = repo.search_entries(PATIENT, CLINIC, "lisinopril dizziness")

    assert [hit.entry_id for hit in hits][:2] == [best.id, recent.id]


def test_decimal_doses_stay_in_one_passage(repo):
    repo.add_entry(_entry("Reduce warfarin to 2.5 mg daily. Recheck INR."))

    [hit] = repo.search_entries(PATIENT, CLINIC, "warfarin 2.5")

    assert hit.text == "Reduce warfarin to 2.5 mg daily."


def test_search_is_scoped_to_patient_and_clinic(repo):
    repo.add_entry(_entry("Allergic to penicillin.", clinic=OTHER_CLINIC))
    repo.add_entry(_entry("Allergic to penicillin.", patient=uuid4()))

    assert repo.search_entries(PATIENT, CLINIC, "penicillin") == []


def test_internal_entries_can_be_excluded_for_patient_facing_use(repo):
    repo.add_entry(_entry("Internal: query penicillin allergy.", internal=True))
    shared = repo.add_entry(_entry("Avoid penicillin.", internal=False))

    hits = repo.search_entries(PATIENT, CLINIC, "penicillin", include_internal=False)

    assert [hit.entry_id for hit in hits] == [shared.id]


def test_archived_entries_are_not_searched(repo):
    entry = repo.add_entry(_entry("Stop aspirin before surgery."))
    repo.archive_entries([entry.id])

    assert repo.search_entries(PATIENT, CLINIC, "aspirin") == []


def test_stopword_only_queries_return_nothing_and_k_is_bounded(repo):
    repo.add_entry(_entry("The plan is to review in a week."))

    assert repo.search_entries(PATIENT, CLINIC, "the and of") == []
    for k in (0, 51):
        with pytest.raises(ValueError):
            repo.search_entries(PATIENT, CLINIC, "plan", k=k)


def test_span_rejects_text_that_does_not_match_its_range():
    with pytest.raises(ValueError):
        Span(entry_id=uuid4(), start=0, end=5, text="abc")
