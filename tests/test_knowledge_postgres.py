"""The encrypted PostgreSQL knowledge store: persistence, encryption, clinic scope, immutability.

Uses the shared `database` fixture: CARENOTE_TEST_DATABASE_URL (CI), else a local pgserver,
else the tests skip.
"""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from postgres_support import restricted_url

from carenote.core.security import ClinicalDataCipher
from carenote.domain.models import Actor, Role
from carenote.knowledge.models import KnowledgeDocumentCreate, KnowledgeVersionCreate
from carenote.knowledge.service import KnowledgeService
from carenote.knowledge.store import PostgresKnowledgeStore, migrate_knowledge
from carenote.repositories.memory import InMemoryCareNoteRepository

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
CIPHER = ClinicalDataCipher("test-only-knowledge-key")
CLINIC = uuid4()
ADMIN = Actor(id=uuid4(), role=Role.ADMIN, clinic_id=CLINIC)
CLINICIAN = Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=CLINIC)
REQUEST = KnowledgeDocumentCreate(
    kind="clinic_policy",
    title="Warfarin monitoring policy",
    source="Synthetic clinic policy",
    version_label="v1",
    effective_date="2026-01-01",
    content="Check the INR within one week of any warfarin dose change.",
)


@pytest.fixture
def app_url(database):
    # Tables first, then a non-superuser login so row-level security applies.
    migrate_knowledge(database)
    return restricted_url(database)


def _service(url, clinic=CLINIC):
    return KnowledgeService(
        PostgresKnowledgeStore(url, clinic, CIPHER, migrate=False),
        InMemoryCareNoteRepository(),
        now=lambda: NOW,
    )


def test_documents_and_versions_survive_a_restart(app_url):
    document, _ = _service(app_url).create_document(ADMIN, REQUEST)
    _service(app_url).add_version(
        ADMIN,
        document.id,
        KnowledgeVersionCreate(
            version_label="v2",
            effective_date="2026-09-01",
            content="Check the INR within five days of any warfarin dose change.",
            expected_version=1,
        ),
    )

    restarted = _service(app_url)

    [loaded] = restarted.list_documents(CLINICIAN)
    assert loaded.latest_version == 2
    versions = restarted.list_versions(CLINICIAN, document.id)
    assert [item.label for item in versions] == ["v1", "v2"]
    assert "five days" in versions[1].content


def test_content_is_encrypted_at_rest(app_url, database):
    _service(app_url).create_document(ADMIN, REQUEST)

    with psycopg.connect(database) as connection:
        payloads = connection.execute(
            "SELECT encrypted_payload FROM knowledge_documents "
            "UNION ALL SELECT encrypted_payload FROM knowledge_versions"
        ).fetchall()

    assert len(payloads) == 2
    for (payload,) in payloads:
        assert b"warfarin" not in bytes(payload).lower()
        assert b"Warfarin" not in bytes(payload)


def test_other_clinics_see_nothing_and_cannot_write(app_url):
    document, version = _service(app_url).create_document(ADMIN, REQUEST)
    other_clinic = uuid4()

    other = _service(app_url, other_clinic)

    assert other.store.list_documents(other_clinic) == []
    assert other.store.get_document(CLINIC, document.id) is None
    with pytest.raises(PermissionError):
        other.store.add_version(version.model_copy(update={"version": 2}))


def test_the_database_keeps_versions_immutable(app_url):
    document, version = _service(app_url).create_document(ADMIN, REQUEST)

    with psycopg.connect(app_url, options=f"-c app.current_clinic_id={CLINIC}") as connection:
        updated = connection.execute(
            "UPDATE knowledge_versions SET version = 9 WHERE document_id = %s", (document.id,)
        ).rowcount
        deleted = connection.execute(
            "DELETE FROM knowledge_versions WHERE document_id = %s", (document.id,)
        ).rowcount
    assert (updated, deleted) == (0, 0)

    with pytest.raises(ValueError, match="immutable"):
        _service(app_url).store.add_version(version)
