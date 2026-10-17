import re
from datetime import UTC, datetime
from difflib import unified_diff
from uuid import UUID, uuid4

from carenote.domain.models import (
    ActionStatus,
    Actor,
    AIIngestRequest,
    AIIngestResponse,
    AnnotationTarget,
    AnnotationTargetType,
    ArchiveBatch,
    ArchiveCandidate,
    AuditEvent,
    Comment,
    Conflict,
    ConflictStatus,
    ConflictUpdateRequest,
    EntryOrigin,
    EntryType,
    Highlight,
    HighlightCreateRequest,
    HighlightUpdateRequest,
    ImportanceFeedback,
    PatientAction,
    PatientDetailResponse,
    ProvenancePointer,
    ProvenanceSource,
    ResourceRevision,
    ReviewStatus,
    Role,
    SectionDiff,
    SectionRevision,
    SectionState,
    SourceArtifact,
    SourceArtifactKind,
    SourceArtifactPointer,
    SourceEvidence,
    TimelineEntry,
    utc_now,
)
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.ai import OpenAIClinicalGateway
from carenote.services.conflicts import DeterministicConflictDetector
from carenote.services.importance import DeterministicImportanceScorer

AI_ENTRY_TYPES = {
    EntryType.AI_DOCTOR_CONSULT_SUMMARY,
    EntryType.AI_NURSE_CONSULT_SUMMARY,
    EntryType.AI_PATIENT_SESSION_SUMMARY,
}


class AccessDeniedError(PermissionError):
    pass


class ConcurrentEditError(RuntimeError):
    pass


class PatientNotFoundError(LookupError):
    pass


