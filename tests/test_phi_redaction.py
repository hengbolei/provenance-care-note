import pytest

from carenote.privacy import PHIRedactionGateway


def test_phi_gateway_redacts_names_identity_numbers_and_phones():
    source = (
        "Maya Chen can be reached at +65 9123 4567. NRIC: S1234567D; backup phone +86 13800138000."
    )
    result = PHIRedactionGateway().redact(source, known_names=["Maya Chen"])

    assert "Maya Chen" not in result.text
    assert "9123 4567" not in result.text
    assert "S1234567D" not in result.text
    assert "13800138000" not in result.text
    assert result.counts["name"] == 1
    assert result.counts["phone"] == 2
    assert result.counts["national_id"] + result.counts["labelled_id"] == 1


def test_llm_boundary_returns_only_redacted_text():
    safe_text = PHIRedactionGateway().prepare_for_llm(
        "Patient Maya Chen called 91234567.", known_names=["Maya Chen"]
    )
    assert safe_text == "Patient [REDACTED_NAME] called [REDACTED_PHONE]."


@pytest.mark.parametrize(
    "text",
    [
        "No identifiers here.",
        "Idiopathic headache, likely benign.",
        "Icterus noted on examination.",
        "ID verified at the front desk.",
        "Passport holder travelling next week.",
    ],
)
def test_labelled_id_ignores_ordinary_words(text):
    result = PHIRedactionGateway().redact(text)
    assert result.text == text
    assert result.counts["labelled_id"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "ID 12345678",
        "IC: A1234567",
        "NRIC no. 987654321",
        "passport number E1234567",
        "ID#ABC-123456",
    ],
)
def test_labelled_id_still_redacts_labelled_identifiers(text):
    result = PHIRedactionGateway().redact(f"Patient {text} confirmed.")
    assert result.text == "Patient [REDACTED_LABELLED_ID] confirmed."


SOURCE = "Maya Chen reports dizziness after metformin 500 mg; call Maya Chen on 91234567 today."


def _redact():
    return PHIRedactionGateway().redact(SOURCE, known_names=["Maya Chen"])


def test_redaction_records_every_replacement_with_exact_offsets():
    result = _redact()

    assert [span.label for span in result.spans] == ["name", "name", "phone"]
    for span in result.spans:
        assert result.text[span.redacted_start : span.redacted_end] == (
            f"[REDACTED_{span.label.upper()}]"
        )
    assert [SOURCE[s.original_start : s.original_end] for s in result.spans] == [
        "Maya Chen",
        "Maya Chen",
        "91234567",
    ]


def test_redacted_range_after_placeholders_maps_back_to_original_text():
    result = _redact()
    for phrase in ["dizziness", "metformin 500 mg", "today"]:
        start = result.text.index(phrase)
        original = result.to_original(start, start + len(phrase))
        assert SOURCE[original[0] : original[1]] == phrase


def test_range_touching_a_placeholder_expands_to_the_whole_original_value():
    result = _redact()
    start = result.text.index("[REDACTED_PHONE]") + 3
    original = result.to_original(start, start + 2)
    assert SOURCE[original[0] : original[1]] == "91234567"

    whole = result.to_original(0, result.text.index(" reports"))
    assert SOURCE[whole[0] : whole[1]] == "Maya Chen"


def test_original_range_round_trips_through_redacted_text():
    result = _redact()
    start = SOURCE.index("metformin")
    redacted = result.to_redacted(start, start + len("metformin 500 mg"), len(SOURCE))
    assert result.text[redacted[0] : redacted[1]] == "metformin 500 mg"
    assert result.to_original(*redacted) == (start, start + len("metformin 500 mg"))


def test_text_without_phi_maps_offsets_unchanged():
    result = PHIRedactionGateway().redact("No personal details here.")
    assert result.spans == []
    assert result.to_original(3, 14) == (3, 14)


def test_out_of_range_offsets_are_rejected():
    result = _redact()
    for start, end in [(-1, 2), (5, 3), (0, len(result.text) + 1)]:
        with pytest.raises(ValueError):
            result.to_original(start, end)
