"""LangGraph state machine for the Clinical Review Agent (design §6, phase 1 subset).

    load_context → check_conflicts → draft → verify → human_review ─┬→ commit
                                       ↑                             ├→ discard
                                       └──── revise (instruction) ───┘

Only `draft` calls a model. Every other node is deterministic. The run pauses at `human_review`
with a LangGraph `interrupt`, so its state is checkpointed until a clinician answers; nothing
reaches the record except through the injected `commit` callable, which runs only after a
clinician's decisions. Each node counts as one step against the run's `Budget`; when a ceiling is
exceeded the run stops with its partial draft and a visible reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from carenote.agent.gateway import DataTag, ModelGateway, ModelRequest, Usage
from carenote.agent.prompts import load_prompt
from carenote.agent.state import (
    Budget,
    DraftReview,
    ReviewDecisions,
    ReviewState,
    RunStatus,
)
from carenote.agent.tools import ReviewTools
from carenote.agent.verify import resolve_citation, verify_draft
from carenote.domain.models import Actor, AgentActor, Role
from carenote.services.care_notes import CareNoteService

PROMPT_VERSION = "draft_v1"
MAX_CONTEXT_ENTRIES = 40

Committer = Callable[[ReviewState, ReviewTools], None]


@dataclass
class GraphDeps:
    service: CareNoteService
    gateway: ModelGateway
    commit: Committer
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    prompt_version: str = PROMPT_VERSION
    max_context_entries: int = MAX_CONTEXT_ENTRIES
    # Tools cache per run; rebuilt from the service after a restart, since they hold no state that
    # cannot be recomputed (the patient-view snapshot and per-entry redactions).
    tools: dict[UUID, ReviewTools] = field(default_factory=dict)

    def tools_for(self, state: ReviewState) -> ReviewTools:
        run_id = state["run_id"]
        if run_id not in self.tools:
            self.tools[run_id] = ReviewTools(self.service, state["actor"], state["patient_id"])
        return self.tools[run_id]


def initial_state(
    run_id: UUID,
    actor: AgentActor,
    patient_id: UUID,
    started_at: datetime,
    budget: Budget | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> ReviewState:
    return ReviewState(
        run_id=run_id,
        actor=actor,
        patient_id=patient_id,
        status=RunStatus.RUNNING,
        stop_reason=None,
        prompt_version=prompt_version,
        context=[],
        conflicts=[],
        questions=[],
        evidence={},
        draft=None,
        verification=None,
        highlight_citations=[],
        budget=budget or Budget(started_at=started_at),
        decisions=None,
        reviewer=None,
    )


def build_review_graph(
    deps: GraphDeps, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    def counted(node: Callable[[ReviewState], dict[str, Any]]):
        def run(state: ReviewState) -> dict[str, Any]:
            budget = state["budget"].model_copy(update={"steps": state["budget"].steps + 1})
            update = node({**state, "budget": budget})
            update.setdefault("budget", budget)
            return update

        run.__name__ = node.__name__
        return run

    def load_context(state: ReviewState) -> dict[str, Any]:
        tools = deps.tools_for(state)
        return {
            "status": RunStatus.RUNNING,
            "context": tools.list_entries(limit=deps.max_context_entries),
        }

    def check_conflicts(state: ReviewState) -> dict[str, Any]:
        return {"conflicts": deps.tools_for(state).detect_conflicts()}

    def draft(state: ReviewState) -> dict[str, Any]:
        tools = deps.tools_for(state)
        names = tools.known_names()
        inputs: list[Any] = []
        for ref in state["context"]:
            tools.read_entry(ref.entry_id)
            inputs += [
                DataTag.open(
                    "entry",
                    id=ref.entry_id,
                    type=ref.type,
                    author=ref.author_role,
                    at=ref.timestamp,
                    internal=ref.internal,
                ),
                tools.redactions[ref.entry_id],
                DataTag.close("entry"),
            ]
        for conflict in state["conflicts"]:
            inputs += [
                DataTag.open("conflict", id=conflict.id, category=conflict.category),
                tools.redactor.redact(conflict.summary, names),
                DataTag.close("conflict"),
            ]
        budget = state["budget"]
        decisions = state["decisions"]
        if decisions is not None and decisions.action == "revise":
            inputs += [
                DataTag.open("clinician_instruction"),
                tools.redactor.redact(decisions.revise_instruction or "", names),
                DataTag.close("clinician_instruction"),
            ]
            budget = budget.model_copy(update={"revisions": budget.revisions + 1})
        result = deps.gateway.structured(
            ModelRequest(
                purpose="agent_draft",
                instructions=load_prompt(state["prompt_version"]),
                inputs=inputs,
                safety_subject=str(state["actor"].principal.id),
            ),
            DraftReview,
        )
        return {
            "draft": result.output,
            "budget": _charge(budget, result.usage),
            "decisions": None,
        }

    def verify(state: ReviewState) -> dict[str, Any]:
        tools = deps.tools_for(state)
        entries = tools.context_entries()
        draft_review = state["draft"] or DraftReview(claims=[])
        return {
            "verification": verify_draft(
                draft_review, entries, state["conflicts"], tools.redactions
            ),
            "highlight_citations": [
                resolve_citation(item.citation, entries, tools.redactions)
                for item in draft_review.candidate_highlights
            ],
        }

    def human_review(state: ReviewState) -> dict[str, Any]:
        answer = interrupt(
            {
                "run_id": str(state["run_id"]),
                "unsupported_claims": len(state["verification"].unsupported)
                if state["verification"]
                else 0,
                "stop_reason": state["stop_reason"],
            }
        )
        reviewer = Actor.model_validate(answer["reviewer"])
        if reviewer.role is not Role.CLINICIAN or reviewer.clinic_id != state["actor"].clinic_id:
            raise PermissionError("only a clinician in the patient's clinic can review this run")
        decisions = ReviewDecisions.model_validate(answer["decisions"])
        # The wall-clock ceiling covers automated work, not the time a clinician spends reviewing.
        budget = state["budget"].model_copy(update={"started_at": deps.now()})
        stop_reason = None
        if decisions.action == "revise" and not budget.can_revise:
            stop_reason = f"revision limit of {budget.max_revisions} reached"
            decisions = None
        return {
            "status": RunStatus.RUNNING,
            "decisions": decisions,
            "reviewer": reviewer,
            "budget": budget,
            "stop_reason": stop_reason,
        }

    def commit(state: ReviewState) -> dict[str, Any]:
        deps.commit(state, deps.tools_for(state))
        deps.tools.pop(state["run_id"], None)
        return {"status": RunStatus.COMMITTED}

    def discard(state: ReviewState) -> dict[str, Any]:
        deps.tools.pop(state["run_id"], None)
        return {"status": RunStatus.DISCARDED}

    def stop(state: ReviewState) -> dict[str, Any]:
        deps.tools.pop(state["run_id"], None)
        return {
            "status": RunStatus.BUDGET_EXCEEDED,
            "stop_reason": state["budget"].exceeded(deps.now()),
        }

    def within_budget(next_node: str) -> Callable[[ReviewState], str]:
        def route(state: ReviewState) -> str:
            return "stop" if state["budget"].exceeded(deps.now()) else next_node

        return route

    def after_review(state: ReviewState) -> str:
        decisions = state["decisions"]
        if decisions is None:
            return "human_review"
        if decisions.action == "revise":
            return "draft"
        return decisions.action

    graph = StateGraph(ReviewState)
    for node in (load_context, check_conflicts, draft, verify, human_review, commit, discard):
        graph.add_node(node.__name__, counted(node))
    graph.add_node("stop", stop)

    graph.add_edge(START, "load_context")
    for source, target in (
        ("load_context", "check_conflicts"),
        ("check_conflicts", "draft"),
        ("draft", "verify"),
        ("verify", "human_review"),
    ):
        graph.add_conditional_edges(source, within_budget(target), [target, "stop"])
    graph.add_conditional_edges(
        "human_review", after_review, ["commit", "discard", "draft", "human_review"]
    )
    for terminal in ("commit", "discard", "stop"):
        graph.add_edge(terminal, END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _charge(budget: Budget, usage: Usage) -> Budget:
    return budget.model_copy(
        update={
            "input_tokens": budget.input_tokens + usage.input_tokens,
            "output_tokens": budget.output_tokens + usage.output_tokens,
            "cost_usd": budget.cost_usd + (usage.cost_usd or 0),
        }
    )
