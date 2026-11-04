"""The patient record: view, timeline, highlights, comments, sections, audit and archive."""

from uuid import UUID

from fastapi import APIRouter, Request, WebSocket, WebSocketException, status

from carenote.api.container import container
from carenote.api.dependencies import SESSION_COOKIE, ActorDependency
from carenote.api.errors import EDIT_ERRORS, MODEL_ERRORS, READ_ERRORS, maps_errors
from carenote.core.identity import AuthenticationError, session_service
from carenote.domain.models import (
    AIIngestRequest,
    AIIngestResponse,
    ArchiveApplyRequest,
    ArchiveApplyResponse,
    ArchiveBatch,
    AuditEvent,
    Comment,
    CommentCreateRequest,
    CommentResolveRequest,
    Conflict,
    ConflictUpdateRequest,
    Highlight,
    HighlightCreateRequest,
    HighlightImpressionRequest,
    HighlightUpdateRequest,
    PatientDetailResponse,
    ResourceRevision,
    SectionDiff,
    SectionRevertRequest,
    SectionRevision,
    SectionState,
    SectionUpdateRequest,
    SourceEvidence,
    TimelineEntry,
    TimelineEntryCreateRequest,
    TranscriptionResponse,
)
from carenote.services.care_notes import (
    ConcurrentEditError,
    PatientNotFoundError,
)
from carenote.services.realtime import realtime_hub

router = APIRouter(prefix="/patients/{patient_id}", tags=["patients"])
# The WebSocket keeps its own path, which the browser client connects to.
realtime_router = APIRouter(tags=["patients"])

AUDIO_TYPES = frozenset({"audio/webm", "audio/wav", "audio/mpeg", "audio/mp4", "audio/ogg"})
MAX_AUDIO_BYTES = 25 * 1024 * 1024
VIEW_ERRORS = (PatientNotFoundError, PermissionError)


# Realtime presence and refresh


@realtime_router.websocket("/ws/patients/{patient_id}")
async def patient_realtime(socket: WebSocket, patient_id: UUID) -> None:
    token = socket.cookies.get(SESSION_COOKIE)
    if not token:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    try:
        actor = session_service.verify(token).actor
        container.service.get_patient_view(actor, patient_id)
    except (AuthenticationError, PatientNotFoundError, PermissionError) as exc:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION) from exc
    await realtime_hub.connect(patient_id, socket, actor)
    await realtime_hub.serve(
        patient_id,
        socket,
        lambda: len(container.repository.list_audit_events(patient_id, actor.clinic_id)),
    )


# Record and evidence


@router.get("", response_model=PatientDetailResponse)
@maps_errors(*VIEW_ERRORS)
def patient_view(patient_id: UUID, actor: ActorDependency) -> PatientDetailResponse:
    return container.service.get_patient_view(actor, patient_id)


@router.get("/entries/{entry_id}/source", response_model=SourceEvidence)
@maps_errors(*READ_ERRORS)
def entry_source(patient_id: UUID, entry_id: UUID, actor: ActorDependency) -> SourceEvidence:
    return container.service.get_source_evidence(actor, patient_id, entry_id)


@router.get("/highlights/{highlight_id}/source", response_model=SourceEvidence)
@maps_errors(*READ_ERRORS)
def highlight_source(
    patient_id: UUID, highlight_id: UUID, actor: ActorDependency
) -> SourceEvidence:
    return container.service.get_highlight_source_evidence(actor, patient_id, highlight_id)


@router.post("/entries", response_model=TimelineEntry, status_code=201)
@maps_errors(*VIEW_ERRORS)
def create_timeline_entry(
    patient_id: UUID, payload: TimelineEntryCreateRequest, actor: ActorDependency
) -> TimelineEntry:
    return container.service.create_manual_entry(actor, patient_id, payload.title, payload.content)


# AI intake


@router.post("/ai-ingest", response_model=AIIngestResponse, status_code=201)
@maps_errors(*MODEL_ERRORS, *VIEW_ERRORS, ValueError)
def ai_ingest(
    patient_id: UUID, payload: AIIngestRequest, actor: ActorDependency
) -> AIIngestResponse:
    return container.service.ingest_ai_summary(actor, patient_id, payload)


@router.post("/transcriptions", response_model=TranscriptionResponse)
@maps_errors(*MODEL_ERRORS, *VIEW_ERRORS, ValueError)
async def transcribe_audio(
    patient_id: UUID, request: Request, actor: ActorDependency
) -> TranscriptionResponse:
    # Check access before reading the upload.
    container.service.require_care_team_access(actor, patient_id)
    content_type = request.headers.get("content-type", "audio/webm")
    if content_type.split(";", 1)[0] not in AUDIO_TYPES:
        raise ValueError("unsupported audio content type")
    audio = await request.body()
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("audio must contain 1 byte to 25 MiB")
    return container.service.transcribe(actor, patient_id, audio, content_type)


# Highlights and conflicts


@router.post("/highlights", response_model=Highlight, status_code=201)
@maps_errors(*READ_ERRORS, ValueError)
def create_highlight(
    patient_id: UUID, payload: HighlightCreateRequest, actor: ActorDependency
) -> Highlight:
    return container.service.create_highlight(actor, patient_id, payload)


