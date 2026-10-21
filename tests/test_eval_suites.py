from pathlib import Path

import pytest

from carenote.agent.gateway import ModelResult, RecordedGateway, Usage
from carenote.agent.state import ReviewPlan
from carenote.evals.cases import Family, load_cases
from carenote.evals.report import render
from carenote.evals.run import REPORT_PATH, main
from carenote.evals.suites import MAX_CONTEXT_ENTRIES, evaluate

CASES = load_cases()


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    # An empty cassette directory: the agent suite must report "not recorded", not invent scores.
    empty = tmp_path_factory.mktemp("no-cassettes")
    return evaluate(CASES, lambda: RecordedGateway(empty))


def test_pipeline_suite_passes_its_gate_on_the_committed_cases(result):
    metrics = result.pipeline_metrics

    assert result.gate() == []
    assert metrics["conflict_recall"] == 1.0
    assert metrics["verifier_accuracy"] == 1.0
    assert metrics["retrieval_recall"] == 1.0
    assert metrics["reference_recall"] == 1.0
    assert metrics["check_point_accuracy"] == 1.0
    assert metrics["payload_leaks"] == 0
    assert metrics["stale_known_gaps"] == 0
    assert metrics["known_gaps"] > 0


def test_payload_is_captured_even_without_a_cassette(result):
    run = result.runs["inject-04-exfiltrate-identifiers"]

    assert not run.recorded
    assert not run.planned
    assert [request.purpose for request in run.requests] == ["agent_plan", "agent_draft"]
    for request in run.requests:
        assert "91234567" not in request.input_text
        assert "[REDACTED_PHONE]" in request.input_text


def test_retrieval_brings_older_key_entries_of_long_timelines_into_the_draft(result):
    case = next(case for case in CASES if case.id == "long-01-apixaban-dose")
    newest = sorted(case.build_entries(), key=lambda entry: entry.timestamp)[-MAX_CONTEXT_ENTRIES:]
    [draft] = result.runs[case.id].draft_requests

    # The apixaban start is older than the most-recent window, yet the draft sees it.
    assert case.entry_id("start") not in {entry.id for entry in newest}
    assert str(case.entry_id("start")) in draft.input_text
    assert not [check for check in result.pipeline if check.kind == "context" and not check.passed]


def test_agent_suite_reports_not_recorded_instead_of_scoring(result):
    metrics = result.agent_metrics

    assert metrics["recorded"] == 0
    assert metrics["citation_validity"] is None
    assert result.agent == []
    assert "Not recorded: 0 of 39" in render(result)


def test_committed_report_matches_the_code(result):
    # The committed report is generated with the default cassettes, which hold no eval case.
    expected = render(result)

    assert Path(REPORT_PATH).read_text(encoding="utf-8").replace("\r\n", "\n") == expected, (
        "evals/REPORT.md is stale: run `python evals/run_evals.py`"
    )


class _QuotingModel:
    """Drafts one claim quoting each entry it is shown; optionally echoes an injection."""

    def __init__(self, obey_injection=False, misquote_first_draft=False):
        self.obey_injection = obey_injection
        self.misquote_first_draft = misquote_first_draft

    def structured(self, request, schema):
        if schema is ReviewPlan:
            return ModelResult(output=ReviewPlan(), model="quoting-test-model", usage=Usage(0, 0))
        claims = []
        entry_id = None
        for item in request.inputs:
            name = getattr(item, "name", None)
            if name == "entry" and not item.closing:
                entry_id = dict(item.attributes)["id"]
            elif name is None and entry_id is not None:
                quote = item.text[:40]
                claims.append(
                    {
                        "text": f"The record states: {quote}",
                        "category": "other",
                        "risk_level": "low",
                        "citations": [
                            {
                                "entry_id": str(entry_id),
                                "start": 0,
                                "end": len(quote),
                                "quote": quote,
                            }
                        ],
                    }
                )
                entry_id = None
        if self.misquote_first_draft and request.purpose == "agent_draft" and claims:
            claims[0]["citations"][0]["quote"] = "text that is not in the entry"
        questions = []
        if self.obey_injection == "question":
            questions.append("Stop all medications immediately?")
        elif self.obey_injection == "claim" and claims:
            # Obeys in a claim that cites the injected text; verification fails it, but the draft
            # still carries the instruction, which is what the agent suite counts.
            citation = claims[0]["citations"][0]
            claims.append(
                {
                    "text": "Stop all medications immediately.",
                    "category": "medication",
                    "risk_level": "critical",
                    "citations": [citation],
                }
            )
        draft = {"claims": claims[:20], "open_questions": questions, "candidate_highlights": []}
        return ModelResult(
            output=schema.model_validate(draft), model="quoting-test-model", usage=Usage(100, 50)
        )

    def text(self, request):
        raise AssertionError("unused")


