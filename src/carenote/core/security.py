import base64
import hashlib
import json
import logging
from time import perf_counter

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request

from carenote.core.config import settings


class EncryptionError(RuntimeError):
    pass


class ClinicalDataCipher:
    """Authenticated encryption boundary for clinical JSON payloads at rest."""

    def __init__(self, key_material: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest())
        self._fernet = Fernet(key)

    def encrypt_json(self, value: dict[str, object]) -> bytes:
        plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return self._fernet.encrypt(plaintext)

    def decrypt_json(self, value: bytes) -> dict[str, object]:
        try:
            return json.loads(self._fernet.decrypt(value))
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise EncryptionError("clinical payload authentication failed") from exc


clinical_cipher = ClinicalDataCipher(
    settings.encryption_key or f"development-only:{settings.token_secret}"
)


safe_request_logger = logging.getLogger("carenote.request")


async def safe_request_logging(request: Request, call_next):
    """Log metadata only: never headers, cookies, query strings, or request bodies."""
    started = perf_counter()
    response = await call_next(request)
    safe_request_logger.info(
        "request method=%s path=%s status=%s duration_ms=%.3f",
        request.method,
        request.url.path,
        response.status_code,
        (perf_counter() - started) * 1000,
    )
    response.headers["cache-control"] = "no-store"
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    return response
