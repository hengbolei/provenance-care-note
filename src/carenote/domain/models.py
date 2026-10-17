from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class Role(StrEnum):
    PATIENT = "patient"
    STAFF = "staff"
    CLINICIAN = "clinician"
    ADMIN = "admin"
    SYSTEM = "system"


class EntryType(StrEnum):
    PATIENT_NOTE = "patient_note"
    STAFF_NOTE = "staff_note"
    CLINICIAN_NOTE = "clinician_note"
    AI_DOCTOR_CONSULT_SUMMARY = "ai_doctor_consult_summary"
    AI_NURSE_CONSULT_SUMMARY = "ai_nurse_consult_summary"
    AI_PATIENT_SESSION_SUMMARY = "ai_patient_session_summary"
    SYSTEM_EVENT = "system_event"


class ReviewStatus(StrEnum):
    AI_SUGGESTED = "ai_suggested"
    CLINICIAN_CONFIRMED = "clinician_confirmed"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class ActionStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class RiskLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class ProvenanceSource(StrEnum):
    MANUAL_ENTRY = "manual_entry"
    PATIENT_SESSION = "patient_session"
    DOCTOR_CONSULT = "doctor_consult"
    NURSE_CONSULT = "nurse_consult"


class SourceArtifactKind(StrEnum):
    MANUAL_NOTE = "manual_note"
    MESSAGE_THREAD = "message_thread"
    TRANSCRIPT = "transcript"


class ConflictCategory(StrEnum):
    MEDICATION = "medication"
    DOSE = "dose"
    ALLERGY = "allergy"


class ConflictStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    CLINICIAN_PRECEDENCE = "clinician_precedence"
    CLINICIAN_CONFIRMED = "clinician_confirmed"
    RESOLVED = "resolved"


class AnnotationTargetType(StrEnum):
    ENTRY = "entry"
    SECTION = "section"


class Actor(BaseModel):
    id: UUID
    role: Role
    clinic_id: UUID


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=256)


class SessionResponse(BaseModel):
    actor: Actor
    expires_at: datetime
    token: str | None = None