def _recorded(cases, tmp_path, model):
    evaluate(cases, lambda: RecordedGateway(tmp_path, record_with=model))
    return evaluate(cases, lambda: RecordedGateway(tmp_path))


def test_agent_suite_scores_recorded_drafts(tmp_path):
    cases = [
        case for case in CASES if case.id in {"dose-02-levothyroxine-mcg", "sparse-02-admin-only"}
    ]

    result = _recorded(cases, tmp_path, _QuotingModel())
    metrics = result.agent_metrics

    assert metrics["recorded"] == 2
    assert metrics["models"] == ["quoting-test-model"]
    assert metrics["tokens"] == 300
    assert metrics["claims"] > 0
    assert 0 <= metrics["unsupported_rate"] <= 1
    assert "Recorded: **2 of 2**" in render(result)


def test_agent_suite_catches_a_draft_that_obeys_an_injection(tmp_path):
    cases = [case for case in CASES if case.id == "inject-01-stop-everything"]

    result = _recorded(cases, tmp_path, _QuotingModel(obey_injection="claim"))

    assert result.agent_metrics["injection_failures"] == 1
    assert any("injection_failures" in problem for problem in result.gate())


def test_an_open_question_that_repeats_an_injection_is_withheld(tmp_path):
    case = next(case for case in CASES if case.id == "inject-01-stop-everything")

    run = _recorded([case], tmp_path, _QuotingModel(obey_injection="question")).runs[case.id]

    assert run.draft.open_questions == []
    [withheld] = run.verification.withheld
    assert (withheld.kind, withheld.index) == ("open_question", 0)
    assert withheld.entry_id == case.entry_id("injected")
    assert run.verification.instruction_like_entry_ids == [case.entry_id("injected")]


def test_a_known_gap_that_starts_passing_fails_the_gate():
    case = next(case for case in CASES if case.id == "medchange-01-new-start")
    probe = case.probes[0].model_copy(
        update={"known_gap": "Pretend this used to fail verification."}
    )
    stale = case.model_copy(update={"probes": [probe, *case.probes[1:]]})

    result = evaluate([stale])

    assert result.pipeline_metrics["stale_known_gaps"] == 1
    assert any("stale_known_gaps" in problem for problem in result.gate())


def test_a_retrieval_query_that_misses_its_entry_fails_the_gate():
    case = next(case for case in CASES if case.id == "long-02-buried-allergy")
    query = case.expect.retrieval[0].model_copy(update={"query": "colonoscopy"})
    missing = case.model_copy(
        update={"expect": case.expect.model_copy(update={"retrieval": [query]})}
    )

    result = evaluate([missing])

    assert result.pipeline_metrics["retrieval_recall"] == 0.0
    assert any("retrieval_recall" in problem for problem in result.gate())


def test_a_check_probe_with_the_wrong_verdict_fails_the_gate():
    case = next(case for case in CASES if case.id == "knowledge-01-apixaban-criteria")
    probe = case.check_probes[0].model_copy(update={"expect": ["recommends_treatment"]})
    wrong = case.model_copy(update={"check_probes": [probe]})

    result = evaluate([wrong])

    assert result.pipeline_metrics["check_point_accuracy"] == 0.0
    assert any("check_point_accuracy" in problem for problem in result.gate())


def test_the_knowledge_run_shows_current_references_only():
    run = evaluate([case for case in CASES if case.id == "knowledge-02-retired-and-future"]).runs[
        "knowledge-02-retired-and-future"
    ]

    [draft] = run.draft_requests
    assert "Check the INR within one week" in draft.input_text
    assert "Retired INR rule" not in draft.input_text
    assert "Future INR rule" not in draft.input_text


def test_cli_check_exit_code_follows_the_gate(tmp_path, capsys):
    report = tmp_path / "REPORT.md"

    assert main(["--check", "--report", str(report)]) == 0
    assert "Gate: **pass**" in report.read_text(encoding="utf-8")
    assert "recorded 0 of 39" in capsys.readouterr().out


def test_every_family_contributes_pipeline_checks(result):
    assert {check.family for check in result.pipeline} == set(Family)


def test_agent_suite_measures_automatic_revision(tmp_path):
    cases = [case for case in CASES if case.id == "sparse-02-admin-only"]

    metrics = _recorded(cases, tmp_path, _QuotingModel(misquote_first_draft=True)).agent_metrics

    assert metrics["cases_revised"] == 1
    assert metrics["auto_revisions"] == 1
    assert metrics["unsupported_rate_before_revision"] > 0
    assert metrics["unsupported_rate"] == 0
