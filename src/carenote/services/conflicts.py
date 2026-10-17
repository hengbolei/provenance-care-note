import re
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from carenote.domain.models import (
    Conflict,
    ConflictCategory,
    ConflictStatus,
    Role,
    TimelineEntry,
)


@dataclass(frozen=True, slots=True)
class ClinicalFact:
    category: ConflictCategory
    entity: str
    value: str
    entry: TimelineEntry


class DeterministicConflictDetector:
    """Narrow deterministic checks for medication state, dose, and allergy polarity."""

    DOSE = re.compile(
        r"\b(?P<med>[A-Za-z][A-Za-z-]{2,})\s+(?P<dose>\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units?))\b",
        re.IGNORECASE,
    )
    MEDICATION_STATE = re.compile(
        r"\b(?P<state>hold|stop|discontinue|continue|take|start|started)\s+"
        r"(?:the\s+|morning\s+)?(?P<med>[A-Za-z][A-Za-z-]{2,})\b",
        re.IGNORECASE,
    )
    ALLERGY_POSITIVE = re.compile(
        r"(?<!not )\ballergic\s+(?:to\s+)?(?P<agent>[A-Za-z][A-Za-z-]{2,30})\b|"
        r"(?<!no )\ballergy\s+(?:to\s+)?(?P<allergy_agent>[A-Za-z][A-Za-z-]{2,30})\b",
        re.IGNORECASE,
    )
    ALLERGY_NEGATIVE = re.compile(
        r"\b(?:not allergic to|no allergy to)\s+(?P<agent>[A-Za-z][A-Za-z-]{2,30})\b",
        re.IGNORECASE,
    )

    def detect(self, patient_id: UUID, entries: list[TimelineEntry]) -> list[Conflict]:
        facts: list[ClinicalFact] = []
        for entry in entries:
            for match in self.DOSE.finditer(entry.content):
                facts.append(
                    ClinicalFact(
                        ConflictCategory.DOSE,
                        match.group("med").lower(),
                        re.sub(r"\s+", "", match.group("dose").lower()),
                        entry,
                    )
                )
            for match in self.MEDICATION_STATE.finditer(entry.content):
                raw_state = match.group("state").lower()
                state = "hold" if raw_state in {"hold", "stop", "discontinue"} else "take"
                facts.append(
                    ClinicalFact(
                        ConflictCategory.MEDICATION,
                        match.group("med").lower(),
                        state,
                        entry,
                    )
                )
            for match in self.ALLERGY_POSITIVE.finditer(entry.content):
                agent = match.group("agent") or match.group("allergy_agent")
                facts.append(
                    ClinicalFact(
                        ConflictCategory.ALLERGY,
                        agent.lower(),
                        "present",
                        entry,
                    )
                )
            for match in self.ALLERGY_NEGATIVE.finditer(entry.content):
                facts.append(
                    ClinicalFact(
                        ConflictCategory.ALLERGY,
                        match.group("agent").lower(),
                        "absent",
                        entry,
                    )
                )

        grouped: dict[tuple[ConflictCategory, str], list[ClinicalFact]] = {}
        for fact in facts:
            grouped.setdefault((fact.category, fact.entity), []).append(fact)

        conflicts: list[Conflict] = []
        for (category, entity), candidates in grouped.items():
            values = {candidate.value for candidate in candidates}
            if len(values) < 2:
                continue
            unique_entries = sorted(
                {candidate.entry.id: candidate.entry for candidate in candidates}.values(),
                key=lambda item: item.timestamp,
            )
            clinician_entries = [
                entry for entry in unique_entries if entry.author_role is Role.CLINICIAN
            ]
            preferred = clinician_entries[-1] if clinician_entries else None
            status = (
                ConflictStatus.CLINICIAN_PRECEDENCE
                if preferred is not None
                else ConflictStatus.NEEDS_REVIEW
            )
            summary = (
                f"Conflicting {category.value} facts for {entity}: "
                + " versus ".join(sorted(values))
                + "."
            )
            rationale = (
                "The latest clinician-authored entry takes precedence; keep the conflict visible "
                "until the care team verifies the source."
                if preferred is not None
                else "No clinician-authored fact resolves this conflict; clinical review is required."
            )
            identity = f"{patient_id}:{category.value}:{entity}:" + ":".join(
                str(entry.id) for entry in unique_entries
            )
            conflicts.append(
                Conflict(
                    id=uuid5(NAMESPACE_URL, identity),
                    patient_id=patient_id,
                    clinic_id=unique_entries[0].clinic_id,
                    category=category,
                    entity=entity,
                    summary=summary,
                    status=status,
                    entry_ids=[entry.id for entry in unique_entries],
                    preferred_entry_id=preferred.id if preferred is not None else None,
                    rationale=rationale,
                )
            )
        return sorted(conflicts, key=lambda item: (item.category.value, item.entity))
