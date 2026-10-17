import re
from collections.abc import Iterable
from re import Pattern
from typing import ClassVar

from pydantic import BaseModel, Field


class RedactedSpan(BaseModel):
    """One replacement, located in both the original and the redacted text."""

    label: str
    original_start: int
    original_end: int
    redacted_start: int
    redacted_end: int


class RedactionResult(BaseModel):
    text: str
    counts: dict[str, int] = Field(default_factory=dict)
    spans: list[RedactedSpan] = Field(default_factory=list)

    def to_original(self, start: int, end: int) -> tuple[int, int]:
        """Map a [start, end) range in the redacted text back to the original text.

        A range that touches a placeholder expands to cover the whole original value, so a
        citation can never point at half a name or identifier.
        """
        self._check_range(start, end, len(self.text))
        return (
            self._map(start, is_end=False, to_original=True),
            self._map(end, is_end=True, to_original=True),
        )

    def to_redacted(self, start: int, end: int, original_length: int) -> tuple[int, int]:
        """Map a [start, end) range in the original text into the redacted text."""
        self._check_range(start, end, original_length)
        return (
            self._map(start, is_end=False, to_original=False),
            self._map(end, is_end=True, to_original=False),
        )

    def _map(self, position: int, *, is_end: bool, to_original: bool) -> int:
        delta = 0
        for span in self.spans:
            if to_original:
                source_start, source_end = span.redacted_start, span.redacted_end
                target_start, target_end = span.original_start, span.original_end
            else:
                source_start, source_end = span.original_start, span.original_end
                target_start, target_end = span.redacted_start, span.redacted_end
            inside = (
                source_start < position <= source_end
                if is_end
                else source_start <= position < source_end
            )
            if inside:
                return target_end if is_end else target_start
            if position < source_end:
                break
            delta += (target_end - target_start) - (source_end - source_start)
        return position + delta

    @staticmethod
    def _check_range(start: int, end: int, length: int) -> None:
        if not 0 <= start <= end <= length:
            raise ValueError(f"range [{start}, {end}) is outside text of length {length}")


class PHIRedactionGateway:
    """Mandatory deterministic boundary for any future external model adapter."""

    _patterns: ClassVar[dict[str, Pattern[str]]] = {
        "national_id": re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE),
        "labelled_id": re.compile(
            # The label must be a whole word and the value must contain a digit, so ordinary
            # words such as "idiopathic" or "ID verified" are not treated as identifiers.
            r"\b(?:IC|ID|NRIC|passport)\b\s*(?:number\b|no\b\.?|#)?\s*[:=-]?\s*"
            r"(?=[A-Z-]*\d)[A-Z0-9-]{6,20}\b",
            re.IGNORECASE,
        ),
        "phone": re.compile(
            r"(?<!\w)(?:\+?65[ -]?)?[689]\d{3}[ -]?\d{4}(?!\w)|"
            r"(?<!\w)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\w)"
        ),
    }

    def redact(self, text: str, known_names: Iterable[str] = ()) -> RedactionResult:
        # Matches are found on the original text and applied in one pass, so every replacement
        # keeps exact offsets. Earlier rules win on overlap: names, then patterns in order.
        matches: list[tuple[int, int, str]] = []
        counts: dict[str, int] = {}
        names = [name.strip() for name in known_names if name.strip()]
        rules: list[tuple[str, Pattern[str]]] = [
            ("name", re.compile(re.escape(name), re.IGNORECASE))
            for name in sorted(set(names), key=len, reverse=True)
        ]
        rules.extend(self._patterns.items())
        if names:
            counts["name"] = 0
        for label, pattern in rules:
            counts.setdefault(label, 0)
            for match in pattern.finditer(text):
                start, end = match.span()
                overlaps = any(
                    start < taken_end and taken_start < end for taken_start, taken_end, _ in matches
                )
                if start == end or overlaps:
                    continue
                matches.append((start, end, label))
                counts[label] += 1

        pieces: list[str] = []
        spans: list[RedactedSpan] = []
        cursor = 0
        redacted_length = 0
        for start, end, label in sorted(matches):
            pieces.append(text[cursor:start])
            redacted_length += start - cursor
            placeholder = f"[REDACTED_{label.upper()}]"
            spans.append(
                RedactedSpan(
                    label=label,
                    original_start=start,
                    original_end=end,
                    redacted_start=redacted_length,
                    redacted_end=redacted_length + len(placeholder),
                )
            )
            pieces.append(placeholder)
            redacted_length += len(placeholder)
            cursor = end
        pieces.append(text[cursor:])
        return RedactionResult(text="".join(pieces), counts=counts, spans=spans)

    def prepare_for_llm(self, text: str, known_names: Iterable[str] = ()) -> str:
        """Return only redacted text so callers cannot accidentally bypass the boundary."""
        return self.redact(text, known_names).text
