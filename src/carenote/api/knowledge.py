"""Reference knowledge base (design §19): administrators write, the care team reads."""

from uuid import UUID

from fastapi import APIRouter

from carenote.api.container import container
from carenote.api.dependencies import ActorDependency
from carenote.api.errors import KNOWLEDGE_ERRORS, maps_errors
from carenote.domain.models import AuditEvent
from carenote.knowledge.models import (
    KnowledgeDocument,
    KnowledgeDocumentCreate,
    KnowledgeDocumentDetail,
    KnowledgeRetire,
    KnowledgeVersion,
    KnowledgeVersionCreate,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("", response_model=list[KnowledgeDocument])
@maps_errors(*KNOWLEDGE_ERRORS)
def list_knowledge(
    actor: ActorDependency, include_retired: bool = False
) -> list[KnowledgeDocument]:
    return container.knowledge.list_documents(actor, include_retired)


@router.post("", response_model=KnowledgeDocumentDetail, status_code=201)
@maps_errors(*KNOWLEDGE_ERRORS)
def create_knowledge(
    request: KnowledgeDocumentCreate, actor: ActorDependency
) -> KnowledgeDocumentDetail:
    document, _ = container.knowledge.create_document(actor, request)
    return container.knowledge.document_detail(actor, document.id)


# Declared before "/{document_id}" so "audit" is not read as a document ID.
@router.get("/audit", response_model=list[AuditEvent])
@maps_errors(*KNOWLEDGE_ERRORS)
def knowledge_audit(actor: ActorDependency) -> list[AuditEvent]:
    return container.knowledge.list_audit_events(actor)


@router.get("/{document_id}", response_model=KnowledgeDocumentDetail)
@maps_errors(*KNOWLEDGE_ERRORS)
def get_knowledge(document_id: UUID, actor: ActorDependency) -> KnowledgeDocumentDetail:
    return container.knowledge.document_detail(actor, document_id)


@router.get("/{document_id}/versions/{version}", response_model=KnowledgeVersion)
@maps_errors(*KNOWLEDGE_ERRORS)
def get_knowledge_version(
    document_id: UUID, version: int, actor: ActorDependency
) -> KnowledgeVersion:
    return container.knowledge.get_version(actor, document_id, version)


@router.post("/{document_id}/versions", response_model=KnowledgeDocumentDetail, status_code=201)
@maps_errors(*KNOWLEDGE_ERRORS)
def add_knowledge_version(
    document_id: UUID, request: KnowledgeVersionCreate, actor: ActorDependency
) -> KnowledgeDocumentDetail:
    container.knowledge.add_version(actor, document_id, request)
    return container.knowledge.document_detail(actor, document_id)


@router.post("/{document_id}/retire", response_model=KnowledgeDocumentDetail)
@maps_errors(*KNOWLEDGE_ERRORS)
def retire_knowledge(
    document_id: UUID, request: KnowledgeRetire, actor: ActorDependency
) -> KnowledgeDocumentDetail:
    container.knowledge.retire_document(actor, document_id, request)
    return container.knowledge.document_detail(actor, document_id)
