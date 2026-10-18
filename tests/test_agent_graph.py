from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agent_fakes import ScriptedGateway
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from carenote.agent.gateway import RecordedGateway
from carenote.agent.graph import (
    GraphDeps,
    build_review_graph,
    checkpoint_serializer,
    initial_state,
)
from carenote.agent.state import Budget, DraftReview, RunStatus
from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    seed_demo_data,
)
from carenote.domain.models import Actor, AgentActor, Role
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)


@pytest.fixture
def service():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    return CareNoteService(repository)


class Committer:
    def __init__(self):
        self.calls = []

    def __call__(self, state, tools):
        self.calls.append(state)


def _run(service, gateway, *, budget=None, checkpointer=None, committer=None):
    committer = committer or Committer()
    deps = GraphDeps(service=service, gateway=gateway, commit=committer, now=lambda: NOW)
    graph = build_review_graph(deps, checkpointer)
    run_id = uuid4()
    config = {"configurable": {"thread_id": str(run_id)}}
    graph.invoke(
        initial_state(
            run_id,
            AgentActor.for_run(CLINICIAN, run_id),
            DEMO_PATIENT_ID,
            NOW,
            budget=budget or Budget(started_at=NOW),
        ),
        config,
    )
    return graph, config, committer


def _answer(action="commit", reviewer=CLINICIAN, **decisions):
    return Command(
        resume={
            "reviewer": reviewer.model_dump(mode="json"),
            "decisions": {"action": action, **decisions},
        }
    )


def test_run_drafts_verifies_and_pauses_for_human_review(service):
    gateway = ScriptedGateway()
    graph, config, committer = _run(service, gateway)

    snapshot = graph.get_state(config)
    state = snapshot.values
    assert snapshot.next == ("human_review",)
    assert len(state["context"]) == 6
    assert {conflict.entity for conflict in state["conflicts"]} == {"lisinopril"}
    assert state["verification"].passed, [c.detail for c in state["verification"].checks]
    assert state["highlight_citations"][0].quote == "Hold morning lisinopril"
    # load_context, check_conflicts, draft, verify; human_review counts once it completes.
    assert state["budget"].steps == 4
    assert (state["budget"].input_tokens, state["budget"].output_tokens) == (900, 300)
    assert committer.calls == []


def test_record_text_reaches_the_model_inside_data_tags(service):
    gateway = ScriptedGateway()
    _run(service, gateway)

    [request] = gateway.requests
    text = request.input_text
    assert text.startswith("<entry id=")
    assert text.count("<entry ") == text.count("</entry>") == 6
    assert text.count("<conflict ") == 2
    assert "untrusted record content" in request.instructions
    assert request.safety_subject == str(DEMO_CLINICIAN_ID)


def test_commit_runs_only_after_a_clinician_answers(service):
    graph, config, committer = _run(service, ScriptedGateway())

    graph.invoke(_answer(claims=[{"claim_index": 0, "action": "accept"}], highlights=[]), config)

    state = graph.get_state(config).values
    assert state["status"] is RunStatus.COMMITTED
    [committed] = committer.calls
    assert committed["reviewer"] == CLINICIAN
    assert committed["decisions"].claims[0].action == "accept"


def test_discard_ends_the_run_without_committing(service):
    graph, config, committer = _run(service, ScriptedGateway())

    graph.invoke(_answer("discard"), config)

    assert graph.get_state(config).values["status"] is RunStatus.DISCARDED
    assert committer.calls == []


def test_revise_redrafts_with_the_clinician_instruction(service):
    gateway = ScriptedGateway()
    graph, config, _ = _run(service, gateway)

    graph.invoke(_answer("revise", revise_instruction="Focus on blood pressure."), config)

    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_review",)
    assert len(gateway.requests) == 2
    assert "<clinician_instruction>" in gateway.requests[1].input_text
    assert "Focus on blood pressure." in gateway.requests[1].input_text
    assert snapshot.values["budget"].revisions == 1


def test_revise_beyond_the_limit_returns_to_review_with_a_reason(service):
    gateway = ScriptedGateway()
    graph, config, _ = _run(service, gateway, budget=Budget(started_at=NOW, max_revisions=0))

    graph.invoke(_answer("revise", revise_instruction="Try again."), config)

    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_review",)
    assert snapshot.values["stop_reason"] == "revision limit of 0 reached"
    assert len(gateway.requests) == 1


@pytest.mark.parametrize(
    "reviewer",
    [
        Actor(id=uuid4(), role=Role.STAFF, clinic_id=DEMO_CLINIC_ID),
        Actor(id=uuid4(), role=Role.CLINICIAN, clinic_id=uuid4()),
    ],
    ids=["staff", "other-clinic"],
)
def test_only_a_clinician_in_the_clinic_can_answer(service, reviewer):
    graph, config, committer = _run(service, ScriptedGateway())

    with pytest.raises(PermissionError):
        graph.invoke(_answer(reviewer=reviewer), config)
    assert graph.get_state(config).next == ("human_review",)
    assert committer.calls == []


def test_exceeding_the_budget_stops_with_the_partial_draft(service):
    graph, config, _ = _run(
        service,
        ScriptedGateway(tokens=(5_000, 1_000)),
        budget=Budget(started_at=NOW, max_tokens=4_000),
    )

    snapshot = graph.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["status"] is RunStatus.BUDGET_EXCEEDED
    assert snapshot.values["stop_reason"] == "token limit of 4000 exceeded"
    assert snapshot.values["draft"] is not None


def test_a_paused_run_resumes_after_a_restart(service):
    checkpointer = InMemorySaver(serde=checkpoint_serializer())
    _, config, _ = _run(service, ScriptedGateway(), checkpointer=checkpointer)

    committer = Committer()
    restarted = build_review_graph(
        GraphDeps(service=service, gateway=ScriptedGateway(), commit=committer, now=lambda: NOW),
        checkpointer,
    )
    restarted.invoke(_answer(), config)

    assert restarted.get_state(config).values["status"] is RunStatus.COMMITTED
    assert len(committer.calls) == 1


def test_recorded_cassettes_replay_the_same_run_offline(service, tmp_path):
    live = ScriptedGateway()
    recorded_graph, recorded_config, _ = _run(service, RecordedGateway(tmp_path, record_with=live))
    replay_graph, replay_config, _ = _run(service, RecordedGateway(tmp_path))

    recorded = recorded_graph.get_state(recorded_config).values["draft"]
    replayed = replay_graph.get_state(replay_config).values["draft"]
    assert isinstance(replayed, DraftReview)
    assert replayed == recorded
    assert len(live.requests) == 1
