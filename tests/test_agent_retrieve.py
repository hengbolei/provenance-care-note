"""The plan and retrieve nodes: questions, evidence, and what reaches the draft."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agent_fakes import ScriptedGateway

from carenote.agent.graph import (
    STANDING_QUESTIONS,
    GraphDeps,
    build_review_graph,
    initial_state,
)
from carenote.agent.state import QuestionSource
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID, seed_demo_data
from carenote.domain.models import Actor, AgentActor, Role
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)


@pytest.fixture
def repository():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    return repository


def _state(repository, gateway, max_context_entries=40):
    deps = GraphDeps(
        service=CareNoteService(repository),
        gateway=gateway,
        commit=lambda state, tools: None,
        now=lambda: NOW,
        max_context_entries=max_context_entries,
    )
    graph = build_review_graph(deps)
    run_id = uuid4()
    config = {"configurable": {"thread_id": str(run_id)}}
    graph.invoke(
        initial_state(run_id, AgentActor.for_run(CLINICIAN, run_id), DEMO_PATIENT_ID, NOW),
        config,
    )
    return graph.get_state(config).values


def test_questions_are_standing_then_conflicts_then_the_models(repository):
    gateway = ScriptedGateway(
        plan_questions=[
            ("Has the blood pressure check been done?", "blood pressure check"),
            # Repeats a standing question, so it is dropped.
            (STANDING_QUESTIONS[0].question, "anything"),
        ]
    )

    state = _state(repository, gateway)

    sources = [item.source for item in state["questions"]]
    assert sources == [
        *[QuestionSource.STANDING] * len(STANDING_QUESTIONS),
        QuestionSource.CONFLICT,
        QuestionSource.MODEL,
    ]
    assert state["questions"][len(STANDING_QUESTIONS)].query == "lisinopril"
    assert set(state["evidence"]) == {item.question for item in state["questions"]}
    hits = state["evidence"]["Has the blood pressure check been done?"]
    assert hits and all("blood pressure" in hit.text.lower() for hit in hits[:1])


def test_the_plan_request_sees_standing_questions_and_no_identifiers(repository):
    gateway = ScriptedGateway()

    _state(repository, gateway)

    [plan] = gateway.plan_requests
    assert plan.purpose == "agent_plan"
    assert plan.input_text.count("<standing_question>") == len(STANDING_QUESTIONS) + 1
    assert "Maya Chen" not in plan.input_text


def test_the_draft_request_carries_every_question_with_its_source(repository):
    gateway = ScriptedGateway(plan_questions=[("Is the dizziness resolving?", "dizziness")])

    state = _state(repository, gateway)

    [draft] = gateway.requests
    assert draft.input_text.count("<question source=") == len(state["questions"])
    assert '<question source="model">' in draft.input_text
    assert "Is the dizziness resolving?" in draft.input_text


def test_retrieval_swaps_older_evidence_into_a_full_context_window(repository):
    entries = repository.list_entries(DEMO_PATIENT_ID, DEMO_CLINIC_ID)
    newest_two = {entry.id for entry in entries[:2]}
    gateway = ScriptedGateway()

    state = _state(repository, gateway, max_context_entries=4)

    context = [ref.entry_id for ref in state["context"]]
    conflicting = {entry_id for c in state["conflicts"] for entry_id in c.entry_ids}
    assert len(context) == 4
    # Half the window stays most recent; the other half is older retrieved evidence, with the
    # conflicting entries first.
    assert newest_two <= set(context)
    assert conflicting - newest_two <= set(context)
    timestamps = [ref.timestamp for ref in state["context"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_a_question_nothing_answers_keeps_empty_evidence(repository):
    gateway = ScriptedGateway(plan_questions=[("Any falls?", "fall")])

    state = _state(repository, gateway)

    assert state["evidence"]["Any falls?"] == []
