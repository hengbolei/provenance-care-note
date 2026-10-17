from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from carenote.domain.models import (
    Conflict,
    ConflictCategory,
    ConflictStatus,
    Role,
    TimelineEntry,
)
from carenote.services.entities import extract_entities


@dataclass(frozen=True, slots=True)
class ClinicalFact:
    category: ConflictCategory
    entity: str
    value: str
    entry: TimelineEntry


class DeterministicConflictDetector:
    """Narrow deterministic checks for medication state, dose, and allergy polarity."""

    def detect(self, patient_id: UUID, entries: list[TimelineEntry]) -> list[Conflict]:
        facts = [
            ClinicalFact(item.category, item.entity, item.value, entry)
            for entry in entries
            for item in extract_entities(entry.content)
        ]

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
