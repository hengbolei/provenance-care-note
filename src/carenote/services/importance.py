from datetime import datetime
from typing import ClassVar

from carenote.domain.models import (
    ActionStatus,
    Highlight,
    PriorityFactor,
    ReviewStatus,
    RiskLevel,
    Role,
    TimelineEntry,
)


class DeterministicImportanceScorer:
    """Explainable clinical-workflow ranking with no model-assigned confidence."""

    RISK_POINTS: ClassVar[dict[RiskLevel, int]] = {
        RiskLevel.LOW: 8,
        RiskLevel.MODERATE: 20,
        RiskLevel.HIGH: 34,
        RiskLevel.CRITICAL: 48,
    }
    ENTITY_POINTS: ClassVar[dict[str, int]] = {
        "allergy": 18,
        "medication": 12,
        "dose": 10,
        "symptom": 6,
    }

    def score(
        self,
        highlight: Highlight,
        source: TimelineEntry,
        reference_time: datetime,
        learned_adjustment: tuple[int, str] | None = None,
    ) -> Highlight:
        factors: list[PriorityFactor] = []

        risk_points = self.RISK_POINTS[highlight.risk_level]
        factors.append(
            PriorityFactor(
                key="risk_level",
                label=f"{highlight.risk_level.value.title()} risk",
                points=risk_points,
                explanation="Deterministic floor selected by the clinician or safety rule.",
            )
        )

        age_days = max(0, (reference_time - source.timestamp).days)
        if age_days <= 2:
            recency_points, label = 15, "Source is at most 2 days old"
        elif age_days <= 7:
            recency_points, label = 10, "Source is at most 7 days old"
        elif age_days <= 30:
            recency_points, label = 5, "Source is at most 30 days old"
        else:
            recency_points, label = 0, "Source is older than 30 days"
        factors.append(
            PriorityFactor(
                key="recency",
                label="Recency",
                points=recency_points,
                explanation=f"{label} ({age_days} day(s) relative to the latest entry).",
            )
        )

        entity_points = sum(
            self.ENTITY_POINTS.get(entity.lower(), 0) for entity in set(highlight.clinical_entities)
        )
        if entity_points:
            factors.append(
                PriorityFactor(
                    key="clinical_entities",
                    label="Safety-sensitive clinical entities",
                    points=entity_points,
                    explanation=(
                        "Matched deterministic categories: "
                        + ", ".join(sorted(set(highlight.clinical_entities)))
                        + "."
                    ),
                )
            )

        action_points = {
            ActionStatus.OPEN: 15,
            ActionStatus.IN_PROGRESS: 7,
            ActionStatus.COMPLETED: -20,
        }[highlight.action_status]
        factors.append(
            PriorityFactor(
                key="action_status",
                label=f"Action {highlight.action_status.value.replace('_', ' ')}",
                points=action_points,
                explanation="Unresolved work is promoted; completed work is deliberately demoted.",
            )
        )

        if highlight.status is ReviewStatus.CLINICIAN_CONFIRMED:
            factors.append(
                PriorityFactor(
                    key="clinical_review",
                    label="Clinician confirmed",
                    points=12,
                    explanation="Human-confirmed information ranks above unreviewed suggestions.",
                )
            )
        elif highlight.status is ReviewStatus.REJECTED:
            factors.append(
                PriorityFactor(
                    key="clinical_review",
                    label="Rejected suggestion",
                    points=-35,
                    explanation="Rejected suggestions remain auditable but are strongly demoted.",
                )
            )

        if source.author_role is Role.CLINICIAN:
            factors.append(
                PriorityFactor(
                    key="source_authority",
                    label="Clinician-authored source",
                    points=8,
                    explanation="The source is a clinician-authored record rather than AI output.",
                )
            )

        if learned_adjustment is not None:
            points, explanation = learned_adjustment
            factors.append(
                PriorityFactor(
                    key="reviewed_outcomes",
                    label="Reviewed-outcome adjustment",
                    points=points,
                    explanation=explanation,
                )
            )

        score = max(0, min(100, sum(factor.points for factor in factors)))
        return highlight.model_copy(update={"priority": score, "priority_factors": factors})
