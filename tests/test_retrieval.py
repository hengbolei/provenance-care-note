from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from carenote.domain.models import EntryOrigin, EntryType, ProvenanceSource, Role, TimelineEntry
from carenote.retrieval import MAX_RESULTS, BM25Retriever
from carenote.retrieval.synonyms import CLASSES, GROUPS, concept

BASE = datetime(2026, 10, 1, tzinfo=UTC)
retriever = BM25Retriever()


def _entry(content, *, days=0):
    return TimelineEntry(
        patient_id=uuid4(),
        clinic_id=uuid4(),
        author_role=Role.CLINICIAN,
        author_id=uuid4(),
        timestamp=BASE + timedelta(days=days),
        type=EntryType.CLINICIAN_NOTE,
        title="Note",
        content=content,
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY, source_id="manual", source_label="Manual"
        ),
    )


def _ids(hits):
    return [hit.entry_id for hit in hits]


def test_brand_and_generic_names_find_each_other():
    brand = _entry("Glucophage 850 mg twice daily continues.")
    generic = _entry("Pharmacy list shows metformin 500 mg twice daily.")

    assert set(_ids(retriever.search([brand, generic], "metformin", 5))) == {brand.id, generic.id}
    assert set(_ids(retriever.search([brand, generic], "Glucophage", 5))) == {brand.id, generic.id}


def test_spelling_variants_and_plurals_match():
    note = _entry("Low haemoglobin. Three observations recorded.")

    [hit] = retriever.search([note], "hemoglobin", 5)
    assert hit.text == "Low haemoglobin."
    [hit] = retriever.search([note], "observation", 5)
    assert hit.text == "Three observations recorded."


def test_a_class_query_finds_its_members_below_exact_mentions():
    member = _entry("Started apixaban 5 mg twice daily.", days=5)
    named = _entry("Anticoagulant reviewed at clinic.")

    assert _ids(retriever.search([member, named], "anticoagulants", 5)) == [named.id, member.id]


def test_a_member_query_does_not_find_other_members_of_its_class():
    warfarin = _entry("Warfarin 3 mg at night.")

    assert retriever.search([warfarin], "apixaban", 5) == []


def test_rare_terms_outweigh_terms_that_fill_the_timeline():
    routine = [_entry(f"Routine observation {n} recorded.", days=n) for n in range(1, 30)]
    rare = _entry("Lip swelling after cefalexin.")

    hits = retriever.search([*routine, rare], "routine cefalexin", 5)

    assert hits[0].entry_id == rare.id


def test_equal_matches_favour_the_more_recent_entry():
    older = _entry("Dizziness reported after standing.")
    newer = _entry("Dizziness again today.", days=3)

    assert _ids(retriever.search([older, newer], "dizzy", 5)) == [newer.id, older.id]


def test_hits_are_citable_spans():
    note = _entry("Slept well.  Reduce warfarin to 2.5 mg daily. Recheck INR.")

    [hit] = retriever.search([note], "Coumadin 2.5", 5)

    assert hit.text == "Reduce warfarin to 2.5 mg daily."
    assert note.content[hit.start : hit.end] == hit.text


def test_empty_queries_and_records_return_nothing_and_k_is_bounded():
    note = _entry("The plan is to review in a week.")

    assert retriever.search([note], "the and of", 5) == []
    assert retriever.search([], "plan", 5) == []
    for k in (0, MAX_RESULTS + 1):
        with pytest.raises(ValueError):
            retriever.search([note], "plan", k)


def test_synonym_groups_do_not_overlap_and_classes_name_canonical_drugs():
    words = [word for group in GROUPS for word in group]
    assert len(words) == len(set(words))
    for members in CLASSES.values():
        for member in members:
            assert concept(member) == member
