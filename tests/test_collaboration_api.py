from fastapi.testclient import TestClient

from carenote.core.identity import session_service
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID
from carenote.domain.models import Actor, Role
from carenote.main import app

client = TestClient(app)


def _headers(role="clinician"):
    actor = Actor(id=DEMO_CLINICIAN_ID, role=Role(role), clinic_id=DEMO_CLINIC_ID)
    token, _ = session_service.issue(actor)
    return {"authorization": f"Bearer {token}"}


def test_comment_mention_assignment_and_resolution_round_trip():
    created = client.post(
        f"/api/patients/{DEMO_PATIENT_ID}/comments",
        headers=_headers(),
        json={"content": "@staff please arrange the BP check", "assigned_to": "staff"},
    )
    assert created.status_code == 201
    comment = created.json()
    assert comment["mentions"] == ["staff"]
    assert comment["assigned_to"] == "staff"
    assert comment["target"] == {
        "resource_type": "section",
        "resource_id": "plan",
        "start": None,
        "end": None,
    }

    reply = client.post(
        f"/api/patients/{DEMO_PATIENT_ID}/comments",
        headers=_headers(),
        json={"content": "Acknowledged", "parent_id": comment["id"]},
    )
    assert reply.status_code == 201
    assert reply.json()["parent_id"] == comment["id"]

    resolved = client.patch(
        f"/api/patients/{DEMO_PATIENT_ID}/comments/{comment['id']}",
        headers=_headers(),
        json={"resolved": True, "expected_version": comment["version"]},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True

    reopened = client.patch(
        f"/api/patients/{DEMO_PATIENT_ID}/comments/{comment['id']}",
        headers=_headers(),
        json={
            "resolved": False,
            "expected_version": resolved.json()["version"],
        },
    )
    assert reopened.status_code == 200
    assert reopened.json()["resolved"] is False

    revisions = client.get(
        f"/api/patients/{DEMO_PATIENT_ID}/comments/{comment['id']}/revisions",
        headers=_headers(),
    )
    assert revisions.status_code == 200
    assert [item["version"] for item in revisions.json()] == [1, 2, 3]
    assert revisions.json()[-1]["snapshot"]["resolved"] is False

    audit = client.get(f"/api/patients/{DEMO_PATIENT_ID}/audit", headers=_headers())
    assert [event["operation"] for event in audit.json()[-4:]] == [
        "comment_create",
        "comment_reply",
        "comment_resolve",
        "comment_reopen",
    ]


def test_section_edit_history_and_revert_round_trip():
    detail = client.get(f"/api/patients/{DEMO_PATIENT_ID}", headers=_headers()).json()
    plan = next(item for item in detail["sections"] if item["section"] == "plan")
    updated = client.put(
        f"/api/patients/{DEMO_PATIENT_ID}/sections/plan",
        headers=_headers(),
        json={"content": "Synthetic updated plan", "expected_version": plan["version"]},
    )
    assert updated.status_code == 200

    history = client.get(
        f"/api/patients/{DEMO_PATIENT_ID}/sections/plan/revisions", headers=_headers()
    )
    assert history.status_code == 200
    assert history.json()[-1]["content"] == "Synthetic updated plan"
    assert "Synthetic updated plan" in history.json()[-1]["diff"]

    reverted = client.post(
        f"/api/patients/{DEMO_PATIENT_ID}/sections/plan/revert",
        headers=_headers(),
        json={"target_version": 1},
    )
    assert reverted.status_code == 200
    assert reverted.json()["content"] == history.json()[0]["content"]

    audit = client.get(f"/api/patients/{DEMO_PATIENT_ID}/audit", headers=_headers())
    assert audit.status_code == 200
    assert audit.json()[-1]["operation"] == "revert"
    assert "content" not in audit.json()[-1]


def test_patient_cannot_use_internal_collaboration_api():
    token, _ = session_service.issue(
        Actor(id=DEMO_PATIENT_ID, role=Role.PATIENT, clinic_id=DEMO_CLINIC_ID)
    )
    response = client.post(
        f"/api/patients/{DEMO_PATIENT_ID}/comments",
        headers={"authorization": f"Bearer {token}"},
        json={"content": "Should be rejected"},
    )
    assert response.status_code == 403
