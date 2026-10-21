from datetime import UTC, date, datetime
from uuid import UUID

from carenote.domain.models import (
    EntryOrigin,
    EntryType,
    Highlight,
    Patient,
    ProvenancePointer,
    ProvenanceSource,
    ResourceRevision,
    ReviewStatus,
    RiskLevel,
    Role,
    SectionRevision,
    SectionState,
    SourceArtifact,
    SourceArtifactKind,
    SourceArtifactPointer,
    TimelineEntry,
)
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.importance import DeterministicImportanceScorer

DEMO_CLINIC_ID = UUID("10000000-0000-4000-8000-000000000001")
DEMO_PATIENT_ID = UUID("20000000-0000-4000-8000-000000000001")
DEMO_STAFF_ID = UUID("30000000-0000-4000-8000-000000000001")
DEMO_CLINICIAN_ID = UUID("40000000-0000-4000-8000-000000000001")
DEMO_ADMIN_ID = UUID("70000000-0000-4000-8000-000000000001")


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _origin(
    source: ProvenanceSource,
    source_id: str,
    label: str,
    artifact: SourceArtifact | None = None,
    evidence_text: str | None = None,
) -> EntryOrigin:
    pointer = None
    if artifact is not None and evidence_text is not None:
        start = artifact.content.index(evidence_text)
        pointer = SourceArtifactPointer(
            source_id=artifact.id,
            start=start,
            end=start + len(evidence_text),
        )
    return EntryOrigin(
        source=source,
        source_id=source_id,
        source_label=label,
        source_pointer=pointer,
    )


def _highlight(
    highlight_id: UUID,
    entry: TimelineEntry,
    text: str,
    risk_reason: str,
    suggested_action: str,
    priority: int,
    status: ReviewStatus,
    patient_instruction: str | None = None,
    risk_level: RiskLevel = RiskLevel.MODERATE,
    clinical_entities: list[str] | None = None,
) -> Highlight:
    start = entry.content.index(text)
    return Highlight(
        id=highlight_id,
        patient_id=entry.patient_id,
        text=text,
        risk_reason=risk_reason,
        suggested_action=suggested_action,
        patient_instruction=patient_instruction,
        risk_level=risk_level,
        clinical_entities=clinical_entities or [],
        priority=priority,
        status=status,
        provenance_pointer=ProvenancePointer(
            entry_id=entry.id,
            start=start,
            end=start + len(text),
        ),
    )


