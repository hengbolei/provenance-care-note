import re
from collections.abc import Iterable

from carenote.domain.models import Span, TimelineEntry

MAX_RESULTS = 50

# Digits and letters are separate tokens so "500mg" and "500 mg" match each other.
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z]+")
# A full stop ends a passage only before whitespace, so "2.5 mg" stays in one passage.
_PASSAGE = re.compile(r"(?:[^.!?;\n]|\.(?=\S))+[.!?;]?")
_STOPWORDS = frozenset(
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


def _terms(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text.lower()) if token not in _STOPWORDS}


def keyword_search(entries: Iterable[TimelineEntry], query: str, k: int) -> list[Span]:
    """Rank sentence-sized passages by how many distinct query terms they contain.

    Every hit carries exact character offsets into its entry, so it can be cited as is. Ties
    favour more recent entries, then earlier passages in the same entry. Deterministic and
    dependency-free, so it serves tests, the offline demo and the encrypted snapshot store alike.
    """
    if not 1 <= k <= MAX_RESULTS:
        raise ValueError(f"k must be between 1 and {MAX_RESULTS}")
    wanted = _terms(query)
    if not wanted:
        return []
    scored: list[tuple[int, float, str, int, Span]] = []
    for entry in entries:
        for match in _PASSAGE.finditer(entry.content):
            raw = match.group()
            start = match.start() + len(raw) - len(raw.lstrip())
            end = match.end() - (len(raw) - len(raw.rstrip()))
            if end <= start:
                continue
            text = entry.content[start:end]
            score = len(wanted & _terms(text))
            if score:
                span = Span(entry_id=entry.id, start=start, end=end, text=text)
                scored.append((score, entry.timestamp.timestamp(), str(entry.id), start, span))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    return [span for *_, span in scored[:k]]
