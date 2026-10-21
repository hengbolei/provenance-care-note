"""Search over the reference versions currently in effect, with the record's BM25 retriever."""

from __future__ import annotations

from collections.abc import Iterable

from carenote.knowledge.models import KnowledgeDocument, KnowledgeVersion, ReferenceSpan
from carenote.retrieval.bm25 import BM25Retriever, split_passages

_RETRIEVER = BM25Retriever()


def search_references(
    current: Iterable[tuple[KnowledgeDocument, KnowledgeVersion]], query: str, k: int
) -> list[ReferenceSpan]:
    """The k best passages for the query. Pass only current versions of active documents
    (`KnowledgeService.current_versions`), so superseded and retired text is never found."""
    units = [
        passage
        for document, version in current
        for passage in split_passages(
            (document, version),
            version.content,
            # Ties favour the version that took effect most recently.
            float(version.effective_date.toordinal()),
            f"{document.id}:{version.version}",
        )
    ]
    return [
        ReferenceSpan(
            document_id=document.id,
            version=version.version,
            kind=document.kind,
            start=item.start,
            end=item.end,
            text=item.text,
        )
        for item in _RETRIEVER.rank(units, query, k)
        for document, version in [item.source]
    ]
