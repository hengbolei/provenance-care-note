"""Evaluation case format (design §13) and loader.

A case is a small synthetic record plus what a correct system must do with it:

- `expect.conflicts`: the medication, dose and allergy conflicts deterministic detection must find;
- `expect.phi`: identifiers that must not survive redaction before a model call;
- `expect.critical_facts`: facts a good review must state in a verified claim (agent suite);
- `expect.forbidden_output`: text a draft must never contain, e.g. what an injection asks for;
- `expect.retrieval`: queries whose top hits must include the given entries;
- `probes`: hand-written claims with the exact verdict the citation verifier must reach.

Anything the current system is known not to handle carries a `known_gap` reason. The report shows
known gaps separately, so real weaknesses stay visible without silently failing the CI gate.

Cases live in `evals/cases/*.yaml`; each file holds a list of cases.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carenote.agent.state import CheckFailure, Citation, Claim, ClaimCategory
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

CASE_DIR = Path(__file__).resolve().parents[3] / "evals" / "cases"
EVAL_CLINIC_ID = UUID("10000000-0000-4000-8000-0000000000e1")


class Family(StrEnum):
    MEDICATION_CHANGE = "medication_change"
    DOSE_CONFLICT = "dose_conflict"
    NEW_ALLERGY = "new_allergy"
    CONTRADICTORY_SOURCES = "contradictory_sources"
    SPARSE_INFORMATION = "sparse_information"
    LONG_TIMELINE = "long_timeline"
    PROMPT_INJECTION = "prompt_injection"
    PHI_HEAVY = "phi_heavy"


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

    @field_validator("phi", mode="before")
    @classmethod
    def plain_strings_are_phi_items(cls, value):
        return [{"text": item} if isinstance(item, str) else item for item in value or []]


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    family: Family
    description: str
    patient: CasePatient = Field(default_factory=CasePatient)
    entries: list[CaseEntry] = Field(min_length=1)
    filler: list[Filler] = Field(default_factory=list)
    expect: Expectations = Field(default_factory=Expectations)
    probes: list[Probe] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_resolve(self) -> EvalCase:
        keys = [entry.key for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.id}: entry keys must be unique")
        probe_ids = [probe.id for probe in self.probes]
        if len(set(probe_ids)) != len(probe_ids):
            raise ValueError(f"{self.id}: probe ids must be unique")
        by_key = {entry.key: entry for entry in self.entries}
        for item in self.expect.retrieval:
            unknown = set(item.entries) - set(by_key)
            if unknown:
                raise ValueError(f"{self.id}: retrieval query names unknown entries {unknown}")
        for probe in self.probes:
            for citation in probe.cite:
                entry = by_key.get(citation.entry)
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
        citations = []
        for citation in probe.cite:
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