def seed_demo_data(repository: InMemoryCareNoteRepository) -> None:
    """Load deterministic synthetic data; safe to call more than once."""
    patient = Patient(
        id=DEMO_PATIENT_ID,
        clinic_id=DEMO_CLINIC_ID,
        display_name="Maya Chen (Synthetic)",
        date_of_birth=date(1985, 4, 12),
        medical_record_number="SYN-000184",
        pronouns="she/her",
    )
    repository.add_patient(patient)

    artifacts = {
        "patient": SourceArtifact(
            id="ai-patient-session-20260821",
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            kind=SourceArtifactKind.MESSAGE_THREAD,
            source=ProvenanceSource.PATIENT_SESSION,
            label="Synthetic AI patient session messages",
            content=(
                "Patient: I felt light-headed twice before breakfast this week.\n"
                "Assistant: Did you lose consciousness?\n"
                "Patient: No, I did not pass out."
            ),
            timestamp=_timestamp("2026-08-21T09:00:00"),
        ),
        "nurse": SourceArtifact(
            id="ai-nurse-consult-20260823",
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            kind=SourceArtifactKind.TRANSCRIPT,
            source=ProvenanceSource.NURSE_CONSULT,
            label="Synthetic nurse consultation transcript",
            content=(
                "Nurse: Which medicine and dose did you take this morning?\n"
                "Patient: Lisinopril 20 mg. I became dizzy afterward.\n"
                "Patient: My home blood pressure was 98/62 mmHg.\n"
                "Nurse: Hydrate and seek urgent help if you faint."
            ),
            timestamp=_timestamp("2026-08-23T11:35:00"),
        ),
        "doctor": SourceArtifact(
            id="ai-doctor-consult-20260825",
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            kind=SourceArtifactKind.TRANSCRIPT,
            source=ProvenanceSource.DOCTOR_CONSULT,
            label="Synthetic doctor consultation transcript",
            content=(
                "Clinician: The low blood pressure may be related to the new medicine.\n"
                "Clinician: The follow-up blood pressure check is still unresolved.\n"
                "Patient: I understand that the team will arrange the check."
            ),
            timestamp=_timestamp("2026-08-25T15:05:00"),
        ),
    }
    for artifact in artifacts.values():
        repository.add_source_artifact(artifact)

    entries = [
        TimelineEntry(
            id=UUID("50000000-0000-4000-8000-000000000001"),
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            author_role=Role.PATIENT,
            author_id=patient.id,
            timestamp=_timestamp("2026-08-20T08:30:00"),
            type=EntryType.PATIENT_NOTE,
            title="Pre-visit symptom update",
            content="I have felt light-headed twice this week, usually before breakfast.",
            origin=_origin(
                ProvenanceSource.MANUAL_ENTRY,
                "patient-message-20260820",
                "Synthetic patient portal message",
            ),
            internal=False,
        ),
        TimelineEntry(
            id=UUID("50000000-0000-4000-8000-000000000002"),
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            author_role=Role.SYSTEM,
            author_id=None,
            timestamp=_timestamp("2026-08-21T09:05:00"),
            type=EntryType.AI_PATIENT_SESSION_SUMMARY,
            title="AI patient session summary",
            content=(
                "Patient reports intermittent light-headedness before breakfast and no loss "
                "of consciousness. This summary is AI-generated and needs review."
            ),
            origin=_origin(
                ProvenanceSource.PATIENT_SESSION,
                "ai-patient-session-20260821",
                "Synthetic AI patient session",
                artifacts["patient"],
                "I felt light-headed twice before breakfast this week.",
            ),
            review_status=ReviewStatus.NEEDS_REVIEW,
        ),
        TimelineEntry(
            id=UUID("50000000-0000-4000-8000-000000000003"),
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            author_role=Role.STAFF,
            author_id=DEMO_STAFF_ID,
            timestamp=_timestamp("2026-08-22T10:15:00"),
            type=EntryType.STAFF_NOTE,
            title="Medication reconciliation",
            content="Medication list reconciled. Patient started lisinopril 10 mg seven days ago.",
            origin=_origin(
                ProvenanceSource.MANUAL_ENTRY,
                "staff-note-20260822",
                "Synthetic staff medication reconciliation",
            ),
        ),
        TimelineEntry(
            id=UUID("50000000-0000-4000-8000-000000000004"),
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            author_role=Role.SYSTEM,
            author_id=None,
            timestamp=_timestamp("2026-08-23T11:40:00"),
            type=EntryType.AI_NURSE_CONSULT_SUMMARY,
            title="AI nurse consultation summary",
            content=(
                "Patient describes new dizziness after lisinopril 20 mg morning dose. Home blood "
                "pressure was 98/62 mmHg. Nurse advised hydration and escalation if fainting occurs."
            ),
            origin=_origin(
                ProvenanceSource.NURSE_CONSULT,
                "ai-nurse-consult-20260823",
                "Synthetic AI nurse consultation",
                artifacts["nurse"],
                "Lisinopril 20 mg. I became dizzy afterward.",
            ),
            review_status=ReviewStatus.AI_SUGGESTED,
        ),
        TimelineEntry(
            id=UUID("50000000-0000-4000-8000-000000000005"),
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            author_role=Role.CLINICIAN,
            author_id=DEMO_CLINICIAN_ID,
            timestamp=_timestamp("2026-08-24T14:20:00"),
            type=EntryType.CLINICIAN_NOTE,
            title="Clinician assessment",
            content=(
                "Likely medication-related hypotension. Hold morning lisinopril pending review "
                "and arrange a blood pressure check within 48 hours."
            ),
            origin=_origin(
                ProvenanceSource.MANUAL_ENTRY,
                "clinician-note-20260824",
                "Synthetic clinician assessment",
            ),
            review_status=ReviewStatus.CLINICIAN_CONFIRMED,
        ),
        TimelineEntry(
            id=UUID("50000000-0000-4000-8000-000000000006"),
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            author_role=Role.SYSTEM,
            author_id=None,
            timestamp=_timestamp("2026-08-25T15:10:00"),
            type=EntryType.AI_DOCTOR_CONSULT_SUMMARY,
            title="AI doctor consultation summary",
            content=(
                "Discussion focused on symptomatic low blood pressure after a new medicine. "
                "Follow-up blood pressure check remains unresolved. AI-generated summary; "
                "clinician confirmation required."
            ),
            origin=_origin(
                ProvenanceSource.DOCTOR_CONSULT,
                "ai-doctor-consult-20260825",
                "Synthetic AI doctor consultation",
                artifacts["doctor"],
                "The follow-up blood pressure check is still unresolved.",
            ),
            review_status=ReviewStatus.NEEDS_REVIEW,
        ),
    ]
    for index, entry in enumerate(entries):
        if entry.origin.source_pointer is None:
            artifact = repository.add_source_artifact(
                SourceArtifact(
                    id=entry.origin.source_id,
                    patient_id=entry.patient_id,
                    clinic_id=entry.clinic_id,
                    kind=SourceArtifactKind.MANUAL_NOTE,
                    source=entry.origin.source,
                    label=f"{entry.origin.source_label} source",
                    content=entry.content,
                    timestamp=entry.timestamp,
                )
            )
            entry = entry.model_copy(
                update={
                    "origin": entry.origin.model_copy(
                        update={
                            "source_pointer": SourceArtifactPointer(
                                source_id=artifact.id,
                                start=0,
                                end=len(artifact.content),
                            )
                        }
                    )
                }
            )
            entries[index] = entry
        repository.add_entry(entry)

    nurse_summary = entries[3]
    clinician_note = entries[4]
    doctor_summary = entries[5]
    highlights = [
        _highlight(
            UUID("60000000-0000-4000-8000-000000000001"),
            clinician_note,
            "Hold morning lisinopril pending review",
            "Clinician-confirmed medication action",
            "Confirm the hold instruction with the patient",
            100,
            ReviewStatus.CLINICIAN_CONFIRMED,
            "Follow the clinician's instruction to hold the morning lisinopril until review.",
            RiskLevel.HIGH,
            ["medication", "dose"],
        ),
        _highlight(
            UUID("60000000-0000-4000-8000-000000000002"),
            nurse_summary,
            "98/62 mmHg",
            "Low home blood-pressure reading after a new medication",
            "Repeat blood pressure and assess symptoms",
            92,
            ReviewStatus.AI_SUGGESTED,
            "Repeat your blood pressure reading and record any symptoms.",
            RiskLevel.HIGH,
            ["medication", "dose", "symptom"],
        ),
        _highlight(
            UUID("60000000-0000-4000-8000-000000000003"),
            doctor_summary,
            "Follow-up blood pressure check remains unresolved",
            "Time-sensitive follow-up task has no completion record",
            "Assign the blood-pressure follow-up",
            88,
            ReviewStatus.NEEDS_REVIEW,
            risk_level=RiskLevel.MODERATE,
            clinical_entities=["symptom"],
        ),
    ]
    scorer = DeterministicImportanceScorer()
    reference_time = max(entry.timestamp for entry in entries)
    for index, highlight in enumerate(highlights):
        source = repository.get_entry(highlight.provenance_pointer.entry_id)
        if source is None:
            raise ValueError("seed Highlight source is missing")
        entry_pointer = source.origin.source_pointer
        if entry_pointer is not None:
            artifact = repository.get_source_artifact(
                entry_pointer.source_id, patient.id, patient.clinic_id
            )
            if artifact is not None:
                claim_start = artifact.content.find(highlight.text)
                claim_pointer = (
                    SourceArtifactPointer(
                        source_id=artifact.id,
                        start=claim_start,
                        end=claim_start + len(highlight.text),
                    )
                    if claim_start >= 0
                    else entry_pointer
                )
                highlight = highlight.model_copy(update={"source_evidence_pointer": claim_pointer})
        highlight = scorer.score(highlight, source, reference_time)
        highlights[index] = highlight
        repository.add_highlight(highlight)
        repository.add_resource_revision(
            ResourceRevision(
                resource="highlight",
                resource_id=str(highlight.id),
                version=highlight.version,
                snapshot=highlight.model_dump(mode="json"),
                operation="seed",
            )
        )

    if repository.get_section(patient.id, "plan") is None:
        repository.save_section(
            SectionState(
                patient_id=patient.id,
                clinic_id=patient.clinic_id,
                section="plan",
                owner_role=Role.CLINICIAN,
                content=(
                    "Hold morning lisinopril pending review. Repeat blood pressure within "
                    "48 hours and reassess dizziness."
                ),
                version=1,
            ),
            SectionRevision(
                section="plan",
                version=1,
                content=(
                    "Hold morning lisinopril pending review. Repeat blood pressure within "
                    "48 hours and reassess dizziness."
                ),
                changed_by=DEMO_CLINICIAN_ID,
                operation="seed",
            ),
        )


