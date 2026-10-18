from datetime import UTC, datetime
from uuid import uuid4

import pytest

from carenote.agent.state import CheckFailure, Citation, Claim, DraftReview
from carenote.agent.verify import verify_draft
from carenote.domain.models import (
    Conflict,
    ConflictCategory,
    ConflictStatus,
    EntryOrigin,
    EntryType,
    ProvenanceSource,
    RiskLevel,
    Role,
    TimelineEntry,
)
from carenote.privacy.redaction import PHIRedactionGateway

CLINIC = uuid4()
PATIENT = uuid4()

CLINICIAN_TEXT = "Plan: started metformin 500 mg daily. Not allergic to latex."
PATIENT_TEXT = "I feel dizzy every morning and I am allergic to penicillin."


def _entry(content, *, role=Role.CLINICIAN, entry_type=EntryType.CLINICIAN_NOTE):
    return TimelineEntry(
        patient_id=PATIENT,
        clinic_id=CLINIC,
        author_role=role,
        author_id=uuid4(),
        timestamp=datetime(2026, 10, 1, tzinfo=UTC),
        type=entry_type,
        title="Note",
        content=content,
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY, source_id="manual", source_label="Manual"
        ),
    )


@pytest.fixture
def clinician_entry():
    return _entry(CLINICIAN_TEXT)


@pytest.fixture
def patient_entry():
    return _entry(PATIENT_TEXT, role=Role.PATIENT, entry_type=EntryType.PATIENT_NOTE)


@pytest.fixture
def entries(clinician_entry, patient_entry):
    return {clinician_entry.id: clinician_entry, patient_entry.id: patient_entry}


def _cite(entry, phrase, *, start=None, quote=None):
    begin = entry.content.index(phrase) if start is None else start
    return Citation(entry_id=entry.id, start=begin, end=begin + len(phrase), quote=quote or phrase)


def _claim(text, *citations, risk=RiskLevel.MODERATE, category="medication"):
    return Claim(text=text, category=category, risk_level=risk, citations=list(citations))


def _verify(entries, *claims, conflicts=(), redactions=None):
    return verify_draft(DraftReview(claims=list(claims)), entries, conflicts, redactions)


def _only_check(report):
    [check] = report.checks
    return check


# Checks 1-3: existence, bounds, quote match


def test_exact_citation_verifies(entries, clinician_entry):
    report = _verify(
        entries,
        _claim(
            "Metformin was started at 500 mg daily.",
            _cite(clinician_entry, "started metformin 500 mg daily"),
        ),
    )

    check = _only_check(report)
    assert check.verified, check.detail
    assert report.passed
    assert check.citations[0].quote == "started metformin 500 mg daily"


def test_citation_to_an_entry_outside_the_context_fails(entries, clinician_entry):
    stranger = _entry(CLINICIAN_TEXT)
    check = _only_check(
        _verify(entries, _claim("Metformin started.", _cite(stranger, "started metformin")))
    )

    assert check.failures == [CheckFailure.UNKNOWN_ENTRY]
    assert check.citations == []


def test_out_of_bounds_citation_without_a_findable_quote_fails(entries, clinician_entry):
    citation = Citation(entry_id=clinician_entry.id, start=500, end=520, quote="insulin glargine")
    check = _only_check(_verify(entries, _claim("Insulin was started.", citation)))

    assert check.failures == [CheckFailure.OUT_OF_BOUNDS]


def test_misquoted_citation_fails(entries, clinician_entry):
    citation = _cite(clinician_entry, "started metformin", quote="stopped metformin")
    check = _only_check(_verify(entries, _claim("Metformin was stopped.", citation)))

    assert check.failures == [CheckFailure.QUOTE_MISMATCH]


def test_drifted_offsets_are_repaired_from_the_quote(entries, clinician_entry):
    citation = _cite(clinician_entry, "started metformin 500 mg", start=0)
    check = _only_check(_verify(entries, _claim("Metformin 500 mg was started.", citation)))

    assert check.verified, check.detail
    repaired = check.citations[0]
    assert clinician_entry.content[repaired.start : repaired.end] == "started metformin 500 mg"


