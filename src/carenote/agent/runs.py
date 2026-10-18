"""Agent run lifecycle: start, observe, revise, commit, discard (design §11, §15).

`AgentRunService` owns the review graph and its checkpointer. It runs graph segments (start and
revise) on a small thread pool so API calls return at once, and reports progress three ways:

- the checkpointed graph state, read back by `get_run`;
- metadata-only audit events, all carrying the run id, through the existing audit chain;
- realtime `agent.step`, `agent.draft` and `agent.done` events to care-team sockets.

Nothing here writes to the record except the injected committer, called from `commit` once a
clinician has submitted decisions from their own session.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from carenote.agent.commit import check_commit_decisions
from carenote.agent.gateway import CassetteMissingError, ModelGateway
from carenote.agent.graph import Committer, GraphDeps, build_review_graph, initial_state
from carenote.agent.persistence import InMemoryRunStore, RunRecord, RunStore
from carenote.agent.state import (
    Budget,
    Citation,
    ClaimDecision,
    DraftReview,
    HighlightDecision,
    ReviewDecisions,
    RunStatus,
    VerificationReport,
)
from carenote.agent.tracing import RunTracer, TimelineStep, trace_id_for
from carenote.domain.models import Actor, AgentActor, AuditEvent, Role
from carenote.services.ai import ExternalModelError, ModelNotConfiguredError
from carenote.services.care_notes import AccessDeniedError, CareNoteService

logger = logging.getLogger(__name__)

Publisher = Callable[[UUID, dict[str, Any]], None]


class RunNotFoundError(LookupError):
    pass


class RunStateError(RuntimeError):
    """The run is not in a state that allows the requested action."""


class CommitUnavailableError(RuntimeError):
    pass


class AgentReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=500)


class AgentCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[ClaimDecision] = Field(default_factory=list)
    highlights: list[HighlightDecision] = Field(default_factory=list)


class BudgetUsage(BaseModel):
    steps: int
    max_steps: int
    revisions: int
    max_revisions: int
    auto_revisions: int = 0
    max_auto_revisions: int = 0
    input_tokens: int
    output_tokens: int
    cost_usd: float


class AgentRunView(BaseModel):
    run_id: UUID
    patient_id: UUID
    status: RunStatus
    stop_reason: str | None = None
    error: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    created_at: datetime
    draft: DraftReview | None = None
    verification: VerificationReport | None = None
    highlight_citations: list[Citation | None] = Field(default_factory=list)
    budget: BudgetUsage | None = None
    # One OpenTelemetry trace per run; its ID is the run ID, as carried by the audit events.
    trace_id: str | None = None
    timeline: list[TimelineStep] = Field(default_factory=list)


_SAFE_ERRORS = (CassetteMissingError, ModelNotConfiguredError, ExternalModelError)
_CLAIM_OPERATIONS = {
    "accept": "agent_claim_accepted",
    "edit": "agent_claim_edited",
    "reject": "agent_claim_rejected",
}


class AgentRunService:
    def __init__(
        self,
        service: CareNoteService,
        gateway: ModelGateway,
        committer: Committer | None = None,
        publish: Publisher | None = None,
        executor: Executor | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        store: RunStore | None = None,
        tracer: RunTracer | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        budget_factory: Callable[[datetime], Budget] = lambda now: Budget(started_at=now),
    ) -> None:
        self.service = service
        self.committer = committer
        self.publish = publish or (lambda patient_id, event: None)
        # Graph segments run here; pass an inline executor in tests for deterministic runs.
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
        self.now = now
        self.budget_factory = budget_factory
        self.deps = GraphDeps(
            service=service,
            gateway=gateway,
            commit=self._commit,
            now=now,
            on_step=self._on_step,
            tracer=tracer or RunTracer(),
        )
        self.graph = build_review_graph(self.deps, checkpointer)
        # Durable registry (Postgres when configured); records are cached here with their locks.
        self.store = store or InMemoryRunStore()
        self._runs: dict[UUID, RunRecord] = {}
        self._registry_lock = threading.Lock()

    # Public API

    def start(self, clinician: Actor, patient_id: UUID) -> AgentRunView:
        # Same read check as opening the patient; then only a clinician may start a run.
        self.service.get_patient_view(clinician, patient_id)
        run_id = uuid4()
        try:
            agent = AgentActor.for_run(clinician, run_id)
        except PermissionError as exc:
            raise AccessDeniedError(str(exc)) from exc
        now = self.now()
        record = RunRecord(
            run_id=run_id,
            patient_id=patient_id,
            clinic_id=clinician.clinic_id,
            started_by=clinician.id,
            agent_id=agent.id,
            created_at=now,
        )
        with self._registry_lock:
            self._runs[run_id] = record
        self.store.save(record)
        self._audit(record, "agent_run_started", clinician.id)
        state = initial_state(run_id, agent, patient_id, now, budget=self.budget_factory(now))
        self._submit(record, state)
        return self.get_run(clinician, run_id)

    def get_run(self, clinician: Actor, run_id: UUID) -> AgentRunView:
        record = self._authorised(clinician, run_id)
        snapshot = self.graph.get_state(self._config(run_id))
        values = snapshot.values
        if record.error is not None:
            status = RunStatus.FAILED
        elif snapshot.next == ("human_review",):
            status = RunStatus.AWAITING_REVIEW
        else:
            status = values.get("status", RunStatus.RUNNING)
        budget = values.get("budget")
        return AgentRunView(
            run_id=run_id,
            patient_id=record.patient_id,
            status=status,
            stop_reason=values.get("stop_reason"),
            error=record.error,
            prompt_version=values.get("prompt_version"),
            model=values.get("model"),
            created_at=record.created_at,
            draft=values.get("draft"),
            verification=values.get("verification"),
            highlight_citations=values.get("highlight_citations") or [],
            budget=_usage(budget) if budget is not None else None,
            trace_id=trace_id_for(run_id),
            timeline=[TimelineStep.model_validate(step) for step in record.timeline],
        )

    def revise(self, clinician: Actor, run_id: UUID, request: AgentReviseRequest) -> AgentRunView:
        decisions = ReviewDecisions(action="revise", revise_instruction=request.instruction)
        record = self._resume_checks(clinician, run_id)
        self._submit(record, self._answer(clinician, decisions))
        return self.get_run(clinician, run_id)

    def commit(self, clinician: Actor, run_id: UUID, request: AgentCommitRequest) -> AgentRunView:
        if self.committer is None:
            raise CommitUnavailableError("committing agent reviews is not available yet")
        decisions = ReviewDecisions(
            action="commit", claims=request.claims, highlights=request.highlights
        )
        record = self._resume_checks(clinician, run_id)
        values = self.graph.get_state(self._config(run_id)).values
        draft = values.get("draft")
        claim_count = len(draft.claims) if draft else 0
        highlight_count = len(draft.candidate_highlights) if draft else 0
        if any(item.claim_index >= claim_count for item in decisions.claims) or any(
            item.highlight_index >= highlight_count for item in decisions.highlights
        ):
            raise ValueError("a decision refers to a claim or highlight the draft does not have")
        check_commit_decisions(
            decisions, values.get("verification"), values.get("highlight_citations") or []
        )
        self._run_segment(record, self._answer(clinician, decisions))
        return self.get_run(clinician, run_id)

    def discard(self, clinician: Actor, run_id: UUID) -> AgentRunView:
        record = self._resume_checks(clinician, run_id)
        self._run_segment(record, self._answer(clinician, ReviewDecisions(action="discard")))
        return self.get_run(clinician, run_id)

    # Graph execution

    def _submit(self, record: RunRecord, payload: Any) -> None:
        self.executor.submit(self._run_segment, record, payload)

    def _run_segment(self, record: RunRecord, payload: Any) -> None:
        with record.lock:
            try:
                for chunk in self.graph.stream(
                    payload, self._config(record.run_id), stream_mode="updates"
                ):
                    self._observe(record, chunk)
            except PermissionError:
                raise
            except Exception as exc:
                record.error = (
                    str(exc) if isinstance(exc, _SAFE_ERRORS) else "the review run failed"
                )
                self.store.save(record)
                logger.exception("agent run %s failed", record.run_id)
                self._audit(record, "agent_run_failed", record.agent_id)
                self._event(record, "agent.done", status=RunStatus.FAILED.value)

    def _observe(self, record: RunRecord, chunk: dict[str, Any]) -> None:
        self._audit_tool_calls(record)
        for node, update in chunk.items():
            if node == "__interrupt__":
                self._event(record, "agent.done", status=RunStatus.AWAITING_REVIEW.value)
                continue
            self._event(record, "agent.step", node=node)
            update = update or {}
            if node == "verify":
                report: VerificationReport = update["verification"]
                self._audit(record, "agent_draft_proposed", record.agent_id)
                self._event(
                    record,
                    "agent.draft",
                    claims=len(report.checks),
                    unsupported=len(report.unsupported),
                    uncovered_conflicts=len(report.uncovered_conflict_ids),
                )
            elif node == "revise":
                self._audit(record, "agent_draft_revised", record.agent_id)
            elif node == "stop":
                self._audit(record, "agent_budget_exceeded", record.agent_id)
                self._event(record, "agent.done", status=RunStatus.BUDGET_EXCEEDED.value)
            elif node == "discard":
                self._audit(record, "agent_run_discarded", self._reviewer_id(record))
                self._event(record, "agent.done", status=RunStatus.DISCARDED.value)
            elif node == "commit":
                self._event(record, "agent.done", status=RunStatus.COMMITTED.value)

    def _commit(self, state, tools) -> None:
        """Graph commit node: persist through the committer, then audit each decision."""
        assert self.committer is not None
        record = self._record(state["run_id"])
        reviewer = state["reviewer"]
        self.committer(state, tools)
        for decision in state["decisions"].claims:
            self._audit(
                record,
                _CLAIM_OPERATIONS[decision.action],
                reviewer.id,
                resource="agent_claim",
                resource_id=f"{record.run_id}:{decision.claim_index}",
            )
        self._audit(record, "agent_run_committed", reviewer.id)

    def _on_step(self, run_id: UUID, step: TimelineStep) -> None:
        record = self._record(run_id)
        if record is None:
            return
        record.timeline.append(step.model_dump(mode="json"))
        self.store.save(record)

    # Checks and helpers

    def _record(self, run_id: UUID) -> RunRecord | None:
        """The cached record, or the stored one after a restart."""
        with self._registry_lock:
            record = self._runs.get(run_id)
            if record is None:
                record = self.store.load(run_id)
                if record is not None:
                    self._runs[run_id] = record
            return record

    def _authorised(self, clinician: Actor, run_id: UUID) -> RunRecord:
        record = self._record(run_id)
        if record is None or record.clinic_id != clinician.clinic_id:
            raise RunNotFoundError("agent run not found")
        if clinician.role is not Role.CLINICIAN:
            raise AccessDeniedError("only clinicians can work with agent runs")
        self.service.get_patient_view(clinician, record.patient_id)
        return record

    def _resume_checks(self, clinician: Actor, run_id: UUID) -> RunRecord:
        record = self._authorised(clinician, run_id)
        if record.lock.locked():
            raise RunStateError("the run is busy")
        snapshot = self.graph.get_state(self._config(run_id))
        if record.error is not None or snapshot.next != ("human_review",):
            raise RunStateError("the run is not waiting for review")
        return record

    @staticmethod
    def _answer(clinician: Actor, decisions: ReviewDecisions) -> Command:
        return Command(
            resume={
                "reviewer": clinician.model_dump(mode="json"),
                "decisions": decisions.model_dump(mode="json"),
            }
        )

    def _reviewer_id(self, record: RunRecord) -> UUID:
        reviewer = self.graph.get_state(self._config(record.run_id)).values.get("reviewer")
        return reviewer.id if reviewer is not None else record.started_by

    def _audit_tool_calls(self, record: RunRecord) -> None:
        tools = self.deps.tools.get(record.run_id)
        if tools is None:
            return
        for call in tools.calls[record.tool_calls_audited :]:
            self._audit(
                record,
                "agent_tool_called",
                record.agent_id,
                resource="agent_tool",
                resource_id=f"{record.run_id}:{call.name}",
            )
        if record.tool_calls_audited != len(tools.calls):
            record.tool_calls_audited = len(tools.calls)
            self.store.save(record)

    def _audit(
        self,
        record: RunRecord,
        operation: str,
        changed_by: UUID,
        resource: str = "agent_run",
        resource_id: str | None = None,
    ) -> None:
        record.audit_version += 1
        self.store.save(record)
        self.service.repository.add_audit_event(
            AuditEvent(
                patient_id=record.patient_id,
                clinic_id=record.clinic_id,
                resource=resource,
                resource_id=resource_id or str(record.run_id),
                version=record.audit_version,
                operation=operation,
                changed_by=changed_by,
            )
        )

    def _event(self, record: RunRecord, kind: str, **fields: Any) -> None:
        self.publish(
            record.patient_id,
            {"type": kind, "run_id": str(record.run_id), "audience": "care_team", **fields},
        )

    @staticmethod
    def _config(run_id: UUID) -> dict[str, Any]:
        return {"configurable": {"thread_id": str(run_id)}}


def _usage(budget: Budget) -> BudgetUsage:
    return BudgetUsage(
        steps=budget.steps,
        max_steps=budget.max_steps,
        revisions=budget.revisions,
        max_revisions=budget.max_revisions,
        auto_revisions=budget.auto_revisions,
        max_auto_revisions=budget.max_auto_revisions,
        input_tokens=budget.input_tokens,
        output_tokens=budget.output_tokens,
        cost_usd=budget.cost_usd,
    )