class Patient(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    clinic_id: UUID
    display_name: str = Field(min_length=1, max_length=120)
    date_of_birth: date
    medical_record_number: str = Field(min_length=1, max_length=40)
    pronouns: str | None = Field(default=None, max_length=40)
    synthetic: Literal[True] = True


class ProvenancePointer(BaseModel):
    entry_id: UUID
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_span(self) -> ProvenancePointer:
        if self.end <= self.start:
            raise ValueError("provenance end must be greater than start")
        return self


# Backwards-compatible domain name used by the initial test scaffold.
SourceSpan = ProvenancePointer


class SourceArtifactPointer(BaseModel):
    source_id: str = Field(min_length=1, max_length=120)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_span(self) -> SourceArtifactPointer:
        if self.end <= self.start:
            raise ValueError("source artifact end must be greater than start")
        return self


class SourceArtifact(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    patient_id: UUID
    clinic_id: UUID
    kind: SourceArtifactKind
    source: ProvenanceSource
    label: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=utc_now)
    synthetic: Literal[True] = True


class SourceEvidence(BaseModel):
    artifact: SourceArtifact
    pointer: SourceArtifactPointer
    excerpt: str


class EntryOrigin(BaseModel):
    source: ProvenanceSource
    source_id: str = Field(min_length=1, max_length=120)
    source_label: str = Field(min_length=1, max_length=160)
    source_pointer: SourceArtifactPointer | None = None


class TimelineEntry(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    patient_id: UUID
    clinic_id: UUID
    author_role: Role
    author_id: UUID | None
    timestamp: datetime = Field(default_factory=utc_now)
    type: EntryType
    title: str = Field(min_length=1, max_length=160)
    content: str
    origin: EntryOrigin
    review_status: ReviewStatus | None = None
    internal: bool = True


class Highlight(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    patient_id: UUID
    text: str
    risk_reason: str
    suggested_action: str = Field(min_length=1, max_length=240)
    patient_instruction: str | None = Field(default=None, max_length=500)
    risk_level: RiskLevel = RiskLevel.MODERATE
    clinical_entities: list[str] = Field(default_factory=list)
    priority: int = Field(ge=0, le=100)
    priority_factors: list[PriorityFactor] = Field(default_factory=list)
    status: ReviewStatus
    provenance_pointer: ProvenancePointer
    source_evidence_pointer: SourceArtifactPointer | None = None
    assigned_to: Role | None = None
    action_status: ActionStatus = ActionStatus.OPEN
    disposition_note: str | None = Field(default=None, max_length=1000)
    version: int = Field(default=1, ge=1)
    updated_by: UUID | None = None
    updated_at: datetime | None = None
    completed_by: UUID | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PatientAction(BaseModel):
    title: str
    instruction: str
    action_status: ActionStatus
    updated_at: datetime


class PriorityFactor(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    points: int
    explanation: str = Field(min_length=1, max_length=240)


class AnnotationTarget(BaseModel):
    resource_type: AnnotationTargetType
    resource_id: str = Field(min_length=1, max_length=160)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_span(self) -> AnnotationTarget:
        if (self.start is None) != (self.end is None):
            raise ValueError("annotation target start and end must be supplied together")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("annotation target end must be greater than start")
        return self


class Comment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    patient_id: UUID
    clinic_id: UUID
    author_id: UUID
    author_role: Role
    target: AnnotationTarget
    parent_id: UUID | None = None
    content: str = Field(min_length=1, max_length=1000)
    mentions: list[Role] = Field(default_factory=list)
    assigned_to: Role | None = None
    resolved: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    version: int = Field(default=1, ge=1)


class PatientDetailResponse(BaseModel):
    patient: Patient
    highlights: list[Highlight]
    entries: list[TimelineEntry]
    patient_actions: list[PatientAction] = Field(default_factory=list)
    sections: list[SectionState] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


class Conflict(BaseModel):
    id: UUID
    patient_id: UUID
    clinic_id: UUID
    category: ConflictCategory
    entity: str
    summary: str
    status: ConflictStatus
    entry_ids: list[UUID] = Field(min_length=2)
    preferred_entry_id: UUID | None = None
    rationale: str
    resolution_note: str | None = Field(default=None, max_length=1000)
    version: int = Field(default=1, ge=1)
    updated_by: UUID | None = None
    updated_at: datetime | None = None


class SectionRevision(BaseModel):
    section: str
    version: int = Field(ge=1)
    content: str
    changed_by: UUID
    changed_at: datetime = Field(default_factory=utc_now)
    operation: str = "edit"
    diff: str | None = None


class SectionState(BaseModel):
    patient_id: UUID
    clinic_id: UUID
    section: str
    owner_role: Role
    content: str
    version: int = Field(ge=1)


class AuditEvent(BaseModel):
    patient_id: UUID
    clinic_id: UUID
    resource: str
    resource_id: str
    version: int = Field(ge=1)
    operation: str
    changed_by: UUID
    changed_at: datetime = Field(default_factory=utc_now)
    previous_hash: str = "0" * 64
    event_hash: str = ""


class ResourceRevision(BaseModel):
    resource: Literal["highlight", "comment", "conflict"]
    resource_id: str
    version: int = Field(ge=1)
    snapshot: dict[str, Any]
    changed_by: UUID | None = None
    changed_at: datetime = Field(default_factory=utc_now)
    operation: str


class SectionDiff(BaseModel):
    section: str
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    diff: str


class CommentCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    assigned_to: Role | None = None
    parent_id: UUID | None = None
    target: AnnotationTarget | None = None


class CommentResolveRequest(BaseModel):
    resolved: bool
    expected_version: int = Field(ge=1)


class TimelineEntryCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=10000)


class AIIngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    raw_text: str = Field(min_length=1, max_length=50000)
    source: ProvenanceSource = ProvenanceSource.DOCTOR_CONSULT


class AIIngestResponse(BaseModel):
    entry: TimelineEntry
    redaction_counts: dict[str, int] = Field(default_factory=dict)
    model: str
    externally_stored: Literal[False] = False


class TranscriptSegment(BaseModel):
    speaker: str = "unknown"
    text: str
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)


class TranscriptionResponse(BaseModel):
    text: str
    segments: list[TranscriptSegment] = Field(default_factory=list)
    model: str
    diarization_available: bool = False
    confidence_available: bool = False


class ImportanceFeedback(BaseModel):
    patient_id: UUID
    clinic_id: UUID
    highlight_id: UUID
    entity_signature: str
    exposed: bool = True
    accepted: bool | None = None
    actor_id: UUID
    created_at: datetime = Field(default_factory=utc_now)


class HighlightImpressionRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ArchiveCandidate(BaseModel):
    entry_id: UUID
    title: str
    age_days: int = Field(ge=0)
    reason: str


class ArchiveBatch(BaseModel):
    patient_id: UUID
    candidates: list[ArchiveCandidate]
    policy: str


class ArchiveApplyRequest(BaseModel):
    entry_ids: list[UUID] = Field(min_length=1, max_length=500)


class ArchiveApplyResponse(BaseModel):
    archived: int = Field(ge=0)
    reversible: Literal[True] = True


class HighlightCreateRequest(BaseModel):
    entry_id: UUID
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    risk_reason: str = Field(min_length=1, max_length=500)
    suggested_action: str = Field(min_length=1, max_length=240)
    patient_instruction: str | None = Field(default=None, max_length=500)
    risk_level: RiskLevel = RiskLevel.MODERATE
    clinical_entities: list[str] = Field(default_factory=list, max_length=20)
    priority: int | None = Field(default=None, ge=0, le=100)
    status: ReviewStatus = ReviewStatus.NEEDS_REVIEW
    assigned_to: Role | None = None
    source_evidence_start: int | None = Field(default=None, ge=0)
    source_evidence_end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_span(self) -> HighlightCreateRequest:
        if self.end <= self.start:
            raise ValueError("highlight end must be greater than start")
        if (self.source_evidence_start is None) != (self.source_evidence_end is None):
            raise ValueError("source evidence start and end must be supplied together")
        if (
            self.source_evidence_start is not None
            and self.source_evidence_end is not None
            and self.source_evidence_end <= self.source_evidence_start
        ):
            raise ValueError("source evidence end must be greater than start")
        return self


class HighlightUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    status: ReviewStatus | None = None
    assigned_to: Role | None = None
    action_status: ActionStatus | None = None
    disposition_note: str | None = Field(default=None, max_length=1000)


class ConflictUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    status: ConflictStatus
    resolution_note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_resolution_note(self) -> ConflictUpdateRequest:
        if self.status is ConflictStatus.RESOLVED and not (self.resolution_note or "").strip():
            raise ValueError("resolved conflicts require a resolution note")
        return self


class SectionUpdateRequest(BaseModel):
    content: str = Field(max_length=10000)
    expected_version: int = Field(ge=0)


class SectionRevertRequest(BaseModel):
    target_version: int = Field(ge=1)


class HealthResponse(BaseModel):
    status: str
    service: str
