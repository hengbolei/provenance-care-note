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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from uuid import UUID

from carenote.agent.state import (
    CheckFailure,
    CheckPoint,
    CheckPointCheck,
    CheckPointFailure,
    Citation,
    Claim,
    ClaimCheck,
    DraftReview,
    ReferenceCitation,
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
# A check point must read as something to verify, not as a fact or an order.
_CHECK_PHRASING = re.compile(
    r"\b(?:check|checks|checked|checking|confirm|confirms|verify|verifies|review|reassess|"
    r"assess|ensure|whether)\b"
    r"|\bno\b[^.]{0,80}?\b(?:appears?|recorded|documented|found|shown)\b"
    r"|\bnot (?:recorded|documented|shown)\b",
    re.IGNORECASE,
)
_TREATMENT_VERBS = (
    r"start(?:ed)?|stop(?:ped)?|begin|increased?|decreased?|reduced?|doubled?|halved?|give|given|"
    r"prescribed?|administer(?:ed)?|switch(?:ed)?|discontinued?|resumed?|restart(?:ed)?|titrated?"
)
# An order to treat: an imperative treatment verb, or "should/must/recommend ... <verb>".
_TREATMENT_ADVICE = re.compile(
    rf"^\s*(?:{_TREATMENT_VERBS})\b"
    rf"|\b(?:should|must|needs? to|recommend(?:ed|s)?|advised?)\s+"
    rf"(?:(?:be|the|patient|they|we|you|clinician)\s+){{0,2}}(?:{_TREATMENT_VERBS})\b",
    re.IGNORECASE,
)
# Text after "whether" or "if" is a question, so "check whether the dose should be reduced" is fine.
_QUESTION_CLAUSE = re.compile(r"\b(?:whether|if)\b.*", re.IGNORECASE | re.DOTALL)
_DOSE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|micrograms?|g|units?|ml|mmol)\b", re.IGNORECASE)
# (original text, redaction of the text the model saw) for each reference version in the run.
ReferenceTexts = Mapping[tuple[UUID, int], tuple[str, RedactionResult]]

Fail = Callable[[CheckFailure, str], None]


def verify_draft(
    draft: DraftReview,
    entries: Mapping[UUID, TimelineEntry],
    conflicts: Sequence[Conflict] = (),
    redactions: Mapping[UUID, RedactionResult] | None = None,
    reference_ids: Collection[UUID] = (),
) -> VerificationReport:
    """Check every claim in `draft` against the entries the run loaded.

    `entries` must hold only entries the requesting clinician can read; a citation to anything
    else fails as unknown. Returns one `ClaimCheck` per claim, in order, and the deterministic
    conflicts that no verified claim mentions.
    """
    redactions = redactions or {}
    known_medications = _known_medications(entries.values())
    checks = [
        _check_claim(index, claim, entries, redactions, known_medications, reference_ids)
        for index, claim in enumerate(draft.claims)
    ]
    verified_texts = [draft.claims[check.claim_index].text for check in checks if check.verified]
    uncovered = [
        conflict.id
        for conflict in conflicts
        if not any(_mentions(text, conflict.entity) for text in verified_texts)
    ]
    return VerificationReport(checks=checks, uncovered_conflict_ids=uncovered)


def resolve_citation(
    citation: Citation,
    entries: Mapping[UUID, TimelineEntry],
    redactions: Mapping[UUID, RedactionResult] | None = None,
) -> Citation | None:
    """Checks 1–3 for a single citation, such as a candidate highlight's.

    Returns the citation mapped to original offsets with its exact original quote, or None when
    the entry is out of context or the quote cannot be found.
    """
    entry = entries.get(citation.entry_id)
    if entry is None:
        return None
    redaction = (redactions or {}).get(entry.id)
    return _resolve_citation(citation, entry, redaction, "citation", lambda *_: None)


