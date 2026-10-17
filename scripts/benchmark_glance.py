import argparse
import json
from statistics import mean
from time import perf_counter

from fastapi.testclient import TestClient

from carenote.core.identity import session_service
from carenote.data.seed import DEMO_CLINIC_ID, DEMO_CLINICIAN_ID, DEMO_PATIENT_ID
from carenote.domain.models import Actor, Role
from carenote.main import app


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * percentile_value))
    return ordered[index]


def run_benchmark(iterations: int, warmup: int) -> dict[str, float | int | str]:
    token, _ = session_service.issue(
        Actor(
            id=DEMO_CLINICIAN_ID,
            role=Role.CLINICIAN,
            clinic_id=DEMO_CLINIC_ID,
        )
    )
    headers = {"authorization": f"Bearer {token}"}
    path = f"/api/patients/{DEMO_PATIENT_ID}"
    timings: list[float] = []
    with TestClient(app) as client:
        for _ in range(warmup):
            response = client.get(path, headers=headers)
            response.raise_for_status()
        for _ in range(iterations):
            start = perf_counter()
            response = client.get(path, headers=headers)
            elapsed_ms = (perf_counter() - start) * 1000
            response.raise_for_status()
            timings.append(elapsed_ms)
    return {
        "endpoint": path,
        "adapter": "in-memory synthetic dataset",
        "iterations": iterations,
        "warmup_iterations": warmup,
        "mean_ms": round(mean(timings), 3),
        "p50_ms": round(percentile(timings, 0.50), 3),
        "p95_ms": round(percentile(timings, 0.95), 3),
        "max_ms": round(max(timings), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure warm Glance endpoint latency.")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()
    if args.iterations < 1 or args.warmup < 0:
        parser.error("iterations must be positive and warmup cannot be negative")
    result = run_benchmark(args.iterations, args.warmup)
    print(json.dumps(result, indent=2))
    if float(result["p95_ms"]) > 300:
        raise SystemExit("P95 exceeded the 300 ms prototype target")


if __name__ == "__main__":
    main()