# Reference library for the demo clinic (design §19). Synthetic text written for the demo, not
# copied from any real label or guideline. Fixed IDs keep the demo cassette's requests stable.
DEMO_LISINOPRIL_LABEL_ID = UUID("80000000-0000-4000-8000-000000000001")
DEMO_BP_POLICY_ID = UUID("80000000-0000-4000-8000-000000000002")
DEMO_KNOWLEDGE = (
    (
        DEMO_LISINOPRIL_LABEL_ID,
        "drug_label",
        "Lisinopril tablets (synthetic label)",
        "Synthetic demonstration label; not a real product label",
        "2025 synthetic edition",
        (
            "Lisinopril tablets (synthetic label for demonstration).\n"
            "Symptomatic hypotension may occur after the first dose or after a dose increase, "
            "especially in volume-depleted patients.\n"
            "Check blood pressure and kidney function after starting treatment or changing the "
            "dose.\n"
            "Patients who become dizzy or light-headed should have their blood pressure measured "
            "before the next dose."
        ),
    ),
    (
        DEMO_BP_POLICY_ID,
        "clinic_policy",
        "Follow-up after antihypertensive dose changes",
        "Synthetic demonstration clinic policy",
        "v1",
        (
            "Follow-up after an antihypertensive dose change (synthetic clinic policy).\n"
            "Record a blood pressure check within 48 hours of any antihypertensive dose change.\n"
            "Confirm the current dose with the patient and the pharmacy when records disagree."
        ),
    ),
)


def seed_demo_knowledge(store, clinic_id: UUID = DEMO_CLINIC_ID) -> None:
    """Add the demo reference documents to an empty knowledge store; leave a used one alone."""
    from carenote.knowledge.models import KnowledgeDocument, KnowledgeVersion

    if store.list_documents(clinic_id):
        return
    created = _timestamp("2025-01-01T09:00:00+00:00")
    for document_id, kind, title, source, label, content in DEMO_KNOWLEDGE:
        store.save_document(
            KnowledgeDocument(
                id=document_id,
                clinic_id=clinic_id,
                kind=kind,
                title=title,
                source=source,
                created_by=DEMO_ADMIN_ID,
                created_at=created,
                updated_at=created,
            )
        )
        store.add_version(
            KnowledgeVersion(
                document_id=document_id,
                clinic_id=clinic_id,
                version=1,
                label=label,
                effective_date=date(2025, 1, 1),
                content=content,
                created_by=DEMO_ADMIN_ID,
                created_at=created,
            )
        )
