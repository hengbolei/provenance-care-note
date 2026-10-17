import re
from dataclasses import dataclass

from carenote.domain.models import ConflictCategory

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

_HOLD_STATES = {"hold", "stop", "discontinue"}


@dataclass(frozen=True, slots=True)
class ClinicalEntity:
    """A medication, dose or allergy fact found in text, with its exact span."""

    category: ConflictCategory
    entity: str
    value: str
    start: int
    end: int


def extract_entities(text: str) -> list[ClinicalEntity]:
    """Deterministically extract clinical facts shared by conflict detection and verification.

    Entity names are lower-cased; values are normalised (doses without whitespace, medication
    state as "hold" or "take", allergy polarity as "present" or "absent"). Results are ordered by
    rule, then by position, matching the order conflict detection has always used.
    """
    found: list[ClinicalEntity] = []
    for match in DOSE.finditer(text):
        found.append(
            ClinicalEntity(
                ConflictCategory.DOSE,
                match.group("med").lower(),
                re.sub(r"\s+", "", match.group("dose").lower()),
                *match.span(),
            )
        )
    for match in MEDICATION_STATE.finditer(text):
        state = "hold" if match.group("state").lower() in _HOLD_STATES else "take"
        found.append(
            ClinicalEntity(
                ConflictCategory.MEDICATION, match.group("med").lower(), state, *match.span()
            )
        )
    for match in ALLERGY_POSITIVE.finditer(text):
        agent = match.group("agent") or match.group("allergy_agent")
        found.append(
            ClinicalEntity(ConflictCategory.ALLERGY, agent.lower(), "present", *match.span())
        )
    for match in ALLERGY_NEGATIVE.finditer(text):
        found.append(
            ClinicalEntity(
                ConflictCategory.ALLERGY, match.group("agent").lower(), "absent", *match.span()
            )
        )
    return found
