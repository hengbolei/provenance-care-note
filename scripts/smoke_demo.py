"""Smoke-test a running demo: sign in, run the review agent, and commit its draft.

    python scripts/smoke_demo.py [base_url]                # default http://127.0.0.1:8000
    python scripts/smoke_demo.py --start-only              # print the id of a run left for review
    python scripts/smoke_demo.py --resume RUN_ID           # e.g. after restarting the server

Standard library only, so it runs against the Docker demo without installing anything. Uses
the synthetic development clinician account and changes only synthetic seed data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

PATIENT_ID = "20000000-0000-4000-8000-000000000001"
DEMO_CLINICIAN = {"username": "clinician", "password": "clinician-demo-2026"}


def call(base: str, method: str, path: str, token: str | None = None, body=None) -> dict:
    request = urllib.request.Request(
        f"{base}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            **({"authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def _wait_for_review(base: str, token: str, run: dict) -> dict:
    deadline = time.monotonic() + 30
    while run["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        run = call(base, "GET", f"/api/agent-runs/{run['run_id']}", token)
    return run


def _check_draft(run: dict) -> str | None:
    if run["status"] != "awaiting_review":
        return f"run is {run['status']}: {run.get('error') or run.get('stop_reason')}"
    verification = run["verification"]
    if any(check["failures"] for check in verification["checks"]):
        return "draft did not fully verify"
    if verification["uncovered_conflict_ids"]:
        return "draft left a conflict unmentioned"
    return None


def _commit_all(base: str, token: str, run: dict) -> str | None:
    claims = len(run["verification"]["checks"])
    committed = call(
        base,
        "POST",
        f"/api/agent-runs/{run['run_id']}/commit",
        token,
        {"claims": [{"claim_index": index, "action": "accept"} for index in range(claims)]},
    )
    if committed["status"] != "committed":
        return f"commit ended as {committed['status']}"
    detail = call(base, "GET", f"/api/patients/{PATIENT_ID}", token)
    if "agent_review" not in {section["section"] for section in detail["sections"]}:
        return "agent_review section missing after commit"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--start-only", action="store_true", help="leave the run awaiting review; print its id"
    )
    parser.add_argument("--resume", metavar="RUN_ID", help="commit a run started earlier")
    args = parser.parse_args(argv)
    base = args.base.rstrip("/")

    assert call(base, "GET", "/api/health")["status"] == "ok"
    token = call(base, "POST", "/api/auth/login", body=DEMO_CLINICIAN)["token"]

    if args.resume:
        run = call(base, "GET", f"/api/agent-runs/{args.resume}", token)
    else:
        run = _wait_for_review(
            base, token, call(base, "POST", f"/api/patients/{PATIENT_ID}/agent-runs", token)
        )
    problem = _check_draft(run)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if args.start_only:
        print(run["run_id"])
        return 0
    checks = run["verification"]["checks"]
    print(
        f"draft: {len(checks)} verified claims, {len(run['draft']['open_questions'])} questions",
        file=sys.stderr,
    )
    problem = _commit_all(base, token, run)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    print("committed; agent_review section present", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
