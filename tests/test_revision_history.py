def test_revision_increments_and_revert_restores_content(
    service, repository, clinician, patient_id
):
    first = service.update_section(clinician, patient_id, "plan", "Initial plan", 0)
    second = service.update_section(clinician, patient_id, "plan", "Updated plan", first.version)
    reverted = service.revert_section(clinician, patient_id, "plan", first.version)

    assert second.version == 2
    assert reverted.version == 3
    assert reverted.content == "Initial plan"
    audit = repository.list_revisions(patient_id, "plan")
    assert audit[-1].changed_by == clinician.id
    assert audit[-1].operation == "revert"


def test_arbitrary_versions_can_be_compared(service, clinician, patient_id):
    first = service.update_section(clinician, patient_id, "assessment", "Line one", 0)
    second = service.update_section(
        clinician, patient_id, "assessment", "Line one\nLine two", first.version
    )
    third = service.update_section(
        clinician, patient_id, "assessment", "Line three", second.version
    )

    comparison = service.compare_section_versions(
        clinician, patient_id, "assessment", first.version, third.version
    )

    assert comparison.from_version == 1
    assert comparison.to_version == 3
    assert "-Line one" in comparison.diff
    assert "+Line three" in comparison.diff
