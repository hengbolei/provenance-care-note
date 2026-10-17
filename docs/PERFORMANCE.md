# Glance Warm-Path Performance

Measured on 2026-08-28 with Python 3.12.10 on the local Windows development environment after
enabling deterministic priority explanations and conflict detection.

## Method

- Endpoint: `GET /api/patients/{patient_id}`
- Transport harness: FastAPI/Starlette `TestClient`
- Adapter: in-memory repository
- Dataset: one synthetic patient, six timeline entries, three highlights and one section
- Warm-up: 100 requests
- Measured sample: 1,000 sequential requests
- Timing: wall-clock duration around each complete request/response

## Result

| Metric | Result |
| --- | ---: |
| Mean | 1.242 ms |
| P50 | 1.199 ms |
| P95 | 1.535 ms |
| Maximum | 2.509 ms |
| Prototype target | ≤ 300 ms P95 |

The measured prototype passes its warm-path target. This result does not include a network hop,
durable database, production authentication, concurrent load or a representative multi-patient
dataset, so it must not be used as a production capacity claim.

Reproduce with:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_glance.py --iterations 1000 --warmup 100
```
