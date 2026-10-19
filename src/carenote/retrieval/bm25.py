import math
from collections import Counter
from collections.abc import Iterable

from carenote.domain.models import Span, TimelineEntry
from carenote.retrieval.synonyms import expand
from carenote.retrieval.text import concepts, passages

MAX_RESULTS = 50


class BM25Retriever:
    """BM25 over the sentence-sized passages of the entries it is given.

    Document frequencies come from those passages only, so a word that fills a long routine
    timeline ("observation", "check-in") weighs little and a rare one ("apixaban") weighs a lot.
    Query words are folded through the synonym table, and a drug class also matches its members at
    a lower weight. Each query concept scores once, by its best-matching variant.

    `b` defaults to 0: passages are single sentences, whose length says little about relevance.
    Ties favour more recent entries, then earlier passages in the same entry. Deterministic and
    dependency-free, so it serves tests, the offline demo and both repositories alike.
    """

    def __init__(self, k1: float = 1.2, b: float = 0.0) -> None:
        self.k1 = k1
        self.b = b

    def search(self, entries: Iterable[TimelineEntry], query: str, k: int) -> list[Span]:
        if not 1 <= k <= MAX_RESULTS:
            raise ValueError(f"k must be between 1 and {MAX_RESULTS}")
        wanted = [expand(term) for term in dict.fromkeys(concepts(query))]
        if not wanted:
            return []

        units = [
            (entry, start, end, Counter(concepts(entry.content[start:end])))
            for entry in entries
            for start, end in passages(entry.content)
        ]
        if not units:
            return []
        frequency = Counter(term for *_, terms in units for term in terms)
        average = sum(sum(terms.values()) for *_, terms in units) / len(units)

        def idf(term: str) -> float:
            seen = frequency[term]
            return math.log(1 + (len(units) - seen + 0.5) / (seen + 0.5))

        scored: list[tuple[float, float, str, int, Span]] = []
        for entry, start, end, terms in units:
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
                span = Span(entry_id=entry.id, start=start, end=end, text=entry.content[start:end])
                # Rounded so equal matches tie exactly and fall through to recency.
                scored.append(
                    (round(score, 9), entry.timestamp.timestamp(), str(entry.id), start, span)
                )
        scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
        return [span for *_, span in scored[:k]]
