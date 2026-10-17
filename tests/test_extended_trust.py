from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from carenote.api import routes
from carenote.core.identity import AuthenticationError, SignedSessionService, session_service
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID
from carenote.domain.models import (
    Actor,
    ConflictCategory,
    EntryOrigin,
    EntryType,
    ProvenanceSource,
    Role,
    TimelineEntry,
)
from carenote.main import app
from carenote.privacy.redaction import RedactionResult
from carenote.services.conflicts import DeterministicConflictDetector


def _clinician_headers() -> dict[str, str]:
    token, _ = session_service.issue(
        Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
    )
    return {"authorization": f"Bearer {token}"}


def _entry(content: str, role: Role = Role.STAFF) -> TimelineEntry:
    return TimelineEntry(
        patient_id=DEMO_PATIENT_ID,
        clinic_id=DEMO_CLINIC_ID,
        author_role=role,
        author_id=uuid4(),
        type=EntryType.STAFF_NOTE if role is Role.STAFF else EntryType.CLINICIAN_NOTE,
        title="Synthetic allergy fact",
        content=content,
        origin=EntryOrigin(
            source=ProvenanceSource.MANUAL_ENTRY,
            source_id=str(uuid4()),
            source_label="Synthetic test source",
        ),
    )


def test_allergy_polarity_conflict_is_detected_and_clinician_source_is_preferred():
    conflicts = DeterministicConflictDetector().detect(
        DEMO_PATIENT_ID,
        [
            _entry("Patient is allergic to penicillin."),
            _entry("No allergy to penicillin.", Role.CLINICIAN),
        ],
    )
    allergy = next(item for item in conflicts if item.category is ConflictCategory.ALLERGY)
    assert allergy.entity == "penicillin"
    assert allergy.preferred_entry_id is not None
    assert len(set(allergy.entry_ids)) == 2


def test_section_comment_exact_span_round_trip():
    client = TestClient(app)
    detail = client.get(f"/api/patients/{DEMO_PATIENT_ID}", headers=_clinician_headers()).json()
    plan = next(item for item in detail["sections"] if item["section"] == "plan")
    end = min(12, len(plan["content"]))
    response = client.post(
        f"/api/patients/{DEMO_PATIENT_ID}/comments",
        headers=_clinician_headers(),
        json={
            "content": "Exact plan claim",
            "target": {
                "resource_type": "section",
                "resource_id": "plan",
                "start": 0,
                "end": end,
            },
        },
    )
    assert response.status_code == 201
    assert response.json()["target"] == {
        "resource_type": "section",
        "resource_id": "plan",
        "start": 0,
        "end": end,
    }


def test_signed_session_expiry_and_logout_revocation():
    sessions = SignedSessionService("unit-test-secret", ttl_seconds=30)
    actor = Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
    expired, _ = sessions.issue(actor, datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(AuthenticationError, match="expired"):
        sessions.verify(expired)

    active, _ = sessions.issue(actor)
    sessions.revoke(active)
    with pytest.raises(AuthenticationError, match="not active"):
        sessions.verify(active)


def test_seeded_highlight_priority_matches_initial_snapshot():
    for highlight in routes.repository.highlights.values():
        initial = routes.repository.list_resource_revisions("highlight", str(highlight.id))[0]
        assert initial.snapshot["priority"] == highlight.priority
        assert initial.snapshot["priority_factors"] == [
            item.model_dump(mode="json") for item in highlight.priority_factors
        ]


def test_ai_ingestion_sends_redacted_text_and_marks_result_unconfirmed(monkeypatch):
    captured: dict[str, str] = {}
    entry_ids_before = set(routes.repository.entries)
    source_ids_before = set(routes.repository.source_artifacts)
    audit_count_before = len(routes.repository.audit_events)

    class FakeGateway:
        model = "fake-clinical-model"

        def summarize(self, raw_text, known_names, safety_subject):
            assert "Maya Chen (Synthetic)" in known_names
            captured["external"] = raw_text.replace("Maya Chen (Synthetic)", "[REDACTED_NAME]")
            return "Patient described a new symptom; clinical review is required.", RedactionResult(
                text=captured["external"], counts={"name": 1}
            )

    monkeypatch.setattr(routes.service, "ai_gateway", FakeGateway())
    response = TestClient(app).post(
        f"/api/patients/{DEMO_PATIENT_ID}/ai-ingest",
        headers=_clinician_headers(),
        json={
            "title": "Synthetic consult",
            "raw_text": "Maya Chen (Synthetic) described dizziness.",
            "source": "doctor_consult",
        },
    )
    assert response.status_code == 201
    assert "Maya Chen" not in captured["external"]
    assert response.json()["entry"]["review_status"] == "ai_suggested"
    assert response.json()["externally_stored"] is False
    for entry_id in set(routes.repository.entries) - entry_ids_before:
        routes.repository.entries.pop(entry_id)
    for source_id in set(routes.repository.source_artifacts) - source_ids_before:
        routes.repository.source_artifacts.pop(source_id)
    del routes.repository.audit_events[audit_count_before:]


def test_websocket_reports_authenticated_presence():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "clinician", "password": "clinician-demo-2026"},
    )
    assert login.status_code == 200
    with client.websocket_connect(f"/api/ws/patients/{DEMO_PATIENT_ID}") as socket:
        message = socket.receive_json()
        assert message["type"] == "presence"
        assert message["count"] >= 1
        assert "clinician" in message["roles"]
