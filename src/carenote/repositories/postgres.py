import hashlib
from pathlib import Path
from uuid import UUID

import psycopg

from carenote.core.config import settings
from carenote.core.security import ClinicalDataCipher
from carenote.data.seed import (
    DEMO_ADMIN_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    DEMO_STAFF_ID,
)
from carenote.domain.models import (
    AuditEvent,
    Comment,
    Conflict,
    Highlight,
    ImportanceFeedback,
    Patient,
    ResourceRevision,
    SectionRevision,
    SectionState,
    SourceArtifact,
    TimelineEntry,
)
from carenote.repositories.memory import InMemoryCareNoteRepository


class PostgresCareNoteRepository(InMemoryCareNoteRepository):
    """Encrypted durable adapter with PostgreSQL-enforced clinic row isolation."""

    def __init__(self, database_url: str, clinic_id: UUID, cipher: ClinicalDataCipher) -> None:
        super().__init__()
        self.database_url = database_url
        self.clinic_id = clinic_id
        self.cipher = cipher
        self._hydrating = True
        self.migrate()
        if settings.environment != "production":
            self._provision_demo_memberships()
        self._load()
        self._hydrating = False

    def migrate(self) -> None:
        migration = (Path(__file__).parents[3] / "migrations" / "001_postgres_rls.sql").read_text(
            encoding="utf-8"
        )
        with psycopg.connect(self.database_url, autocommit=True) as connection:
            connection.execute(migration)

    def _execute_scoped(self, callback):
        with psycopg.connect(self.database_url) as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.current_clinic_id', %s, true)",
                (str(self.clinic_id),),
            )
            return callback(connection)

    def _provision_demo_memberships(self) -> None:
        memberships = (
            ("patient", "patient-demo-2026", DEMO_PATIENT_ID, "patient"),
            ("staff", "staff-demo-2026", DEMO_STAFF_ID, "staff"),
            ("clinician", "clinician-demo-2026", DEMO_CLINICIAN_ID, "clinician"),
            ("admin", "admin-demo-2026", DEMO_ADMIN_ID, "admin"),
        )

        def provision(connection):
            for username, password, actor_id, role in memberships:
                connection.execute(
                    """
                    INSERT INTO clinic_memberships (actor_id, clinic_id, role, active)
                    VALUES (%s, %s, %s, true)
                    ON CONFLICT (actor_id, clinic_id) DO UPDATE
                    SET role = EXCLUDED.role, active = true
                    """,
                    (actor_id, self.clinic_id, role),
                )
                salt = hashlib.sha256(f"carenote-demo:{username}".encode()).digest()[:16]
                password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
                connection.execute(
                    """
                    INSERT INTO user_accounts
                        (username, actor_id, clinic_id, password_salt, password_hash, active)
                    VALUES (%s, %s, %s, %s, %s, true)
                    ON CONFLICT (username) DO UPDATE SET
                        actor_id = EXCLUDED.actor_id,
                        clinic_id = EXCLUDED.clinic_id,
                        password_salt = EXCLUDED.password_salt,
                        password_hash = EXCLUDED.password_hash,
                        active = true
                    """,
                    (username, actor_id, self.clinic_id, salt, password_hash),
                )

        self._execute_scoped(provision)

    def _snapshot(self) -> dict[str, object]:
        return {
            "patients": [item.model_dump(mode="json") for item in self.patients.values()],
            "entries": [item.model_dump(mode="json") for item in self.entries.values()],
            "highlights": [item.model_dump(mode="json") for item in self.highlights.values()],
            "sections": [item.model_dump(mode="json") for item in self.sections.values()],
            "revisions": [
                item.model_dump(mode="json")
                for revisions in self.revisions.values()
                for item in revisions
            ],
            "comments": [item.model_dump(mode="json") for item in self.comments.values()],
            "sources": [item.model_dump(mode="json") for item in self.source_artifacts.values()],
            "audit": [item.model_dump(mode="json") for item in self.audit_events],
            "resource_revisions": [
                item.model_dump(mode="json")
                for revisions in self.resource_revisions.values()
                for item in revisions
            ],
            "conflicts": [item.model_dump(mode="json") for item in self.conflicts.values()],
            "importance_feedback": [
                item.model_dump(mode="json") for item in self.importance_feedback
            ],
        }

    def _persist(self) -> None:
        if self._hydrating:
            return
        encrypted = self.cipher.encrypt_json(self._snapshot())

        def save(connection):
            connection.execute(
                """
                INSERT INTO care_note_snapshots (clinic_id, encrypted_payload, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (clinic_id) DO UPDATE
                SET encrypted_payload = EXCLUDED.encrypted_payload, updated_at = now()
                """,
                (self.clinic_id, encrypted),
            )

        self._execute_scoped(save)

    def _load(self) -> None:
        row = self._execute_scoped(
            lambda connection: connection.execute(
                "SELECT encrypted_payload FROM care_note_snapshots WHERE clinic_id = %s",
                (self.clinic_id,),
            ).fetchone()
        )
        if row is None:
            return
        data = self.cipher.decrypt_json(bytes(row[0]))
        for raw in data.get("patients", []):
            item = Patient.model_validate(raw)
            self.patients[item.id] = item
        for raw in data.get("entries", []):
            item = TimelineEntry.model_validate(raw)
            self.entries[item.id] = item
        for raw in data.get("highlights", []):
            item = Highlight.model_validate(raw)
            self.highlights[item.id] = item
        for raw in data.get("sections", []):
            item = SectionState.model_validate(raw)
            self.sections[(item.patient_id, item.section)] = item
        for raw in data.get("revisions", []):
            item = SectionRevision.model_validate(raw)
            patient_id = next(
                state.patient_id
                for state in self.sections.values()
                if state.section == item.section
            )
            self.revisions.setdefault((patient_id, item.section), []).append(item)
        for raw in data.get("comments", []):
            item = Comment.model_validate(raw)
            self.comments[item.id] = item
        for raw in data.get("sources", []):
            item = SourceArtifact.model_validate(raw)
            self.source_artifacts[item.id] = item
        self.audit_events = [AuditEvent.model_validate(raw) for raw in data.get("audit", [])]
        for raw in data.get("resource_revisions", []):
            item = ResourceRevision.model_validate(raw)
            self.resource_revisions.setdefault((item.resource, item.resource_id), []).append(item)
        for raw in data.get("conflicts", []):
            item = Conflict.model_validate(raw)
            self.conflicts[item.id] = item
        self.importance_feedback = [
            ImportanceFeedback.model_validate(raw) for raw in data.get("importance_feedback", [])
        ]

    def add_patient(self, patient: Patient) -> Patient:
        saved = super().add_patient(patient)
        self._persist()
        return saved

    def add_entry(self, entry: TimelineEntry) -> TimelineEntry:
        saved = super().add_entry(entry)
        self._persist()
        return saved

    def add_source_artifact(self, artifact: SourceArtifact) -> SourceArtifact:
        saved = super().add_source_artifact(artifact)
        self._persist()
        return saved

    def add_highlight(self, highlight: Highlight) -> Highlight:
        saved = super().add_highlight(highlight)
        self._persist()
        return saved

    def save_highlight(self, highlight: Highlight) -> Highlight:
        saved = super().save_highlight(highlight)
        self._persist()
        return saved

    def save_section(self, state: SectionState, revision: SectionRevision) -> SectionState:
        saved = super().save_section(state, revision)
        self._persist()
        return saved

    def add_comment(self, comment: Comment) -> Comment:
        saved = super().add_comment(comment)
        self._persist()
        return saved

    def save_comment(self, comment: Comment) -> Comment:
        saved = super().save_comment(comment)
        self._persist()
        return saved

    def add_audit_event(self, event: AuditEvent) -> AuditEvent:
        saved = super().add_audit_event(event)
        self._persist()
        self._execute_scoped(
            lambda connection: connection.execute(
                """
                INSERT INTO audit_chain_anchors (clinic_id, event_hash)
                VALUES (%s, %s)
                """,
                (self.clinic_id, saved.event_hash),
            )
        )
        return saved

    def add_resource_revision(self, revision: ResourceRevision) -> ResourceRevision:
        saved = super().add_resource_revision(revision)
        self._persist()
        return saved

    def sync_conflicts(self, conflicts: list[Conflict]) -> None:
        before = len(self.conflicts)
        super().sync_conflicts(conflicts)
        if len(self.conflicts) != before:
            self._persist()

    def save_conflict(self, conflict: Conflict) -> Conflict:
        saved = super().save_conflict(conflict)
        self._persist()
        return saved

    def add_importance_feedback(self, feedback: ImportanceFeedback) -> ImportanceFeedback:
        saved = super().add_importance_feedback(feedback)
        self._persist()
        return saved

    def archive_entries(self, entry_ids: list[UUID]) -> int:
        items = [self.entries[item] for item in entry_ids if item in self.entries]

        def save_cold(connection):
            for entry in items:
                summary = " ".join(entry.content.split())[:240]
                encrypted = self.cipher.encrypt_json(entry.model_dump(mode="json"))
                connection.execute(
                    """
                    INSERT INTO cold_entry_archives
                        (clinic_id, patient_id, entry_id, compressed_summary, encrypted_payload)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (entry_id) DO NOTHING
                    """,
                    (self.clinic_id, entry.patient_id, entry.id, summary, encrypted),
                )

        self._execute_scoped(save_cold)
        archived = super().archive_entries(entry_ids)
        self._persist()
        return archived

    def replace_last_audit_operation(self, operation: str) -> None:
        super().replace_last_audit_operation(operation)
        self._persist()
