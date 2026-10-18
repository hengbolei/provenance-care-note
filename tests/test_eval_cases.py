"""The evaluation case set is well formed. Scoring the system against it is the suites' job."""

from collections import Counter

import pytest
from pydantic import ValidationError

from carenote.agent.state import CheckFailure
from carenote.evals.cases import EvalCase, Family, load_cases

CASES = load_cases()


def test_there_are_at_least_thirty_cases_covering_every_family():
    families = Counter(case.family for case in CASES)

    assert len(CASES) >= 30
    assert set(families) == set(Family)
    assert min(families.values()) >= 3


def test_every_case_builds_a_record_and_its_probes():
    for case in CASES:
        entries = case.build_entries()
        ids = {entry.id for entry in entries}
        assert len(ids) == len(entries), case.id
        assert all(entry.patient_id == case.patient_id() for entry in entries)
        for probe in case.probes:
            claim = case.probe_claim(probe)
            assert claim.citations, (case.id, probe.id)


def test_supported_probes_quote_their_entries_exactly():
    for case in CASES:
        contents = {entry.key: entry.content for entry in case.entries}
        for probe in case.probes:
            if probe.expect:
                continue
            for citation in probe.cite:
                assert citation.quote in contents[citation.entry], (case.id, probe.id)


def test_long_timeline_cases_have_at_least_fifty_entries():
    for case in CASES:
        if case.family is Family.LONG_TIMELINE:
            assert len(case.build_entries()) >= 50, case.id


def test_probes_exercise_every_verifier_failure_and_supported_claims():
    expected = Counter(
        failure for case in CASES for probe in case.probes for failure in probe.expect
    )
    supported = sum(not probe.expect for case in CASES for probe in case.probes)

    assert set(expected) == set(CheckFailure)
    assert supported >= 15


def test_known_gaps_always_explain_themselves():
    for case in CASES:
        gaps = [item.known_gap for item in case.expect.conflicts + case.expect.phi]
        gaps += [probe.known_gap for probe in case.probes]
        for gap in gaps:
            assert gap is None or len(gap.strip()) >= 20, case.id


def _case(**overrides):
    case = {
        "id": "schema-check",
        "family": "sparse_information",
        "description": "Schema check",
        "entries": [
            {
                "key": "note",
                "type": "clinician_note",
                "author": "clinician",
                "at": "2026-08-01T09:00:00Z",
                "title": "Note",
                "content": "Continue aspirin 75 mg daily.",
            }
        ],
    }
    case.update(overrides)
    return case


def test_a_supported_probe_must_quote_exactly():
    probe = {
        "id": "p",
        "claim": "Aspirin 75 mg.",
        "cite": [{"entry": "note", "quote": "aspirin 81 mg"}],
    }
    with pytest.raises(ValidationError, match="must quote"):
        EvalCase.model_validate(_case(probes=[probe]))


def test_an_unknown_entry_is_only_allowed_when_the_probe_expects_it():
    probe = {"id": "p", "claim": "Aspirin.", "cite": [{"entry": "ghost", "quote": "aspirin"}]}
    with pytest.raises(ValidationError, match="unknown entry"):
        EvalCase.model_validate(_case(probes=[probe]))

    EvalCase.model_validate(_case(probes=[probe | {"expect": ["unknown_entry"]}]))


def test_entry_keys_and_unknown_fields_are_rejected():
    entry = _case()["entries"][0]
    with pytest.raises(ValidationError, match="unique"):
        EvalCase.model_validate(_case(entries=[entry, entry]))
    with pytest.raises(ValidationError):
        EvalCase.model_validate(_case(severity="high"))
