"""Tokens and passages shared by every retriever."""

import re
from collections.abc import Iterator

from carenote.retrieval.synonyms import concept

# Digits and letters are separate tokens so "500mg" and "500 mg" match each other.
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z]+")
# A full stop ends a passage only before whitespace, so "2.5 mg" stays in one passage.
_PASSAGE = re.compile(r"(?:[^.!?;\n]|\.(?=\S))+[.!?;]?")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "since",
        "the",
        "to",
        "was",
        "were",
        "with",
    ]
)


def concepts(text: str) -> list[str]:
    """The text's tokens as concepts: stopwords dropped, synonyms and plurals folded together."""
    return [concept(token) for token in _TOKEN.findall(text.lower()) if token not in STOPWORDS]


def passages(content: str) -> Iterator[tuple[int, int]]:
    """Sentence-sized passages as (start, end) offsets, trimmed of surrounding whitespace."""
    for match in _PASSAGE.finditer(content):
        raw = match.group()
        start = match.start() + len(raw) - len(raw.lstrip())
        end = match.end() - (len(raw) - len(raw.rstrip()))
        if end > start:
            yield start, end
