from fastapi.testclient import TestClient

from carenote.core.identity import session_service
from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    DEMO_STAFF_ID,
)
from carenote.domain.models import Actor, Role
from carenote.main import app

client = TestClient(app)


def _headers(actor_id=DEMO_CLINICIAN_ID, role="clinician"):
    token, _ = session_service.issue(Actor(id=actor_id, role=Role(role), clinic_id=DEMO_CLINIC_ID))
    return {"authorization": f"Bearer {token}"}


def test_highlight_assignment_completion_and_audit_round_trip():
    detail = client.get(f"/api/patients/{DEMO_PATIENT_ID}", headers=_headers()).json()
    highlight = detail["highlights"][0]

    updated = client.patch(
        f"/api/patients/{DEMO_PATIENT_ID}/highlights/{highlight['id']}",
        headers=_headers(),
        json={
            "expected_version": highlight["version"],
            "assigned_to": "staff",
            "action_status": "completed",
            "disposition_note": "Patient contacted; follow-up arranged.",
        },
    )

    assert updated.status_code == 200
    body = updated.json()
    assert body["version"] == highlight["version"] + 1
    assert body["assigned_to"] == "staff"
    assert body["action_status"] == "completed"
    assert body["completed_by"] == str(DEMO_CLINICIAN_ID)
    assert body["completed_at"] is not None

    audit = client.get(f"/api/patients/{DEMO_PATIENT_ID}/audit", headers=_headers()).json()
    assert audit[-1]["resource"] == "highlight"
    assert audit[-1]["resource_id"] == highlight["id"]
    assert audit[-1]["operation"] == "highlight_complete"

    stale = client.patch(
        f"/api/patients/{DEMO_PATIENT_ID}/highlights/{highlight['id']}",
        headers=_headers(),
        json={"expected_version": highlight["version"], "action_status": "open"},
    )
    assert stale.status_code == 409


def test_staff_cannot_clinically_confirm_or_reject_highlight():
    detail = client.get(
        f"/api/patients/{DEMO_PATIENT_ID}",
        headers=_headers(DEMO_STAFF_ID, "staff"),
    ).json()
    highlight = detail["highlights"][-1]

    response = client.patch(
        f"/api/patients/{DEMO_PATIENT_ID}/highlights/{highlight['id']}",
        headers=_headers(DEMO_STAFF_ID, "staff"),
        json={"expected_version": highlight["version"], "status": "rejected"},
    )

    assert response.status_code == 403


def test_highlight_assignment_has_specific_audit_operation():
    detail = client.get(f"/api/patients/{DEMO_PATIENT_ID}", headers=_headers()).json()
    highlight = detail["highlights"][-1]

    response = client.patch(
        f"/api/patients/{DEMO_PATIENT_ID}/highlights/{highlight['id']}",
        headers=_headers(),
        json={"expected_version": highlight["version"], "assigned_to": "staff"},
    )

    assert response.status_code == 200
    audit = client.get(f"/api/patients/{DEMO_PATIENT_ID}/audit", headers=_headers()).json()
    assert audit[-1]["operation"] == "highlight_assign"


def test_patient_cannot_update_highlight():
    detail = client.get(f"/api/patients/{DEMO_PATIENT_ID}", headers=_headers()).json()
    highlight = detail["highlights"][0]

    response = client.patch(
        f"/api/patients/{DEMO_PATIENT_ID}/highlights/{highlight['id']}",
        headers=_headers(DEMO_PATIENT_ID, "patient"),
        json={"expected_version": highlight["version"], "action_status": "completed"},
    )

    assert response.status_code == 403
