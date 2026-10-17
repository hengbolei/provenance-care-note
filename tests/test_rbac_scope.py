from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from carenote.core.identity import AuthenticationError, session_service
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_PATIENT_ID
from carenote.domain.models import (
    Actor,
    EntryOrigin,
    EntryType,
    Patient,
    ProvenanceSource,
    Role,
    TimelineEntry,
)
from carenote.main import app
from carenote.services.care_notes import AccessDeniedError


def test_staff_cannot_write_clinician_note(service, staff, patient_id, clinic_id):
    entry = TimelineEntry(
        patient_id=patient_id,
        clinic_id=clinic_id,
        author_role=Role.STAFF,
        author_id=staff.id,
        type=EntryType.CLINICIAN_NOTE,
        title="Synthetic clinician note",
        content="Synthetic note",
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY,
            source_id="test-clinician-note",
            source_label="Synthetic test entry",
        ),
    )
    with pytest.raises(AccessDeniedError):
        service.add_entry(staff, entry)


def test_clinician_cannot_write_staff_note(service, clinician, patient_id, clinic_id):
    entry = TimelineEntry(
        patient_id=patient_id,
        clinic_id=clinic_id,
        author_role=Role.CLINICIAN,
        author_id=clinician.id,
        type=EntryType.STAFF_NOTE,
        title="Forged staff note",
        content="Synthetic note",
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY,
            source_id="forged-staff-note",
            source_label="Synthetic test entry",
        ),
    )
    with pytest.raises(AccessDeniedError):
        service.add_entry(clinician, entry)


def test_system_role_cannot_open_interactive_view(service, patient_id, clinic_id):
    system = Actor(id=uuid4(), role=Role.SYSTEM, clinic_id=clinic_id)
    with pytest.raises(AccessDeniedError):
        service.get_patient_view(system, patient_id)


def test_signed_login_rejects_legacy_headers_tampering_and_system_identity():
    client = TestClient(app)
    path = f"/api/patients/{DEMO_PATIENT_ID}"
    login = client.post(
        "/api/auth/login",
        json={"username": "clinician", "password": "clinician-demo-2026"},
    )
    assert login.status_code == 200
    token = login.json()["token"]
    header, body, signature = token.split(".")
    tampered = f"{header}.{body[:-1]}A.{signature}"

    assert client.get(path, headers={"authorization": f"Bearer {tampered}"}).status_code == 401
    unauthenticated = TestClient(app)
    legacy = unauthenticated.get(
        path,
        headers={"x-actor-role": "clinician", "x-clinic-id": str(DEMO_CLINIC_ID)},
    )
    assert legacy.status_code == 401
    with pytest.raises(AuthenticationError):
        session_service.issue(Actor(id=uuid4(), role=Role.SYSTEM, clinic_id=DEMO_CLINIC_ID))


def test_cross_clinic_write_is_denied(service, staff, patient_id):
    entry = TimelineEntry(
        patient_id=patient_id,
        clinic_id=uuid4(),
        author_role=Role.STAFF,
        author_id=staff.id,
        type=EntryType.STAFF_NOTE,
        title="Synthetic staff note",
        content="Synthetic note",
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY,
            source_id="test-staff-note",
            source_label="Synthetic test entry",
        ),
    )
    with pytest.raises(AccessDeniedError):
        service.add_entry(staff, entry)


def test_patient_view_excludes_internal_and_raw_ai_entries(
    service, repository, patient_id, clinic_id
):
    from carenote.domain.models import Actor

    repository.add_patient(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            display_name="Synthetic Patient",
            date_of_birth=date(1990, 1, 1),
            medical_record_number="SYN-TEST",
        )
    )
    repository.add_entry(
        TimelineEntry(
            patient_id=patient_id,
            clinic_id=clinic_id,
            author_role=Role.SYSTEM,
            author_id=None,
            type=EntryType.AI_PATIENT_SESSION_SUMMARY,
            title="Synthetic AI session summary",
            content="Raw synthetic AI output",
            origin=EntryOrigin(
                source=ProvenanceSource.PATIENT_SESSION,
                source_id="test-patient-session",
                source_label="Synthetic patient session",
            ),
        )
    )
    patient = Actor(id=uuid4(), role=Role.PATIENT, clinic_id=clinic_id)
    with pytest.raises(AccessDeniedError):
        service.get_patient_view(patient, patient_id)

    patient = patient.model_copy(update={"id": patient_id})
    assert service.get_patient_view(patient, patient_id).entries == []
