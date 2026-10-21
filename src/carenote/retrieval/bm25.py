import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from carenote.domain.models import Span, TimelineEntry
from carenote.retrieval.synonyms import expand
from carenote.retrieval.text import concepts, passages

MAX_RESULTS = 50


@dataclass(frozen=True)
class Passage:
    """A sentence-sized span of some text, with what it came from and how to break ties."""

    source: Any
    start: int
    end: int
    text: str
    # Higher wins a tie (e.g. a later timestamp), then `order`, then the earlier `start`.
    recency: float = 0.0
    order: str = ""


def split_passages(
    source: Any, content: str, recency: float = 0.0, order: str = ""
) -> list[Passage]:
    return [
        Passage(source, start, end, content[start:end], recency, order)
        for start, end in passages(content)
    ]


class BM25Retriever:
    """BM25 over sentence-sized passages.

    Document frequencies come from the passages searched, so a word that fills a long routine
    timeline ("observation", "check-in") weighs little and a rare one ("apixaban") weighs a lot.
    Query words are folded through the synonym table, and a drug class also matches its members at
    a lower weight. Each query concept scores once, by its best-matching variant.

    `b` defaults to 0: passages are single sentences, whose length says little about relevance.
    Ties favour more recent passages, then earlier passages in the same text. Deterministic and
    dependency-free, so it serves tests, the offline demo, both repositories and the reference
    knowledge base alike.
    """

    def __init__(self, k1: float = 1.2, b: float = 0.0) -> None:
        self.k1 = k1
        self.b = b

    def search(self, entries: Iterable[TimelineEntry], query: str, k: int) -> list[Span]:
        units = [
            passage
            for entry in entries
            for passage in split_passages(
                entry, entry.content, entry.timestamp.timestamp(), str(entry.id)
            )
        ]
        return [
            Span(entry_id=item.source.id, start=item.start, end=item.end, text=item.text)
            for item in self.rank(units, query, k)
        ]

    def rank(self, units: Sequence[Passage], query: str, k: int) -> list[Passage]:
        """The k best passages for the query, best first."""
        if not 1 <= k <= MAX_RESULTS:
            raise ValueError(f"k must be between 1 and {MAX_RESULTS}")
        wanted = [expand(term) for term in dict.fromkeys(concepts(query))]
        if not wanted or not units:
            return []
        counted = [(unit, Counter(concepts(unit.text))) for unit in units]
        frequency = Counter(term for _, terms in counted for term in terms)
        average = sum(sum(terms.values()) for _, terms in counted) / len(counted)

        def idf(term: str) -> float:
            seen = frequency[term]
            return math.log(1 + (len(counted) - seen + 0.5) / (seen + 0.5))

        scored: list[tuple[float, float, str, int, Passage]] = []
        for unit, terms in counted:
            norm = self.k1 * (1 - self.b + self.b * sum(terms.values()) / (average or 1))
            score = 0.0
            for variants in wanted:
                score += max(
                    (
                        weight * idf(term) * terms[term] * (self.k1 + 1) / (terms[term] + norm)
                        for term, weight in variants
                        if terms[term]
                    ),
                    default=0.0,
                )
            if score:
                # Rounded so equal matches tie exactly and fall through to recency.
                scored.append((round(score, 9), unit.recency, unit.order, unit.start, unit))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
        return [unit for *_, unit in scored[:k]]
