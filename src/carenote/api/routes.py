from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketException,
    status,
)

from carenote.agent.commit import commit_review
from carenote.agent.gateway import gateway_from_settings
from carenote.agent.persistence import PostgresRunStore, postgres_checkpointer
from carenote.agent.runs import (
    AgentCommitRequest,
    AgentReviseRequest,
    AgentRunService,
    AgentRunView,
    CommitUnavailableError,
    RunNotFoundError,
    RunStateError,
)
from carenote.core.config import settings
from carenote.core.identity import AuthenticationError, session_service
from carenote.core.security import clinical_cipher
from carenote.data.seed import DEMO_CLINIC_ID, seed_demo_data
from carenote.domain.models import (
    Actor,
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
    HealthResponse,
    Highlight,
    HighlightCreateRequest,
    HighlightImpressionRequest,
    HighlightUpdateRequest,
    LoginRequest,
    PatientDetailResponse,
    ResourceRevision,
    SectionDiff,
    SectionRevertRequest,
    SectionRevision,
    SectionState,
    SectionUpdateRequest,
    SessionResponse,
    SourceEvidence,
    TimelineEntry,
    TimelineEntryCreateRequest,
    TranscriptionResponse,
)
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.repositories.postgres import PostgresCareNoteRepository
from carenote.services.ai import ExternalModelError, ModelNotConfiguredError
from carenote.services.care_notes import (
    CareNoteService,
    ConcurrentEditError,
    PatientNotFoundError,
)
from carenote.services.realtime import realtime_hub

router = APIRouter(prefix="/api")
repository = (
    PostgresCareNoteRepository(settings.database_url, DEMO_CLINIC_ID, clinical_cipher)
    if settings.database_url
    else InMemoryCareNoteRepository()
)
seed_demo_data(repository)
service = CareNoteService(repository)
# With PostgreSQL, agent runs are checkpointed (encrypted) and registered there, so they survive a
# restart; without it they live in memory like the rest of the development store.
agent_runs = AgentRunService(
    service,
    gateway_from_settings(),
    committer=commit_review(service),
    publish=realtime_hub.publish,
    checkpointer=(
        postgres_checkpointer(settings.database_url, DEMO_CLINIC_ID, clinical_cipher)
        if settings.database_url
        else None
    ),
    store=PostgresRunStore(settings.database_url, DEMO_CLINIC_ID)
    if settings.database_url
    else None,
)


def session_token(
    authorization: Annotated[str | None, Header()] = None,
    carenote_session: Annotated[str | None, Cookie()] = None,
) -> str:
    if authorization is not None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise HTTPException(status_code=401, detail="invalid authorization scheme")
        return value
    if carenote_session:
        return carenote_session
    raise HTTPException(status_code=401, detail="authentication required")


def current_actor(token: Annotated[str, Depends(session_token)]) -> Actor:
    try:
        return session_service.verify(token).actor
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


ActorDependency = Annotated[Actor, Depends(current_actor)]


def translate_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RunStateError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CommitUnavailableError):
        return HTTPException(status_code=501, detail=str(exc))
    if isinstance(exc, PatientNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ConcurrentEditError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=404, detail=str(exc))


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="provenance-care-note")


@router.websocket("/ws/patients/{patient_id}")
async def patient_realtime(socket: WebSocket, patient_id: UUID) -> None:
    token = socket.cookies.get("carenote_session")
    if not token:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    try:
        actor = session_service.verify(token).actor
        service.get_patient_view(actor, patient_id)
    except (AuthenticationError, PatientNotFoundError, PermissionError) as exc:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION) from exc
    await realtime_hub.connect(patient_id, socket, actor)
    await realtime_hub.serve(
        patient_id,
        socket,
        lambda: len(repository.list_audit_events(patient_id, actor.clinic_id)),
    )


