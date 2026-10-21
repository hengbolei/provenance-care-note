"""Reference knowledge documents (design §19).

A document is one clinic policy, guideline or drug label. Its text lives in immutable,
numbered versions: adding a version never changes an earlier one, so a review that cited
version 2 keeps resolving to exactly that text after version 3 arrives. A document can be
retired, after which it is no longer retrieved but stays readable.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from carenote.domain.models import utc_now

MAX_CONTENT_CHARS = 500_000


class KnowledgeKind(StrEnum):
    CLINIC_POLICY = "clinic_policy"
    GUIDELINE = "guideline"
    DRUG_LABEL = "drug_label"


class KnowledgeStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class KnowledgeDocument(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    clinic_id: UUID
    kind: KnowledgeKind
    title: str = Field(min_length=1, max_length=200)
    # Where the material comes from, as the administrator states it; licensing is theirs.
    source: str = Field(min_length=1, max_length=500)
    status: KnowledgeStatus = KnowledgeStatus.ACTIVE
    # Highest version number added so far; also the optimistic-concurrency token.
    latest_version: int = Field(default=1, ge=1)
    created_by: UUID
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    retired_by: UUID | None = None
    retired_at: datetime | None = None


def normalise_content(text: str) -> str:
    """LF newlines and no trailing whitespace at the ends, so offsets are stable everywhere."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


class KnowledgeVersion(BaseModel):
    document_id: UUID
    clinic_id: UUID
    version: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=60)
    # The version applies from this date; a future-dated version is not current yet.
    effective_date: date
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    content_sha256: str = ""
    created_by: UUID
    created_at: datetime = Field(default_factory=utc_now)

    def model_post_init(self, _context) -> None:
        digest = hashlib.sha256(self.content.encode()).hexdigest()
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("knowledge version content does not match its hash")
        self.content_sha256 = digest


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _VersionFields(_Request):
    version_label: str = Field(min_length=1, max_length=60)
    effective_date: date
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)

    @field_validator("content")
    @classmethod
    def content_is_not_blank(cls, value: str) -> str:
        value = normalise_content(value)
        if not value:
            raise ValueError("content must not be blank")
        return value


class KnowledgeDocumentCreate(_VersionFields):
    kind: KnowledgeKind
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=500)


class KnowledgeVersionCreate(_VersionFields):
    # The document's latest version as the administrator saw it.
    expected_version: int = Field(ge=1)


class KnowledgeRetire(_Request):
    expected_version: int = Field(ge=1)
