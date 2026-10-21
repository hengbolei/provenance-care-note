import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from carenote.domain.models import Actor, Role
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService


@pytest.fixture
def clinic_id():
    return uuid4()


@pytest.fixture
def patient_id():
    return uuid4()


@pytest.fixture
def staff(clinic_id):
    return Actor(id=uuid4(), role=Role.STAFF, clinic_id=clinic_id)


@pytest.fixture
def clinician(clinic_id):
    return Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=clinic_id)


@pytest.fixture
def repository():
    return InMemoryCareNoteRepository()


@pytest.fixture
def service(repository):
    return CareNoteService(repository)


# PostgreSQL: CARENOTE_TEST_DATABASE_URL when set (CI), else a local pgserver, else skip.


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory):
    url = os.environ.get("CARENOTE_TEST_DATABASE_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip(
        "pgserver", reason="set CARENOTE_TEST_DATABASE_URL or install pgserver"
    )
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    yield server.get_uri()


@pytest.fixture
def database(postgres_server):
    name = f"carenote_test_{uuid4().hex[:12]}"
    with psycopg.connect(postgres_server, autocommit=True) as connection:
        connection.execute(f"CREATE DATABASE {name}")
    yield make_conninfo(postgres_server, dbname=name)
    with psycopg.connect(postgres_server, autocommit=True) as connection:
        connection.execute(f"DROP DATABASE {name} WITH (FORCE)")