@router.post("/auth/login", response_model=SessionResponse)
def login(payload: LoginRequest, response: Response) -> SessionResponse:
    try:
        token, session = session_service.authenticate(payload.username, payload.password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.set_cookie(
        "carenote_session",
        token,
        max_age=settings.token_ttl_seconds,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        path="/",
    )
    return SessionResponse(
        actor=session.actor,
        expires_at=session.expires_at,
        token=token,
    )


@router.get("/auth/me", response_model=SessionResponse)
def session_details(
    token: Annotated[str, Depends(session_token)],
    actor: ActorDependency,
) -> SessionResponse:
    session = session_service.verify(token)
    return SessionResponse(actor=actor, expires_at=session.expires_at)


@router.post("/auth/logout", status_code=204)
def logout(
    response: Response,
    token: Annotated[str, Depends(session_token)],
) -> None:
    try:
        session_service.revoke(token)
    except AuthenticationError:
        pass
    response.delete_cookie("carenote_session", path="/")


@router.get("/patients/{patient_id}", response_model=PatientDetailResponse)
def patient_view(
    patient_id: UUID,
    actor: ActorDependency,
) -> PatientDetailResponse:
    try:
        return service.get_patient_view(actor, patient_id)
    except (PatientNotFoundError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.get(
    "/patients/{patient_id}/entries/{entry_id}/source",
    response_model=SourceEvidence,
)
def entry_source(patient_id: UUID, entry_id: UUID, actor: ActorDependency) -> SourceEvidence:
    try:
        return service.get_source_evidence(actor, patient_id, entry_id)
    except (PatientNotFoundError, LookupError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.get(
    "/patients/{patient_id}/highlights/{highlight_id}/source",
    response_model=SourceEvidence,
)
def highlight_source(
    patient_id: UUID, highlight_id: UUID, actor: ActorDependency
) -> SourceEvidence:
    try:
        return service.get_highlight_source_evidence(actor, patient_id, highlight_id)
    except (PatientNotFoundError, LookupError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.post("/patients/{patient_id}/comments", response_model=Comment, status_code=201)
def create_comment(
    patient_id: UUID, payload: CommentCreateRequest, actor: ActorDependency
) -> Comment:
    try:
        return service.add_comment(
            actor,
            patient_id,
            payload.content,
            payload.assigned_to,
            payload.parent_id,
            payload.target,
        )
    except (PatientNotFoundError, PermissionError, LookupError, ValueError) as exc:
        raise translate_service_error(exc) from exc


@router.post("/patients/{patient_id}/entries", response_model=TimelineEntry, status_code=201)
def create_timeline_entry(
    patient_id: UUID,
    payload: TimelineEntryCreateRequest,
    actor: ActorDependency,
) -> TimelineEntry:
    try:
        return service.create_manual_entry(actor, patient_id, payload.title, payload.content)
    except (PatientNotFoundError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.post("/patients/{patient_id}/highlights", response_model=Highlight, status_code=201)
def create_highlight(
    patient_id: UUID,
    payload: HighlightCreateRequest,
    actor: ActorDependency,
) -> Highlight:
    try:
        return service.create_highlight(actor, patient_id, payload)
    except (PatientNotFoundError, LookupError, PermissionError, ValueError) as exc:
        raise translate_service_error(exc) from exc


@router.post(
    "/patients/{patient_id}/highlights/{highlight_id}/impressions",
    status_code=204,
)
def record_highlight_impression(
    patient_id: UUID,
    highlight_id: UUID,
    payload: HighlightImpressionRequest,
    actor: ActorDependency,
) -> None:
    try:
        service.record_highlight_impression(
            actor, patient_id, highlight_id, payload.expected_version
        )
    except (
        PatientNotFoundError,
        LookupError,
        PermissionError,
        ConcurrentEditError,
    ) as exc:
        raise translate_service_error(exc) from exc


@router.post(
    "/patients/{patient_id}/ai-ingest",
    response_model=AIIngestResponse,
    status_code=201,
)
def ai_ingest(
    patient_id: UUID, payload: AIIngestRequest, actor: ActorDependency
) -> AIIngestResponse:
    try:
        return service.ingest_ai_summary(actor, patient_id, payload)
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ExternalModelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (PatientNotFoundError, PermissionError, ValueError) as exc:
        raise translate_service_error(exc) from exc


@router.post(
    "/patients/{patient_id}/transcriptions",
    response_model=TranscriptionResponse,
)
async def transcribe_audio(
    patient_id: UUID, request: Request, actor: ActorDependency
) -> TranscriptionResponse:
    try:
        service._require_internal_collaborator(actor)
        if repository.get_patient(patient_id, actor.clinic_id) is None:
            raise PatientNotFoundError("patient not found in actor's clinic")
        content_type = request.headers.get("content-type", "audio/webm")
        if content_type.split(";", 1)[0] not in {
            "audio/webm",
            "audio/wav",
            "audio/mpeg",
            "audio/mp4",
            "audio/ogg",
        }:
            raise ValueError("unsupported audio content type")
        audio = await request.body()
        if not audio or len(audio) > 25 * 1024 * 1024:
            raise ValueError("audio must contain 1 byte to 25 MiB")
        return service.ai_gateway.transcribe(audio, content_type)
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ExternalModelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (PatientNotFoundError, PermissionError, ValueError) as exc:
        raise translate_service_error(exc) from exc


@router.patch("/patients/{patient_id}/comments/{comment_id}", response_model=Comment)
def resolve_comment(
    patient_id: UUID,
    comment_id: UUID,
    payload: CommentResolveRequest,
    actor: ActorDependency,
) -> Comment:
    try:
        return service.set_comment_resolved(
            actor,
            patient_id,
            comment_id,
            payload.resolved,
            payload.expected_version,
        )
    except (LookupError, PermissionError, ConcurrentEditError) as exc:
        raise translate_service_error(exc) from exc


@router.patch("/patients/{patient_id}/highlights/{highlight_id}", response_model=Highlight)
def update_highlight(
    patient_id: UUID,
    highlight_id: UUID,
    payload: HighlightUpdateRequest,
    actor: ActorDependency,
) -> Highlight:
    try:
        return service.update_highlight(actor, patient_id, highlight_id, payload)
    except (
        PatientNotFoundError,
        LookupError,
        PermissionError,
        ConcurrentEditError,
    ) as exc:
        raise translate_service_error(exc) from exc


@router.patch(
    "/patients/{patient_id}/conflicts/{conflict_id}",
    response_model=Conflict,
)
def update_conflict(
    patient_id: UUID,
    conflict_id: UUID,
    payload: ConflictUpdateRequest,
    actor: ActorDependency,
) -> Conflict:
    try:
        return service.update_conflict(actor, patient_id, conflict_id, payload)
    except (LookupError, PermissionError, ConcurrentEditError) as exc:
        raise translate_service_error(exc) from exc


@router.get(
    "/patients/{patient_id}/conflicts/{conflict_id}/revisions",
    response_model=list[ResourceRevision],
)
def conflict_revisions(
    patient_id: UUID, conflict_id: UUID, actor: ActorDependency
) -> list[ResourceRevision]:
    try:
        return service.list_resource_revisions(actor, patient_id, "conflict", conflict_id)
    except (PatientNotFoundError, LookupError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.put("/patients/{patient_id}/sections/{section}", response_model=SectionState)
def update_section(
    patient_id: UUID,
    section: str,
    payload: SectionUpdateRequest,
    actor: ActorDependency,
) -> SectionState:
    try:
        service.get_patient_view(actor, patient_id)
        return service.update_section(
            actor, patient_id, section, payload.content, payload.expected_version
        )
    except (PatientNotFoundError, PermissionError, ConcurrentEditError) as exc:
        raise translate_service_error(exc) from exc


@router.get(
    "/patients/{patient_id}/sections/{section}/revisions",
    response_model=list[SectionRevision],
)
def section_revisions(
    patient_id: UUID, section: str, actor: ActorDependency
) -> list[SectionRevision]:
    try:
        return service.list_revisions(actor, patient_id, section)
    except (LookupError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.get(
    "/patients/{patient_id}/sections/{section}/compare",
    response_model=SectionDiff,
)
def compare_section_revisions(
    patient_id: UUID,
    section: str,
    from_version: int,
    to_version: int,
    actor: ActorDependency,
) -> SectionDiff:
    try:
        return service.compare_section_versions(
            actor, patient_id, section, from_version, to_version
        )
    except (LookupError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.get(
    "/patients/{patient_id}/highlights/{highlight_id}/revisions",
    response_model=list[ResourceRevision],
)
def highlight_revisions(
    patient_id: UUID, highlight_id: UUID, actor: ActorDependency
) -> list[ResourceRevision]:
    try:
        return service.list_resource_revisions(actor, patient_id, "highlight", highlight_id)
    except (PatientNotFoundError, LookupError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.get(
    "/patients/{patient_id}/comments/{comment_id}/revisions",
    response_model=list[ResourceRevision],
)
def comment_revisions(
    patient_id: UUID, comment_id: UUID, actor: ActorDependency
) -> list[ResourceRevision]:
    try:
        return service.list_resource_revisions(actor, patient_id, "comment", comment_id)
    except (PatientNotFoundError, LookupError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.post("/patients/{patient_id}/sections/{section}/revert", response_model=SectionState)
def revert_section(
    patient_id: UUID,
    section: str,
    payload: SectionRevertRequest,
    actor: ActorDependency,
) -> SectionState:
    try:
        service.get_patient_view(actor, patient_id)
        return service.revert_section(actor, patient_id, section, payload.target_version)
    except (LookupError, PermissionError, ConcurrentEditError) as exc:
        raise translate_service_error(exc) from exc


@router.get("/patients/{patient_id}/audit", response_model=list[AuditEvent])
def patient_audit(patient_id: UUID, actor: ActorDependency) -> list[AuditEvent]:
    try:
        return service.list_audit_events(actor, patient_id)
    except (PatientNotFoundError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.get("/patients/{patient_id}/archive-preview", response_model=ArchiveBatch)
def archive_preview(patient_id: UUID, actor: ActorDependency) -> ArchiveBatch:
    try:
        return service.archive_preview(actor, patient_id)
    except (PatientNotFoundError, PermissionError) as exc:
        raise translate_service_error(exc) from exc


@router.post("/patients/{patient_id}/archive", response_model=ArchiveApplyResponse)
def apply_archive(
    patient_id: UUID, payload: ArchiveApplyRequest, actor: ActorDependency
) -> ArchiveApplyResponse:
    try:
        return ArchiveApplyResponse(
            archived=service.apply_archive(actor, patient_id, payload.entry_ids)
        )
    except (PatientNotFoundError, PermissionError, ValueError) as exc:
        raise translate_service_error(exc) from exc


@router.post("/maintenance/audit-retention")
def enforce_audit_retention(actor: ActorDependency) -> dict[str, int | bool]:
    if actor.role.value != "admin":
        raise HTTPException(status_code=403, detail="only admins can enforce retention")
    deleted = repository.purge_expired_audit_events(settings.audit_retention_days)
    return {
        "deleted": deleted,
        "retention_days": settings.audit_retention_days,
        "chain_valid": repository.verify_audit_chain(),
    }


AGENT_RUN_ERRORS = (
    PatientNotFoundError,
    RunNotFoundError,
    RunStateError,
    CommitUnavailableError,
    PermissionError,
    ValueError,
)


@router.post("/patients/{patient_id}/agent-runs", response_model=AgentRunView, status_code=202)
def start_agent_run(patient_id: UUID, actor: ActorDependency) -> AgentRunView:
    try:
        return agent_runs.start(actor, patient_id)
    except AGENT_RUN_ERRORS as exc:
        raise translate_service_error(exc) from exc


@router.get("/agent-runs/{run_id}", response_model=AgentRunView)
def get_agent_run(run_id: UUID, actor: ActorDependency) -> AgentRunView:
    try:
        return agent_runs.get_run(actor, run_id)
    except AGENT_RUN_ERRORS as exc:
        raise translate_service_error(exc) from exc


@router.post("/agent-runs/{run_id}/review", response_model=AgentRunView, status_code=202)
def revise_agent_run(
    run_id: UUID, payload: AgentReviseRequest, actor: ActorDependency
) -> AgentRunView:
    try:
        return agent_runs.revise(actor, run_id, payload)
    except AGENT_RUN_ERRORS as exc:
        raise translate_service_error(exc) from exc


@router.post("/agent-runs/{run_id}/commit", response_model=AgentRunView)
def commit_agent_run(
    run_id: UUID, payload: AgentCommitRequest, actor: ActorDependency
) -> AgentRunView:
    try:
        return agent_runs.commit(actor, run_id, payload)
    except AGENT_RUN_ERRORS as exc:
        raise translate_service_error(exc) from exc


@router.post("/agent-runs/{run_id}/discard", response_model=AgentRunView)
def discard_agent_run(run_id: UUID, actor: ActorDependency) -> AgentRunView:
    try:
        return agent_runs.discard(actor, run_id)
    except AGENT_RUN_ERRORS as exc:
        raise translate_service_error(exc) from exc
