"""Request dependencies: the session token and the actor it signs for."""

from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException

from carenote.core.identity import AuthenticationError, session_service
from carenote.domain.models import Actor

SESSION_COOKIE = "carenote_session"


def session_token(
    authorization: Annotated[str | None, Header()] = None,
    carenote_session: Annotated[str | None, Cookie()] = None,
) -> str:
    """The bearer token if one is sent, otherwise the session cookie."""
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


TokenDependency = Annotated[str, Depends(session_token)]
ActorDependency = Annotated[Actor, Depends(current_actor)]
