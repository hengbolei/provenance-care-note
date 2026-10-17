from concurrent.futures import ThreadPoolExecutor

import pytest

from carenote.services.care_notes import ConcurrentEditError


def test_different_roles_editing_different_sections_do_not_overwrite_each_other(
    service, clinician, staff, patient_id
):
    with ThreadPoolExecutor(max_workers=2) as pool:
        assessment_future = pool.submit(
            service.update_section,
            clinician,
            patient_id,
            "assessment",
            "Stable",
            0,
        )
        handoff_future = pool.submit(
            service.update_section,
            staff,
            patient_id,
            "handoff",
            "Follow up",
            0,
        )
        assessment = assessment_future.result()
        plan = handoff_future.result()
    assert assessment.content == "Stable"
    assert plan.content == "Follow up"
    assert assessment.owner_role == "clinician"
    assert plan.owner_role == "staff"


def test_stale_same_section_edit_is_rejected(service, staff, patient_id):
    service.update_section(staff, patient_id, "handoff", "First edit", 0)
    with pytest.raises(ConcurrentEditError):
        service.update_section(staff, patient_id, "handoff", "Stale edit", 0)
