"""Storage for reference knowledge, one clinic per store.

`InMemoryKnowledgeStore` serves development and tests. `PostgresKnowledgeStore` keeps documents
and versions in their own tables (migration 004), apart from the clinical snapshot, so large
guidelines are not rewritten on every record write. Payloads are encrypted with the clinical data
key, both tables are clinic-scoped by forced row-level security, and versions have no update or
delete policy, so the database itself keeps them immutable. Reads are served from memory after
the store loads, as retrieval runs in memory (roadmap decision for Phase 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

import psycopg

from carenote.core.security import ClinicalDataCipher
from carenote.knowledge.models import KnowledgeDocument, KnowledgeVersion

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "004_knowledge.sql"


class KnowledgeStore(Protocol):
    def save_document(self, document: KnowledgeDocument) -> KnowledgeDocument: ...

    def add_version(self, version: KnowledgeVersion) -> KnowledgeVersion: ...

    def get_document(self, clinic_id: UUID, document_id: UUID) -> KnowledgeDocument | None: ...

    def list_documents(self, clinic_id: UUID) -> list[KnowledgeDocument]: ...

    def list_versions(self, clinic_id: UUID, document_id: UUID) -> list[KnowledgeVersion]: ...


class InMemoryKnowledgeStore:
    def __init__(self) -> None:
        self.documents: dict[UUID, KnowledgeDocument] = {}
        self.versions: dict[UUID, list[KnowledgeVersion]] = {}

    def save_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        self.documents[document.id] = document
        return document

    def add_version(self, version: KnowledgeVersion) -> KnowledgeVersion:
        versions = self.versions.setdefault(version.document_id, [])
        if any(item.version == version.version for item in versions):
            raise ValueError("knowledge versions are immutable")
        versions.append(version)
        return version

    def get_document(self, clinic_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        document = self.documents.get(document_id)
        return document if document is not None and document.clinic_id == clinic_id else None

    def list_documents(self, clinic_id: UUID) -> list[KnowledgeDocument]:
        return sorted(
            (item for item in self.documents.values() if item.clinic_id == clinic_id),
            key=lambda item: (item.kind, item.title.lower(), str(item.id)),
        )

    def list_versions(self, clinic_id: UUID, document_id: UUID) -> list[KnowledgeVersion]:
        if self.get_document(clinic_id, document_id) is None:
            return []
        return sorted(self.versions.get(document_id, []), key=lambda item: item.version)


class PostgresKnowledgeStore(InMemoryKnowledgeStore):
    """Encrypted, clinic-scoped knowledge tables, cached in memory for one clinic."""

    def __init__(
        self,
        database_url: str,
        clinic_id: UUID,
        cipher: ClinicalDataCipher,
        migrate: bool = True,
    ) -> None:
        super().__init__()
        self.database_url = database_url
        self.clinic_id = clinic_id
        self.cipher = cipher
        # An application role without DDL rights connects with migrate=False after the schema
        # owner has run migration 004.
        if migrate:
            migrate_knowledge(database_url)
        self._load()

    def _connect(self):
        return psycopg.connect(
            self.database_url, options=f"-c app.current_clinic_id={self.clinic_id}"
        )

    def _check_clinic(self, clinic_id: UUID) -> None:
        if clinic_id != self.clinic_id:
            raise PermissionError("this knowledge store serves another clinic")

    def save_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        self._check_clinic(document.clinic_id)
        payload = self.cipher.encrypt_json(document.model_dump(mode="json"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_documents (document_id, clinic_id, encrypted_payload, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (document_id) DO UPDATE
                SET encrypted_payload = EXCLUDED.encrypted_payload, updated_at = now()
                """,
                (document.id, document.clinic_id, payload),
            )
        return super().save_document(document)

    def add_version(self, version: KnowledgeVersion) -> KnowledgeVersion:
        self._check_clinic(version.clinic_id)
        payload = self.cipher.encrypt_json(version.model_dump(mode="json"))
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT INTO knowledge_versions (document_id, version, clinic_id, encrypted_payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (document_id, version) DO NOTHING
                """,
                (version.document_id, version.version, version.clinic_id, payload),
            ).rowcount
        if inserted != 1:
            raise ValueError("knowledge versions are immutable")
        return super().add_version(version)

    def _load(self) -> None:
        with self._connect() as connection:
            documents = connection.execute(
                "SELECT encrypted_payload FROM knowledge_documents"
            ).fetchall()
            versions = connection.execute(
                "SELECT encrypted_payload FROM knowledge_versions"
            ).fetchall()
        for (payload,) in documents:
            document = KnowledgeDocument.model_validate(self.cipher.decrypt_json(bytes(payload)))
            self.documents[document.id] = document
        for (payload,) in versions:
            version = KnowledgeVersion.model_validate(self.cipher.decrypt_json(bytes(payload)))
            self.versions.setdefault(version.document_id, []).append(version)


def migrate_knowledge(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(MIGRATION.read_text(encoding="utf-8"))
