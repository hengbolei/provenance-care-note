"""The shared public demo: banner info, the periodic reset to the seed, and its guard rails."""

import pytest
from fastapi.testclient import TestClient

from carenote.api import system
from carenote.api.container import container, reset_demo_state
from carenote.core.config import Settings
from carenote.data.seed import DEMO_PATIENT_ID
from carenote.main import app


def _client(username):
    client = TestClient(app)
    login = client.post(
        "/api/auth/login", json={"username": username, "password": f"{username}-demo-2026"}
    )
    assert login.status_code == 200
    return client


def test_a_reset_puts_the_store_back_to_the_seed_and_keeps_sessions():
    clinician = _client("clinician")
    seeded = len(container.repository.entries)

    added = clinician.post(
        f"/api/patients/{DEMO_PATIENT_ID}/entries",
        json={"title": "Visitor note", "content": "Written by a demo visitor."},
    )
    assert added.status_code == 201
    assert len(container.repository.entries) == seeded + 1

    reset_demo_state()

    assert len(container.repository.entries) == seeded
    assert added.json()["id"] not in {str(entry_id) for entry_id in container.repository.entries}
    # Signed sessions survive: the seed recreates the same users.
    assert clinician.get("/api/auth/me").status_code == 200


def test_the_demo_endpoint_reports_the_mode(monkeypatch):
    client = TestClient(app)
    assert client.get("/api/demo").json() == {"public_demo": False, "reset_minutes": None}

    monkeypatch.setattr(system, "settings", Settings(public_demo=True, demo_reset_minutes=30))
    assert client.get("/api/demo").json() == {"public_demo": True, "reset_minutes": 30}


def test_a_public_demo_serves_secure_cookies_even_in_development():
    assert not Settings(environment="development").secure_cookies
    assert Settings(environment="development", public_demo=True).secure_cookies


@pytest.mark.parametrize(
    "unsafe",
    [
        {"database_url": "postgresql://example/db"},
        {"environment": "production"},
        {"demo_reset_minutes": 0},
    ],
)
def test_a_public_demo_refuses_real_storage_and_bad_settings(unsafe):
    with pytest.raises(RuntimeError):
        Settings(public_demo=True, **unsafe).validate_production()
