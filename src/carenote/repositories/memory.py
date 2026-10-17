import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from carenote.domain.models import (
    AuditEvent,
    Comment,
    Conflict,
    ConflictStatus,
    Highlight,
    ImportanceFeedback,
    Patient,
    ResourceRevision,
    SectionRevision,
    SectionState,
    SourceArtifact,
    Span,
    TimelineEntry,
)
from carenote.repositories.search import keyword_search


class InMemoryCareNoteRepository:
    """Development adapter; replace with a clinic-scoped database adapter."""

    def __init__(self) -> None:
        self.patients: dict[UUID, Patient] = {}
        self.entries: dict[UUID, TimelineEntry] = {}
        self.highlights: dict[UUID, Highlight] = {}
        self.sections: dict[tuple[UUID, str], SectionState] = {}
        self.revisions: dict[tuple[UUID, str], list[SectionRevision]] = {}
        self.comments: dict[UUID, Comment] = {}
        self.source_artifacts: dict[str, SourceArtifact] = {}
        self.audit_events: list[AuditEvent] = []
        self.resource_revisions: dict[tuple[str, str], list[ResourceRevision]] = {}
        self.conflicts: dict[UUID, Conflict] = {}
        self.importance_feedback: list[ImportanceFeedback] = []
        self.archived_entries: dict[UUID, tuple[TimelineEntry, str]] = {}

    def add_patient(self, patient: Patient) -> Patient:
        self.patients[patient.id] = patient
        return patient

    def get_patient(self, patient_id: UUID, clinic_id: UUID) -> Patient | None:
        patient = self.patients.get(patient_id)
        if patient is None or patient.clinic_id != clinic_id:
            return None
        return patient

    def add_entry(self, entry: TimelineEntry) -> TimelineEntry:
        self.entries[entry.id] = entry
        return entry

    def add_source_artifact(self, artifact: SourceArtifact) -> SourceArtifact:
        self.source_artifacts[artifact.id] = artifact
        return artifact

    def get_source_artifact(
        self, source_id: str, patient_id: UUID, clinic_id: UUID
    ) -> SourceArtifact | None:
        artifact = self.source_artifacts.get(source_id)
        if artifact is None or artifact.patient_id != patient_id or artifact.clinic_id != clinic_id:
            return None
        return artifact

    def list_entries(self, patient_id: UUID, clinic_id: UUID) -> list[TimelineEntry]:
        return sorted(
            (
                item
                for item in self.entries.values()
                if item.patient_id == patient_id and item.clinic_id == clinic_id
            ),
            key=lambda item: item.timestamp,
            reverse=True,
        )

    def search_entries(
        self,
        patient_id: UUID,
        clinic_id: UUID,
        query: str,
        k: int = 10,
        include_internal: bool = True,
    ) -> list[Span]:
        """Keyword search over one patient's active entries in one clinic.

        Archived entries are excluded. Pass include_internal=False for anything patient-facing.
        """
        entries = [
            entry
            for entry in self.list_entries(patient_id, clinic_id)
            if include_internal or not entry.internal
        ]
        return keyword_search(entries, query, k)

    def get_entry(self, entry_id: UUID) -> TimelineEntry | None:
        return self.entries.get(entry_id)

    def add_highlight(self, highlight: Highlight) -> Highlight:
        source = self.entries.get(highlight.provenance_pointer.entry_id)
        if source is None or source.patient_id != highlight.patient_id:
            raise ValueError("provenance_pointer must resolve to this patient's timeline")
        if highlight.provenance_pointer.end > len(source.content):
            raise ValueError("provenance span is outside the source entry")
        self.highlights[highlight.id] = highlight
        return highlight

    def list_highlights(self, patient_id: UUID) -> list[Highlight]:
        return sorted(
            (item for item in self.highlights.values() if item.patient_id == patient_id),
            key=lambda item: item.priority,
            reverse=True,
        )

    def get_highlight(self, highlight_id: UUID) -> Highlight | None:
        return self.highlights.get(highlight_id)

    def save_highlight(self, highlight: Highlight) -> Highlight:
        if highlight.id not in self.highlights:
            raise LookupError("highlight does not exist")
        self.highlights[highlight.id] = highlight
        return highlight

    def get_section(self, patient_id: UUID, section: str) -> SectionState | None:
        return self.sections.get((patient_id, section))

    def save_section(self, state: SectionState, revision: SectionRevision) -> SectionState:
        key = (state.patient_id, state.section)
        self.sections[key] = state
        self.revisions.setdefault(key, []).append(revision)
        return state

    def list_revisions(self, patient_id: UUID, section: str) -> list[SectionRevision]:
        return list(self.revisions.get((patient_id, section), []))

    def list_sections(self, patient_id: UUID, clinic_id: UUID) -> list[SectionState]:
        return sorted(
            (
                state
                for state in self.sections.values()
                if state.patient_id == patient_id and state.clinic_id == clinic_id
            ),
            key=lambda state: state.section,
        )

    def add_comment(self, comment: Comment) -> Comment:
        self.comments[comment.id] = comment
        return comment

    def get_comment(self, comment_id: UUID) -> Comment | None:
        return self.comments.get(comment_id)

    def save_comment(self, comment: Comment) -> Comment:
        self.comments[comment.id] = comment
        return comment

    def list_comments(self, patient_id: UUID, clinic_id: UUID) -> list[Comment]:
        return sorted(
            (
                comment
                for comment in self.comments.values()
                if comment.patient_id == patient_id and comment.clinic_id == clinic_id
            ),
            key=lambda comment: comment.created_at,
            reverse=True,
        )

    def add_audit_event(self, event: AuditEvent) -> AuditEvent:
        previous_hash = self.audit_events[-1].event_hash if self.audit_events else "0" * 64
        payload = event.model_dump(mode="json", exclude={"previous_hash", "event_hash"})
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        event_hash = hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()
        chained = event.model_copy(
            update={"previous_hash": previous_hash, "event_hash": event_hash}
        )
        self.audit_events.append(chained)
        return chained

    def verify_audit_chain(self) -> bool:
        previous_hash = "0" * 64
        for event in self.audit_events:
            payload = event.model_dump(mode="json", exclude={"previous_hash", "event_hash"})
            canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            expected = hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()
            if event.previous_hash != previous_hash or not hmac.compare_digest(
                event.event_hash, expected
            ):
                return False
            previous_hash = event.event_hash
        return True

    def replace_last_audit_operation(self, operation: str) -> None:
        if not self.audit_events:
            raise LookupError("audit trail is empty")
        event = self.audit_events.pop()
        self.add_audit_event(event.model_copy(update={"operation": operation}))

    def purge_expired_audit_events(self, retention_days: int, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        before = len(self.audit_events)
        self.audit_events = [event for event in self.audit_events if event.changed_at >= cutoff]
        if self.audit_events:
            # A retained segment starts a new verifiable chain after policy-based deletion.
            retained = list(self.audit_events)
            self.audit_events = []
            for event in retained:
                self.add_audit_event(
                    event.model_copy(update={"previous_hash": "", "event_hash": ""})
                )
        return before - len(self.audit_events)

    def add_resource_revision(self, revision: ResourceRevision) -> ResourceRevision:
        key = (revision.resource, revision.resource_id)
        self.resource_revisions.setdefault(key, []).append(revision)
        return revision

    def list_resource_revisions(self, resource: str, resource_id: str) -> list[ResourceRevision]:
        return list(self.resource_revisions.get((resource, resource_id), []))

    def sync_conflicts(self, conflicts: list[Conflict]) -> None:
        for conflict in conflicts:
            if conflict.id not in self.conflicts:
                self.conflicts[conflict.id] = conflict
                self.add_resource_revision(
                    ResourceRevision(
                        resource="conflict",
                        resource_id=str(conflict.id),
                        version=conflict.version,
                        snapshot=conflict.model_dump(mode="json"),
                        operation="conflict_detect",
                    )
                )

    def list_conflicts(self, patient_id: UUID, clinic_id: UUID) -> list[Conflict]:
        return sorted(
            (
                conflict
                for conflict in self.conflicts.values()
                if conflict.patient_id == patient_id and conflict.clinic_id == clinic_id
            ),
            key=lambda item: (item.status is ConflictStatus.RESOLVED, item.category.value),
        )

    def get_conflict(self, conflict_id: UUID) -> Conflict | None:
        return self.conflicts.get(conflict_id)

    def save_conflict(self, conflict: Conflict) -> Conflict:
        if conflict.id not in self.conflicts:
            raise LookupError("conflict does not exist")
        self.conflicts[conflict.id] = conflict
        return conflict

    def add_importance_feedback(self, feedback: ImportanceFeedback) -> ImportanceFeedback:
        self.importance_feedback.append(feedback)
        return feedback

    def has_highlight_impression(self, highlight_id: UUID, actor_id: UUID) -> bool:
        return any(
            item.highlight_id == highlight_id
            and item.actor_id == actor_id
            and item.accepted is None
            for item in self.importance_feedback
        )

    def importance_adjustment(self, highlight: Highlight) -> tuple[int, str] | None:
        signature = "|".join(sorted({item.lower() for item in highlight.clinical_entities}))
        relevant = [
            item
            for item in self.importance_feedback
            if item.patient_id == highlight.patient_id and item.entity_signature == signature
        ]
        reviewed = [item for item in relevant if item.accepted is not None]
        if len(reviewed) < 3:
            return None
        accepted = sum(item.accepted is True for item in reviewed)
        impressions = max(len(relevant), len(reviewed))
        review_rate = len(reviewed) / impressions
        raw = ((accepted / len(reviewed)) - 0.5) * 16
        points = max(-8, min(8, round(raw * review_rate)))
        explanation = (
            f"Bounded adjustment from {len(reviewed)} reviewed of {impressions} exposed "
            f"similar item(s); {accepted} were confirmed. Minimum sample=3, cap=±8."
        )
        return points, explanation

    def archive_entries(self, entry_ids: list[UUID]) -> int:
        archived = 0
        for entry_id in entry_ids:
            entry = self.entries.pop(entry_id, None)
            if entry is None:
                continue
            summary = " ".join(entry.content.split())[:240]
            self.archived_entries[entry_id] = (entry, summary)
            archived += 1
        return archived

    def restore_entry(self, entry_id: UUID) -> TimelineEntry:
        archived = self.archived_entries.pop(entry_id, None)
        if archived is None:
            raise LookupError("archived entry does not exist")
        entry, _ = archived
        self.entries[entry.id] = entry
        return entry

    def list_audit_events(self, patient_id: UUID, clinic_id: UUID) -> list[AuditEvent]:
        return [
            event
            for event in self.audit_events
            if event.patient_id == patient_id and event.clinic_id == clinic_id
        ]