def _check_claim(
    index: int,
    claim: Claim,
    entries: Mapping[UUID, TimelineEntry],
    redactions: Mapping[UUID, RedactionResult],
    known_medications: set[str],
    reference_ids: Collection[UUID] = (),
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
        if entry is None and citation.entry_id in reference_ids:
            fail(
                CheckFailure.REFERENCE_AS_FACT,
                f"{label}: cites a reference document; claims about the patient must cite the "
                "record (use a check point for reference material)",
            )
            continue
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
    located = _resolve_quote(
        citation.start,
        citation.end,
        citation.quote,
        entry.content,
        redaction,
        lambda out_of_bounds, message: fail(
            CheckFailure.OUT_OF_BOUNDS if out_of_bounds else CheckFailure.QUOTE_MISMATCH,
            f"{label}: {message}",
        ),
        "entry",
    )
    if located is None:
        return None
    start, end = located
    # The quote becomes the exact original text. model_copy skips validation on purpose: un-redacting
    # can lengthen a quote past the model-facing limit, and this citation is never sent back out.
    return citation.model_copy(
        update={"start": start, "end": end, "quote": entry.content[start:end]}
    )


def _resolve_quote(
    start: int,
    end: int,
    quote: str,
    original: str,
    redaction: RedactionResult | None,
    fail: Callable[[bool, str], None],
    noun: str,
) -> tuple[int, int] | None:
    """Find `quote` in the text the model saw and return its offsets in the original text."""
    shown = redaction.text if redaction is not None else original
    in_bounds = end <= len(shown)
    if in_bounds and _normalise(shown[start:end]) == _normalise(quote):
        found = start, end
    else:
        located = _locate(shown, quote, near=start)
        if located is None:
            if in_bounds:
                fail(False, f"quoted text is not in the {noun}")
            else:
                fail(True, f"[{start}, {end}) is outside a {noun} of length {len(shown)}")
            return None
        found = located
    return redaction.to_original(*found) if redaction is not None else found


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


def verify_check_points(
    draft: DraftReview,
    references: ReferenceTexts,
    entries: Mapping[UUID, TimelineEntry],
    redactions: Mapping[UUID, RedactionResult] | None = None,
) -> list[CheckPointCheck]:
    """Check every check point against its reference passage and any record citations.

    `references` must hold only the reference versions this run read. A check point passes when
    its reference quote and record citations resolve, it reads as a check, it gives no treatment
    or dosing order, and every medication and dose it names appears in what it cites.
    """
    redactions = redactions or {}
    known_medications = _known_medications(entries.values())
    return [
        _check_check_point(index, item, references, entries, redactions, known_medications)
        for index, item in enumerate(draft.check_points)
    ]


def _check_check_point(
    index: int,
    item: CheckPoint,
    references: ReferenceTexts,
    entries: Mapping[UUID, TimelineEntry],
    redactions: Mapping[UUID, RedactionResult],
    known_medications: set[str],
) -> CheckPointCheck:
    failures: list[CheckPointFailure] = []
    detail: list[str] = []

    def fail(failure: CheckPointFailure, message: str) -> None:
        if failure not in failures:
            failures.append(failure)
        detail.append(message)

    reference = _resolve_reference(item.reference, references, fail)
    citations: list[Citation] = []
    for position, citation in enumerate(item.citations, start=1):
        label = f"record citation {position}"
        entry = entries.get(citation.entry_id)
        if entry is None:
            fail(CheckPointFailure.RECORD_CITATION, f"{label}: entry is not in this review")
            continue
        resolved = _resolve_citation(
            citation,
            entry,
            redactions.get(entry.id),
            label,
            lambda _failure, message: fail(CheckPointFailure.RECORD_CITATION, message),
        )
        if resolved is not None:
            citations.append(resolved)

    if not _CHECK_PHRASING.search(item.text):
        fail(
            CheckPointFailure.CHECK_POINT_PHRASING,
            "a check point must say what to check, confirm or verify",
        )
    if _TREATMENT_ADVICE.search(_QUESTION_CLAUSE.sub("", item.text)):
        fail(
            CheckPointFailure.RECOMMENDS_TREATMENT,
            "a check point must not tell anyone to start, stop or change a treatment",
        )
    if reference is not None and len(citations) == len(item.citations):
        cited = " ".join([reference.quote, *(citation.quote for citation in citations)])
        _check_check_point_support(item.text, cited, known_medications, fail)
    return CheckPointCheck(
        check_point_index=index,
        failures=failures,
        detail=detail,
        reference=reference,
        citations=citations,
    )


def _resolve_reference(
    citation: ReferenceCitation,
    references: ReferenceTexts,
    fail: Callable[[CheckPointFailure, str], None],
) -> ReferenceCitation | None:
    texts = references.get((citation.document_id, citation.version))
    if texts is None:
        fail(
            CheckPointFailure.UNKNOWN_REFERENCE,
            "reference: that document version is not one this review read",
        )
        return None
    original, redaction = texts
    located = _resolve_quote(
        citation.start,
        citation.end,
        citation.quote,
        original,
        redaction,
        lambda _out_of_bounds, message: fail(
            CheckPointFailure.REFERENCE_MISMATCH, f"reference: {message}"
        ),
        "reference",
    )
    if located is None:
        return None
    start, end = located
    return citation.model_copy(update={"start": start, "end": end, "quote": original[start:end]})


def _check_check_point_support(
    text: str,
    cited: str,
    known_medications: set[str],
    fail: Callable[[CheckPointFailure, str], None],
) -> None:
    """Every medication and dose a check point names must appear in what it cites."""
    cited_words = set(_WORD.findall(cited.lower()))
    cited_compact = re.sub(r"\s+", "", cited.lower())
    cited_doses = {
        fact.entity for fact in extract_entities(cited) if fact.category is ConflictCategory.DOSE
    }
    for match in _DOSE.finditer(text):
        value = re.sub(r"\s+", "", match.group().lower())
        if value not in cited_compact:
            fail(
                CheckPointFailure.UNSUPPORTED_ENTITIES,
                f"dose {match.group()} does not appear in the cited text",
            )
    for fact in extract_entities(text):
        if fact.category is ConflictCategory.MEDICATION and not (
            fact.entity in known_medications or fact.entity in cited_doses
        ):
            continue
        if fact.entity not in cited_words:
            fail(
                CheckPointFailure.UNSUPPORTED_ENTITIES,
                f"'{fact.entity}' does not appear in the cited text",
            )


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
