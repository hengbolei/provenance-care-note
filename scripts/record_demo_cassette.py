"""Record the agent cassette for the synthetic seed patient, used by the offline demo and CI.

    python scripts/record_demo_cassette.py              # curated draft, no API key needed
    python scripts/record_demo_cassette.py --source openai   # live model; needs OPENAI_API_KEY
    python scripts/record_demo_cassette.py --source anthropic   # needs ANTHROPIC_API_KEY
    python scripts/record_demo_cassette.py --source compatible  # needs CARENOTE_COMPAT_* settings

The curated source is a hand-written draft that cites the seed record exactly. It is labelled
as curated in the cassette's model field, so it is never mistaken for model output. Re-record
whenever a prompt, the seed data or the request framing changes; the replay test fails
until you do. The script refuses to write a cassette whose draft does not pass verification.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from carenote.agent.gateway import (
    CASSETTE_DIR,
    DataTag,
    ModelRequest,
    ModelResult,
    RecordedGateway,
    Usage,
    live_gateway,
)
from carenote.agent.graph import GraphDeps, build_review_graph, initial_state
from carenote.agent.state import ReviewPlan
from carenote.data.seed import (
    DEMO_BP_POLICY_ID,
    DEMO_CLINIC_ID,
    DEMO_CLINICIAN_ID,
    DEMO_LISINOPRIL_LABEL_ID,
    DEMO_PATIENT_ID,
    seed_demo_data,
    seed_demo_knowledge,
)
from carenote.domain.models import Actor, AgentActor, Role
from carenote.knowledge.service import KnowledgeService
from carenote.knowledge.store import InMemoryKnowledgeStore
from carenote.repositories.memory import InMemoryCareNoteRepository
from carenote.services.care_notes import CareNoteService

CURATED_MODEL = "curated-demo (hand-written, not model output)"

# (claim text, category, risk, [(entry type, exact quote), ...])
CLAIMS = [
    (
        (
            "Lisinopril dose is inconsistent: the staff note records lisinopril 10 mg started seven "
            "days ago, while the nurse consult summary records a lisinopril 20 mg morning dose."
        ),
        "medication",
        "high",
        [
            ("staff_note", "started lisinopril 10 mg seven days ago"),
            ("ai_nurse_consult_summary", "lisinopril 20 mg morning dose"),
        ],
    ),
    (
        (
            "The clinician note puts morning lisinopril on hold pending review; this is the latest "
            "clinician instruction on the medicine."
        ),
        "medication",
        "high",
        [("clinician_note", "Hold morning lisinopril pending review")],
    ),
    (
        (
            "The nurse consult summary (AI-generated, unconfirmed) records a home blood pressure of "
            "98/62 mmHg with new dizziness after the morning dose."
        ),
        "symptom",
        "high",
        [
            ("ai_nurse_consult_summary", "Home blood pressure was 98/62 mmHg"),
            ("ai_nurse_consult_summary", "new dizziness"),
        ],
    ),
    (
        (
            "Patient reports feeling light-headed twice this week, usually before breakfast, with no "
            "loss of consciousness."
        ),
        "symptom",
        "moderate",
        [
            ("patient_note", "felt light-headed twice this week, usually before breakfast"),
            ("ai_patient_session_summary", "no loss of consciousness"),
        ],
    ),
    (
        "The clinician assessment is likely medication-related hypotension.",
        "plan",
        "moderate",
        [("clinician_note", "Likely medication-related hypotension")],
    ),
    (
        (
            "A blood pressure check within 48 hours was arranged, and the consult summary notes the "
            "follow-up check is still unresolved."
        ),
        "follow_up",
        "moderate",
        [
            ("clinician_note", "arrange a blood pressure check within 48 hours"),
            ("ai_doctor_consult_summary", "Follow-up blood pressure check remains unresolved"),
        ],
    ),
]

OPEN_QUESTIONS = [
    "Which lisinopril dose is the patient taking now: 10 mg (staff note) or 20 mg (nurse consult)?",
    "Has the blood pressure check arranged for within 48 hours been completed?",
]

# (check point, reference document, exact reference quote, [(entry type, exact record quote), ...])
CHECK_POINTS = [
    (
        (
            "Check blood pressure and kidney function after the lisinopril dose change; no "
            "kidney function result appears in the record."
        ),
        DEMO_LISINOPRIL_LABEL_ID,
        "Check blood pressure and kidney function after starting treatment or changing the dose.",
        [("staff_note", "started lisinopril 10 mg seven days ago")],
    ),
    (
        (
            "Confirm the current lisinopril dose with the patient and the pharmacy, as the "
            "records disagree."
        ),
        DEMO_BP_POLICY_ID,
        "Confirm the current dose with the patient and the pharmacy when records disagree.",
        [
            ("staff_note", "started lisinopril 10 mg seven days ago"),
            ("ai_nurse_consult_summary", "lisinopril 20 mg morning dose"),
        ],
    ),
]

# (entry type, exact quote, risk reason, suggested action, entities)
HIGHLIGHTS = [
    (
        "ai_nurse_consult_summary",
        "lisinopril 20 mg morning dose",
        "Recorded dose differs from the reconciled medication list (10 mg).",
        "Confirm the current lisinopril dose with the patient and pharmacy.",
        ["lisinopril", "dose"],
    ),
    (
        "clinician_note",
        "arrange a blood pressure check within 48 hours",
        "Time-bound follow-up from the clinician plan has no completion record.",
        "Book the blood pressure check and record the result.",
        ["blood pressure"],
    ),
]


# (question, search words) the curated plan asks beyond the standing questions.
PLAN = [
    ("Which lisinopril dose is the patient taking now?", "lisinopril dose pharmacy"),
    ("Has the blood pressure check been completed?", "blood pressure check"),
]


class CuratedDemoGateway:
    """Builds the curated plan and draft from the exact entry text in the request it receives."""

    def structured(self, request: ModelRequest, schema):
        if schema is ReviewPlan:
            plan = {"questions": [{"question": q, "query": query} for q, query in PLAN]}
            return ModelResult(
                output=schema.model_validate(plan), model=CURATED_MODEL, usage=Usage()
            )
        entries = _entries_by_type(request)
        references = _references(request)

        def cite_reference(document_id: UUID, quote: str) -> dict:
            version, start, text = next(
                item for item in references[document_id] if quote in item[2]
            )
            offset = start + text.index(quote)
            return {
                "document_id": str(document_id),
                "version": version,
                "start": offset,
                "end": offset + len(quote),
                "quote": quote,
            }

        def cite(entry_type: str, quote: str) -> dict:
            entry_id, text = entries[entry_type]
            start = text.index(quote)
            return {
                "entry_id": str(entry_id),
                "start": start,
                "end": start + len(quote),
                "quote": quote,
            }

        draft = {
            "claims": [
                {
                    "text": text,
                    "category": category,
                    "risk_level": risk,
                    "citations": [cite(entry_type, quote) for entry_type, quote in sources],
                }
                for text, category, risk, sources in CLAIMS
            ],
            "open_questions": OPEN_QUESTIONS,
            "check_points": [
                {
                    "text": text,
                    "reference": cite_reference(document_id, quote),
                    "citations": [cite(entry_type, record) for entry_type, record in sources],
                }
                for text, document_id, quote, sources in CHECK_POINTS
                if any(quote in passage for *_, passage in references.get(document_id, []))
            ],
            "candidate_highlights": [
                {
                    "citation": cite(entry_type, quote),
                    "risk_reason": reason,
                    "suggested_action": action,
                    "clinical_entities": entities,
                }
                for entry_type, quote, reason, action, entities in HIGHLIGHTS
            ],
        }
        return ModelResult(output=schema.model_validate(draft), model=CURATED_MODEL, usage=Usage())

    def text(self, request: ModelRequest):
        raise NotImplementedError("the review graph only makes structured calls")


def _entries_by_type(request: ModelRequest) -> dict[str, tuple[UUID, str]]:
    found: dict[str, tuple[UUID, str]] = {}
    current: dict | None = None
    for item in request.inputs:
        if isinstance(item, DataTag):
            current = dict(item.attributes) if item.name == "entry" and not item.closing else None
        elif current is not None:
            found[current["type"].value] = (current["id"], item.text)
    return found


def _cassettes(directory: Path) -> list[Path]:
    return [*directory.glob("agent_plan-*.json"), *directory.glob("agent_draft-*.json")]


def _references(request: ModelRequest) -> dict[UUID, list[tuple[int, int, str]]]:
    """Reference passages shown, by document: (version, start offset, passage text)."""
    found: dict[UUID, list[tuple[int, int, str]]] = {}
    current: dict | None = None
    for item in request.inputs:
        if isinstance(item, DataTag):
            current = (
                dict(item.attributes) if item.name == "reference" and not item.closing else None
            )
        elif current is not None:
            found.setdefault(current["id"], []).append(
                (current["version"], current["start"], item.text)
            )
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", choices=["curated", "openai", "anthropic", "compatible"], default="curated"
    )
    parser.add_argument("--directory", type=Path, default=CASSETTE_DIR)
    args = parser.parse_args()

    live = CuratedDemoGateway() if args.source == "curated" else live_gateway(args.source, "")
    for stale in _cassettes(args.directory):
        stale.unlink()
    repository = InMemoryCareNoteRepository()
    seed_demo_data(repository)
    knowledge = KnowledgeService(InMemoryKnowledgeStore(), repository)
    seed_demo_knowledge(knowledge.store)
    deps = GraphDeps(
        service=CareNoteService(repository),
        gateway=RecordedGateway(args.directory, record_with=live),
        commit=lambda state, tools: None,
        knowledge=knowledge,
    )
    graph = build_review_graph(deps)
    run_id = uuid4()
    clinician = Actor(id=DEMO_CLINICIAN_ID, role=Role.CLINICIAN, clinic_id=DEMO_CLINIC_ID)
    config = {"configurable": {"thread_id": str(run_id)}}
    graph.invoke(
        initial_state(
            run_id, AgentActor.for_run(clinician, run_id), DEMO_PATIENT_ID, datetime.now(UTC)
        ),
        config,
    )
    report = graph.get_state(config).values["verification"]
    for check in report.checks:
        mark = "ok  " if check.verified else "FAIL"
        print(f"{mark} claim {check.claim_index}: {'; '.join(check.detail) or 'verified'}")
    if not report.passed:
        for stale in _cassettes(args.directory):
            stale.unlink()
        print("Draft did not pass verification; no cassette written.", file=sys.stderr)
        return 1
    print(f"Cassette written to {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
