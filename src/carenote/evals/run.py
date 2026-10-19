"""Command line for the evaluation suites. See `python evals/run_evals.py --help`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from carenote.agent.gateway import CASSETTE_DIR, RecordedGateway, live_gateway
from carenote.evals.cases import CASE_DIR, load_cases
from carenote.evals.report import render
from carenote.evals.suites import evaluate

REPORT_PATH = CASE_DIR.parent / "REPORT.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent evaluation suites.")
    parser.add_argument("--report", type=Path, default=REPORT_PATH, help="Markdown report path")
    parser.add_argument("--no-report", action="store_true", help="print metrics only")
    parser.add_argument("--check", action="store_true", help="exit with status 1 if any gate fails")
    parser.add_argument(
        "--record",
        action="store_true",
        help="call the live provider (CARENOTE_AGENT_PROVIDER, with CARENOTE_AGENT_FALLBACK) and "
        "record a cassette for every case; needs that provider's API key",
    )
    parser.add_argument("--cases", type=Path, default=CASE_DIR)
    parser.add_argument("--cassettes", type=Path, default=CASSETTE_DIR)
    args = parser.parse_args(argv)

    if args.record:
        live = live_gateway()

        def factory():
            return RecordedGateway(args.cassettes, record_with=live)
    else:

        def factory():
            return RecordedGateway(args.cassettes)

    result = evaluate(load_cases(args.cases), factory)
    pipeline = result.pipeline_metrics
    agent = result.agent_metrics
    print(f"cases: {len(result.cases)}")
    print("pipeline: " + ", ".join(f"{key}={value}" for key, value in pipeline.items()))
    print(f"agent: recorded {agent['recorded']} of {agent['cases']}")
    if not args.no_report:
        args.report.write_text(render(result), encoding="utf-8", newline="\n")
        print(f"report: {args.report}")
    problems = result.gate()
    for problem in problems:
        print(f"GATE FAILED: {problem}", file=sys.stderr)
    return 1 if args.check and problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
