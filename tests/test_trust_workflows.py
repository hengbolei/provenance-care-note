from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    DEMO_STAFF_ID,
    seed_demo_data,
)
from carenote.domain.models import (
    ActionStatus,
    Actor,
    AnnotationTarget,
    AnnotationTargetType,
    ConflictCategory,
    ConflictStatus,
    HighlightUpdateRequest,
    Role,
)
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService


def _seeded_service():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    return repository, CareNoteService(repository)


def test_importance_score_is_deterministic_and_explained():
    _, service = _seeded_service()
    clinician = Actor(
        id=DEMO_CLINICIAN_ID,
        role=Role.CLINICIAN,
        clinic_id=DEMO_CLINIC_ID,
    )

    first = service.get_patient_view(clinician, DEMO_PATIENT_ID).highlights
    second = service.get_patient_view(clinician, DEMO_PATIENT_ID).highlights

    assert [(item.id, item.priority) for item in first] == [
        (item.id, item.priority) for item in second
    ]
    for highlight in first:
        assert highlight.priority_factors
        expected = max(0, min(100, sum(item.points for item in highlight.priority_factors)))
        assert highlight.priority == expected


def test_completed_highlight_is_deterministically_demoted():
    repository, service = _seeded_service()
    clinician = Actor(
        id=DEMO_CLINICIAN_ID,
        role=Role.CLINICIAN,
        clinic_id=DEMO_CLINIC_ID,
    )
    current = service.get_patient_view(clinician, DEMO_PATIENT_ID).highlights[0]

    updated = service.update_highlight(
        clinician,
        DEMO_PATIENT_ID,
        current.id,
        HighlightUpdateRequest(
            expected_version=repository.get_highlight(current.id).version,
            action_status=ActionStatus.COMPLETED,
        ),
    )

    action_factor = next(
        factor for factor in updated.priority_factors if factor.key == "action_status"
    )
    assert action_factor.points == -20
    assert updated.priority < current.priority


def test_medication_and_dose_conflicts_are_detected_with_precedence():
    _, service = _seeded_service()
    clinician = Actor(
        id=DEMO_CLINICIAN_ID,
        role=Role.CLINICIAN,
        clinic_id=DEMO_CLINIC_ID,
    )

    conflicts = service.get_patient_view(clinician, DEMO_PATIENT_ID).conflicts

    dose = next(item for item in conflicts if item.category is ConflictCategory.DOSE)
    medication = next(item for item in conflicts if item.category is ConflictCategory.MEDICATION)
    assert dose.entity == "lisinopril"
    assert dose.status is ConflictStatus.NEEDS_REVIEW
    assert medication.status is ConflictStatus.CLINICIAN_PRECEDENCE
    assert medication.preferred_entry_id is not None


def test_comment_span_and_full_comment_highlight_revisions_are_retained():
    repository, service = _seeded_service()
    staff = Actor(id=DEMO_STAFF_ID, role=Role.STAFF, clinic_id=DEMO_CLINIC_ID)
    clinician = Actor(
        id=DEMO_CLINICIAN_ID,
        role=Role.CLINICIAN,
        clinic_id=DEMO_CLINIC_ID,
    )
    entry = repository.list_entries(DEMO_PATIENT_ID, DEMO_CLINIC_ID)[0]
    target = AnnotationTarget(
        resource_type=AnnotationTargetType.ENTRY,
        resource_id=str(entry.id),
        start=0,
        end=10,
    )
    comment = service.add_comment(
        staff,
        DEMO_PATIENT_ID,
        "Please verify this exact phrase.",
        target=target,
    )
    resolved = service.set_comment_resolved(
        staff,
        DEMO_PATIENT_ID,
        comment.id,
        True,
        expected_version=comment.version,
    )
    comment_history = service.list_resource_revisions(staff, DEMO_PATIENT_ID, "comment", comment.id)

    highlight = service.get_patient_view(clinician, DEMO_PATIENT_ID).highlights[0]
    service.update_highlight(
        clinician,
        DEMO_PATIENT_ID,
        highlight.id,
        HighlightUpdateRequest(
            expected_version=repository.get_highlight(highlight.id).version,
            assigned_to=Role.STAFF,
        ),
    )
    highlight_history = service.list_resource_revisions(
        clinician, DEMO_PATIENT_ID, "highlight", highlight.id
    )

    assert resolved.target == target
    assert [revision.version for revision in comment_history] == [1, 2]
    assert comment_history[-1].snapshot["resolved"] is True
    assert [revision.version for revision in highlight_history] == [1, 2]
    assert highlight_history[-1].snapshot["assigned_to"] == "staff"
