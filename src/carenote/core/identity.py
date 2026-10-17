import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID

import psycopg

from carenote.core.config import settings
from carenote.data.seed import (
    DEMO_ADMIN_ID,
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    DEMO_STAFF_ID,
)
from carenote.domain.models import Actor, Role


class AuthenticationError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class UserAccount:
    username: str
    actor: Actor
    salt: bytes
    password_hash: bytes


@dataclass(slots=True)
class SessionRecord:
    id: str
    actor: Actor
    expires_at: datetime
    revoked: bool = False


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)


def _account(username: str, password: str, actor_id: UUID, role: Role) -> UserAccount:
    salt = hashlib.sha256(f"carenote-demo:{username}".encode()).digest()[:16]
    return UserAccount(
        username=username,
        actor=Actor(id=actor_id, role=role, clinic_id=DEMO_CLINIC_ID),
        salt=salt,
        password_hash=_password_hash(password, salt),
    )


DEMO_ACCOUNTS = {
    account.username: account
    for account in (
        _account("patient", "patient-demo-2026", DEMO_PATIENT_ID, Role.PATIENT),
        _account("staff", "staff-demo-2026", DEMO_STAFF_ID, Role.STAFF),
        _account("clinician", "clinician-demo-2026", DEMO_CLINICIAN_ID, Role.CLINICIAN),
        _account("admin", "admin-demo-2026", DEMO_ADMIN_ID, Role.ADMIN),
    )
}


class SignedSessionService:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self.secret = secret.encode()
        self.ttl_seconds = ttl_seconds
        self.sessions: dict[str, SessionRecord] = {}
        self._lock = RLock()

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def authenticate(self, username: str, password: str) -> tuple[str, SessionRecord]:
        account = self._database_account(username.lower())
        if account is None and not settings.database_url and settings.environment != "production":
            account = DEMO_ACCOUNTS.get(username.lower())
        if account is None or not hmac.compare_digest(
            account.password_hash, _password_hash(password, account.salt)
        ):
            raise AuthenticationError("invalid username or password")
        self._require_membership(account.actor)
        return self.issue(account.actor)

    def _database_account(self, username: str) -> UserAccount | None:
        connection = self._scoped_connection(DEMO_CLINIC_ID)
        if connection is None:
            return None
        with connection:
            row = connection.execute(
                """
                SELECT u.actor_id, u.clinic_id, m.role, u.password_salt, u.password_hash
                FROM user_accounts u
                JOIN clinic_memberships m
                  ON m.actor_id = u.actor_id AND m.clinic_id = u.clinic_id
                WHERE u.username = %s AND u.active = true AND m.active = true
                """,
                (username,),
            ).fetchone()
        if row is None:
            return None
        return UserAccount(
            username=username,
            actor=Actor(id=row[0], clinic_id=row[1], role=Role(row[2])),
            salt=bytes(row[3]),
            password_hash=bytes(row[4]),
        )

    def issue(self, actor: Actor, expires_at: datetime | None = None) -> tuple[str, SessionRecord]:
        if actor.role is Role.SYSTEM:
            raise AuthenticationError("system cannot receive an interactive session")
        now = datetime.now(UTC)
        record = SessionRecord(
            id=secrets.token_urlsafe(24),
            actor=actor,
            expires_at=expires_at or now + timedelta(seconds=self.ttl_seconds),
        )
        payload = {
            "sub": str(actor.id),
            "role": actor.role.value,
            "clinic": str(actor.clinic_id),
            "sid": record.id,
            "iat": int(now.timestamp()),
            "exp": int(record.expires_at.timestamp()),
        }
        header = self._encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = self._encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = self._encode(hmac.digest(self.secret, f"{header}.{body}".encode(), "sha256"))
        with self._lock:
            self.sessions[record.id] = record
        self._persist_session(record)
        return f"{header}.{body}.{signature}", record

    def verify(self, token: str) -> SessionRecord:
        try:
            header, body, signature = token.split(".")
            expected = self._encode(hmac.digest(self.secret, f"{header}.{body}".encode(), "sha256"))
            if not hmac.compare_digest(signature, expected):
                raise AuthenticationError("invalid session signature")
            payload = json.loads(self._decode(body))
            session_id = str(payload["sid"])
            with self._lock:
                record = self.sessions.get(session_id)
            if record is None:
                record = self._load_persisted_session(payload)
            if record is None or record.revoked:
                raise AuthenticationError("session is not active")
            now = datetime.now(UTC)
            if record.expires_at <= now or int(payload["exp"]) <= int(now.timestamp()):
                raise AuthenticationError("session has expired")
            if (
                payload["sub"] != str(record.actor.id)
                or payload["role"] != record.actor.role.value
                or payload["clinic"] != str(record.actor.clinic_id)
            ):
                raise AuthenticationError("session claims do not match membership")
            return record
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError("malformed session token") from exc

    def revoke(self, token: str) -> None:
        record = self.verify(token)
        with self._lock:
            record.revoked = True
        self._revoke_persisted_session(record)

    @staticmethod
    def _session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode()).hexdigest()

    @staticmethod
    def _scoped_connection(clinic_id: UUID):
        if not settings.database_url:
            return None
        connection = psycopg.connect(settings.database_url)
        connection.execute(
            "SELECT set_config('app.current_clinic_id', %s, false)",
            (str(clinic_id),),
        )
        return connection

    def _require_membership(self, actor: Actor) -> None:
        connection = self._scoped_connection(actor.clinic_id)
        if connection is None:
            return
        with connection:
            row = connection.execute(
                """
                SELECT role FROM clinic_memberships
                WHERE actor_id = %s AND clinic_id = %s AND active = true
                """,
                (actor.id, actor.clinic_id),
            ).fetchone()
        if row is None or row[0] != actor.role.value:
            raise AuthenticationError("clinic membership is not active")

    def _persist_session(self, record: SessionRecord) -> None:
        connection = self._scoped_connection(record.actor.clinic_id)
        if connection is None:
            return
        with connection:
            connection.execute(
                """
                INSERT INTO authenticated_sessions
                    (session_hash, actor_id, clinic_id, expires_at, revoked_at)
                VALUES (%s, %s, %s, %s, NULL)
                """,
                (
                    self._session_hash(record.id),
                    record.actor.id,
                    record.actor.clinic_id,
                    record.expires_at,
                ),
            )

    def _load_persisted_session(self, payload: dict[str, object]) -> SessionRecord | None:
        clinic_id = UUID(str(payload["clinic"]))
        connection = self._scoped_connection(clinic_id)
        if connection is None:
            return None
        with connection:
            row = connection.execute(
                """
                SELECT actor_id, clinic_id, expires_at, revoked_at
                FROM authenticated_sessions
                WHERE session_hash = %s
                """,
                (self._session_hash(str(payload["sid"])),),
            ).fetchone()
        if row is None or row[3] is not None:
            return None
        record = SessionRecord(
            id=str(payload["sid"]),
            actor=Actor(id=row[0], role=Role(str(payload["role"])), clinic_id=row[1]),
            expires_at=row[2],
        )
        with self._lock:
            self.sessions[record.id] = record
        return record

    def _revoke_persisted_session(self, record: SessionRecord) -> None:
        connection = self._scoped_connection(record.actor.clinic_id)
        if connection is None:
            return
        with connection:
            connection.execute(
                """
                UPDATE authenticated_sessions SET revoked_at = now()
                WHERE session_hash = %s
                """,
                (self._session_hash(record.id),),
            )


session_service = SignedSessionService(settings.token_secret, settings.token_ttl_seconds)
