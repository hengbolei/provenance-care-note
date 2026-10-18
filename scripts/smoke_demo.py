"""Smoke-test a running demo: sign in, run the review agent, and commit its draft.

    python scripts/smoke_demo.py [base_url]     # default http://127.0.0.1:8000

Standard library only, so it runs against the Docker demo without installing anything. Uses
the synthetic development clinician account and changes only synthetic seed data.
"""

from __future__ import annotations

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


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
    assert call(base, "GET", "/api/health")["status"] == "ok"
    token = call(base, "POST", "/api/auth/login", body=DEMO_CLINICIAN)["token"]

    run = call(base, "POST", f"/api/patients/{PATIENT_ID}/agent-runs", token)
    deadline = time.monotonic() + 30
    while run["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        run = call(base, "GET", f"/api/agent-runs/{run['run_id']}", token)
    if run["status"] != "awaiting_review":
        print(f"run ended as {run['status']}: {run.get('error') or run.get('stop_reason')}")
        return 1
    checks = run["verification"]["checks"]
    if any(check["failures"] for check in checks) or run["verification"]["uncovered_conflict_ids"]:
        print("draft did not fully verify")
        return 1
    print(f"draft: {len(checks)} verified claims, {len(run['draft']['open_questions'])} questions")

    committed = call(
        base,
        "POST",
        f"/api/agent-runs/{run['run_id']}/commit",
        token,
        {"claims": [{"claim_index": index, "action": "accept"} for index in range(len(checks))]},
    )
    if committed["status"] != "committed":
        print(f"commit ended as {committed['status']}")
        return 1
    detail = call(base, "GET", f"/api/patients/{PATIENT_ID}", token)
    if "agent_review" not in {section["section"] for section in detail["sections"]}:
        print("agent_review section missing after commit")
        return 1
    print("committed; agent_review section present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
