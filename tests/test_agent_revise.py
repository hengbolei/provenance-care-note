"""Automatic revision: verifier failures go back to the model, at most max_auto_revisions times."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agent_fakes import ScriptedGateway

from carenote.agent.gateway import DataTag, ModelResult
from carenote.agent.graph import REVISE_PROMPT_VERSION, GraphDeps, build_review_graph, initial_state
from carenote.agent.prompts import load_prompt
from carenote.agent.runs import AgentRunService
from carenote.agent.state import Budget, Citation, Claim, RunStatus
from carenote.data.seed import (
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_PATIENT_ID,
    seed_demo_data,
)
from carenote.domain.models import Actor, AgentActor, RiskLevel, Role
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
PATIENT_NAME = "Maya Chen (Synthetic)"


def _bad_claim(draft):
    good = draft.claims[0].citations[0]
    return Claim(
        text=f"{PATIENT_NAME} stopped lisinopril tonight.",
        category="medication",
        risk_level=RiskLevel.MODERATE,
        citations=[
            Citation(entry_id=good.entry_id, start=0, end=10, quote="stopped lisinopril tonight")
        ],
    )


class RevisingModel(ScriptedGateway):
    """First draft adds a misquoted claim; how revisions respond is set per test."""

    def __init__(self, fixes=True, drop_conflict_claims=False):
        super().__init__()
        self.fixes = fixes
        self.drop_conflict_claims = drop_conflict_claims

    def structured(self, request, schema):
        result = super().structured(request, schema)
        draft = result.output
        claims = list(draft.claims)
        if self.drop_conflict_claims and request.purpose == "agent_draft":
            # A verified claim that names no conflicting medicine.
            claims = [
                Claim(
                    text="Home blood pressure readings are being monitored.",
                    category="symptom",
                    risk_level=RiskLevel.LOW,
                    citations=[draft.claims[0].citations[0]],
                )
            ]
        elif request.purpose == "agent_draft" or not self.fixes:
            claims.append(_bad_claim(draft))
        return ModelResult(
            output=draft.model_copy(update={"claims": claims}),
            model=result.model,
            usage=result.usage,
        )


@pytest.fixture
def service():
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    return CareNoteService(repository)


def _run(service, model, budget=None):
    deps = GraphDeps(
        service=service, gateway=model, commit=lambda state, tools: None, now=lambda: NOW
    )
    graph = build_review_graph(deps)
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
    return graph.get_state(config)


def _purposes(model):
    return [request.purpose for request in model.requests]


def test_a_failing_draft_is_revised_and_then_verifies(service):
    model = RevisingModel()

    snapshot = _run(service, model)
    state = snapshot.values

    assert snapshot.next == ("human_review",)
    assert _purposes(model) == ["agent_draft", "agent_revise"]
    assert len(state["initial_verification"].unsupported) == 1
    assert state["verification"].passed
    assert state["budget"].auto_revisions == 1
    # load_context, check_conflicts, draft, verify, revise, verify
    assert state["budget"].steps == 6


def test_the_revise_request_carries_the_verifiers_findings_as_redacted_data(service):
    model = RevisingModel()

    _run(service, model)

    revise = model.requests[1]
    assert revise.instructions == load_prompt(REVISE_PROMPT_VERSION)
    tags = [item for item in revise.inputs if isinstance(item, DataTag)]
    previous = [
        dict(tag.attributes) for tag in tags if tag.name == "previous_claim" and not tag.closing
    ]
    failures = [dict(tag.attributes) for tag in tags if tag.name == "failure" and not tag.closing]
    assert [claim["verified"] for claim in previous] == [True, True, False]
    assert [str(failure["reason"]) for failure in failures] == ["quote_mismatch"]
    assert any(tag.name == "citation" for tag in tags)
    assert PATIENT_NAME not in revise.input_text
    assert "[REDACTED_NAME] stopped lisinopril tonight." in revise.input_text


def test_revision_stops_at_the_limit_and_leaves_failures_for_the_clinician(service):
    model = RevisingModel(fixes=False)

    snapshot = _run(service, model)
    state = snapshot.values

    assert snapshot.next == ("human_review",)
    assert _purposes(model) == ["agent_draft", "agent_revise", "agent_revise"]
    assert state["budget"].auto_revisions == 2
    assert len(state["verification"].unsupported) == 1


def test_an_uncovered_conflict_triggers_revision(service):
    model = RevisingModel(drop_conflict_claims=True)

    state = _run(service, model).values

    assert state["initial_verification"].uncovered_conflict_ids
    assert _purposes(model) == ["agent_draft", "agent_revise"]
    assert state["verification"].passed


def test_a_draft_that_verifies_is_not_revised(service):
    model = ScriptedGateway()

    state = _run(service, model).values

    assert _purposes(model) == ["agent_draft"]
    assert state["budget"].auto_revisions == 0
    assert state["initial_verification"] == state["verification"]


def test_automatic_revision_can_be_switched_off(service):
    model = RevisingModel()

    state = _run(service, model, Budget(started_at=NOW, max_auto_revisions=0)).values

    assert _purposes(model) == ["agent_draft"]
    assert len(state["verification"].unsupported) == 1


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


def test_revisions_are_audited_and_reported_in_the_run_view(service):
    runs = AgentRunService(service, RevisingModel(), executor=InlineExecutor(), now=lambda: NOW)

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    assert view.status is RunStatus.AWAITING_REVIEW
    assert view.budget.auto_revisions == 1
    assert view.budget.max_auto_revisions == 2
    operations = [event.operation for event in service.repository.audit_events]
    assert operations.count("agent_draft_revised") == 1
    assert operations.count("agent_draft_proposed") == 2
