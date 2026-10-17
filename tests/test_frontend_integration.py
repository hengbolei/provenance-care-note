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


def test_patient_page_serves_accessible_app_shell_and_modules():
    page = client.get("/")
    api_module = client.get("/static/api.js")
    app_module = client.get("/static/app.js")

    assert page.status_code == api_module.status_code == app_module.status_code == 200
    assert 'aria-labelledby="patient-name"' in page.text
    assert 'id="screen-state"' in page.text
    assert 'id="highlight-list"' in page.text
    assert 'id="timeline-list"' in page.text
    assert 'id="workspace-panel"' in page.text
    assert 'id="save-section"' in page.text
    assert 'id="comment-list"' in page.text
    assert 'type="module" src="/static/app.js"' in page.text
    assert "/api/patients/${context.patientId}" in api_module.text
    assert 'credentials: "same-origin"' in api_module.text
    assert "/api/auth/login" in api_module.text
    assert "/sections/${section}/revisions" in api_module.text
    assert "/comments/${commentId}" in api_module.text
    assert "/highlights/${highlightId}" in api_module.text
    assert "/patients/${context.patientId}/entries" in api_module.text
    assert "/patients/${context.patientId}/highlights" in api_module.text
    assert "/patients/${context.patientId}/audit" in api_module.text
    assert "/entries/${entryId}/source" in api_module.text
    assert 'id="login-panel"' in page.text
    assert 'id="staff-note-card"' in page.text
    assert 'id="highlight-composer-card"' in page.text
    assert 'id="audit-list"' in page.text
    assert 'id="patient-dashboard"' in page.text
    assert 'id="patient-action-list"' in page.text
    assert 'id="add-patient-entry"' in page.text


def test_patient_api_supplies_every_field_required_by_frontend():
    token, _ = session_service.issue(
        Actor(
            id=DEMO_CLINICIAN_ID,
            role=Role.CLINICIAN,
            clinic_id=DEMO_CLINIC_ID,
        )
    )
    response = client.get(
        f"/api/patients/{DEMO_PATIENT_ID}",
        headers={"authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert {
        "display_name",
        "date_of_birth",
        "medical_record_number",
        "pronouns",
        "synthetic",
    } <= body["patient"].keys()
    assert all(
        {
            "text",
            "risk_reason",
            "suggested_action",
            "priority",
            "priority_factors",
            "risk_level",
            "status",
            "provenance_pointer",
            "assigned_to",
            "action_status",
            "version",
        }
        <= highlight.keys()
        for highlight in body["highlights"]
    )
    assert body["conflicts"]
    assert all(
        {
            "id",
            "timestamp",
            "type",
            "title",
            "content",
            "origin",
            "author_role",
        }
        <= entry.keys()
        for entry in body["entries"]
    )


def test_frontend_declares_loading_empty_forbidden_and_error_states():
    app_module = client.get("/static/app.js").text
    for state in ("loading", "empty", "forbidden", "notFound", "error"):
        assert f"{state}:" in app_module
