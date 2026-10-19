from datetime import UTC, datetime

import pytest
from agent_fakes import ScriptedGateway
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_agent_revise import RevisingModel

from carenote.agent.commit import commit_review
from carenote.agent.runs import AgentCommitRequest, AgentRunService
from carenote.agent.tracing import RunTracer, configure_tracing
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID, seed_demo_data
from carenote.domain.models import Actor, Role
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CLINICIAN = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


def _runs(exporter, model=None):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    service = CareNoteService(repository)
    return AgentRunService(
        service,
        model or ScriptedGateway(),
        committer=commit_review(service),
        executor=InlineExecutor(),
        tracer=RunTracer(provider.get_tracer("test")),
        now=lambda: NOW,
    )


def _spans(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


def test_a_run_is_one_trace_whose_id_is_the_run_id(exporter):
    runs = _runs(exporter)
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)
    runs.commit(
        CLINICIAN,
        view.run_id,
        AgentCommitRequest.model_validate({"claims": [{"claim_index": 0, "action": "accept"}]}),
    )

    spans = exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert {"agent.load_context", "agent.check_conflicts", "agent.draft", "agent.verify"} <= names
    assert {
        "agent.model",
        "agent.commit",
        "agent.tool.list_entries",
        "agent.tool.read_entry",
    } <= names
    assert {span.context.trace_id for span in spans} == {view.run_id.int}
    assert view.trace_id == view.run_id.hex


def test_model_spans_carry_model_tokens_and_prompt(exporter):
    runs = _runs(exporter)
    runs.start(CLINICIAN, DEMO_PATIENT_ID)

    model = _spans(exporter)["agent.model"]
    draft = _spans(exporter)["agent.draft"]
    assert model.attributes["gen_ai.response.model"] == "scripted"
    assert model.attributes["gen_ai.usage.input_tokens"] == 900
    assert model.attributes["gen_ai.usage.output_tokens"] == 300
    assert model.attributes["carenote.prompt"] == "draft_v3"
    assert model.attributes["carenote.purpose"] == "agent_draft"
    assert model.parent.span_id == draft.context.span_id
    assert _spans(exporter)["agent.verify"].attributes["carenote.unsupported_claims"] == 0


def test_spans_never_carry_record_or_claim_text(exporter):
    runs = _runs(exporter)
    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    values = " ".join(
        str(value) for span in exporter.get_finished_spans() for value in span.attributes.values()
    ).lower()
    assert "lisinopril" not in values
    assert "maya chen" not in values
    for claim in view.draft.claims:
        assert claim.text.lower() not in values


def test_the_run_view_carries_a_metadata_only_timeline(exporter):
    runs = _runs(exporter)

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    steps = {step.node: step for step in view.timeline}
    assert [step.node for step in view.timeline] == [
        "load_context",
        "check_conflicts",
        "plan",
        "retrieve",
        "draft",
        "verify",
    ]
    assert steps["draft"].model == "scripted"
    assert steps["draft"].prompt == "draft_v3"
    assert steps["draft"].input_tokens + steps["draft"].output_tokens == 1200
    assert steps["load_context"].tool_calls == 1
    assert steps["plan"].prompt == "plan_v2"
    assert steps["retrieve"].tool_calls >= 3
    assert steps["verify"].unsupported_claims == 0
    assert all(step.duration_ms >= 0 for step in view.timeline)


def test_automatic_revisions_appear_in_the_timeline_and_trace(exporter):
    runs = _runs(exporter, RevisingModel())

    view = runs.start(CLINICIAN, DEMO_PATIENT_ID)

    assert [step.node for step in view.timeline] == [
        "load_context",
        "check_conflicts",
        "plan",
        "retrieve",
        "draft",
        "verify",
        "revise",
        "verify",
    ]
    revise = next(step for step in view.timeline if step.node == "revise")
    assert revise.prompt == "revise_v2"
    assert [step.unsupported_claims for step in view.timeline if step.node == "verify"] == [1, 0]
    assert "agent.revise" in _spans(exporter)


def test_tracing_export_is_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)

    assert configure_tracing() is False
