"""Reference knowledge documents: versions, retirement, roles, clinic scope and audit."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from carenote.domain.models import Actor, Role
from carenote.knowledge.models import (
    KnowledgeDocumentCreate,
    KnowledgeKind,
    KnowledgeRetire,
    KnowledgeStatus,
    KnowledgeVersion,
    KnowledgeVersionCreate,
)
from carenote.knowledge.service import AUDIT_RESOURCE, KnowledgeService
from carenote.knowledge.store import InMemoryKnowledgeStore
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import AccessDeniedError, ConcurrentEditError

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
CLINIC = uuid4()
ADMIN = Actor(id=uuid4(), role=Role.ADMIN, clinic_id=CLINIC)
CLINICIAN = Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=CLINIC)
STAFF = Actor(id=uuid4(), role=Role.STAFF, clinic_id=CLINIC)
PATIENT = Actor(id=uuid4(), role=Role.PATIENT, clinic_id=CLINIC)
SYSTEM = Actor(id=uuid4(), role=Role.SYSTEM, clinic_id=CLINIC)
OTHER_ADMIN = Actor(id=uuid4(), role=Role.ADMIN, clinic_id=uuid4())


def _create(**overrides):
    fields = {
        "kind": "guideline",
        "title": "Anticoagulation in atrial fibrillation",
        "source": "Synthetic guideline for tests",
        "version_label": "2025",
        "effective_date": "2025-01-01",
        "content": "Review apixaban dose criteria:\r\nage, weight and creatinine.  ",
    }
    return KnowledgeDocumentCreate.model_validate(fields | overrides)


def _new_version(expected, label="2026", effective="2026-06-01", content="Updated text."):
    return KnowledgeVersionCreate(
        version_label=label,
        effective_date=effective,
        content=content,
        expected_version=expected,
    )


@pytest.fixture
def knowledge():
    return KnowledgeService(InMemoryKnowledgeStore(), InMemoryCareNoteRepository(), now=lambda: NOW)


def test_an_administrator_creates_a_document_with_its_first_version(knowledge):
    document, version = knowledge.create_document(ADMIN, _create())

    assert document.kind is KnowledgeKind.GUIDELINE
    assert document.status is KnowledgeStatus.ACTIVE
    assert (document.latest_version, version.version, version.label) == (1, 1, "2025")
    # Newlines are normalised and the ends trimmed, so offsets are stable everywhere.
    assert version.content == "Review apixaban dose criteria:\nage, weight and creatinine."
    assert len(version.content_sha256) == 64
    assert knowledge.list_documents(CLINICIAN) == [document]


def test_versions_are_added_never_changed(knowledge):
    document, first = knowledge.create_document(ADMIN, _create())

    document, second = knowledge.add_version(ADMIN, document.id, _new_version(1))

    assert (document.latest_version, second.version) == (2, 2)
    assert knowledge.get_version(CLINICIAN, document.id, 1) == first
    assert knowledge.get_version(CLINICIAN, document.id).content == "Updated text."
    with pytest.raises(ValueError, match="immutable"):
        knowledge.store.add_version(first.model_copy(update={"content": "Rewritten."}))


def test_a_stale_expected_version_is_a_concurrent_edit(knowledge):
    document, _ = knowledge.create_document(ADMIN, _create())
    knowledge.add_version(ADMIN, document.id, _new_version(1))

    with pytest.raises(ConcurrentEditError, match="version 2, not 1"):
        knowledge.add_version(ADMIN, document.id, _new_version(1))
    with pytest.raises(ConcurrentEditError):
        knowledge.retire_document(ADMIN, document.id, KnowledgeRetire(expected_version=1))


def test_retired_documents_stay_readable_but_take_no_versions(knowledge):
    document, first = knowledge.create_document(ADMIN, _create())

    retired = knowledge.retire_document(ADMIN, document.id, KnowledgeRetire(expected_version=1))

    assert (retired.status, retired.retired_by) == (KnowledgeStatus.RETIRED, ADMIN.id)
    assert knowledge.list_documents(CLINICIAN) == []
    assert knowledge.list_documents(CLINICIAN, include_retired=True) == [retired]
    assert knowledge.get_version(CLINICIAN, document.id, 1) == first
    with pytest.raises(ValueError, match="retired"):
        knowledge.add_version(ADMIN, document.id, _new_version(1))
    with pytest.raises(ValueError, match="already retired"):
        knowledge.retire_document(ADMIN, document.id, KnowledgeRetire(expected_version=1))


def test_only_the_version_in_effect_of_active_documents_is_current(knowledge):
    current, _ = knowledge.create_document(ADMIN, _create())
    knowledge.add_version(ADMIN, current.id, _new_version(1, effective="2026-06-01"))
    knowledge.add_version(ADMIN, current.id, _new_version(2, label="2027", effective="2027-01-01"))
    knowledge.create_document(ADMIN, _create(title="Future policy", effective_date="2027-03-01"))
    retired, _ = knowledge.create_document(ADMIN, _create(title="Old drug label"))
    knowledge.retire_document(ADMIN, retired.id, KnowledgeRetire(expected_version=1))

    found = knowledge.current_versions(CLINICIAN, today=date(2026, 10, 21))

    # Version 2 is in effect; version 3 is future-dated, as is the whole future policy.
    assert [(document.id, version.version) for document, version in found] == [(current.id, 2)]


def test_writes_need_an_administrator_and_reads_the_care_team(knowledge):
    document, _ = knowledge.create_document(ADMIN, _create())

    for actor in (CLINICIAN, STAFF, PATIENT, SYSTEM):
        with pytest.raises(AccessDeniedError):
            knowledge.create_document(actor, _create())
        with pytest.raises(AccessDeniedError):
            knowledge.add_version(actor, document.id, _new_version(1))
    for actor in (STAFF, CLINICIAN, ADMIN):
        assert knowledge.list_documents(actor)
    for actor in (PATIENT, SYSTEM):
        with pytest.raises(AccessDeniedError):
            knowledge.list_documents(actor)
        with pytest.raises(AccessDeniedError):
            knowledge.get_version(actor, document.id)


def test_documents_are_scoped_to_their_clinic(knowledge):
    document, _ = knowledge.create_document(ADMIN, _create())

    assert knowledge.list_documents(OTHER_ADMIN) == []
    with pytest.raises(LookupError):
        knowledge.get_version(OTHER_ADMIN, document.id)
    with pytest.raises(LookupError):
        knowledge.add_version(OTHER_ADMIN, document.id, _new_version(1))


def test_every_write_is_audited_at_clinic_level_without_content(knowledge):
    document, _ = knowledge.create_document(ADMIN, _create())
    knowledge.add_version(ADMIN, document.id, _new_version(1))
    knowledge.retire_document(ADMIN, document.id, KnowledgeRetire(expected_version=2))

    events = knowledge.list_audit_events(ADMIN)

    assert [(event.operation, event.version) for event in events] == [
        ("knowledge_document_created", 1),
        ("knowledge_version_added", 2),
        ("knowledge_document_retired", 2),
    ]
    assert all(event.patient_id is None for event in events)
    assert all(event.resource == AUDIT_RESOURCE for event in events)
    assert "apixaban" not in str([event.model_dump() for event in events]).lower()
    assert knowledge.repository.verify_audit_chain()
    with pytest.raises(AccessDeniedError):
        knowledge.list_audit_events(CLINICIAN)


def test_requests_are_validated():
    with pytest.raises(ValidationError):
        _create(content="   \r\n ")
    with pytest.raises(ValidationError):
        _create(kind="blog_post")
    with pytest.raises(ValidationError):
        _create(unexpected="field")
    with pytest.raises(ValueError, match="hash"):
        KnowledgeVersion(
            document_id=uuid4(),
            clinic_id=CLINIC,
            version=1,
            label="x",
            effective_date="2026-01-01",
            content="Text.",
            content_sha256="0" * 64,
            created_by=ADMIN.id,
        )
