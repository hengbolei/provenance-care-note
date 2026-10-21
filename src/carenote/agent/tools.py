"""Scoped, typed tools for the Clinical Review Agent (design §7).

The model never sees repository objects. It sees what these tools return, and every read goes
through `CareNoteService` as the run's principal clinician, so the agent can read exactly what
that clinician can and nothing more. All record text leaves the tools redacted, with offsets in
the redacted text; the `RedactionResult` for each entry is kept so citations can be mapped back
during verification.

There is no tool that confirms, publishes, edits a section or changes access. The one "propose"
tool only adds to run state; persistence happens at commit, driven by the clinician.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from carenote.agent.state import MAX_CANDIDATE_HIGHLIGHTS, CandidateHighlight, Citation
from carenote.domain.models import (
    ActionStatus,
    AgentActor,
    Conflict,
    EntryRef,
    EntryType,
    PatientDetailResponse,
    ReviewStatus,
    RiskLevel,
    Role,
    Span,
    TimelineEntry,
)
from carenote.knowledge.models import KnowledgeDocument, KnowledgeVersion, ReferenceSpan
from carenote.knowledge.search import search_references
from carenote.knowledge.service import KnowledgeService
from carenote.privacy.redaction import PHIRedactionGateway, RedactionResult
from carenote.services.care_notes import CareNoteService

MAX_LIST_LIMIT = 50


class HighlightSummary(BaseModel):
    id: UUID
    text: str
    risk_level: RiskLevel
    status: ReviewStatus
    priority: int


class SectionSummary(BaseModel):
    section: str
    version: int
    owner_role: Role


class PatientOverview(BaseModel):
    """What the agent may know about the patient: no name, date of birth or record number."""

    age_years: int
    pronouns: str | None
    entry_count: int
    open_highlights: list[HighlightSummary]
    sections: list[SectionSummary]


class EntryText(BaseModel):
    ref: EntryRef
    title: str
    text: str = Field(description="Redacted entry content; citation offsets refer to this text.")


class SourceExcerpt(BaseModel):
    entry_id: UUID
    source_label: str
    excerpt: str


class ToolCall(BaseModel):
    """Metadata-only record of a tool call, for `agent_tool_called` audit events."""

    name: str
    entry_id: UUID | None = None
    result_count: int = 0


class ReviewTools:
    def __init__(
        self,
        service: CareNoteService,
        actor: AgentActor,
        patient_id: UUID,
        redactor: PHIRedactionGateway | None = None,
        today: date | None = None,
        knowledge: KnowledgeService | None = None,
    ) -> None:
        self.service = service
        self.knowledge = knowledge
        self.actor = actor
        self.patient_id = patient_id
        self.redactor = redactor or PHIRedactionGateway()
        self.today = today or datetime.now(UTC).date()
        self.calls: list[ToolCall] = []
        self.proposed: list[CandidateHighlight] = []
        self.redactions: dict[UUID, RedactionResult] = {}
        # Redacted reference versions, keyed by (document, version).
        self.reference_redactions: dict[tuple[UUID, int], RedactionResult] = {}
        self._view: PatientDetailResponse | None = None
        self._references: list[tuple[KnowledgeDocument, KnowledgeVersion]] | None = None

    # Reads

    def get_patient_overview(self) -> PatientOverview:
        view = self._patient_view()
        patient = view.patient
        born = patient.date_of_birth
        age = (
            self.today.year
            - born.year
            - ((self.today.month, self.today.day) < (born.month, born.day))
        )
        open_highlights = [
            HighlightSummary(
                id=item.id,
                text=self._redact_text(item.text),
                risk_level=item.risk_level,
                status=item.status,
                priority=item.priority,
            )
            for item in view.highlights
            if item.action_status is not ActionStatus.COMPLETED
        ]
        overview = PatientOverview(
            age_years=age,
            pronouns=patient.pronouns,
            entry_count=len(view.entries),
            open_highlights=open_highlights,
            sections=[
                SectionSummary(
                    section=item.section, version=item.version, owner_role=item.owner_role
                )
                for item in view.sections
            ],
        )
        self._record("get_patient_overview", result_count=len(open_highlights))
        return overview

    def list_entries(
        self,
        since: datetime | None = None,
        types: list[EntryType] | None = None,
        limit: int = 20,
    ) -> list[EntryRef]:
        """Entry references, newest first."""
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        refs = [
            EntryRef.from_entry(entry)
            for entry in self._patient_view().entries
            if (since is None or entry.timestamp >= since) and (not types or entry.type in types)
        ][:limit]
        self._record("list_entries", result_count=len(refs))
        return refs

    def read_entry(self, entry_id: UUID) -> EntryText:
        entry = self._entry(entry_id)
        redaction = self._redact_entry(entry)
        self._record("read_entry", entry_id=entry_id, result_count=1)
        return EntryText(ref=EntryRef.from_entry(entry), title=entry.title, text=redaction.text)

    def search_entries(self, query: str, k: int = 10) -> list[Span]:
        """Search hits as spans of the redacted entry text, ready to cite."""
        hits = self.service.search_entries(self.actor.principal, self.patient_id, query, k)
        spans: list[Span] = []
        for hit in hits:
            entry = self._entry(hit.entry_id)
            redaction = self._redact_entry(entry)
            start, end = redaction.to_redacted(hit.start, hit.end, len(entry.content))
            spans.append(
                Span(entry_id=hit.entry_id, start=start, end=end, text=redaction.text[start:end])
            )
        self._record("search_entries", result_count=len(spans))
        return spans

    def search_knowledge(self, query: str, k: int = 3) -> list[ReferenceSpan]:
        """Passages of the reference versions in effect, as spans of their redacted text.

        Read as the principal clinician; with no knowledge base configured this finds nothing.
        """
        spans: list[ReferenceSpan] = []
        for hit in search_references(self._current_references(), query, k):
            version = self.reference_version(hit.document_id, hit.version)
            redaction = self.reference_redaction(version)
            start, end = redaction.to_redacted(hit.start, hit.end, len(version.content))
            spans.append(
                hit.model_copy(
                    update={"start": start, "end": end, "text": redaction.text[start:end]}
                )
            )
        self._record("search_knowledge", result_count=len(spans))
        return spans

    def reference_version(self, document_id: UUID, version: int) -> KnowledgeVersion:
        """A version this run may cite: one of the versions in effect when the run read them."""
        for document, item in self._current_references():
            if document.id == document_id and item.version == version:
                return item
        raise LookupError("reference version is not in effect for this run")

    def reference_redaction(self, version: KnowledgeVersion) -> RedactionResult:
        key = (version.document_id, version.version)
        if key not in self.reference_redactions:
            self.reference_redactions[key] = self.redactor.redact(
                version.content, self.known_names()
            )
        return self.reference_redactions[key]

    def get_source_evidence(self, entry_id: UUID) -> SourceExcerpt:
        """The original message or transcript excerpt behind an AI-scribed entry."""
        evidence = self.service.get_source_evidence(self.actor.principal, self.patient_id, entry_id)
        self._record("get_source_evidence", entry_id=entry_id, result_count=1)
        return SourceExcerpt(
            entry_id=entry_id,
            source_label=evidence.artifact.label,
            excerpt=self._redact_text(evidence.excerpt),
        )

    def detect_conflicts(self) -> list[Conflict]:
        """Deterministic medication, dose and allergy conflicts; they outrank model opinions."""
        conflicts = list(self._patient_view().conflicts)
        self._record("detect_conflicts", result_count=len(conflicts))
        return conflicts

    # Proposals: run state only, never persisted here

    def propose_highlight(
        self,
        citation: Citation,
        risk_reason: str,
        suggested_action: str,
        clinical_entities: list[str] | None = None,
    ) -> int:
        """Hold a candidate highlight in run state and return its index."""
        if len(self.proposed) >= MAX_CANDIDATE_HIGHLIGHTS:
            raise ValueError(f"at most {MAX_CANDIDATE_HIGHLIGHTS} highlights can be proposed")
        redaction = self._redact_entry(self._entry(citation.entry_id))
        if citation.end > len(redaction.text):
            raise ValueError("highlight citation is outside the entry")
        self.proposed.append(
            CandidateHighlight(
                citation=citation,
                risk_reason=risk_reason,
                suggested_action=suggested_action,
                clinical_entities=clinical_entities or [],
            )
        )
        self._record("propose_highlight", entry_id=citation.entry_id, result_count=1)
        return len(self.proposed) - 1

    def redacted(self, entry_id: UUID) -> RedactionResult:
        """The entry's redacted text as the model sees it; not a tool call, so not recorded."""
        return self._redact_entry(self._entry(entry_id))

    # Context for verification

    def context_entries(self) -> dict[UUID, TimelineEntry]:
        """Every entry the principal can read for this patient: the citation scope."""
        return {entry.id: entry for entry in self._patient_view().entries}

    # Internals

    def _patient_view(self) -> PatientDetailResponse:
        # One snapshot per run, read as the clinician, so every tool sees the same record.
        if self._view is None:
            self._view = self.service.get_patient_view(self.actor.principal, self.patient_id)
        return self._view

    def _current_references(self) -> list[tuple[KnowledgeDocument, KnowledgeVersion]]:
        # One snapshot per run, like the patient view, so every search sees the same versions.
        if self._references is None:
            self._references = (
                self.knowledge.current_versions(self.actor.principal, self.today)
                if self.knowledge is not None
                else []
            )
        return self._references

    def _entry(self, entry_id: UUID) -> TimelineEntry:
        entry = self.context_entries().get(entry_id)
        if entry is None:
            raise LookupError("entry is not in this patient's record")
        return entry

    def known_names(self) -> list[str]:
        patient = self._patient_view().patient
        return [patient.display_name, patient.medical_record_number]

    def _redact_entry(self, entry: TimelineEntry) -> RedactionResult:
        if entry.id not in self.redactions:
            self.redactions[entry.id] = self.redactor.redact(entry.content, self.known_names())
        return self.redactions[entry.id]

    def _redact_text(self, text: str) -> str:
        return self.redactor.prepare_for_llm(text, self.known_names())

    def _record(self, name: str, entry_id: UUID | None = None, result_count: int = 0) -> None:
        self.calls.append(ToolCall(name=name, entry_id=entry_id, result_count=result_count))
