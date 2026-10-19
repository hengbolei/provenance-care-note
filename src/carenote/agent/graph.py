"""LangGraph state machine for the Clinical Review Agent (design §6).

    load_context → check_conflicts → plan → retrieve → draft → verify ──→ human_review ─┬→ commit
                                                         ↑      ↑   │                     ├→ discard
                                                         │      └ revise (failures)       │
                                                         └──────── redraft (clinician instruction)

Only `plan`, `draft` and `revise` call a model; every other node is deterministic. `plan` adds the
model's questions to standing ones and one per deterministic conflict; `retrieve` searches the
whole record for each question and swaps the best older hits, and every conflicting entry, into the
most-recent context window, so evidence outside the window still reaches the draft. When verification
fails, `revise` sends the verifier's reasons back to the model and the corrected draft is verified
again, at most `max_auto_revisions` times; anything still failing is shown to the clinician as
unsupported. The run pauses at `human_review` with a LangGraph `interrupt`, so its state is
checkpointed until a clinician answers; nothing reaches the record except through the injected
`commit` callable, which runs only after a clinician's decisions. Each automated node is one step
against the run's `Budget`; when a ceiling is exceeded the run stops with its partial draft and a
visible reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from carenote.agent.gateway import DataTag, ModelGateway, ModelRequest, Usage
from carenote.agent.injection import flagged_passages, withhold_echoes
from carenote.agent.prompts import load_prompt
from carenote.agent.state import (
    Budget,
    CandidateHighlight,
    CheckFailure,
    Citation,
    Claim,
    ClaimCheck,
    ClaimDecision,
    DraftReview,
    HighlightDecision,
    QuestionSource,
    ReviewDecisions,
    ReviewPlan,
    ReviewQuestion,
    ReviewState,
    RunStatus,
    VerificationReport,
    WithheldItem,
)
from carenote.agent.tools import ReviewTools
from carenote.agent.tracing import RunTracer, TimelineStep, TracingGateway, timeline_step
from carenote.agent.verify import resolve_citation, verify_draft
from carenote.domain.models import (
    Actor,
    AgentActor,
    Conflict,
    ConflictCategory,
    ConflictStatus,
    EntryRef,
    EntryType,
    RiskLevel,
    Role,
    Span,
)
from carenote.services.care_notes import CareNoteService

PROMPT_VERSION = "draft_v3"
PLAN_PROMPT_VERSION = "plan_v2"
REVISE_PROMPT_VERSION = "revise_v2"
MAX_CONTEXT_ENTRIES = 40
# Hits kept per question, and the share of the context window older retrieved entries may take.
RETRIEVE_K = 5
MAX_RETRIEVED_SHARE = 0.5
MAX_CONFLICT_QUESTIONS = 6

# Asked on every run, so retrieval works even when the model asks nothing useful.
STANDING_QUESTIONS = (
    ReviewQuestion(
        question="Which medications were started, stopped, held or changed?",
        query="started stopped hold held increased reduced changed switched continue dose",
        source=QuestionSource.STANDING,
    ),
    ReviewQuestion(
        question="Which allergies or reactions are recorded?",
        query="allergy reaction rash swelling anaphylaxis intolerance",
        source=QuestionSource.STANDING,
    ),
    ReviewQuestion(
        question="Which follow-ups, referrals or tests are planned or outstanding?",
        query="follow arrange referral booked repeat recheck pending outstanding",
        source=QuestionSource.STANDING,
    ),
)

Committer = Callable[[ReviewState, ReviewTools], None]

# Every type the review state carries. Checkpoints deserialize only these (plus LangGraph's
# built-in safe types), so a tampered or foreign checkpoint cannot instantiate anything else.
CHECKPOINT_TYPES = (
    Actor,
    AgentActor,
    Budget,
    CandidateHighlight,
    CheckFailure,
    Citation,
    Claim,
    ClaimCheck,
    ClaimDecision,
    Conflict,
    ConflictCategory,
    ConflictStatus,
    DraftReview,
    EntryRef,
    EntryType,
    HighlightDecision,
    QuestionSource,
    ReviewDecisions,
    ReviewQuestion,
    RiskLevel,
    Role,
    RunStatus,
    Span,
    VerificationReport,
    WithheldItem,
)


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[(item.__module__, item.__name__) for item in CHECKPOINT_TYPES]
    )


@dataclass
class GraphDeps:
    service: CareNoteService
    gateway: ModelGateway
    commit: Committer
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    prompt_version: str = PROMPT_VERSION
    max_context_entries: int = MAX_CONTEXT_ENTRIES
    tracer: RunTracer = field(default_factory=RunTracer)
    # Receives a metadata-only timeline step as each node finishes.
    on_step: Callable[[UUID, TimelineStep], None] | None = None
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
        model=None,
        context=[],
        conflicts=[],
        questions=[],
        evidence={},
        draft=None,
        verification=None,
        initial_verification=None,
        highlight_citations=[],
        budget=budget or Budget(started_at=started_at),
        decisions=None,
        reviewer=None,
    )


def build_review_graph(
    deps: GraphDeps, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    model = TracingGateway(deps.gateway, deps.tracer)

    def traced(node: Callable[[ReviewState], dict[str, Any]], prompt=None):
        """Run the node inside its span and report a timeline step when it finishes."""

        def run(state: ReviewState) -> dict[str, Any]:
            run_id = state["run_id"]
            tools = deps.tools.get(run_id)
            calls_before = len(tools.calls) if tools else 0
            with deps.tracer.node(run_id, node.__name__, prompt(state) if prompt else None) as step:
                update = node(state)
                tools = deps.tools.get(run_id)
                if tools is not None:
                    step.tool_calls = [
                        (call.name, call.result_count) for call in tools.calls[calls_before:]
                    ]
                report = update.get("verification")
                if report is not None:
                    step.unsupported_claims = len(report.unsupported)
            if deps.on_step is not None:
                deps.on_step(run_id, timeline_step(step))
            return update

        run.__name__ = node.__name__
        return run

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

    def plan(state: ReviewState) -> dict[str, Any]:
        """Standing and conflict questions, plus what the model asks after reading the recent record."""
        tools = deps.tools_for(state)
        names = tools.known_names()
        # Two conflicts about the same medicine ask one question.
        known = {
            item.question: item
            for item in (
                *STANDING_QUESTIONS,
                *(
                    ReviewQuestion(
                        question=f"What does the record say about {conflict.entity}?",
                        query=conflict.entity,
                        source=QuestionSource.CONFLICT,
                    )
                    for conflict in state["conflicts"][:MAX_CONFLICT_QUESTIONS]
                ),
            )
        }
        inputs = record_inputs(state)
        for item in known.values():
            inputs += [
                DataTag.open("standing_question"),
                tools.redactor.redact(item.question, names),
                DataTag.close("standing_question"),
            ]
        result = model.structured(
            ModelRequest(
                purpose="agent_plan",
                instructions=load_prompt(PLAN_PROMPT_VERSION),
                inputs=inputs,
                safety_subject=str(state["actor"].principal.id),
            ),
            ReviewPlan,
        )
        questions = dict(known)
        for item in result.output.questions:
            questions.setdefault(
                item.question,
                ReviewQuestion(
                    question=item.question, query=item.query, source=QuestionSource.MODEL
                ),
            )
        return {
            "questions": list(questions.values()),
            "budget": _charge(state["budget"], result.usage),
        }

    def retrieve(state: ReviewState) -> dict[str, Any]:
        """Search the whole record per question and widen the context with what is found."""
        tools = deps.tools_for(state)
        entries = tools.context_entries()
        evidence = {
            item.question: tools.search_entries(item.query, k=RETRIEVE_K)
            for item in state["questions"]
        }
        # Conflicting entries first, then hits round-robin by rank, so every question's best hit
        # is kept before any question's second best.
        found = [entry_id for conflict in state["conflicts"] for entry_id in conflict.entry_ids]
        for rank in range(RETRIEVE_K):
            found += [spans[rank].entry_id for spans in evidence.values() if rank < len(spans)]
        recent = [ref.entry_id for ref in state["context"]]
        older = [
            entry_id
            for entry_id in dict.fromkeys(found)
            if entry_id not in recent and entry_id in entries
        ][: int(deps.max_context_entries * MAX_RETRIEVED_SHARE)]
        chosen = recent[: deps.max_context_entries - len(older)] + older
        context = sorted(
            (EntryRef.from_entry(entries[entry_id]) for entry_id in chosen),
            key=lambda ref: ref.timestamp,
            reverse=True,
        )
        return {"evidence": evidence, "context": context}

    def record_inputs(state: ReviewState) -> list[Any]:
        """The redacted record, framed as data: entries, then deterministic conflicts."""
        tools = deps.tools_for(state)
        names = tools.known_names()
        inputs: list[Any] = []
        for ref in state["context"]:
            tools.read_entry(ref.entry_id)
            flags = {"instruction_like": True} if _flagged(tools, ref.entry_id) else {}
            inputs += [
                DataTag.open(
                    "entry",
                    id=ref.entry_id,
                    type=ref.type,
                    author=ref.author_role,
                    at=ref.timestamp,
                    internal=ref.internal,
                    **flags,
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
        return inputs

    def draft(state: ReviewState) -> dict[str, Any]:
        tools = deps.tools_for(state)
        names = tools.known_names()
        inputs = record_inputs(state)
        for item in state["questions"]:
            inputs += [
                DataTag.open("question", source=item.source),
                tools.redactor.redact(item.question, names),
                DataTag.close("question"),
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
        result = model.structured(
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
            "model": result.model,
            "budget": _charge(budget, result.usage),
            "decisions": None,
            "initial_verification": None,
        }

    def revise(state: ReviewState) -> dict[str, Any]:
        """Send the verifier's findings back to the model and take its corrected draft."""
        tools = deps.tools_for(state)
        names = tools.known_names()
        report = state["verification"]
        previous = state["draft"] or DraftReview(claims=[])
        inputs = record_inputs(state)
        for check in report.checks:
            claim = previous.claims[check.claim_index]
            inputs += [
                DataTag.open("previous_claim", index=check.claim_index, verified=check.verified),
                tools.redactor.redact(claim.text, names),
            ]
            for citation in claim.citations:
                inputs += [
                    DataTag.open(
                        "citation", entry=citation.entry_id, start=citation.start, end=citation.end
                    ),
                    tools.redactor.redact(citation.quote, names),
                    DataTag.close("citation"),
                ]
            for failure, detail in zip(check.failures, check.detail or [], strict=False):
                inputs += [
                    DataTag.open("failure", reason=failure),
                    tools.redactor.redact(detail, names),
                    DataTag.close("failure"),
                ]
            inputs.append(DataTag.close("previous_claim"))
        for conflict_id in report.uncovered_conflict_ids:
            inputs.append(DataTag.open("uncovered_conflict", id=conflict_id))
            inputs.append(DataTag.close("uncovered_conflict"))
        budget = state["budget"]
        budget = budget.model_copy(update={"auto_revisions": budget.auto_revisions + 1})
        result = model.structured(
            ModelRequest(
                purpose="agent_revise",
                instructions=load_prompt(REVISE_PROMPT_VERSION),
                inputs=inputs,
                safety_subject=str(state["actor"].principal.id),
            ),
            DraftReview,
        )
        return {
            "draft": result.output,
            "model": result.model,
            "budget": _charge(budget, result.usage),
        }

    def verify(state: ReviewState) -> dict[str, Any]:
        tools = deps.tools_for(state)
        entries = tools.context_entries()
        flagged = {
            ref.entry_id: passages
            for ref in state["context"]
            if (passages := _flagged(tools, ref.entry_id))
        }
        draft_review, withheld = withhold_echoes(state["draft"] or DraftReview(claims=[]), flagged)
        report = verify_draft(draft_review, entries, state["conflicts"], tools.redactions)
        report = report.model_copy(
            update={"instruction_like_entry_ids": list(flagged), "withheld": withheld}
        )
        return {
            "draft": draft_review,
            "verification": report,
            "initial_verification": state.get("initial_verification") or report,
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

    def after_verify(state: ReviewState) -> str:
        if state["budget"].exceeded(deps.now()):
            return "stop"
        report = state["verification"]
        if report is not None and not report.passed and state["budget"].can_auto_revise:
            return "revise"
        return "human_review"

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
    # Automated work counts against the step budget; review, commit and discard do not. Every node
    # but the review pause is traced.
    prompts = {
        "plan": lambda state: PLAN_PROMPT_VERSION,
        "draft": lambda state: state["prompt_version"],
        "revise": lambda state: REVISE_PROMPT_VERSION,
    }
    for node in (load_context, check_conflicts, plan, retrieve, draft, verify, revise):
        graph.add_node(node.__name__, counted(traced(node, prompts.get(node.__name__))))
    for node in (commit, discard, stop):
        graph.add_node(node.__name__, traced(node))
    graph.add_node("human_review", human_review)

    graph.add_edge(START, "load_context")
    for source, target in (
        ("load_context", "check_conflicts"),
        ("check_conflicts", "plan"),
        ("plan", "retrieve"),
        ("retrieve", "draft"),
        ("draft", "verify"),
        ("revise", "verify"),
    ):
        graph.add_conditional_edges(source, within_budget(target), [target, "stop"])
    graph.add_conditional_edges("verify", after_verify, ["revise", "human_review", "stop"])
    graph.add_conditional_edges(
        "human_review", after_review, ["commit", "discard", "draft", "human_review"]
    )
    for terminal in ("commit", "discard", "stop"):
        graph.add_edge(terminal, END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver(serde=checkpoint_serializer()))


def _flagged(tools: ReviewTools, entry_id: UUID) -> list[str]:
    """Instruction-like passages of an entry, in the redacted text the model sees."""
    return flagged_passages(tools.redacted(entry_id).text)


def _charge(budget: Budget, usage: Usage) -> Budget:
    return budget.model_copy(
        update={
            "input_tokens": budget.input_tokens + usage.input_tokens,
            "output_tokens": budget.output_tokens + usage.output_tokens,
            "cost_usd": budget.cost_usd + (usage.cost_usd or 0),
        }
    )
