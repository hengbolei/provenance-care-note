"""Domain errors map to HTTP statuses in one place; unlisted errors stay server errors."""

import asyncio

import pytest
from fastapi import HTTPException

from carenote.agent.runs import CommitUnavailableError, RunStateError
from carenote.api.errors import http_error, maps_errors
from carenote.services.ai import ExternalModelError, ModelNotConfiguredError
from carenote.services.care_notes import (
    AccessDeniedError,
    ConcurrentEditError,
    PatientNotFoundError,
)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ModelNotConfiguredError("no key"), 503),
        (ExternalModelError("down"), 502),
        (RunStateError("not awaiting review"), 409),
        (ConcurrentEditError("stale"), 409),
        (CommitUnavailableError("off"), 501),
        (PatientNotFoundError("missing"), 404),
        (AccessDeniedError("no"), 403),
        (PermissionError("no"), 403),
        (ValueError("bad"), 422),
        (LookupError("missing"), 404),
    ],
)
def test_each_domain_error_has_one_status(error, status):
    mapped = http_error(error)

    assert (mapped.status_code, mapped.detail) == (status, str(error))


def test_listed_errors_become_http_errors_in_sync_and_async_endpoints():
    @maps_errors(PermissionError)
    def sync_endpoint():
        raise PermissionError("denied")

    @maps_errors(ValueError)
    async def async_endpoint():
        raise ValueError("bad input")

    with pytest.raises(HTTPException) as sync_error:
        sync_endpoint()
    with pytest.raises(HTTPException) as async_error:
        asyncio.run(async_endpoint())

    assert sync_error.value.status_code == 403
    assert async_error.value.status_code == 422


def test_unlisted_errors_are_not_masked():
    @maps_errors(PermissionError)
    def endpoint():
        raise ValueError("a bug, not a client error")

    with pytest.raises(ValueError):
        endpoint()


def test_the_wrapper_keeps_the_endpoint_signature_for_fastapi():
    @maps_errors(ValueError)
    def endpoint(patient_id: int, limit: int = 5) -> int:
        return patient_id + limit

    assert endpoint.__wrapped__.__name__ == "endpoint"
    assert endpoint(1) == 6
