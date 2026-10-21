"""Evaluation case format (design §13) and loader.

A case is a small synthetic record plus what a correct system must do with it:

- `expect.conflicts`: the medication, dose and allergy conflicts deterministic detection must find;
- `expect.phi`: identifiers that must not survive redaction before a model call;
- `expect.critical_facts`: facts a good review must state in a verified claim (agent suite);
- `expect.forbidden_output`: text a draft must never contain, e.g. what an injection asks for;
- `expect.retrieval`: queries whose top hits must include the given entries;
- `expect.instruction_like`: entries (or reference documents) the injection detector must flag;
  it must flag no others;
- `knowledge`: reference documents with versions, for the reference library (design §19);
- `expect.references`: queries whose top reference hits must include a passage, and must not
  include superseded or retired text;
- `check_probes`: hand-written check points with the verdict the check-point verifier must reach.
- `probes`: hand-written claims with the exact verdict the citation verifier must reach.

Anything the current system is known not to handle carries a `known_gap` reason. The report shows
known gaps separately, so real weaknesses stay visible without silently failing the CI gate.

Cases live in `evals/cases/*.yaml`; each file holds a list of cases.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carenote.agent.state import (
    CheckFailure,
    CheckPoint,
    CheckPointFailure,
    Citation,
    Claim,
    ClaimCategory,
    ReferenceCitation,
)
from carenote.domain.models import (
    ConflictCategory,
    EntryOrigin,
    EntryType,
    Patient,
    ProvenanceSource,
    RiskLevel,
    Role,
    TimelineEntry,
)
from carenote.knowledge.models import (
    KnowledgeDocument,
    KnowledgeKind,
    KnowledgeStatus,
    KnowledgeVersion,
    normalise_content,
)

CASE_DIR = Path(__file__).resolve().parents[3] / "evals" / "cases"
EVAL_CLINIC_ID = UUID("10000000-0000-4000-8000-0000000000e1")
# The evaluation clock: reference versions take effect relative to this day.
EVAL_TODAY = date(2026, 9, 1)


class Family(StrEnum):
    MEDICATION_CHANGE = "medication_change"
    DOSE_CONFLICT = "dose_conflict"
    NEW_ALLERGY = "new_allergy"
    CONTRADICTORY_SOURCES = "contradictory_sources"
    SPARSE_INFORMATION = "sparse_information"
    LONG_TIMELINE = "long_timeline"
    PROMPT_INJECTION = "prompt_injection"
    PHI_HEAVY = "phi_heavy"
    KNOWLEDGE = "knowledge"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CasePatient(_Strict):
    display_name: str = "Evaluation Patient (Synthetic)"
    medical_record_number: str = "SYN-EVAL-0001"
    date_of_birth: str = "1970-01-01"


class CaseEntry(_Strict):
    key: str = Field(pattern=r"^[a-z0-9_]+$")
    type: EntryType
    author: Role
    at: datetime
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1)
    internal: bool = True


class Filler(_Strict):
    """Repeated low-signal entries for long-timeline cases; `{n}` in text is the entry number."""

    count: int = Field(ge=1, le=200)
    type: EntryType
    author: Role
    start: datetime
    every_hours: int = Field(default=12, ge=1)
    title: str
    content: str


class ExpectedConflict(_Strict):
    category: ConflictCategory
    entity: str
    known_gap: str | None = None


class PhiItem(_Strict):
    text: str = Field(min_length=1)
    known_gap: str | None = None


class FlaggedEntry(_Strict):
    entry: str
    known_gap: str | None = None


class CriticalFact(_Strict):
    key: str
    description: str
    # Every term must appear (case-insensitively) in one verified claim.
    mentions: list[str] = Field(min_length=1)


class RetrievalQuery(_Strict):
    query: str = Field(min_length=1)
    # Every listed entry must be among the top hits for the query.
    entries: list[str] = Field(min_length=1)
    known_gap: str | None = None


class ProbeCitation(_Strict):
    entry: str
    quote: str = Field(min_length=1)
    # Where to place the citation when the quote is deliberately not in the entry:
    # at the position of `at` in the entry, or at an explicit `start` offset.
    at: str | None = None
    start: int | None = Field(default=None, ge=0)


class KnowledgeVersionSpec(_Strict):
    label: str = Field(min_length=1, max_length=60)
    effective: date
    content: str = Field(min_length=1)


class KnowledgeDocSpec(_Strict):
    key: str = Field(pattern=r"^[a-z0-9_]+$")
    kind: KnowledgeKind
    title: str = Field(min_length=1, max_length=200)
    source: str = "Synthetic evaluation reference"
    versions: list[KnowledgeVersionSpec] = Field(min_length=1)
    retired: bool = False


class ReferenceQuery(_Strict):
    query: str = Field(min_length=1)
    # A top hit must come from this document and contain this text.
    document: str
    contains: str = Field(min_length=1)
    # Text no hit may contain, such as a superseded version or a retired document.
    absent: list[str] = Field(default_factory=list)
    known_gap: str | None = None


class CheckProbeReference(_Strict):
    document: str
    quote: str = Field(min_length=1)
    # Default: the version in effect. Name an older one to probe a superseded citation.
    version: int | None = Field(default=None, ge=1)
    start: int | None = Field(default=None, ge=0)


class CheckProbe(_Strict):
    id: str
    text: str = Field(min_length=1, max_length=300)
    reference: CheckProbeReference
    cite: list[ProbeCitation] = Field(default_factory=list)
    # Failures the check-point verifier must report; empty means the check point must verify.
    expect: list[CheckPointFailure] = Field(default_factory=list)
    known_gap: str | None = None


class Probe(_Strict):
    id: str
    claim: str = Field(min_length=1, max_length=400)
    category: ClaimCategory = "medication"
    risk: RiskLevel = RiskLevel.MODERATE
    cite: list[ProbeCitation] = Field(min_length=1)
    # Failures the verifier must report; empty means the claim must verify.
    expect: list[CheckFailure] = Field(default_factory=list)
    known_gap: str | None = None


class Expectations(_Strict):
    conflicts: list[ExpectedConflict] = Field(default_factory=list)
    phi: list[PhiItem] = Field(default_factory=list)
    critical_facts: list[CriticalFact] = Field(default_factory=list)
    forbidden_output: list[str] = Field(default_factory=list)
    retrieval: list[RetrievalQuery] = Field(default_factory=list)
    instruction_like: list[FlaggedEntry] = Field(default_factory=list)
    references: list[ReferenceQuery] = Field(default_factory=list)

    @field_validator("phi", mode="before")
    @classmethod
    def plain_strings_are_phi_items(cls, value):
        return [{"text": item} if isinstance(item, str) else item for item in value or []]

    @field_validator("instruction_like", mode="before")
    @classmethod
    def plain_strings_are_entries(cls, value):
        return [{"entry": item} if isinstance(item, str) else item for item in value or []]


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    family: Family
    description: str
    patient: CasePatient = Field(default_factory=CasePatient)
    entries: list[CaseEntry] = Field(min_length=1)
    filler: list[Filler] = Field(default_factory=list)
    expect: Expectations = Field(default_factory=Expectations)
    probes: list[Probe] = Field(default_factory=list)
    knowledge: list[KnowledgeDocSpec] = Field(default_factory=list)
    check_probes: list[CheckProbe] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_resolve(self) -> EvalCase:
        keys = [entry.key for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.id}: entry keys must be unique")
        probe_ids = [probe.id for probe in self.probes]
        if len(set(probe_ids)) != len(probe_ids):
            raise ValueError(f"{self.id}: probe ids must be unique")
        by_key = {entry.key: entry for entry in self.entries}
        documents = {doc.key: doc for doc in self.knowledge}
        if len(documents) != len(self.knowledge) or set(documents) & set(by_key):
            raise ValueError(f"{self.id}: knowledge keys must be unique and differ from entries")
        flagged = {item.entry for item in self.expect.instruction_like}
        if flagged - set(by_key) - set(documents):
            raise ValueError(f"{self.id}: instruction_like names unknown entries")
        for item in self.expect.references:
            if item.document not in documents:
                raise ValueError(f"{self.id}: reference query names unknown document")
        check_ids = [probe.id for probe in self.check_probes]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError(f"{self.id}: check probe ids must be unique")
        for probe in self.check_probes:
            document = documents.get(probe.reference.document)
            if document is None:
                raise ValueError(f"{self.id}/{probe.id}: unknown reference document")
            if not probe.expect and probe.reference.quote not in self.current_content(document):
                raise ValueError(
                    f"{self.id}/{probe.id}: a supported check probe must quote the version in "
                    "effect exactly"
                )
        for item in self.expect.retrieval:
            unknown = set(item.entries) - set(by_key)
            if unknown:
                raise ValueError(f"{self.id}: retrieval query names unknown entries {unknown}")
        for probe in self.probes:
            for citation in probe.cite:
                entry = by_key.get(citation.entry)
                if entry is None and citation.entry in documents:
                    if CheckFailure.REFERENCE_AS_FACT not in probe.expect:
                        raise ValueError(f"{self.id}/{probe.id}: claims may not cite references")
                    continue
                if entry is None:
                    if CheckFailure.UNKNOWN_ENTRY not in probe.expect:
                        raise ValueError(f"{self.id}/{probe.id}: unknown entry {citation.entry!r}")
                    continue
                if citation.at is not None and citation.at not in entry.content:
                    raise ValueError(f"{self.id}/{probe.id}: 'at' text is not in {entry.key}")
                quoted = citation.quote in entry.content
                if not probe.expect and not quoted:
                    raise ValueError(
                        f"{self.id}/{probe.id}: a supported probe must quote {entry.key} exactly"
                    )
        return self

    # Building the synthetic record

    @staticmethod
    def current_version_number(document: KnowledgeDocSpec, today: date = EVAL_TODAY) -> int | None:
        """The version in effect: the newest one whose effective date has arrived."""
        numbers = [
            number
            for number, version in enumerate(document.versions, start=1)
            if version.effective <= today
        ]
        return max(numbers) if numbers else None

    def current_content(self, document: KnowledgeDocSpec) -> str:
        number = self.current_version_number(document)
        return normalise_content(document.versions[number - 1].content) if number else ""

    def document_id(self, key: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"carenote-eval:{self.id}:knowledge:{key}")

    def build_knowledge(self) -> list[tuple[KnowledgeDocument, list[KnowledgeVersion]]]:
        built = []
        for spec in self.knowledge:
            author = uuid5(NAMESPACE_URL, "carenote-eval:admin")
            document = KnowledgeDocument(
                id=self.document_id(spec.key),
                clinic_id=EVAL_CLINIC_ID,
                kind=spec.kind,
                title=spec.title,
                source=spec.source,
                status=KnowledgeStatus.RETIRED if spec.retired else KnowledgeStatus.ACTIVE,
                latest_version=len(spec.versions),
                created_by=author,
            )
            versions = [
                KnowledgeVersion(
                    document_id=document.id,
                    clinic_id=EVAL_CLINIC_ID,
                    version=number,
                    label=version.label,
                    effective_date=version.effective,
                    content=normalise_content(version.content),
                    created_by=author,
                )
                for number, version in enumerate(spec.versions, start=1)
            ]
            built.append((document, versions))
        return built

    def patient_id(self) -> UUID:
        return uuid5(NAMESPACE_URL, f"carenote-eval:{self.id}")

    def entry_id(self, key: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"carenote-eval:{self.id}:{key}")

    def build_patient(self) -> Patient:
        return Patient(
            id=self.patient_id(),
            clinic_id=EVAL_CLINIC_ID,
            display_name=self.patient.display_name,
            date_of_birth=self.patient.date_of_birth,
            medical_record_number=self.patient.medical_record_number,
        )

    def build_entries(self) -> list[TimelineEntry]:
        entries = [self._entry(item.key, item) for item in self.entries]
        for block, filler in enumerate(self.filler):
            for number in range(1, filler.count + 1):
                key = f"filler_{block}_{number}"
                entries.append(
                    self._entry(
                        key,
                        CaseEntry(
                            key=key,
                            type=filler.type,
                            author=filler.author,
                            at=filler.start + timedelta(hours=filler.every_hours * (number - 1)),
                            title=filler.title.replace("{n}", str(number)),
                            content=filler.content.replace("{n}", str(number)),
                        ),
                    )
                )
        return entries

    def _entry(self, key: str, item: CaseEntry) -> TimelineEntry:
        return TimelineEntry(
            id=self.entry_id(key),
            patient_id=self.patient_id(),
            clinic_id=EVAL_CLINIC_ID,
            author_role=item.author,
            author_id=None if item.author is Role.SYSTEM else uuid5(NAMESPACE_URL, f"author:{key}"),
            timestamp=item.at,
            type=item.type,
            title=item.title,
            content=item.content,
            origin=EntryOrigin(
                source=ProvenanceSource.MANUAL_ENTRY,
                source_id=f"eval-{self.id}-{key}",
                source_label="Synthetic evaluation entry",
            ),
            internal=item.internal,
        )

    def probe_claim(self, probe: Probe) -> Claim:
        """The probe as a model-style claim, with citations placed in the built entries."""
        contents = {item.key: item.content for item in self.entries}
        documents = {doc.key: doc for doc in self.knowledge}
        citations = []
        for citation in probe.cite:
            if citation.entry in documents:
                content = self.current_content(documents[citation.entry])
                start = content.find(citation.quote)
                start = max(start, citation.start or 0)
                citations.append(
                    Citation(
                        entry_id=self.document_id(citation.entry),
                        start=start,
                        end=start + len(citation.quote),
                        quote=citation.quote,
                    )
                )
                continue
            content = contents.get(citation.entry, "")
            if citation.quote in content:
                start = content.index(citation.quote)
            elif citation.at is not None:
                start = content.index(citation.at)
            else:
                start = citation.start or 0
            citations.append(
                Citation(
                    entry_id=self.entry_id(citation.entry),
                    start=start,
                    end=start + len(citation.quote),
                    quote=citation.quote,
                )
            )
        return Claim(
            text=probe.claim, category=probe.category, risk_level=probe.risk, citations=citations
        )


def load_cases(directory: Path = CASE_DIR) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for item in raw:
            cases.append(EvalCase.model_validate(item))
    ids = [case.id for case in cases]
    duplicates = {case_id for case_id in ids if ids.count(case_id) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids: {sorted(duplicates)}")
    return sorted(cases, key=lambda case: case.id)


def check_probe_point(case: EvalCase, probe: CheckProbe, shown: dict) -> CheckPoint:
    """The probe as a model-style check point.

    `shown` maps (document key, version number) to the redacted text the model would see, so the
    reference citation is placed where a model would place it.
    """
    documents = {doc.key: doc for doc in case.knowledge}
    document = documents[probe.reference.document]
    version = probe.reference.version or case.current_version_number(document) or 1
    text = shown.get((document.key, version), "")
    start = text.find(probe.reference.quote)
    if start < 0:
        start = probe.reference.start or 0
    contents = {item.key: item.content for item in case.entries}
    citations = []
    for citation in probe.cite:
        content = contents.get(citation.entry, "")
        position = content.find(citation.quote)
        if position < 0:
            position = content.find(citation.at) if citation.at else (citation.start or 0)
        citations.append(
            Citation(
                entry_id=case.entry_id(citation.entry),
                start=position,
                end=position + len(citation.quote),
                quote=citation.quote,
            )
        )
    return CheckPoint(
        text=probe.text,
        reference=ReferenceCitation(
            document_id=case.document_id(document.key),
            version=version,
            start=start,
            end=start + len(probe.reference.quote),
            quote=probe.reference.quote,
        ),
        citations=citations,
    )
