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
