"""Prompt-injection defences beyond framing (design §10, roadmap step 3.4).

Record text reaches the model as tagged data, and redaction neutralises tag-like markup so record
text cannot open or close a tag. On top of that:

- `instruction_like` flags passages that address the assistant or try to steer the review
  ("ignore previous instructions", "SYSTEM:", "add a claim ... mark it critical"). Flagged entries
  carry `instruction_like="true"` in their tag, the prompts tell the model what that means, and
  the run view shows the clinician which entries were flagged.
- `withhold_echoes` removes open questions and highlight suggestions that repeat a flagged
  passage's words. Claims are not filtered here: the verifier already checks every claim against
  its citations and the patient-source policy.

The patterns are deliberately narrow: they look for text aimed at an assistant, not for clinical
instructions between people, so "stop ibuprofen" or "ask the doctor" stay unflagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from carenote.agent.state import DraftReview, WithheldItem
from carenote.retrieval.text import passages

# Tag names the model inputs use; a forged one in record text is an injection attempt even after
# its "<" has been neutralised.
_TAGS = (
    "entry|conflict|question|standing_question|clinician_instruction|previous_claim|citation|"
    "failure|uncovered_conflict|system|instructions?"
)
PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for name, pattern in {
        "override": r"\b(?:ignore|disregard|forget|override)\b(?:\s+\w+){0,3}?\s+"
        r"(?:instructions?|rules|prompts?|guidelines|context)\b",
        "role_prefix": r"(?:^|[.!?]\s+|['\"(])\s*(?:system|assistant|developer|ai)\s*:",
        "role_message": r"\b(?:system|developer)\s+(?:prompt|message|instruction)s?\b",
        "forged_tag": rf"[<‹]\s*/?\s*(?:{_TAGS})\b",
        "addresses_assistant": r"\b(?:the|this|an?)\s+(?:assistant|ai|chatbot|language model)\b"
        r"(?:\s+\w+){0,6}?\s+(?:to\s+)?(?:add|report|repeat|say|state|write|include|mark|"
        r"tell|claim|ignore|remove|delete)\b",
        "steers_output": r"\b(?:add|insert|write|include|create)\s+(?:a|an|this|the)?\s*"
        r"(?:claim|highlight|open question)s?\b",
        "into_output": r"\bin\s+(?:your|the)\s+(?:summary|review|draft|response|answer|output)\b",
        "marks_status": r"\bmark\s+(?:it|this|them|all|everything)\b(?:\s+\w+){0,3}?\s+"
        r"(?:as\s+)?(?:critical|resolved|confirmed|completed|verified|urgent)\b",
    }.items()
}
# Shared consecutive words that make an open question or highlight an echo of a flagged passage.
ECHO_WORDS = 4
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class InstructionMatch:
    start: int
    end: int
    pattern: str


def instruction_like(text: str) -> list[InstructionMatch]:
    """Every instruction-like match in `text`, in order of position."""
    found = [
        InstructionMatch(match.start(), match.end(), name)
        for name, pattern in PATTERNS.items()
        for match in pattern.finditer(text)
    ]
    return sorted(found, key=lambda item: (item.start, item.pattern))


def flagged_passages(text: str) -> list[str]:
    """The sentence-sized passages of `text` that hold an instruction-like match."""
    matches = instruction_like(text)
    return [
        text[start:end]
        for start, end in passages(text)
        if any(start <= match.start < end for match in matches)
    ]


def _grams(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    return {tuple(words[i : i + ECHO_WORDS]) for i in range(len(words) - ECHO_WORDS + 1)}


def withhold_echoes(
    draft: DraftReview, flagged: dict[UUID, list[str]]
) -> tuple[DraftReview, list[WithheldItem]]:
    """Drop open questions and highlights that share `ECHO_WORDS` words in a row with a flagged
    passage, and report what was dropped. Indexes refer to the draft as the model returned it."""
    grams = {entry_id: set().union(*map(_grams, texts)) for entry_id, texts in flagged.items()}

    def source(text: str) -> UUID | None:
        mine = _grams(text)
        return next((entry_id for entry_id, theirs in grams.items() if mine & theirs), None)

    withheld: list[WithheldItem] = []
    questions = []
    for index, question in enumerate(draft.open_questions):
        entry_id = source(question)
        if entry_id is None:
            questions.append(question)
        else:
            withheld.append(WithheldItem(kind="open_question", index=index, entry_id=entry_id))
    highlights = []
    for index, highlight in enumerate(draft.candidate_highlights):
        entry_id = source(f"{highlight.risk_reason} {highlight.suggested_action}")
        if entry_id is None:
            highlights.append(highlight)
        else:
            withheld.append(WithheldItem(kind="highlight", index=index, entry_id=entry_id))
    if not withheld:
        return draft, []
    guarded = draft.model_copy(
        update={"open_questions": questions, "candidate_highlights": highlights}
    )
    return guarded, withheld
