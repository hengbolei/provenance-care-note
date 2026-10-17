import re
from collections.abc import Iterable
from re import Pattern
from typing import ClassVar

from pydantic import BaseModel, Field


class RedactionResult(BaseModel):
    text: str
    counts: dict[str, int] = Field(default_factory=dict)


class PHIRedactionGateway:
    """Mandatory deterministic boundary for any future external model adapter."""

    _patterns: ClassVar[dict[str, Pattern[str]]] = {
        "national_id": re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE),
        "labelled_id": re.compile(
            r"\b(?:IC|ID|NRIC|passport)\s*(?:number|no\.?|#)?\s*[:=-]?\s*[A-Z0-9-]{6,20}\b",
            re.IGNORECASE,
        ),
        "phone": re.compile(
            r"(?<!\w)(?:\+?65[ -]?)?[689]\d{3}[ -]?\d{4}(?!\w)|"
            r"(?<!\w)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\w)"
        ),
    }

    def redact(self, text: str, known_names: Iterable[str] = ()) -> RedactionResult:
        redacted = text
        counts: dict[str, int] = {}
        for name in sorted(set(known_names), key=len, reverse=True):
            if not name.strip():
                continue
            redacted, count = re.subn(
                re.escape(name.strip()), "[REDACTED_NAME]", redacted, flags=re.IGNORECASE
            )
            counts["name"] = counts.get("name", 0) + count
        for label, pattern in self._patterns.items():
            redacted, count = pattern.subn(f"[REDACTED_{label.upper()}]", redacted)
            counts[label] = count
        return RedactionResult(text=redacted, counts=counts)

    def prepare_for_llm(self, text: str, known_names: Iterable[str] = ()) -> str:
        """Return only redacted text so callers cannot accidentally bypass the boundary."""
        return self.redact(text, known_names).text
