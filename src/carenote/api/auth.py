"""Sign-in, session details and sign-out."""

from fastapi import APIRouter, HTTPException, Response

from carenote.api.dependencies import SESSION_COOKIE, ActorDependency, TokenDependency
from carenote.core.config import settings
from carenote.core.identity import AuthenticationError, session_service
from carenote.domain.models import LoginRequest, SessionResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=SessionResponse)
def login(payload: LoginRequest, response: Response) -> SessionResponse:
    try:
        token, session = session_service.authenticate(payload.username, payload.password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.token_ttl_seconds,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        path="/",
    )
    return SessionResponse(actor=session.actor, expires_at=session.expires_at, token=token)


@router.get("/me", response_model=SessionResponse)
def session_details(token: TokenDependency, actor: ActorDependency) -> SessionResponse:
    session = session_service.verify(token)
    return SessionResponse(actor=actor, expires_at=session.expires_at)


@router.post("/logout", status_code=204)
def logout(response: Response, token: TokenDependency) -> None:
    try:
        session_service.revoke(token)
    except AuthenticationError:
        pass
    response.delete_cookie(SESSION_COOKIE, path="/")