def test_quote_matching_ignores_whitespace_differences(clinician_entry):
    entry = _entry("Plan:\n  started   metformin\n500 mg daily.")
    citation = Citation(entry_id=entry.id, start=0, end=5, quote="started metformin 500 mg")
    check = _only_check(_verify({entry.id: entry}, _claim("Metformin 500 mg started.", citation)))

    assert check.verified, check.detail
    assert check.citations[0].quote == "started   metformin\n500 mg"


def test_out_of_bounds_offsets_with_a_findable_quote_are_repaired(entries, clinician_entry):
    citation = Citation(entry_id=clinician_entry.id, start=400, end=417, quote="started metformin")
    check = _only_check(_verify(entries, _claim("Metformin was started.", citation)))

    assert check.verified, check.detail


# Redacted text: the model cites what it saw, results point into the original


def test_citations_against_redacted_text_map_back_to_the_original():
    entry = _entry("Maya Chen started metformin 500 mg; call 91234567 if dizzy.")
    redaction = PHIRedactionGateway().redact(entry.content, ["Maya Chen"])
    shown = "[REDACTED_NAME] started metformin 500 mg"
    citation = Citation(entry_id=entry.id, start=0, end=len(shown), quote=shown)

    check = _only_check(
        _verify(
            {entry.id: entry},
            _claim("Metformin 500 mg was started.", citation),
            redactions={entry.id: redaction},
        )
    )

    assert check.verified, check.detail
    assert check.citations[0].quote == "Maya Chen started metformin 500 mg"
    assert (check.citations[0].start, check.citations[0].end) == (0, 34)


# Check 4: the cited text must support the claim's clinical facts


def test_claim_naming_a_drug_absent_from_the_citation_fails(entries, clinician_entry):
    check = _only_check(
        _verify(
            entries,
            _claim("Started lisinopril 10 mg daily.", _cite(clinician_entry, "started metformin")),
        )
    )

    assert check.failures == [CheckFailure.UNSUPPORTED_ENTITIES]
    assert "lisinopril" in check.detail[0]


def test_claim_with_a_different_dose_fails(entries, clinician_entry):
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Metformin 1000 mg was started.",
                _cite(clinician_entry, "started metformin 500 mg"),
            ),
        )
    )

    assert check.failures == [CheckFailure.UNSUPPORTED_ENTITIES]
    assert "1000mg" in check.detail[0]


def test_claim_contradicting_allergy_polarity_fails(entries, clinician_entry):
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Patient is allergic to latex.",
                _cite(clinician_entry, "Not allergic to latex"),
                category="allergy",
            ),
        )
    )

    assert check.failures == [CheckFailure.UNSUPPORTED_ENTITIES]
    assert "absent" in check.detail[0]


def test_claim_without_extractable_facts_needs_only_valid_citations(entries, clinician_entry):
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Follow-up planned.",
                _cite(clinician_entry, "Plan"),
                category="follow_up",
            ),
        )
    )

    assert check.verified, check.detail


# Check 5: provenance policy for patient-sourced claims


def test_patient_sourced_claim_phrased_as_report_verifies(entries, patient_entry):
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Patient reports being allergic to penicillin.",
                _cite(patient_entry, "allergic to penicillin"),
                category="allergy",
            ),
        )
    )

    assert check.verified, check.detail


def test_patient_sourced_claim_stated_as_fact_fails(entries, patient_entry):
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Allergic to penicillin.",
                _cite(patient_entry, "allergic to penicillin"),
                category="allergy",
            ),
        )
    )

    assert check.failures == [CheckFailure.PATIENT_SOURCE_PHRASING]


@pytest.mark.parametrize("risk", [RiskLevel.HIGH, RiskLevel.CRITICAL])
def test_patient_sourced_claim_cannot_be_high_risk(entries, patient_entry, risk):
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Patient reports dizziness every morning.",
                _cite(patient_entry, "I feel dizzy every morning"),
                risk=risk,
                category="symptom",
            ),
        )
    )

    assert check.failures == [CheckFailure.PATIENT_SOURCE_RISK]


