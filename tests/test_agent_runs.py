from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agent_fakes import ScriptedGateway
from fastapi.testclient import TestClient

from carenote.agent.gateway import RecordedGateway
from carenote.agent.runs import (
    AgentCommitRequest,
    AgentReviseRequest,
    AgentRunService,
    CommitUnavailableError,
    RunNotFoundError,
    RunStateError,
)
from carenote.agent.state import Budget, ClaimDecision, RunStatus
from carenote.api import routes
from carenote.core.identity import session_service
from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    DEMO_STAFF_ID,
    seed_demo_data,
)
from carenote.domain.models import Actor, Role
from carenote.main import app
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import AccessDeniedError, CareNoteService
from carenote.services.realtime import PatientRealtimeHub

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
STAFF = Actor(id=DEMO_STAFF_ID, role=Role.STAFF, clinic_id=DEMO_CLINIC_ID)
PATIENT = Actor(id=DEMO_PATIENT_ID, role=Role.PATIENT, clinic_id=DEMO_CLINIC_ID)


class InlineExecutor:
    """Runs graph segments immediately so tests see the finished state."""

    def submit(self, fn, *args):
        fn(*args)


class Committer:
    def __init__(self):
        self.calls = []

    def __call__(self, state, tools):
        self.calls.append(state)


@pytest.fixture
def repository():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    return repository


def _service(repository, gateway=None, committer=None, events=None, budget=None):
    return AgentRunService(
        CareNoteService(repository),
        gateway or ScriptedGateway(),
        committer=committer,
        publish=(lambda patient_id, event: events.append(event)) if events is not None else None,
        executor=InlineExecutor(),
        now=lambda: NOW,
        budget_factory=budget or (lambda now: Budget(started_at=now)),
    )


def _operations(repository, run_id):
    return [
        event.operation
        for event in repository.audit_events
        if event.resource_id.startswith(str(run_id))
    ]


def test_start_runs_to_review_with_audit_and_events(repository):
    events = []
    runs = _service(repository, events=events)

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    assert view.status is RunStatus.AWAITING_REVIEW
    assert view.verification.passed
    assert view.budget.steps == 4
    assert view.highlight_citations[0].quote == "Hold morning lisinopril"
    operations = _operations(repository, view.run_id)
    assert operations[0] == "agent_run_started"
    assert operations[-1] == "agent_draft_proposed"
    assert "agent_tool_called" in operations
    assert [event["type"] for event in events] == [
        "agent.step",
        "agent.step",
        "agent.step",
        "agent.step",
        "agent.draft",
        "agent.done",
    ]
    assert events[-1]["status"] == "awaiting_review"
    assert all(event["audience"] == "care_team" for event in events)
    assert "lisinopril" not in str(events)
    assert repository.verify_audit_chain()


def test_only_clinicians_in_the_clinic_can_start_or_read_runs(repository):
    runs = _service(repository)
    with pytest.raises(AccessDeniedError):
        runs.start(STAFF, DEMO_PATIENT_ID)
    with pytest.raises(LookupError):
        runs.start(Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=uuid4()), DEMO_PATIENT_ID)

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)
    with pytest.raises(RunNotFoundError):
        runs.get_run(Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=uuid4()), view.run_id)
    with pytest.raises(AccessDeniedError):
        runs.get_run(STAFF, view.run_id)


def test_revise_redrafts_and_returns_to_review(repository):
    gateway = ScriptedGateway()
    runs = _service(repository, gateway)
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    revised = runs.revise(CLINICIAN, view.run_id, AgentReviseRequest(instruction="Be brief."))

    assert revised.status is RunStatus.AWAITING_REVIEW
    assert revised.budget.revisions == 1
    assert len(gateway.requests) == 2


def test_commit_is_unavailable_until_a_committer_is_wired(repository):
    runs = _service(repository)
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    with pytest.raises(CommitUnavailableError):
        runs.commit(CLINICIAN, view.run_id, AgentCommitRequest())
    assert runs.get_run(CLINICIAN, view.run_id).status is RunStatus.AWAITING_REVIEW


def test_commit_calls_the_committer_and_audits_each_decision(repository):
    committer = Committer()
    runs = _service(repository, committer=committer)
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    committed = runs.commit(
        CLINICIAN,
        view.run_id,
        AgentCommitRequest(
            claims=[
                ClaimDecision(claim_index=0, action="accept"),
                ClaimDecision(claim_index=1, action="reject", reason="Already known"),
            ]
        ),
    )

    assert committed.status is RunStatus.COMMITTED
    assert committer.calls[0]["reviewer"] == CLINICIAN
    assert _operations(repository, view.run_id)[-3:] == [
        "agent_claim_accepted",
        "agent_claim_rejected",
        "agent_run_committed",
    ]
    assert repository.audit_events[-1].changed_by == CLINICIAN.id


