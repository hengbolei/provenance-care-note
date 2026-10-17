from datetime import date

import pytest
from fastapi.testclient import TestClient

from carenote.api import routes
from carenote.core.identity import session_service
from carenote.domain.models import (
    Actor,
    HighlightCreateRequest,
    Patient,
    ReviewStatus,
    Role,
)
from carenote.main import app


def test_staff_note_to_clinician_highlight_is_audited(
    repository, service, staff, clinician, patient_id, clinic_id
):
    repository.add_patient(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            display_name="Synthetic workflow patient",
            date_of_birth=date(1990, 1, 1),
            medical_record_number="SYN-WORKFLOW",
        )
    )
    entry = service.create_manual_entry(
        staff,
        patient_id,
        "Follow-up call",
        "Patient reports dizziness after the morning dose.",
    )
    phrase = "dizziness after the morning dose"
    start = entry.content.index(phrase)

    highlight = service.create_highlight(
        clinician,
        patient_id,
        HighlightCreateRequest(
            entry_id=entry.id,
            start=start,
            end=start + len(phrase),
            risk_reason="Possible medication-related symptom",
            suggested_action="Review the morning medication dose",
            priority=90,
            status=ReviewStatus.CLINICIAN_CONFIRMED,
            assigned_to="staff",
        ),
    )

    assert entry.author_id == staff.id
    assert entry.type == "staff_note"
    assert highlight.text == phrase
    assert highlight.provenance_pointer.entry_id == entry.id
    assert highlight.assigned_to == "staff"
    assert [event.operation for event in repository.audit_events] == [
        "entry_create",
        "highlight_create",
    ]


def test_staff_cannot_create_manual_highlight(repository, service, staff, patient_id, clinic_id):
    repository.add_patient(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            display_name="Synthetic workflow patient",
            date_of_birth=date(1990, 1, 1),
            medical_record_number="SYN-WORKFLOW",
        )
    )
    entry = service.create_manual_entry(staff, patient_id, "Staff note", "Review needed")

    with pytest.raises(PermissionError):
        service.create_highlight(
            staff,
            patient_id,
            HighlightCreateRequest(
                entry_id=entry.id,
                start=0,
                end=6,
                risk_reason="Needs clinical interpretation",
                suggested_action="Ask clinician to review",
                priority=60,
            ),
        )


def test_highlight_text_is_derived_from_server_source(
    repository, service, staff, clinician, patient_id, clinic_id
):
    repository.add_patient(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            display_name="Synthetic workflow patient",
            date_of_birth=date(1990, 1, 1),
            medical_record_number="SYN-WORKFLOW",
        )
    )
    entry = service.create_manual_entry(staff, patient_id, "Staff note", "Exact source text")

    highlight = service.create_highlight(
        clinician,
        patient_id,
        HighlightCreateRequest(
            entry_id=entry.id,
            start=6,
            end=12,
            risk_reason="Synthetic reason",
            suggested_action="Synthetic action",
            priority=50,
        ),
    )

    assert highlight.text == "source"


def test_manual_note_and_highlight_api_round_trip(
    monkeypatch, repository, service, staff, clinician, patient_id, clinic_id
):
    repository.add_patient(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            display_name="Synthetic API patient",
            date_of_birth=date(1990, 1, 1),
            medical_record_number="SYN-API",
        )
    )
    monkeypatch.setattr(routes, "service", service)
    client = TestClient(app)
    staff_token, _ = session_service.issue(staff)
    clinician_token, _ = session_service.issue(clinician)
    staff_headers = {"authorization": f"Bearer {staff_token}"}
    clinician_headers = {"authorization": f"Bearer {clinician_token}"}

    created_entry = client.post(
        f"/api/patients/{patient_id}/entries",
        headers=staff_headers,
        json={"title": "API follow-up", "content": "Blood pressure follow-up is pending."},
    )
    assert created_entry.status_code == 201
    entry = created_entry.json()
    phrase = "follow-up is pending"
    start = entry["content"].index(phrase)

    created_highlight = client.post(
        f"/api/patients/{patient_id}/highlights",
        headers=clinician_headers,
        json={
            "entry_id": entry["id"],
            "start": start,
            "end": start + len(phrase),
            "risk_reason": "Unresolved follow-up",
            "suggested_action": "Assign the follow-up",
            "priority": 85,
            "assigned_to": "staff",
        },
    )
    assert created_highlight.status_code == 201
    assert created_highlight.json()["text"] == phrase
    assert created_highlight.json()["priority_factors"]

    audit = client.get(f"/api/patients/{patient_id}/audit", headers=clinician_headers)
    assert [event["operation"] for event in audit.json()] == [
        "entry_create",
        "highlight_create",
    ]


def test_patient_update_is_patient_visible_and_audited(repository, service, patient_id, clinic_id):
    repository.add_patient(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            display_name="Synthetic patient author",
            date_of_birth=date(1990, 1, 1),
            medical_record_number="SYN-PATIENT",
        )
    )
    patient = Actor(id=patient_id, role=Role.PATIENT, clinic_id=clinic_id)

    entry = service.create_manual_entry(
        patient,
        patient_id,
        "Morning update",
        "I felt less dizzy today.",
    )

    assert entry.internal is False
    assert service.get_patient_view(patient, patient_id).entries == [entry]
    assert repository.audit_events[-1].operation == "entry_create"
    assert repository.audit_events[-1].changed_by == patient_id