class CareNoteService:
    def __init__(
        self,
        repository: InMemoryCareNoteRepository,
        ai_gateway: OpenAIClinicalGateway | None = None,
    ) -> None:
        self.repository = repository
        self.importance_scorer = DeterministicImportanceScorer()
        self.conflict_detector = DeterministicConflictDetector()
        self.ai_gateway = ai_gateway or OpenAIClinicalGateway()

    def get_patient_view(self, actor: Actor, patient_id: UUID) -> PatientDetailResponse:
        if actor.role not in {Role.PATIENT, Role.STAFF, Role.CLINICIAN, Role.ADMIN}:
            raise AccessDeniedError("this role cannot open an interactive patient view")
        patient = self.repository.get_patient(patient_id, actor.clinic_id)
        if patient is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        if actor.role is Role.PATIENT and actor.id != patient_id:
            raise AccessDeniedError("patients can only access their own record")
        entries = self.repository.list_entries(patient_id, actor.clinic_id)
        if actor.role is Role.PATIENT:
            entries = [
                item for item in entries if not item.internal and item.type not in AI_ENTRY_TYPES
            ]
            highlights: list[Highlight] = []
            patient_actions = [
                PatientAction(
                    title=item.text,
                    instruction=item.patient_instruction,
                    action_status=item.action_status,
                    updated_at=item.updated_at or item.created_at,
                )
                for item in self.repository.list_highlights(patient_id)
                if item.status is ReviewStatus.CLINICIAN_CONFIRMED
                and item.patient_instruction is not None
            ]
            sections: list[SectionState] = []
            comments: list[Comment] = []
            conflicts = []
        else:
            highlights = self._rank_highlights(patient_id, entries)
            patient_actions = []
            sections = self.repository.list_sections(patient_id, actor.clinic_id)
            comments = self.repository.list_comments(patient_id, actor.clinic_id)
            self.repository.sync_conflicts(self.conflict_detector.detect(patient_id, entries))
            conflicts = self.repository.list_conflicts(patient_id, actor.clinic_id)
        return PatientDetailResponse(
            patient=patient,
            highlights=highlights,
            entries=entries,
            patient_actions=patient_actions,
            sections=sections,
            comments=comments,
            conflicts=conflicts,
        )

    def _rank_highlights(self, patient_id: UUID, entries: list[TimelineEntry]) -> list[Highlight]:
        if not entries:
            return []
        by_id = {entry.id: entry for entry in entries}
        reference_time = max(entry.timestamp for entry in entries)
        ranked = [
            self.importance_scorer.score(
                highlight,
                by_id[highlight.provenance_pointer.entry_id],
                reference_time,
                self.repository.importance_adjustment(highlight),
            )
            for highlight in self.repository.list_highlights(patient_id)
            if highlight.provenance_pointer.entry_id in by_id
        ]
        return sorted(ranked, key=lambda item: item.priority, reverse=True)

    def get_source_evidence(self, actor: Actor, patient_id: UUID, entry_id: UUID) -> SourceEvidence:
        self._require_internal_collaborator(actor)
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        entry = self.repository.get_entry(entry_id)
        if entry is None or entry.patient_id != patient_id or entry.clinic_id != actor.clinic_id:
            raise LookupError("timeline entry does not exist")
        pointer = entry.origin.source_pointer
        if pointer is None:
            raise LookupError("this entry has no resolvable original source")
        artifact = self.repository.get_source_artifact(
            pointer.source_id, patient_id, actor.clinic_id
        )
        if artifact is None or pointer.end > len(artifact.content):
            raise LookupError("original source does not exist")
        return SourceEvidence(
            artifact=artifact,
            pointer=pointer,
            excerpt=artifact.content[pointer.start : pointer.end],
        )

    def get_highlight_source_evidence(
        self, actor: Actor, patient_id: UUID, highlight_id: UUID
    ) -> SourceEvidence:
        self._require_internal_collaborator(actor)
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        highlight = self.repository.get_highlight(highlight_id)
        if highlight is None or highlight.patient_id != patient_id:
            raise LookupError("highlight does not exist")
        pointer = highlight.source_evidence_pointer
        if pointer is None:
            raise LookupError("highlight has no claim-level source evidence")
        artifact = self.repository.get_source_artifact(
            pointer.source_id, patient_id, actor.clinic_id
        )
        if artifact is None or pointer.end > len(artifact.content):
            raise LookupError("claim-level source evidence does not exist")
        return SourceEvidence(
            artifact=artifact,
            pointer=pointer,
            excerpt=artifact.content[pointer.start : pointer.end],
        )

    def add_comment(
        self,
        actor: Actor,
        patient_id: UUID,
        content: str,
        assigned_to: Role | None = None,
        parent_id: UUID | None = None,
        target: AnnotationTarget | None = None,
    ) -> Comment:
        self._require_internal_collaborator(actor)
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        mentions = [
            role
            for role in (Role.STAFF, Role.CLINICIAN, Role.ADMIN)
            if re.search(rf"(?<!\w)@{role.value}\b", content, flags=re.IGNORECASE)
        ]
        if assigned_to is Role.PATIENT or assigned_to is Role.SYSTEM:
            raise AccessDeniedError("comments can only be assigned to an internal care role")
        if parent_id is not None:
            parent = self.repository.get_comment(parent_id)
            if (
                parent is None
                or parent.patient_id != patient_id
                or parent.clinic_id != actor.clinic_id
            ):
                raise LookupError("parent comment does not exist")
            if target is not None and target != parent.target:
                raise ValueError("a reply must use the parent comment target")
            target = parent.target
        if target is None:
            target = AnnotationTarget(
                resource_type=AnnotationTargetType.SECTION,
                resource_id="plan",
            )
        self._validate_annotation_target(patient_id, actor.clinic_id, target)
        saved = self.repository.add_comment(
            Comment(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                author_id=actor.id,
                author_role=actor.role,
                target=target,
                parent_id=parent_id,
                content=content,
                mentions=mentions,
                assigned_to=assigned_to,
            )
        )
        self.repository.add_resource_revision(
            ResourceRevision(
                resource="comment",
                resource_id=str(saved.id),
                version=saved.version,
                snapshot=saved.model_dump(mode="json"),
                changed_by=actor.id,
                operation="comment_reply" if parent_id is not None else "comment_create",
            )
        )
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="comment",
                resource_id=str(saved.id),
                version=saved.version,
                operation="comment_reply" if parent_id is not None else "comment_create",
                changed_by=actor.id,
            )
        )
        return saved

    def set_comment_resolved(
        self,
        actor: Actor,
        patient_id: UUID,
        comment_id: UUID,
        resolved: bool,
        expected_version: int,
    ) -> Comment:
        self._require_internal_collaborator(actor)
        comment = self.repository.get_comment(comment_id)
        if (
            comment is None
            or comment.patient_id != patient_id
            or comment.clinic_id != actor.clinic_id
        ):
            raise LookupError("comment does not exist")
        if expected_version != comment.version:
            raise ConcurrentEditError(
                f"expected version {expected_version}, current version is {comment.version}"
            )
        updated = comment.model_copy(
            update={
                "resolved": resolved,
                "resolved_at": utc_now() if resolved else None,
                "resolved_by": actor.id if resolved else None,
                "version": comment.version + 1,
            }
        )
        saved = self.repository.save_comment(updated)
        operation = "comment_resolve" if resolved else "comment_reopen"
        self.repository.add_resource_revision(
            ResourceRevision(
                resource="comment",
                resource_id=str(saved.id),
                version=saved.version,
                snapshot=saved.model_dump(mode="json"),
                changed_by=actor.id,
                operation=operation,
            )
        )
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="comment",
                resource_id=str(comment_id),
                version=saved.version,
                operation=operation,
                changed_by=actor.id,
            )
        )
        return saved

    def update_highlight(
        self,
        actor: Actor,
        patient_id: UUID,
        highlight_id: UUID,
        payload: HighlightUpdateRequest,
    ) -> Highlight:
        self._require_internal_collaborator(actor)
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        current = self.repository.get_highlight(highlight_id)
        if current is None or current.patient_id != patient_id:
            raise LookupError("highlight does not exist")
        if payload.expected_version != current.version:
            raise ConcurrentEditError(
                f"expected version {payload.expected_version}, current version is {current.version}"
            )
        if (
            payload.status is not None
            and payload.status in {ReviewStatus.CLINICIAN_CONFIRMED, ReviewStatus.REJECTED}
            and actor.role not in {Role.CLINICIAN, Role.ADMIN}
        ):
            raise AccessDeniedError("only clinicians or admins can confirm or reject a highlight")
        if payload.assigned_to in {Role.PATIENT, Role.SYSTEM}:
            raise AccessDeniedError("highlights can only be assigned to an internal care role")

        changes = {
            "version": current.version + 1,
            "updated_by": actor.id,
            "updated_at": utc_now(),
        }
        for field in ("status", "assigned_to", "action_status", "disposition_note"):
            if field in payload.model_fields_set:
                changes[field] = getattr(payload, field)
        next_action_status = changes.get("action_status", current.action_status)
        if next_action_status is ActionStatus.COMPLETED:
            changes["completed_by"] = actor.id
            changes["completed_at"] = utc_now()
        elif "action_status" in changes:
            changes["completed_by"] = None
            changes["completed_at"] = None

        candidate = current.model_copy(update=changes)
        source = self.repository.get_entry(candidate.provenance_pointer.entry_id)
        entries = self.repository.list_entries(patient_id, actor.clinic_id)
        if source is None or not entries:
            raise LookupError("highlight source does not exist")
        if (
            payload.status is not None
            and payload.status is not current.status
            and payload.status in {ReviewStatus.CLINICIAN_CONFIRMED, ReviewStatus.REJECTED}
        ):
            self.repository.add_importance_feedback(
                ImportanceFeedback(
                    patient_id=patient_id,
                    clinic_id=actor.clinic_id,
                    highlight_id=current.id,
                    entity_signature=self._entity_signature(current),
                    accepted=payload.status is ReviewStatus.CLINICIAN_CONFIRMED,
                    actor_id=actor.id,
                )
            )
        saved = self.repository.save_highlight(
            self.importance_scorer.score(
                candidate,
                source,
                max(entry.timestamp for entry in entries),
                self.repository.importance_adjustment(candidate),
            )
        )
        operation = "highlight_update"
        if payload.action_status is not None and payload.action_status is not current.action_status:
            operation = (
                "highlight_complete"
                if payload.action_status is ActionStatus.COMPLETED
                else "highlight_action_update"
            )
        elif (
            "assigned_to" in payload.model_fields_set
            and payload.assigned_to is not current.assigned_to
        ):
            operation = (
                "highlight_assign" if payload.assigned_to is not None else "highlight_unassign"
            )
        elif payload.status is not None and payload.status is not current.status:
            operation = "highlight_review"
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="highlight",
                resource_id=str(highlight_id),
                version=saved.version,
                operation=operation,
                changed_by=actor.id,
            )
        )
        self.repository.add_resource_revision(
            ResourceRevision(
                resource="highlight",
                resource_id=str(saved.id),
                version=saved.version,
                snapshot=saved.model_dump(mode="json"),
                changed_by=actor.id,
                operation=operation,
            )
        )
        return saved

    def create_manual_entry(
        self, actor: Actor, patient_id: UUID, title: str, content: str
    ) -> TimelineEntry:
        patient = self.repository.get_patient(patient_id, actor.clinic_id)
        if patient is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        if actor.role is Role.PATIENT and actor.id != patient_id:
            raise AccessDeniedError("patients can only write to their own record")
        entry_types = {
            Role.PATIENT: EntryType.PATIENT_NOTE,
            Role.STAFF: EntryType.STAFF_NOTE,
            Role.CLINICIAN: EntryType.CLINICIAN_NOTE,
        }
        entry_type = entry_types.get(actor.role)
        if entry_type is None:
            raise AccessDeniedError("this role cannot author a timeline note")
        entry_id = uuid4()
        source_id = f"manual-note-{entry_id}"
        artifact = self.repository.add_source_artifact(
            SourceArtifact(
                id=source_id,
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                kind=SourceArtifactKind.MANUAL_NOTE,
                source=ProvenanceSource.MANUAL_ENTRY,
                label=f"{actor.role.value.title()} manual note source",
                content=content,
            )
        )
        entry = TimelineEntry(
            id=entry_id,
            patient_id=patient_id,
            clinic_id=actor.clinic_id,
            author_role=actor.role,
            author_id=actor.id,
            type=entry_type,
            title=title,
            content=content,
            origin=EntryOrigin(
                source=ProvenanceSource.MANUAL_ENTRY,
                source_id=source_id,
                source_label=f"{actor.role.value.title()} manual note",
                source_pointer=SourceArtifactPointer(
                    source_id=artifact.id,
                    start=0,
                    end=len(artifact.content),
                ),
            ),
            review_status=(
                ReviewStatus.CLINICIAN_CONFIRMED if actor.role is Role.CLINICIAN else None
            ),
            internal=actor.role is not Role.PATIENT,
        )
        saved = self.add_entry(actor, entry)
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="timeline_entry",
                resource_id=str(saved.id),
                version=1,
                operation="entry_create",
                changed_by=actor.id,
            )
        )
        return saved

    def ingest_ai_summary(
        self, actor: Actor, patient_id: UUID, payload: AIIngestRequest
    ) -> AIIngestResponse:
        self._require_internal_collaborator(actor)
        patient = self.repository.get_patient(patient_id, actor.clinic_id)
        if patient is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        summary, redaction = self.ai_gateway.summarize(
            payload.raw_text,
            [patient.display_name, patient.medical_record_number],
            str(actor.id),
        )
        entry_id = uuid4()
        source_id = f"ai-ingest-source-{entry_id}"
        source_kind = (
            SourceArtifactKind.TRANSCRIPT
            if payload.source in {ProvenanceSource.DOCTOR_CONSULT, ProvenanceSource.NURSE_CONSULT}
            else SourceArtifactKind.MESSAGE_THREAD
        )
        artifact = self.repository.add_source_artifact(
            SourceArtifact(
                id=source_id,
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                kind=source_kind,
                source=payload.source,
                label=f"Original {payload.source.value.replace('_', ' ')}",
                content=payload.raw_text,
            )
        )
        entry_type = {
            ProvenanceSource.DOCTOR_CONSULT: EntryType.AI_DOCTOR_CONSULT_SUMMARY,
            ProvenanceSource.NURSE_CONSULT: EntryType.AI_NURSE_CONSULT_SUMMARY,
            ProvenanceSource.PATIENT_SESSION: EntryType.AI_PATIENT_SESSION_SUMMARY,
        }.get(payload.source, EntryType.AI_PATIENT_SESSION_SUMMARY)
        entry = self.repository.add_entry(
            TimelineEntry(
                id=entry_id,
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                author_role=Role.SYSTEM,
                author_id=None,
                type=entry_type,
                title=payload.title,
                content=summary,
                origin=EntryOrigin(
                    source=payload.source,
                    source_id=source_id,
                    source_label=artifact.label,
                    source_pointer=SourceArtifactPointer(
                        source_id=source_id, start=0, end=len(payload.raw_text)
                    ),
                ),
                review_status=ReviewStatus.AI_SUGGESTED,
                internal=True,
            )
        )
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="timeline_entry",
                resource_id=str(entry.id),
                version=1,
                operation="ai_summary_ingest",
                changed_by=actor.id,
            )
        )
        return AIIngestResponse(
            entry=entry,
            redaction_counts=redaction.counts,
            model=self.ai_gateway.model,
        )

    def archive_preview(self, actor: Actor, patient_id: UUID) -> ArchiveBatch:
        if actor.role is not Role.ADMIN:
            raise AccessDeniedError("only admins can run archive maintenance")
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        protected_ids = {
            item.provenance_pointer.entry_id for item in self.repository.list_highlights(patient_id)
        }
        protected_ids.update(
            entry_id
            for conflict in self.repository.list_conflicts(patient_id, actor.clinic_id)
            if conflict.status is not ConflictStatus.RESOLVED
            for entry_id in conflict.entry_ids
        )
        candidates: list[ArchiveCandidate] = []
        now = datetime.now(UTC)
        safety_pattern = re.compile(
            r"\b(?:allerg|\d+\s*(?:mg|mcg|g|ml)|medication)\b", re.IGNORECASE
        )
        for entry in self.repository.list_entries(patient_id, actor.clinic_id):
            age_days = max(0, (now - entry.timestamp).days)
            if (
                age_days >= 180
                and entry.id not in protected_ids
                and entry.author_role in {Role.PATIENT, Role.STAFF}
                and not safety_pattern.search(entry.content)
            ):
                candidates.append(
                    ArchiveCandidate(
                        entry_id=entry.id,
                        title=entry.title,
                        age_days=age_days,
                        reason=(
                            "Older than 180 days, low-signal, not referenced by a Highlight "
                            "or unresolved conflict; original remains reversibly encrypted."
                        ),
                    )
                )
        return ArchiveBatch(
            patient_id=patient_id,
            candidates=candidates,
            policy="cold-v1: age>=180d; preserve safety facts and referenced records",
        )

    def apply_archive(self, actor: Actor, patient_id: UUID, entry_ids: list[UUID]) -> int:
        allowed = {item.entry_id for item in self.archive_preview(actor, patient_id).candidates}
        requested = set(entry_ids)
        if not requested <= allowed:
            raise ValueError("one or more entries do not satisfy the current archive policy")
        archived = self.repository.archive_entries(entry_ids)
        for entry_id in entry_ids:
            self.repository.add_audit_event(
                AuditEvent(
                    patient_id=patient_id,
                    clinic_id=actor.clinic_id,
                    resource="timeline_entry",
                    resource_id=str(entry_id),
                    version=1,
                    operation="archive_to_encrypted_cold_storage",
                    changed_by=actor.id,
                )
            )
        return archived

    def create_highlight(
        self,
        actor: Actor,
        patient_id: UUID,
        payload: HighlightCreateRequest,
    ) -> Highlight:
        if actor.role is not Role.CLINICIAN:
            raise AccessDeniedError("only clinicians can create manual highlights")
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        source = self.repository.get_entry(payload.entry_id)
        if source is None or source.patient_id != patient_id or source.clinic_id != actor.clinic_id:
            raise LookupError("source entry does not exist")
        if payload.end > len(source.content):
            raise ValueError("highlight span is outside the source entry")
        text = source.content[payload.start : payload.end]
        if not text.strip():
            raise ValueError("highlight span must contain visible text")
        if payload.assigned_to in {Role.PATIENT, Role.SYSTEM}:
            raise AccessDeniedError("highlights can only be assigned to an internal care role")
        entries = self.repository.list_entries(patient_id, actor.clinic_id)
        highlight = Highlight(
            patient_id=patient_id,
            text=text,
            risk_reason=payload.risk_reason,
            suggested_action=payload.suggested_action,
            patient_instruction=payload.patient_instruction,
            risk_level=payload.risk_level,
            clinical_entities=payload.clinical_entities,
            priority=0,
            status=payload.status,
            assigned_to=payload.assigned_to,
            provenance_pointer=ProvenancePointer(
                entry_id=source.id,
                start=payload.start,
                end=payload.end,
            ),
            source_evidence_pointer=self._claim_source_pointer(source, text, payload),
            updated_by=actor.id,
            updated_at=utc_now(),
        )
        highlight = self.repository.add_highlight(
            self.importance_scorer.score(
                highlight,
                source,
                max(entry.timestamp for entry in entries),
                self.repository.importance_adjustment(highlight),
            )
        )
        self.repository.add_resource_revision(
            ResourceRevision(
                resource="highlight",
                resource_id=str(highlight.id),
                version=highlight.version,
                snapshot=highlight.model_dump(mode="json"),
                changed_by=actor.id,
                operation="highlight_create",
            )
        )
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="highlight",
                resource_id=str(highlight.id),
                version=highlight.version,
                operation="highlight_create",
                changed_by=actor.id,
            )
        )
        return highlight

    def record_highlight_impression(
        self, actor: Actor, patient_id: UUID, highlight_id: UUID, expected_version: int
    ) -> None:
        self._require_internal_collaborator(actor)
        highlight = self.repository.get_highlight(highlight_id)
        if highlight is None or highlight.patient_id != patient_id:
            raise LookupError("highlight does not exist")
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        if highlight.version != expected_version:
            raise ConcurrentEditError("highlight changed before the impression was recorded")
        if not self.repository.has_highlight_impression(highlight_id, actor.id):
            self.repository.add_importance_feedback(
                ImportanceFeedback(
                    patient_id=patient_id,
                    clinic_id=actor.clinic_id,
                    highlight_id=highlight.id,
                    entity_signature=self._entity_signature(highlight),
                    actor_id=actor.id,
                )
            )

    @staticmethod
    def _entity_signature(highlight: Highlight) -> str:
        return "|".join(sorted({item.lower() for item in highlight.clinical_entities}))

    def _claim_source_pointer(
        self,
        source: TimelineEntry,
        highlight_text: str,
        payload: HighlightCreateRequest,
    ) -> SourceArtifactPointer | None:
        entry_pointer = source.origin.source_pointer
        if entry_pointer is None:
            return None
        artifact = self.repository.get_source_artifact(
            entry_pointer.source_id, source.patient_id, source.clinic_id
        )
        if artifact is None:
            return None
        if payload.source_evidence_start is not None and payload.source_evidence_end is not None:
            if payload.source_evidence_end > len(artifact.content):
                raise ValueError("claim source span is outside the original artifact")
            return SourceArtifactPointer(
                source_id=artifact.id,
                start=payload.source_evidence_start,
                end=payload.source_evidence_end,
            )
        exact_start = artifact.content.find(highlight_text)
        if exact_start >= 0:
            return SourceArtifactPointer(
                source_id=artifact.id,
                start=exact_start,
                end=exact_start + len(highlight_text),
            )
        if entry_pointer.start == 0 and entry_pointer.end == len(artifact.content):
            raise ValueError(
                "claim-level original source offsets are required when summary text is not verbatim"
            )
        return entry_pointer

    def list_revisions(self, actor: Actor, patient_id: UUID, section: str) -> list[SectionRevision]:
        self._require_internal_collaborator(actor)
        current = self.repository.get_section(patient_id, section)
        if current is None or current.clinic_id != actor.clinic_id:
            raise LookupError("section does not exist")
        return self.repository.list_revisions(patient_id, section)

    def compare_section_versions(
        self,
        actor: Actor,
        patient_id: UUID,
        section: str,
        from_version: int,
        to_version: int,
    ) -> SectionDiff:
        revisions = self.list_revisions(actor, patient_id, section)
        by_version = {revision.version: revision for revision in revisions}
        before = by_version.get(from_version)
        after = by_version.get(to_version)
        if before is None or after is None:
            raise LookupError("one or both section revisions do not exist")
        return SectionDiff(
            section=section,
            from_version=from_version,
            to_version=to_version,
            diff="\n".join(
                unified_diff(
                    before.content.splitlines(),
                    after.content.splitlines(),
                    fromfile=f"{section}@{from_version}",
                    tofile=f"{section}@{to_version}",
                    lineterm="",
                )
            ),
        )

    def list_resource_revisions(
        self, actor: Actor, patient_id: UUID, resource: str, resource_id: UUID
    ) -> list[ResourceRevision]:
        self._require_internal_collaborator(actor)
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        if resource == "highlight":
            item = self.repository.get_highlight(resource_id)
        elif resource == "comment":
            item = self.repository.get_comment(resource_id)
        elif resource == "conflict":
            item = self.repository.get_conflict(resource_id)
        else:
            raise LookupError("unsupported revision resource")
        if item is None or item.patient_id != patient_id:
            raise LookupError(f"{resource} does not exist")
        if resource in {"comment", "conflict"} and item.clinic_id != actor.clinic_id:
            raise LookupError(f"{resource} does not exist")
        return self.repository.list_resource_revisions(resource, str(resource_id))

    def update_conflict(
        self,
        actor: Actor,
        patient_id: UUID,
        conflict_id: UUID,
        payload: ConflictUpdateRequest,
    ) -> Conflict:
        if actor.role not in {Role.CLINICIAN, Role.ADMIN}:
            raise AccessDeniedError("only clinicians or admins can adjudicate conflicts")
        current = self.repository.get_conflict(conflict_id)
        if (
            current is None
            or current.patient_id != patient_id
            or current.clinic_id != actor.clinic_id
        ):
            raise LookupError("conflict does not exist")
        if payload.expected_version != current.version:
            raise ConcurrentEditError(
                f"expected version {payload.expected_version}, current version is {current.version}"
            )
        saved = self.repository.save_conflict(
            current.model_copy(
                update={
                    "status": payload.status,
                    "resolution_note": payload.resolution_note,
                    "version": current.version + 1,
                    "updated_by": actor.id,
                    "updated_at": utc_now(),
                }
            )
        )
        operation = (
            "conflict_resolve" if payload.status is ConflictStatus.RESOLVED else "conflict_confirm"
        )
        self.repository.add_resource_revision(
            ResourceRevision(
                resource="conflict",
                resource_id=str(saved.id),
                version=saved.version,
                snapshot=saved.model_dump(mode="json"),
                changed_by=actor.id,
                operation=operation,
            )
        )
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="conflict",
                resource_id=str(saved.id),
                version=saved.version,
                operation=operation,
                changed_by=actor.id,
            )
        )
        return saved

    def _validate_annotation_target(
        self, patient_id: UUID, clinic_id: UUID, target: AnnotationTarget
    ) -> None:
        if target.resource_type is AnnotationTargetType.ENTRY:
            try:
                entry_id = UUID(target.resource_id)
            except ValueError as exc:
                raise ValueError("entry comment target must contain a UUID") from exc
            entry = self.repository.get_entry(entry_id)
            if entry is None or entry.patient_id != patient_id or entry.clinic_id != clinic_id:
                raise LookupError("comment target entry does not exist")
            content = entry.content
        else:
            section = self.repository.get_section(patient_id, target.resource_id)
            if section is None or section.clinic_id != clinic_id:
                raise LookupError("comment target section does not exist")
            content = section.content
        if target.end is not None and target.end > len(content):
            raise ValueError("comment target span is outside the target content")

    @staticmethod
    def _require_internal_collaborator(actor: Actor) -> None:
        if actor.role not in {Role.STAFF, Role.CLINICIAN, Role.ADMIN}:
            raise AccessDeniedError("internal collaboration requires a care-team role")

    @staticmethod
    def _reject_system_writes(actor: Actor) -> None:
        if actor.role is Role.SYSTEM:
            raise AccessDeniedError("system actors cannot write; a clinician must commit changes")

    def add_entry(self, actor: Actor, entry: TimelineEntry) -> TimelineEntry:
        self._reject_system_writes(actor)
        if actor.clinic_id != entry.clinic_id:
            raise AccessDeniedError("cross-clinic writes are forbidden")
        ownership = {
            EntryType.STAFF_NOTE: Role.STAFF,
            EntryType.CLINICIAN_NOTE: Role.CLINICIAN,
            EntryType.PATIENT_NOTE: Role.PATIENT,
        }
        required_role = ownership.get(entry.type)
        if required_role is not None and actor.role is not required_role:
            raise AccessDeniedError(f"{actor.role} cannot write {entry.type}")
        if entry.author_role is not actor.role or entry.author_id != actor.id:
            raise AccessDeniedError("entry authorship must match the authenticated actor")
        return self.repository.add_entry(entry)

    def update_section(
        self,
        actor: Actor,
        patient_id: UUID,
        section: str,
        content: str,
        expected_version: int,
    ) -> SectionState:
        self._reject_system_writes(actor)
        # Sections are internal care-team documents: patients can neither read nor create them.
        self._require_internal_collaborator(actor)
        current = self.repository.get_section(patient_id, section)
        owner = current.owner_role if current else actor.role
        clinic_id = current.clinic_id if current else actor.clinic_id
        if clinic_id != actor.clinic_id:
            raise AccessDeniedError("cross-clinic writes are forbidden")
        if actor.role is not owner:
            raise AccessDeniedError(f"{actor.role} cannot modify a {owner} section")
        current_version = current.version if current else 0
        if expected_version != current_version:
            raise ConcurrentEditError(
                f"expected version {expected_version}, current version is {current_version}"
            )
        next_version = current_version + 1
        state = SectionState(
            patient_id=patient_id,
            clinic_id=actor.clinic_id,
            section=section,
            owner_role=owner,
            content=content,
            version=next_version,
        )
        revision = SectionRevision(
            section=section,
            version=next_version,
            content=content,
            changed_by=actor.id,
            diff="\n".join(
                unified_diff(
                    (current.content if current else "").splitlines(),
                    content.splitlines(),
                    fromfile=f"{section}@{current_version}",
                    tofile=f"{section}@{next_version}",
                    lineterm="",
                )
            ),
        )
        saved = self.repository.save_section(state, revision)
        self.repository.add_audit_event(
            AuditEvent(
                patient_id=patient_id,
                clinic_id=actor.clinic_id,
                resource="section",
                resource_id=section,
                version=next_version,
                operation="edit",
                changed_by=actor.id,
            )
        )
        return saved

    def revert_section(
        self, actor: Actor, patient_id: UUID, section: str, target_version: int
    ) -> SectionState:
        current = self.repository.get_section(patient_id, section)
        if current is None:
            raise LookupError("section does not exist")
        target = next(
            (
                revision
                for revision in self.repository.list_revisions(patient_id, section)
                if revision.version == target_version
            ),
            None,
        )
        if target is None:
            raise LookupError("target revision does not exist")
        state = self.update_section(
            actor, patient_id, section, target.content, expected_version=current.version
        )
        revisions = self.repository.revisions[(patient_id, section)]
        revisions[-1] = revisions[-1].model_copy(update={"operation": "revert"})
        self.repository.replace_last_audit_operation("revert")
        return state

    def list_audit_events(self, actor: Actor, patient_id: UUID) -> list[AuditEvent]:
        self._require_internal_collaborator(actor)
        if self.repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        return self.repository.list_audit_events(patient_id, actor.clinic_id)
