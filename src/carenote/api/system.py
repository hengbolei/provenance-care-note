"""Health and maintenance endpoints."""

from fastapi import APIRouter, HTTPException

from carenote.api.container import container
from carenote.api.dependencies import ActorDependency
from carenote.core.config import settings
from carenote.domain.models import HealthResponse, Role

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="provenance-care-note")


@router.post("/maintenance/audit-retention")
def enforce_audit_retention(actor: ActorDependency) -> dict[str, int | bool]:
    if actor.role is not Role.ADMIN:
        raise HTTPException(status_code=403, detail="only admins can enforce retention")
    repository = container.repository
    deleted = repository.purge_expired_audit_events(settings.audit_retention_days)
    return {
        "deleted": deleted,
        "retention_days": settings.audit_retention_days,
        "chain_valid": repository.verify_audit_chain(),
    }
