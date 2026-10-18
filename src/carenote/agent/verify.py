"""Deterministic citation verification (design §8).

Every draft passes through `verify_draft` before a clinician sees it. The checks are plain Python
with no model in the loop, so a claim is only shown as verified when its citations exist, are in
bounds, quote the text that is really there, mention the clinical facts the claim states, and
respect the provenance policy. An LLM judge may add flags later but never overrides a failure here.

Offsets: the model cites the text it was shown. When that text was redacted, pass the entry's
`RedactionResult`; citations are checked against the redacted text, then mapped back so every
returned citation points into the original entry content.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from uuid import UUID

from carenote.agent.state import (
    CheckFailure,
    Citation,
    Claim,
    ClaimCheck,
    DraftReview,
    VerificationReport,
)
from carenote.domain.models import (
    Conflict,
    ConflictCategory,
    EntryType,
    RiskLevel,
    Role,
    TimelineEntry,
)
from carenote.privacy.redaction import RedactionResult
from carenote.services.entities import extract_entities

_HIGH_RISK = {RiskLevel.HIGH, RiskLevel.CRITICAL}
_PATIENT_SOURCED_TYPES = {EntryType.PATIENT_NOTE, EntryType.AI_PATIENT_SESSION_SUMMARY}
# A claim resting only on patient-sourced text must read as a report, not as established fact.
_REPORTED_PHRASING = re.compile(
    r"\b(?:reports?|reported|reporting|states?|stated|says|said|describes?|described|"
    r"mentions?|mentioned|denies|denied|according to|per (?:the )?patient|patient-reported)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

Fail = Callable[[CheckFailure, str], None]


def verify_draft(
    draft: DraftReview,
    entries: Mapping[UUID, TimelineEntry],
    conflicts: Sequence[Conflict] = (),
    redactions: Mapping[UUID, RedactionResult] | None = None,
) -> VerificationReport:
    """Check every claim in `draft` against the entries the run loaded.

    `entries` must hold only entries the requesting clinician can read; a citation to anything
    else fails as unknown. Returns one `ClaimCheck` per claim, in order, and the deterministic
    conflicts that no verified claim mentions.
    """
    redactions = redactions or {}
    known_medications = _known_medications(entries.values())
    checks = [
        _check_claim(index, claim, entries, redactions, known_medications)
        for index, claim in enumerate(draft.claims)
    ]
    verified_texts = [draft.claims[check.claim_index].text for check in checks if check.verified]
    uncovered = [
        conflict.id
        for conflict in conflicts
        if not any(_mentions(text, conflict.entity) for text in verified_texts)
    ]
    return VerificationReport(checks=checks, uncovered_conflict_ids=uncovered)


def _check_claim(
    index: int,
    claim: Claim,
    entries: Mapping[UUID, TimelineEntry],
    redactions: Mapping[UUID, RedactionResult],
    known_medications: set[str],
) -> ClaimCheck:
    failures: list[CheckFailure] = []
    detail: list[str] = []
    resolved: list[Citation] = []

    def fail(failure: CheckFailure, message: str) -> None:
        if failure not in failures:
            failures.append(failure)
        detail.append(message)

    for position, citation in enumerate(claim.citations, start=1):
        label = f"citation {position}"
        entry = entries.get(citation.entry_id)
        if entry is None:
            fail(CheckFailure.UNKNOWN_ENTRY, f"{label}: entry is not in this review's context")
            continue
        repaired = _resolve_citation(citation, entry, redactions.get(entry.id), label, fail)
        if repaired is not None:
            resolved.append(repaired)

    # Content checks only make sense once every citation resolved to real text.
    if not failures:
        _check_support(claim, resolved, known_medications, fail)
        _check_provenance_policy(claim, [entries[c.entry_id] for c in resolved], fail)

    return ClaimCheck(claim_index=index, failures=failures, detail=detail, citations=resolved)


def _resolve_citation(
    citation: Citation,
    entry: TimelineEntry,
    redaction: RedactionResult | None,
    label: str,
    fail: Fail,
) -> Citation | None:
    """Checks 2 and 3: bounds and quote match, relocating the quote when the offsets drifted."""
    shown = redaction.text if redaction is not None else entry.content
    in_bounds = citation.end <= len(shown)
    if in_bounds and _normalise(shown[citation.start : citation.end]) == _normalise(citation.quote):
        start, end = citation.start, citation.end
    else:
        located = _locate(shown, citation.quote, near=citation.start)
        if located is None:
            if in_bounds:
                fail(CheckFailure.QUOTE_MISMATCH, f"{label}: quoted text is not in the entry")
            else:
                fail(
                    CheckFailure.OUT_OF_BOUNDS,
                    f"{label}: [{citation.start}, {citation.end}) is outside an entry of "
                    f"length {len(shown)}",
                )
            return None
        start, end = located
    if redaction is not None:
        start, end = redaction.to_original(start, end)
    # The quote becomes the exact original text. model_copy skips validation on purpose: un-redacting
    # can lengthen a quote past the model-facing limit, and this citation is never sent back out.
    return citation.model_copy(
        update={"start": start, "end": end, "quote": entry.content[start:end]}
    )


def _locate(text: str, quote: str, near: int) -> tuple[int, int] | None:
    """Find `quote` in `text` ignoring whitespace differences; prefer the match nearest `near`."""
    words = quote.split()
    if not words:
        return None
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words))
    matches = [match.span() for match in pattern.finditer(text)]
    if not matches:
        return None
    return min(matches, key=lambda span: abs(span[0] - near))


def _known_medications(entries: Iterable[TimelineEntry]) -> set[str]:
    """Medication names the record gives a dose for anywhere.

    The medication-state pattern takes any word after "hold", "stop", "start" and so on, so on
    its own it reads "hold pending review" as a drug called "pending". Requiring that the record
    dose the drug somewhere keeps those false facts out of the support check.
    """
    return {
        fact.entity
        for entry in entries
        for fact in extract_entities(entry.content)
        if fact.category is ConflictCategory.DOSE
    }


def _check_support(
    claim: Claim, citations: list[Citation], known_medications: set[str], fail: Fail
) -> None:
    """Check 4: every clinical fact the claim states must appear in, and agree with, its sources."""
    cited = " ".join(citation.quote for citation in citations)
    cited_words = set(_WORD.findall(cited.lower()))
    cited_compact = re.sub(r"\s+", "", cited.lower())
    cited_facts = extract_entities(cited)
    for fact in extract_entities(claim.text):
        if fact.category is ConflictCategory.MEDICATION and fact.entity not in known_medications:
            continue
        if fact.entity not in cited_words:
            fail(
                CheckFailure.UNSUPPORTED_ENTITIES,
                f"'{fact.entity}' does not appear in the cited text",
            )
            continue
        if fact.category is ConflictCategory.DOSE and fact.value not in cited_compact:
            fail(
                CheckFailure.UNSUPPORTED_ENTITIES,
                f"dose {fact.value} for '{fact.entity}' does not appear in the cited text",
            )
            continue
        same = [
            other
            for other in cited_facts
            if other.category is fact.category and other.entity == fact.entity
        ]
        if same and all(other.value != fact.value for other in same):
            fail(
                CheckFailure.UNSUPPORTED_ENTITIES,
                f"cited text says {fact.category.value} '{fact.entity}' is "
                f"{same[0].value}, not {fact.value}",
            )


def _check_provenance_policy(claim: Claim, sources: list[TimelineEntry], fail: Fail) -> None:
    """Check 5: claims resting only on patient-sourced text are capped and phrased as reports."""
    if not sources or not all(_patient_sourced(entry) for entry in sources):
        return
    if claim.risk_level in _HIGH_RISK:
        fail(
            CheckFailure.PATIENT_SOURCE_RISK,
            f"{claim.risk_level.value} risk needs a staff or clinician source, not only "
            "patient-reported text",
        )
    if not _REPORTED_PHRASING.search(claim.text):
        fail(
            CheckFailure.PATIENT_SOURCE_PHRASING,
            "a claim from patient-reported text must be phrased as reported "
            "(for example 'patient reports ...')",
        )


def _patient_sourced(entry: TimelineEntry) -> bool:
    return entry.author_role is Role.PATIENT or entry.type in _PATIENT_SOURCED_TYPES


def _mentions(text: str, entity: str) -> bool:
    return re.search(rf"\b{re.escape(entity)}\b", text, re.IGNORECASE) is not None


def _normalise(text: str) -> str:
    return " ".join(text.split())
