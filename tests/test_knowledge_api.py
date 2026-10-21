"""Knowledge API: administrators write, the care team reads, patients are refused."""

import pytest
from fastapi.testclient import TestClient

from carenote.api import routes
from carenote.knowledge.store import InMemoryKnowledgeStore
from carenote.main import app

DOCUMENT = {
    "kind": "drug_label",
    "title": "Apixaban tablets",
    "source": "Synthetic label for tests",
    "version_label": "2025",
    "effective_date": "2025-01-01",
    "content": "Dose reduction applies when two of age, weight and creatinine criteria are met.",
}


@pytest.fixture(autouse=True)
def empty_library(monkeypatch):
    monkeypatch.setattr(routes.knowledge, "store", InMemoryKnowledgeStore())


def _client(username):
    client = TestClient(app)
    login = client.post(
        "/api/auth/login", json={"username": username, "password": f"{username}-demo-2026"}
    )
    assert login.status_code == 200
    return client


def test_an_administrator_manages_the_full_lifecycle():
    admin = _client("admin")

    created = admin.post("/api/knowledge", json=DOCUMENT)
    assert created.status_code == 201
    detail = created.json()
    document_id = detail["document"]["id"]
    assert detail["current_version"] == 1
    assert detail["versions"][0]["characters"] == len(DOCUMENT["content"])
    assert "content" not in detail["versions"][0]

    added = admin.post(
        f"/api/knowledge/{document_id}/versions",
        json={
            "version_label": "2026",
            "effective_date": "2026-01-01",
            "content": "Updated label text.",
            "expected_version": 1,
        },
    )
    assert added.status_code == 201
    assert added.json()["current_version"] == 2

    first = admin.get(f"/api/knowledge/{document_id}/versions/1")
    assert first.json()["content"] == DOCUMENT["content"]

    retired = admin.post(f"/api/knowledge/{document_id}/retire", json={"expected_version": 2})
    assert retired.status_code == 200
    assert retired.json()["document"]["status"] == "retired"
    assert retired.json()["current_version"] is None
    assert admin.get("/api/knowledge").json() == []
    assert len(admin.get("/api/knowledge?include_retired=true").json()) == 1

    operations = [event["operation"] for event in admin.get("/api/knowledge/audit").json()]
    assert operations == [
        "knowledge_document_created",
        "knowledge_version_added",
        "knowledge_document_retired",
    ]


def test_the_care_team_reads_but_cannot_write():
    document_id = _client("admin").post("/api/knowledge", json=DOCUMENT).json()["document"]["id"]

    for username in ("clinician", "staff"):
        client = _client(username)
        assert [item["id"] for item in client.get("/api/knowledge").json()] == [document_id]
        assert client.get(f"/api/knowledge/{document_id}/versions/1").status_code == 200
        assert client.post("/api/knowledge", json=DOCUMENT).status_code == 403
        assert (
            client.post(
                f"/api/knowledge/{document_id}/retire", json={"expected_version": 1}
            ).status_code
            == 403
        )
        assert client.get("/api/knowledge/audit").status_code == 403


def test_patients_and_anonymous_callers_are_refused():
    document_id = _client("admin").post("/api/knowledge", json=DOCUMENT).json()["document"]["id"]

    patient = _client("patient")
    assert patient.get("/api/knowledge").status_code == 403
    assert patient.get(f"/api/knowledge/{document_id}").status_code == 403
    assert TestClient(app).get("/api/knowledge").status_code == 401


def test_errors_map_to_http_statuses():
    admin = _client("admin")
    document_id = admin.post("/api/knowledge", json=DOCUMENT).json()["document"]["id"]
    stale = {
        "version_label": "x",
        "effective_date": "2026-01-01",
        "content": "Text.",
        "expected_version": 5,
    }

    assert admin.post(f"/api/knowledge/{document_id}/versions", json=stale).status_code == 409
    assert admin.get(f"/api/knowledge/{document_id}/versions/9").status_code == 404
    assert admin.get(f"/api/knowledge/{routes.DEMO_CLINIC_ID}").status_code == 404
    assert admin.post("/api/knowledge", json=DOCUMENT | {"content": "  "}).status_code == 422
    assert admin.post("/api/knowledge", json=DOCUMENT | {"kind": "blog"}).status_code == 422


def test_the_page_ships_the_reference_library_card():
    page = TestClient(app).get("/").text

    assert 'id="knowledge-card"' in page
    assert 'id="knowledge-form"' in page
