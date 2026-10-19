"""Retrieval over one patient's timeline.

Retrievers run in application memory over entries a repository has already scoped to one patient
and clinic, so one implementation serves the in-memory and the encrypted-snapshot Postgres
repositories (see the roadmap's Decisions). Hits are `Span`s with exact offsets into their entry,
so they can be cited as is.
"""

from collections.abc import Iterable
from typing import Protocol

from carenote.domain.models import Span, TimelineEntry
from carenote.retrieval.bm25 import MAX_RESULTS, BM25Retriever


class Retriever(Protocol):
    def search(self, entries: Iterable[TimelineEntry], query: str, k: int) -> list[Span]:
        """The k best passages for the query, best first; raises ValueError for k out of range."""
        ...


__all__ = ["MAX_RESULTS", "BM25Retriever", "Retriever"]
