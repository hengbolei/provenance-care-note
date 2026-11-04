"""The application's services, built once at startup and shared by every router.

Routers read services through `container` at request time (`container.service`, never a copy
taken at import), so tests can swap one with `monkeypatch.setattr(container, "service", ...)`.
"""

from __future__ import annotations

from dataclasses import dataclass

from carenote.agent.commit import commit_review
from carenote.agent.gateway import gateway_from_settings
from carenote.agent.persistence import PostgresRunStore, postgres_checkpointer
from carenote.agent.runs import AgentRunService
from carenote.agent.tracing import configure_tracing
from carenote.core.config import settings
from carenote.core.security import clinical_cipher
from carenote.data.seed import DEMO_CLINIC_ID, seed_demo_data, seed_demo_knowledge
from carenote.knowledge.service import KnowledgeService
from carenote.knowledge.store import InMemoryKnowledgeStore, PostgresKnowledgeStore
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.repositories.postgres import PostgresCareNoteRepository
from carenote.services.care_notes import CareNoteService
from carenote.services.realtime import realtime_hub


@dataclass
class Container:
    repository: InMemoryCareNoteRepository
    service: CareNoteService
    knowledge: KnowledgeService
    agent_runs: AgentRunService


def build_container() -> Container:
    """Wire the demo clinic: PostgreSQL when `CARENOTE_DATABASE_URL` is set, otherwise memory."""
    database_url = settings.database_url
    repository = (
        PostgresCareNoteRepository(database_url, DEMO_CLINIC_ID, clinical_cipher)
        if database_url
        else InMemoryCareNoteRepository()
    )
    seed_demo_data(repository)
    service = CareNoteService(repository)
    configure_tracing()
    knowledge = KnowledgeService(
        PostgresKnowledgeStore(database_url, DEMO_CLINIC_ID, clinical_cipher)
        if database_url
        else InMemoryKnowledgeStore(),
        repository,
    )
    seed_demo_knowledge(knowledge.store)
    # With PostgreSQL, agent runs are checkpointed (encrypted) and registered there, so they
    # survive a restart; without it they live in memory like the rest of the development store.
    agent_runs = AgentRunService(
        service,
        gateway_from_settings(),
        committer=commit_review(service),
        publish=realtime_hub.publish,
        checkpointer=(
            postgres_checkpointer(database_url, DEMO_CLINIC_ID, clinical_cipher)
            if database_url
            else None
        ),
        store=PostgresRunStore(database_url, DEMO_CLINIC_ID) if database_url else None,
        knowledge=knowledge,
    )
    return Container(
        repository=repository, service=service, knowledge=knowledge, agent_runs=agent_runs
    )


container = build_container()
