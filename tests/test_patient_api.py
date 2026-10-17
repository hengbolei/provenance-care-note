from uuid import uuid4

from fastapi.testclient import TestClient

from carenote.core.identity import session_service
from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
)
from carenote.domain.models import Actor, Role
from carenote.main import app

client = TestClient(app)


def _headers(actor_id, role, clinic_id=DEMO_CLINIC_ID):
    token, _ = session_service.issue(Actor(id=actor_id, role=Role(role), clinic_id=clinic_id))
    return {"authorization": f"Bearer {token}"}


def test_clinician_can_get_seeded_patient_detail():
    response = client.get(
        f"/api/patients/{DEMO_PATIENT_ID}",
        headers=_headers(DEMO_CLINICIAN_ID, "clinician"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["patient"]["synthetic"] is True
    assert len(body["entries"]) == 6
    assert len(body["highlights"]) == 3


def test_patient_response_does_not_expose_internal_ai_content():
    response = client.get(
        f"/api/patients/{DEMO_PATIENT_ID}",
        headers=_headers(DEMO_PATIENT_ID, "patient"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["highlights"] == []
    assert len(body["entries"]) == 1
    assert body["entries"][0]["internal"] is False
    assert len(body["patient_actions"]) == 1
    assert set(body["patient_actions"][0]) == {
        "title",
        "instruction",
        "action_status",
        "updated_at",
    }
    assert "lisinopril" in body["patient_actions"][0]["instruction"]


def test_patient_cannot_open_original_internal_source_artifact():
    response = client.get(
        (f"/api/patients/{DEMO_PATIENT_ID}/entries/50000000-0000-4000-8000-000000000006/source"),
        headers=_headers(DEMO_PATIENT_ID, "patient"),
    )
    assert response.status_code == 403


def test_cross_clinic_patient_is_not_discoverable():
    response = client.get(
        f"/api/patients/{DEMO_PATIENT_ID}",
        headers=_headers(DEMO_CLINICIAN_ID, "clinician", uuid4()),
    )
    assert response.status_code == 404
