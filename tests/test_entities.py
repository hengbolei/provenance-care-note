from carenote.domain.models import ConflictCategory
from carenote.services.entities import extract_entities


def _facts(text):
    return [(item.category, item.entity, item.value) for item in extract_entities(text)]


def test_extracts_normalised_dose_medication_state_and_allergy_facts():
    text = (
        "Take Metformin 500 MG twice daily. Hold lisinopril tonight. "
        "Allergic to penicillin; not allergic to latex."
    )

    assert _facts(text) == [
        (ConflictCategory.DOSE, "metformin", "500mg"),
        (ConflictCategory.MEDICATION, "metformin", "take"),
        (ConflictCategory.MEDICATION, "lisinopril", "hold"),
        (ConflictCategory.ALLERGY, "penicillin", "present"),
        (ConflictCategory.ALLERGY, "latex", "absent"),
    ]


def test_entity_spans_point_at_the_matched_text():
    text = "Plan: stop the warfarin. Known allergy to sulfa."
    for item in extract_entities(text):
        assert item.entity in text[item.start : item.end].lower()


def test_negated_allergy_is_not_reported_as_present():
    facts = _facts("No allergy to amoxicillin.")
    assert facts == [(ConflictCategory.ALLERGY, "amoxicillin", "absent")]


def test_text_without_clinical_facts_returns_nothing():
    assert extract_entities("Patient slept well and feels better today.") == []