@router.post("/highlights/{highlight_id}/impressions", status_code=204)
@maps_errors(*EDIT_ERRORS)
def record_highlight_impression(
    patient_id: UUID,
    highlight_id: UUID,
    payload: HighlightImpressionRequest,
    actor: ActorDependency,
) -> None:
    container.service.record_highlight_impression(
        actor, patient_id, highlight_id, payload.expected_version
    )


@router.patch("/highlights/{highlight_id}", response_model=Highlight)
@maps_errors(*EDIT_ERRORS)
def update_highlight(
    patient_id: UUID,
    highlight_id: UUID,
    payload: HighlightUpdateRequest,
    actor: ActorDependency,
) -> Highlight:
    return container.service.update_highlight(actor, patient_id, highlight_id, payload)


@router.get("/highlights/{highlight_id}/revisions", response_model=list[ResourceRevision])
@maps_errors(*READ_ERRORS)
def highlight_revisions(
    patient_id: UUID, highlight_id: UUID, actor: ActorDependency
) -> list[ResourceRevision]:
    return container.service.list_resource_revisions(actor, patient_id, "highlight", highlight_id)


@router.patch("/conflicts/{conflict_id}", response_model=Conflict)
@maps_errors(LookupError, PermissionError, ConcurrentEditError)
def update_conflict(
    patient_id: UUID,
    conflict_id: UUID,
    payload: ConflictUpdateRequest,
    actor: ActorDependency,
) -> Conflict:
    return container.service.update_conflict(actor, patient_id, conflict_id, payload)


@router.get("/conflicts/{conflict_id}/revisions", response_model=list[ResourceRevision])
@maps_errors(*READ_ERRORS)
def conflict_revisions(
    patient_id: UUID, conflict_id: UUID, actor: ActorDependency
) -> list[ResourceRevision]:
    return container.service.list_resource_revisions(actor, patient_id, "conflict", conflict_id)


# Comments


@router.post("/comments", response_model=Comment, status_code=201)
@maps_errors(*READ_ERRORS, ValueError)
def create_comment(
    patient_id: UUID, payload: CommentCreateRequest, actor: ActorDependency
) -> Comment:
    return container.service.add_comment(
        actor,
        patient_id,
        payload.content,
        payload.assigned_to,
        payload.parent_id,
        payload.target,
    )


@router.patch("/comments/{comment_id}", response_model=Comment)
@maps_errors(LookupError, PermissionError, ConcurrentEditError)
def resolve_comment(
    patient_id: UUID,
    comment_id: UUID,
    payload: CommentResolveRequest,
    actor: ActorDependency,
) -> Comment:
    return container.service.set_comment_resolved(
        actor, patient_id, comment_id, payload.resolved, payload.expected_version
    )


@router.get("/comments/{comment_id}/revisions", response_model=list[ResourceRevision])
@maps_errors(*READ_ERRORS)
def comment_revisions(
    patient_id: UUID, comment_id: UUID, actor: ActorDependency
) -> list[ResourceRevision]:
    return container.service.list_resource_revisions(actor, patient_id, "comment", comment_id)


# Sections (care plan, agent review)


@router.put("/sections/{section}", response_model=SectionState)
@maps_errors(*VIEW_ERRORS, ConcurrentEditError)
def update_section(
    patient_id: UUID, section: str, payload: SectionUpdateRequest, actor: ActorDependency
) -> SectionState:
    container.service.get_patient_view(actor, patient_id)
    return container.service.update_section(
        actor, patient_id, section, payload.content, payload.expected_version
    )


@router.get("/sections/{section}/revisions", response_model=list[SectionRevision])
@maps_errors(LookupError, PermissionError)
def section_revisions(
    patient_id: UUID, section: str, actor: ActorDependency
) -> list[SectionRevision]:
    return container.service.list_revisions(actor, patient_id, section)


@router.get("/sections/{section}/compare", response_model=SectionDiff)
@maps_errors(LookupError, PermissionError)
def compare_section_revisions(
    patient_id: UUID,
    section: str,
    from_version: int,
    to_version: int,
    actor: ActorDependency,
) -> SectionDiff:
    return container.service.compare_section_versions(
        actor, patient_id, section, from_version, to_version
    )


@router.post("/sections/{section}/revert", response_model=SectionState)
@maps_errors(LookupError, PermissionError, ConcurrentEditError)
def revert_section(
    patient_id: UUID, section: str, payload: SectionRevertRequest, actor: ActorDependency
) -> SectionState:
    container.service.get_patient_view(actor, patient_id)
    return container.service.revert_section(actor, patient_id, section, payload.target_version)


# Audit and archive


@router.get("/audit", response_model=list[AuditEvent])
@maps_errors(*VIEW_ERRORS)
def patient_audit(patient_id: UUID, actor: ActorDependency) -> list[AuditEvent]:
    return container.service.list_audit_events(actor, patient_id)


@router.get("/archive-preview", response_model=ArchiveBatch)
@maps_errors(*VIEW_ERRORS)
def archive_preview(patient_id: UUID, actor: ActorDependency) -> ArchiveBatch:
    return container.service.archive_preview(actor, patient_id)


@router.post("/archive", response_model=ArchiveApplyResponse)
@maps_errors(*VIEW_ERRORS, ValueError)
def apply_archive(
    patient_id: UUID, payload: ArchiveApplyRequest, actor: ActorDependency
) -> ArchiveApplyResponse:
    return ArchiveApplyResponse(
        archived=container.service.apply_archive(actor, patient_id, payload.entry_ids)
    )
