"""Durable agent runs: encrypted checkpoints and a clinic-scoped run registry (design §11).

With PostgreSQL configured, a run survives a server restart:

- graph state is checkpointed by LangGraph's `PostgresSaver`, with every serialized value encrypted
  by the same key as the clinical snapshot, so drafts, quotes and decisions are never stored in
  clear (LangGraph keeps only primitive values such as the status and prompt version unencrypted);
- `agent_runs` records which clinic, patient and clinician each run belongs to.

Both are clinic-scoped by forced row-level security (migrations 002 and 003). Connections set
`app.current_clinic_id` when they open, so every query sees only the configured clinic's runs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from carenote.agent.graph import checkpoint_serializer
from carenote.core.security import ClinicalDataCipher

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"
ENCRYPTED_PREFIX = "fernet:"


class EncryptedSerializer(SerializerProtocol):
    """Encrypts every value LangGraph serializes, and refuses to load anything unencrypted."""

    def __init__(self, cipher: ClinicalDataCipher, inner: SerializerProtocol | None = None):
        self.cipher = cipher
        self.inner = inner or checkpoint_serializer()

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        type_, data = self.inner.dumps_typed(obj)
        return f"{ENCRYPTED_PREFIX}{type_}", self.cipher.encrypt_bytes(data)

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        type_, payload = data
        if not type_.startswith(ENCRYPTED_PREFIX):
            raise ValueError("refusing to load an unencrypted checkpoint value")
        return self.inner.loads_typed(
            (type_[len(ENCRYPTED_PREFIX) :], self.cipher.decrypt_bytes(payload))
        )


@dataclass
class RunRecord:
    """Who a run belongs to and how far its audit trail has got."""

    run_id: UUID
    patient_id: UUID
    clinic_id: UUID
    started_by: UUID
    agent_id: UUID
    created_at: datetime
    error: str | None = None
    audit_version: int = 0
    tool_calls_audited: int = 0
    # Metadata-only timeline steps (TimelineStep as JSON); never record text.
    timeline: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)


_COLUMNS = [item.name for item in fields(RunRecord) if item.name != "lock"]


class RunStore(Protocol):
    def save(self, record: RunRecord) -> None: ...

    def load(self, run_id: UUID) -> RunRecord | None: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._records: dict[UUID, RunRecord] = {}

    def save(self, record: RunRecord) -> None:
        self._records[record.run_id] = record

    def load(self, run_id: UUID) -> RunRecord | None:
        return self._records.get(run_id)


def _scoped_options(clinic_id: UUID) -> str:
    return f"-c app.current_clinic_id={clinic_id}"


class PostgresRunStore:
    """`agent_runs` rows for one clinic; RLS hides every other clinic's runs."""

    def __init__(self, database_url: str, clinic_id: UUID) -> None:
        self.database_url = database_url
        self.clinic_id = clinic_id

    def _connect(self):
        return psycopg.connect(self.database_url, options=_scoped_options(self.clinic_id))

    def save(self, record: RunRecord) -> None:
        values = [
            Jsonb(record.timeline) if name == "timeline" else getattr(record, name)
            for name in _COLUMNS
        ]
        updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in _COLUMNS if name != "run_id")
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO agent_runs ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join(['%s'] * len(_COLUMNS))}) "
                f"ON CONFLICT (run_id) DO UPDATE SET {updates}",
                values,
            )

    def load(self, run_id: UUID) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM agent_runs WHERE run_id = %s", (run_id,)
            ).fetchone()
        return RunRecord(**dict(zip(_COLUMNS, row, strict=True))) if row else None


def migrate_agent_runs(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute((MIGRATIONS / "002_agent_runs.sql").read_text(encoding="utf-8"))


def postgres_checkpointer(
    database_url: str, clinic_id: UUID, cipher: ClinicalDataCipher, max_connections: int = 4
) -> PostgresSaver:
    """A LangGraph checkpointer whose values are encrypted and whose rows are clinic-scoped."""
    migrate_agent_runs(database_url)
    pool = ConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=max_connections,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": _scoped_options(clinic_id),
        },
        open=True,
    )
    saver = PostgresSaver(pool, serde=EncryptedSerializer(cipher))
    saver.setup()
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            (MIGRATIONS / "003_agent_checkpoint_rls.sql").read_text(encoding="utf-8")
        )
    return saver
