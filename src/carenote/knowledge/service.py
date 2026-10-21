"""Policy boundary for the reference knowledge base (design §19).

Administrators of a clinic create documents, add versions and retire documents; the care team
(staff, clinicians, administrators) reads them. Patients and the agent's SYSTEM actor are denied:
the agent reads through its principal clinician, like every other tool. Every write appends a
metadata-only, clinic-level event to the hash-chained audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

from carenote.domain.models import Actor, AuditEvent, Role
from carenote.knowledge.models import (
    KnowledgeDocument,
    KnowledgeDocumentCreate,
    KnowledgeRetire,
    KnowledgeStatus,
    KnowledgeVersion,
    KnowledgeVersionCreate,
)
from carenote.knowledge.store import KnowledgeStore
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import AccessDeniedError, ConcurrentEditError

AUDIT_RESOURCE = "knowledge_document"
_READERS = {Role.STAFF, Role.CLINICIAN, Role.ADMIN}


class KnowledgeService:
    def __init__(
        self,
        store: KnowledgeStore,
        repository: InMemoryCareNoteRepository,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.repository = repository
        self.now = now

    # Writes: administrators only

    def create_document(
        self, actor: Actor, request: KnowledgeDocumentCreate
    ) -> tuple[KnowledgeDocument, KnowledgeVersion]:
        self._require_admin(actor)
        now = self.now()
        document = KnowledgeDocument(
            clinic_id=actor.clinic_id,
            kind=request.kind,
            title=request.title.strip(),
            source=request.source.strip(),
            created_by=actor.id,
            created_at=now,
            updated_at=now,
        )
        version = self._version(actor, document, 1, request)
        self.store.save_document(document)
        self.store.add_version(version)
        self._audit(actor, document, 1, "knowledge_document_created")
        return document, version

    def add_version(
        self, actor: Actor, document_id: UUID, request: KnowledgeVersionCreate
    ) -> tuple[KnowledgeDocument, KnowledgeVersion]:
        self._require_admin(actor)
        document = self._document(actor, document_id)
        if document.status is KnowledgeStatus.RETIRED:
            raise ValueError("a retired document cannot get new versions")
        self._check_expected(document, request.expected_version)
        number = document.latest_version + 1
        version = self._version(actor, document, number, request)
        self.store.add_version(version)
        document = self.store.save_document(
            document.model_copy(update={"latest_version": number, "updated_at": self.now()})
        )
        self._audit(actor, document, number, "knowledge_version_added")
        return document, version

    def retire_document(
        self, actor: Actor, document_id: UUID, request: KnowledgeRetire
    ) -> KnowledgeDocument:
        self._require_admin(actor)
        document = self._document(actor, document_id)
        if document.status is KnowledgeStatus.RETIRED:
            raise ValueError("the document is already retired")
        self._check_expected(document, request.expected_version)
        now = self.now()
        document = self.store.save_document(
            document.model_copy(
                update={
                    "status": KnowledgeStatus.RETIRED,
                    "retired_by": actor.id,
                    "retired_at": now,
                    "updated_at": now,
                }
            )
        )
        self._audit(actor, document, document.latest_version, "knowledge_document_retired")
        return document

    # Reads: the care team

    def list_documents(
        self, actor: Actor, include_retired: bool = False
    ) -> list[KnowledgeDocument]:
        self._require_reader(actor)
        return [
            item
            for item in self.store.list_documents(actor.clinic_id)
            if include_retired or item.status is KnowledgeStatus.ACTIVE
        ]

    def list_versions(self, actor: Actor, document_id: UUID) -> list[KnowledgeVersion]:
        self._require_reader(actor)
        self._document(actor, document_id)
        return self.store.list_versions(actor.clinic_id, document_id)

    def get_version(
        self, actor: Actor, document_id: UUID, version: int | None = None
    ) -> KnowledgeVersion:
        """A specific version, or the latest one. Retired documents stay readable."""
        versions = self.list_versions(actor, document_id)
        if version is None:
            return versions[-1]
        found = next((item for item in versions if item.version == version), None)
        if found is None:
            raise LookupError("knowledge version not found")
        return found

    def current_versions(
        self, actor: Actor, today: date | None = None
    ) -> list[tuple[KnowledgeDocument, KnowledgeVersion]]:
        """What retrieval may use: per active document, the newest version already in effect.

        Superseded versions, future-dated versions and retired documents are left out.
        """
        today = today or self.now().date()
        current = []
        for document in self.list_documents(actor):
            effective = [
                item
                for item in self.store.list_versions(actor.clinic_id, document.id)
                if item.effective_date <= today
            ]
            if effective:
                current.append((document, max(effective, key=lambda item: item.version)))
        return current

    def list_audit_events(self, actor: Actor) -> list[AuditEvent]:
        self._require_admin(actor)
        return self.repository.list_clinic_audit_events(actor.clinic_id, AUDIT_RESOURCE)

    # Internals

    def _version(
        self, actor: Actor, document: KnowledgeDocument, number: int, request
    ) -> KnowledgeVersion:
        return KnowledgeVersion(
            document_id=document.id,
            clinic_id=document.clinic_id,
            version=number,
            label=request.version_label.strip(),
            effective_date=request.effective_date,
            content=request.content,
            created_by=actor.id,
            created_at=self.now(),
        )

    def _document(self, actor: Actor, document_id: UUID) -> KnowledgeDocument:
        document = self.store.get_document(actor.clinic_id, document_id)
        if document is None:
            raise LookupError("knowledge document not found")
        return document

    @staticmethod
    def _check_expected(document: KnowledgeDocument, expected: int) -> None:
        if expected != document.latest_version:
            raise ConcurrentEditError(
                f"the document is at version {document.latest_version}, not {expected}"
            )

    def _audit(
        self, actor: Actor, document: KnowledgeDocument, version: int, operation: str
    ) -> None:
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=None,
                clinic_id=document.clinic_id,
                resource=AUDIT_RESOURCE,
                resource_id=str(document.id),
                version=version,
                operation=operation,
                changed_by=actor.id,
                changed_at=self.now(),
            )
        )

    @staticmethod
    def _require_admin(actor: Actor) -> None:
        if actor.role is not Role.ADMIN:
            raise AccessDeniedError("only an administrator can change reference documents")

    @staticmethod
    def _require_reader(actor: Actor) -> None:
        if actor.role not in _READERS:
            raise AccessDeniedError("reference documents are for the care team")