def test_patient_session_summaries_count_as_patient_sourced():
    entry = _entry(
        "Patient described chest tightness.",
        role=Role.SYSTEM,
        entry_type=EntryType.AI_PATIENT_SESSION_SUMMARY,
    )
    check = _only_check(
        _verify(
            {entry.id: entry},
            _claim("Chest tightness.", _cite(entry, "chest tightness"), category="symptom"),
        )
    )

    assert check.failures == [CheckFailure.PATIENT_SOURCE_PHRASING]


def test_a_clinician_source_lifts_the_patient_source_policy(
    entries, patient_entry, clinician_entry
):
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Allergic to penicillin; metformin started.",
                _cite(patient_entry, "allergic to penicillin"),
                _cite(clinician_entry, "started metformin"),
                risk=RiskLevel.HIGH,
                category="allergy",
            ),
        )
    )

    assert check.verified, check.detail


# Check 6: conflict coverage


def _conflict(entity, *entries):
    return Conflict(
        id=uuid4(),
        patient_id=PATIENT,
        clinic_id=CLINIC,
        category=ConflictCategory.DOSE,
        entity=entity,
        summary=f"Conflicting dose facts for {entity}.",
        status=ConflictStatus.NEEDS_REVIEW,
        entry_ids=[entry.id for entry in entries],
        rationale="Review required.",
    )


def test_conflicts_must_be_mentioned_by_a_verified_claim(entries, clinician_entry, patient_entry):
    covered = _conflict("metformin", clinician_entry, patient_entry)
    missed = _conflict("warfarin", clinician_entry, patient_entry)

    report = _verify(
        entries,
        _claim("Metformin was started.", _cite(clinician_entry, "started metformin")),
        conflicts=[covered, missed],
    )

    assert report.uncovered_conflict_ids == [missed.id]
    assert not report.passed


def test_an_unsupported_claim_does_not_cover_a_conflict(entries, clinician_entry, patient_entry):
    conflict = _conflict("metformin", clinician_entry, patient_entry)

    report = _verify(
        entries,
        _claim(
            "Metformin was stopped.",
            _cite(clinician_entry, "started metformin", quote="stopped metformin"),
        ),
        conflicts=[conflict],
    )

    assert report.uncovered_conflict_ids == [conflict.id]


# Report shape


def test_each_claim_gets_its_own_check_in_order(entries, clinician_entry, patient_entry):
    report = _verify(
        entries,
        _claim("Metformin was started.", _cite(clinician_entry, "started metformin")),
        _claim("Allergic to penicillin.", _cite(patient_entry, "allergic to penicillin")),
    )

    assert [check.claim_index for check in report.checks] == [0, 1]
    assert [check.verified for check in report.checks] == [True, False]
    assert report.unsupported == [report.checks[1]]


def test_empty_draft_passes_when_there_are_no_conflicts(entries):
    assert verify_draft(DraftReview(claims=[]), entries).passed


def test_words_after_hold_or_start_are_not_treated_as_drugs_unless_dosed(entries, clinician_entry):
    # "hold pending" matches the medication-state pattern; "pending" is never dosed in the record.
    check = _only_check(
        _verify(
            entries,
            _claim(
                "Metformin started; next dose on hold pending review.",
                _cite(clinician_entry, "started metformin"),
            ),
        )
    )

    assert check.verified, check.detail


def test_a_dosed_drug_named_in_a_state_claim_must_still_be_cited(entries, clinician_entry):
    other = _entry("Continue warfarin 5 mg nightly.")
    check = _only_check(
        _verify(
            entries | {other.id: other},
            _claim("Stop warfarin before surgery.", _cite(clinician_entry, "started metformin")),
        )
    )

    assert check.failures == [CheckFailure.UNSUPPORTED_ENTITIES]
    assert "warfarin" in check.detail[0]
