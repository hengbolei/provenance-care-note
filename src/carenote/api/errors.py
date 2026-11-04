"""Mapping domain errors to HTTP responses.

Each endpoint names the domain errors it expects with `@maps_errors(...)`; anything else is an
unexpected failure and stays a 500. The status for each error type is decided in one place.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import HTTPException

from carenote.agent.runs import CommitUnavailableError, RunNotFoundError, RunStateError
from carenote.services.ai import ExternalModelError, ModelNotConfiguredError
from carenote.services.care_notes import ConcurrentEditError, PatientNotFoundError

F = TypeVar("F", bound=Callable[..., Any])

# Error groups shared by several endpoints.
READ_ERRORS = (PatientNotFoundError, LookupError, PermissionError)
EDIT_ERRORS = (*READ_ERRORS, ConcurrentEditError)
MODEL_ERRORS = (ModelNotConfiguredError, ExternalModelError)
AGENT_RUN_ERRORS = (
    PatientNotFoundError,
    RunNotFoundError,
    RunStateError,
    CommitUnavailableError,
    PermissionError,
    ValueError,
)
KNOWLEDGE_ERRORS = (PermissionError, LookupError, ConcurrentEditError, ValueError)


def http_error(exc: Exception) -> HTTPException:
    """The HTTP response for a domain error. Order matters: specific types come first."""
    if isinstance(exc, ModelNotConfiguredError):
        status = 503
    elif isinstance(exc, ExternalModelError):
        status = 502
    elif isinstance(exc, RunStateError | ConcurrentEditError):
        status = 409
    elif isinstance(exc, CommitUnavailableError):
        status = 501
    elif isinstance(exc, PatientNotFoundError):
        status = 404
    elif isinstance(exc, PermissionError):
        status = 403
    elif isinstance(exc, ValueError):
        status = 422
    else:
        status = 404
    return HTTPException(status_code=status, detail=str(exc))


def maps_errors(*errors: type[Exception]) -> Callable[[F], F]:
    """Turn the listed domain errors raised by an endpoint into HTTP errors."""

    def decorate(endpoint: F) -> F:
        if inspect.iscoroutinefunction(endpoint):

            @functools.wraps(endpoint)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await endpoint(*args, **kwargs)
                except errors as exc:
                    raise http_error(exc) from exc

            return run_async  # type: ignore[return-value]

        @functools.wraps(endpoint)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return endpoint(*args, **kwargs)
            except errors as exc:
                raise http_error(exc) from exc

        return run  # type: ignore[return-value]

    return decorate
