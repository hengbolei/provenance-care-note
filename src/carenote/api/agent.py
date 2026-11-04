"""Clinical Review Agent runs (design §15)."""

from uuid import UUID

from fastapi import APIRouter

from carenote.agent.runs import AgentCommitRequest, AgentReviseRequest, AgentRunView
from carenote.api.container import container
from carenote.api.dependencies import ActorDependency
from carenote.api.errors import AGENT_RUN_ERRORS, maps_errors

router = APIRouter(tags=["agent"])


@router.post("/patients/{patient_id}/agent-runs", response_model=AgentRunView, status_code=202)
@maps_errors(*AGENT_RUN_ERRORS)
def start_agent_run(patient_id: UUID, actor: ActorDependency) -> AgentRunView:
    return container.agent_runs.start(actor, patient_id)


@router.get("/agent-runs/{run_id}", response_model=AgentRunView)
@maps_errors(*AGENT_RUN_ERRORS)
def get_agent_run(run_id: UUID, actor: ActorDependency) -> AgentRunView:
    return container.agent_runs.get_run(actor, run_id)


@router.post("/agent-runs/{run_id}/review", response_model=AgentRunView, status_code=202)
@maps_errors(*AGENT_RUN_ERRORS)
def revise_agent_run(
    run_id: UUID, payload: AgentReviseRequest, actor: ActorDependency
) -> AgentRunView:
    return container.agent_runs.revise(actor, run_id, payload)


@router.post("/agent-runs/{run_id}/commit", response_model=AgentRunView)
@maps_errors(*AGENT_RUN_ERRORS)
def commit_agent_run(
    run_id: UUID, payload: AgentCommitRequest, actor: ActorDependency
) -> AgentRunView:
    return container.agent_runs.commit(actor, run_id, payload)


@router.post("/agent-runs/{run_id}/discard", response_model=AgentRunView)
@maps_errors(*AGENT_RUN_ERRORS)
def discard_agent_run(run_id: UUID, actor: ActorDependency) -> AgentRunView:
    return container.agent_runs.discard(actor, run_id)