def test_commit_rejects_decisions_for_items_the_draft_does_not_have(repository):
    runs = _service(repository, committer=Committer())
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    with pytest.raises(ValueError):
        runs.commit(
            CLINICIAN,
            view.run_id,
            AgentCommitRequest(claims=[ClaimDecision(claim_index=9, action="accept")]),
        )


def test_discard_ends_the_run_and_later_actions_conflict(repository):
    runs = _service(repository)
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    discarded = runs.discard(CLINICIAN, view.run_id)

    assert discarded.status is RunStatus.DISCARDED
    assert _operations(repository, view.run_id)[-1] == "agent_run_discarded"
    with pytest.raises(RunStateError):
        runs.discard(CLINICIAN, view.run_id)
    with pytest.raises(RunStateError):
        runs.revise(CLINICIAN, view.run_id, AgentReviseRequest(instruction="Again."))


def test_model_failures_end_the_run_with_a_safe_reason(repository, tmp_path):
    events = []
    runs = _service(repository, gateway=RecordedGateway(tmp_path), events=events)

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    assert view.status is RunStatus.FAILED
    assert "no cassette" in view.error
    assert _operations(repository, view.run_id)[-1] == "agent_run_failed"
    assert events[-1] == {
        "type": "agent.done",
        "run_id": str(view.run_id),
        "audience": "care_team",
        "status": "failed",
    }


def test_exceeding_the_budget_is_audited(repository):
    runs = _service(repository, budget=lambda now: Budget(started_at=now, max_tokens=100))

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    assert view.status is RunStatus.BUDGET_EXCEEDED
    assert view.stop_reason == "token limit of 100 exceeded"
    assert view.draft is not None
    assert _operations(repository, view.run_id)[-1] == "agent_budget_exceeded"


def test_patient_sockets_never_receive_care_team_events():
    hub = PatientRealtimeHub()
    hub.publish(DEMO_PATIENT_ID, {"type": "agent.done", "audience": "care_team"})
    hub.publish(DEMO_PATIENT_ID, {"type": "notice"})

    _, patient_events = hub.events_since(DEMO_PATIENT_ID, 0, PATIENT)
    latest, clinician_events = hub.events_since(DEMO_PATIENT_ID, 0, CLINICIAN)

    assert [event["type"] for event in patient_events] == ["notice"]
    assert [event["type"] for event in clinician_events] == ["agent.done", "notice"]
    assert hub.events_since(DEMO_PATIENT_ID, latest, CLINICIAN) == (latest, [])


# HTTP API


@pytest.fixture
def client(monkeypatch):
    runs = AgentRunService(
        routes.service, ScriptedGateway(), executor=InlineExecutor(), now=lambda: NOW
    )
    monkeypatch.setattr(routes, "agent_runs", runs)
    return TestClient(app)


def _headers(actor):
    token, _ = session_service.issue(actor)
    return {"authorization": f"Bearer {token}"}


def test_api_runs_a_review_to_the_review_step(client):
    started = client.post(
        f"/api/patients/{DEMO_PATIENT_ID}/agent-runs", headers=_headers(CLINICIAN)
    )
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    fetched = client.get(f"/api/agent-runs/{run_id}", headers=_headers(CLINICIAN))
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "awaiting_review"
    assert len(body["draft"]["claims"]) == 2
    assert body["budget"]["steps"] == 4

    commit = client.post(
        f"/api/agent-runs/{run_id}/commit", headers=_headers(CLINICIAN), json={"claims": []}
    )
    assert commit.status_code == 501

    discarded = client.post(f"/api/agent-runs/{run_id}/discard", headers=_headers(CLINICIAN))
    assert discarded.json()["status"] == "discarded"
    again = client.post(f"/api/agent-runs/{run_id}/discard", headers=_headers(CLINICIAN))
    assert again.status_code == 409


def test_api_denies_patients_staff_and_unknown_runs(client):
    path = f"/api/patients/{DEMO_PATIENT_ID}/agent-runs"
    assert client.post(path, headers=_headers(PATIENT)).status_code == 403
    assert client.post(path, headers=_headers(STAFF)).status_code == 403
    assert client.post(path).status_code == 401
    unknown = client.get(f"/api/agent-runs/{uuid4()}", headers=_headers(CLINICIAN))
    assert unknown.status_code == 404


def test_api_validates_revise_and_commit_bodies(client):
    run_id = client.post(
        f"/api/patients/{DEMO_PATIENT_ID}/agent-runs", headers=_headers(CLINICIAN)
    ).json()["run_id"]

    empty = client.post(
        f"/api/agent-runs/{run_id}/review", headers=_headers(CLINICIAN), json={"instruction": ""}
    )
    assert empty.status_code == 422
    revised = client.post(
        f"/api/agent-runs/{run_id}/review",
        headers=_headers(CLINICIAN),
        json={"instruction": "Focus on blood pressure."},
    )
    assert revised.status_code == 202
    assert revised.json()["budget"]["revisions"] == 1
