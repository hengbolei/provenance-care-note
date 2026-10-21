"""Durable agent runs on PostgreSQL: restart survival, encryption at rest and clinic isolation.

Uses CARENOTE_TEST_DATABASE_URL when set (CI runs a Postgres service), otherwise a throwaway local
server from the optional `pgserver` package, and skips when neither is available.
"""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from agent_fakes import ScriptedGateway
from postgres_support import restricted_url

from carenote.agent.commit import commit_review
from carenote.agent.persistence import (
    EncryptedSerializer,
    PostgresRunStore,
    RunRecord,
    postgres_checkpointer,
)
from carenote.agent.runs import AgentCommitRequest, AgentRunService
from carenote.agent.state import RunStatus
from carenote.core.security import ClinicalDataCipher, EncryptionError
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID, seed_demo_data
from carenote.domain.models import Actor, Role
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
CIPHER = ClinicalDataCipher("test-only-agent-checkpoint-key")


# Serializer: no database needed


def test_checkpoint_values_are_encrypted_and_round_trip():
    serializer = EncryptedSerializer(CIPHER)
    value = {"quote": "Hold morning lisinopril", "count": 3}

    type_, data = serializer.dumps_typed(value)

    assert type_.startswith("fernet:")
    assert b"lisinopril" not in data
    assert serializer.loads_typed((type_, data)) == value


def test_unencrypted_or_foreign_checkpoint_values_are_refused():
    serializer = EncryptedSerializer(CIPHER)
    type_, data = serializer.inner.dumps_typed({"quote": "plain"})

    with pytest.raises(ValueError, match="unencrypted"):
        serializer.loads_typed((type_, data))
    encrypted = EncryptedSerializer(ClinicalDataCipher("another-key")).dumps_typed({"a": 1})
    with pytest.raises(EncryptionError):
        serializer.loads_typed(encrypted)


# PostgreSQL


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


def _service(database):
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    service = CareNoteService(repository)
    checkpointer = postgres_checkpointer(database, DEMO_CLINIC_ID, CIPHER, max_connections=2)
    runs = AgentRunService(
        service,
        ScriptedGateway(),
        committer=commit_review(service),
        executor=InlineExecutor(),
        checkpointer=checkpointer,
        store=PostgresRunStore(database, DEMO_CLINIC_ID),
        now=lambda: NOW,
    )
    return runs, checkpointer


def test_a_paused_run_survives_a_restart_and_can_be_committed(database):
    runs, checkpointer = _service(database)
    started = runs.start(CLINICIAN, DEMO_PATIENT_ID)
    assert started.status is RunStatus.AWAITING_REVIEW
    checkpointer.conn.close()

    restarted, restarted_checkpointer = _service(database)
    view = restarted.get_run(CLINICIAN, started.run_id)
    assert view.status is RunStatus.AWAITING_REVIEW
    assert view.draft == started.draft
    assert view.verification.passed

    committed = restarted.commit(
        CLINICIAN,
        started.run_id,
        AgentCommitRequest.model_validate({"claims": [{"claim_index": 0, "action": "accept"}]}),
    )
    assert committed.status is RunStatus.COMMITTED
    restarted_checkpointer.conn.close()


def test_checkpoints_hold_no_clinical_text_in_clear(database):
    runs, checkpointer = _service(database)
    runs.start(CLINICIAN, DEMO_PATIENT_ID)
    checkpointer.conn.close()

    with psycopg.connect(database) as connection:
        blobs = connection.execute(
            "SELECT type, blob FROM checkpoint_blobs WHERE blob IS NOT NULL "
            "UNION ALL SELECT type, blob FROM checkpoint_writes"
        ).fetchall()
        documents = connection.execute("SELECT checkpoint::text FROM checkpoints").fetchall()

    assert blobs
    assert all(type_.startswith("fernet:") for type_, _ in blobs)
    assert not any(b"lisinopril" in bytes(blob).lower() for _, blob in blobs)
    assert not any("lisinopril" in document.lower() for (document,) in documents)


def test_runs_and_checkpoints_are_invisible_to_other_clinics(database):
    runs, checkpointer = _service(database)
    run_id = runs.start(CLINICIAN, DEMO_PATIENT_ID).run_id
    checkpointer.conn.close()
    restricted = restricted_url(database)

    def visible(clinic_id):
        with psycopg.connect(restricted, options=f"-c app.current_clinic_id={clinic_id}") as conn:
            return {
                table: conn.execute(
                    f"SELECT count(*) FROM {table} WHERE "
                    + ("run_id = %s" if table == "agent_runs" else "thread_id = %s::text"),
                    (run_id,),
                ).fetchone()[0]
                for table in ("agent_runs", "checkpoints", "checkpoint_blobs", "checkpoint_writes")
            }

    own, other = visible(DEMO_CLINIC_ID), visible(uuid4())
    assert all(count > 0 for count in own.values()), own
    assert all(count == 0 for count in other.values()), other


def test_run_store_round_trips_and_is_clinic_scoped(database):
    postgres_checkpointer(database, DEMO_CLINIC_ID, CIPHER, max_connections=1).conn.close()
    restricted = restricted_url(database)
    record = RunRecord(
        run_id=uuid4(),
        patient_id=DEMO_PATIENT_ID,
        clinic_id=DEMO_CLINIC_ID,
        started_by=DEMO_CLINICIAN_ID,
        agent_id=uuid4(),
        created_at=NOW,
        audit_version=3,
    )
    store = PostgresRunStore(restricted, DEMO_CLINIC_ID)
    store.save(record)
    record.error = "the review run failed"
    store.save(record)

    loaded = store.load(record.run_id)
    assert loaded == record
    assert PostgresRunStore(restricted, uuid4()).load(record.run_id) is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        PostgresRunStore(restricted, uuid4()).save(record)
