"""Command line for the evaluation suites. See `python evals/run_evals.py --help`.

The pipeline suite runs once, with no model. The agent suite runs once per directory under
`evals/cassettes/models/`, each holding one model's recorded drafts, and the report compares them.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from carenote.agent.gateway import CASSETTE_DIR, MODEL_CASSETTE_DIR, RecordedGateway, live_gateway
from carenote.evals.cases import CASE_DIR, EvalCase, load_cases
from carenote.evals.report import render
from carenote.evals.suites import EvalResult, evaluate

REPORT_PATH = CASE_DIR.parent / "REPORT.md"
LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def model_dirs(root: Path) -> dict[str, Path]:
    """Model label -> cassette directory, for every non-empty directory under `root`."""
    if not root.is_dir():
        return {}
    return {
        path.name: path
        for path in sorted(root.iterdir())
        if path.is_dir() and LABEL.match(path.name) and any(path.glob("*.json"))
    }


def _report_error(case: EvalCase, exc: Exception) -> None:
    print(f"  {case.id}: {type(exc).__name__}: {exc}", file=sys.stderr)


class RecordingAborted(RuntimeError):
    pass


def record(cases: list[EvalCase], directory: Path, jobs: int) -> EvalResult:
    """Record every case; the first one runs alone so a bad key or URL stops before the rest."""
    live = live_gateway()

    def factory():
        return RecordedGateway(directory, record_with=live)

    errors: list[Exception] = []

    def first_error(case: EvalCase, exc: Exception) -> None:
        _report_error(case, exc)
        errors.append(exc)

    evaluate(cases[:1], factory, on_error=first_error)
    if errors:
        raise RecordingAborted(f"the first case failed, so nothing else was sent: {errors[0]}")
    return evaluate(cases, factory, jobs=jobs, on_error=_report_error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent evaluation suites.")
    parser.add_argument("--report", type=Path, default=REPORT_PATH, help="Markdown report path")
    parser.add_argument("--no-report", action="store_true", help="print metrics only")
    parser.add_argument("--check", action="store_true", help="exit with status 1 if any gate fails")
    parser.add_argument(
        "--record",
        metavar="LABEL",
        help="call the live provider (CARENOTE_AGENT_PROVIDER, with CARENOTE_AGENT_FALLBACK) and "
        "record its drafts under <models>/LABEL, e.g. --record deepseek-v3; needs that provider's "
        "API key. Cases already recorded are replayed, so an interrupted run resumes.",
    )
    parser.add_argument("--jobs", type=int, default=4, help="parallel cases while recording")
    parser.add_argument("--cases", type=Path, default=CASE_DIR)
    parser.add_argument("--cassettes", type=Path, default=CASSETTE_DIR, help="pipeline cassettes")
    parser.add_argument(
        "--models", type=Path, default=MODEL_CASSETTE_DIR, help="one cassette directory per model"
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.record:
        if not LABEL.match(args.record):
            parser.error("LABEL must be lower-case letters, digits, '.', '_' or '-'")
        directory = args.models / args.record
        print(f"recording {len(cases)} cases into {directory}")
        try:
            recorded = record(cases, directory, max(1, args.jobs))
        except RecordingAborted as exc:
            print(f"RECORDING ABORTED: {exc}", file=sys.stderr)
            return 2
        print(f"recorded {recorded.agent_metrics['recorded']} of {len(cases)}")

    result = evaluate(cases, lambda: RecordedGateway(args.cassettes))
    models = {
        label: evaluate(cases, lambda directory=directory: RecordedGateway(directory))
        for label, directory in model_dirs(args.models).items()
    }
    pipeline = result.pipeline_metrics
    print(f"cases: {len(result.cases)}")
    print("pipeline: " + ", ".join(f"{key}={value}" for key, value in pipeline.items()))
    if not models:
        print(f"agent: recorded 0 of {len(cases)}")
    for label, model in models.items():
        agent = model.agent_metrics
        print(
            f"agent [{label}]: recorded {agent['recorded']} of {agent['cases']}, "
            f"gate {'pass' if not model.agent_gate() else 'FAIL'}"
        )
    if not args.no_report:
        args.report.write_text(render(result, models), encoding="utf-8", newline="\n")
        print(f"report: {args.report}")
    problems = result.gate()
    for problem in problems:
        print(f"GATE FAILED: {problem}", file=sys.stderr)
    return 1 if args.check and problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
