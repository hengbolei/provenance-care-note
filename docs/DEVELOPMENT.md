# Development and Operation

## Requirements

- Python 3.11 or later
- Node.js for JavaScript syntax checks
- Optional PostgreSQL 14+ for durable/RLS validation
- Optional approved OpenAI project key for AI summary and transcription validation

All bundled data is synthetic. Development defaults to the in-memory adapter and resets clinical
state when the application process restarts.

## Installation and start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn carenote.main:app --reload
```

Open:

- Application: `http://127.0.0.1:8000`
- OpenAPI UI: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/api/health`

## Development accounts

The browser signs in through `/api/auth/login`; actor IDs, roles, and clinic IDs are not supplied
by client headers. Development-only credentials are:

| Role | Username | Password |
| --- | --- | --- |
| Patient | `patient` | `patient-demo-2026` |
| Staff | `staff` | `staff-demo-2026` |
| Clinician | `clinician` | `clinician-demo-2026` |
| Admin | `admin` | `admin-demo-2026` |

Example cookie-backed API session:

```powershell
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/auth/login" `
  -WebSession $session `
  -ContentType "application/json" `
  -Body '{"username":"clinician","password":"clinician-demo-2026"}'

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/patients/20000000-0000-4000-8000-000000000001" `
  -WebSession $session
```

Sessions are signed, expire, can be revoked on logout, and reject an interactive `system` role.
When PostgreSQL is enabled, active account membership is checked and hashed session IDs persist.

## Optional PostgreSQL and production settings

Copy `.env.example` into your secret-management workflow; do not commit populated secrets. The
important settings are:

- `CARENOTE_DATABASE_URL`: enables the encrypted PostgreSQL adapter and RLS migration;
- `CARENOTE_TOKEN_SECRET`: signs session tokens;
- `CARENOTE_ENCRYPTION_KEY`: protects clinical snapshots and cold archives;
- `CARENOTE_PUBLIC_BASE_URL`: must be HTTPS in production;
- `OPENAI_API_KEY`: explicitly enables external summary/transcription calls; and
- `CARENOTE_OPENAI_MODEL`: selects the Responses API model.

Production mode refuses to start without the database URL, signing secret, encryption key, and an
HTTPS public URL. Use a non-superuser PostgreSQL application role that cannot bypass RLS. The
included migration is `migrations/001_postgres_rls.sql`; the reverse-proxy baseline is
`deploy/nginx.conf`.

No target PostgreSQL, TLS certificate, KMS, WORM audit store, or approved OpenAI key is bundled.
Those environment-gated integrations must be validated before production use.

## PHI and model boundary

`PHIRedactionGateway` is called by the text-summary adapter before an external request. It removes
known patient names/MRNs and supported ID/phone patterns. The request uses `store=false`; the
generated entry remains AI-suggested.

Audio transcription currently sends audio to the configured external transcription endpoint
before text redaction is possible. Treat this as a documented prototype limitation. Clinical
deployment requires approved local/on-premises ASR or a compliant covered transcription service.

## Clinical review agent

The agent's model gateway is chosen with `CARENOTE_AGENT_GATEWAY`:

| Value | Behaviour |
| --- | --- |
| `recorded` (default) | Replays cassettes from `evals/cassettes/`; no key, no network |
| `openai` | Live calls with `OPENAI_API_KEY` and `CARENOTE_OPENAI_MODEL` |
| `record` | Live calls that also write new cassettes |

Cassettes are keyed by a hash of the prompt, the redacted input and the output schema, so
changing the draft prompt, the seed data or how requests are framed makes the replay test
(`tests/test_agent_demo.py`) fail until the seed cassette is re-recorded:

```powershell
.\.venv\Scripts\python.exe scripts\record_demo_cassette.py                  # curated draft
.\.venv\Scripts\python.exe scripts\record_demo_cassette.py --source openai   # live model
```

The script refuses to write a draft that does not pass citation verification.

Run the evaluation suites and regenerate `evals/REPORT.md` (CI fails if the committed report is
stale or a gate fails):

```powershell
.\.venv\Scripts\python.exe evals\run_evals.py --check
```

Agent persistence tests (restart survival, encrypted checkpoints, clinic isolation) need
PostgreSQL. Set `CARENOTE_TEST_DATABASE_URL`, or install the optional `pgserver` package to
start a throwaway local server; otherwise they are skipped. CI runs them against a Postgres
service container.

To run the full stack offline, use `docker compose up --build` and then
`python scripts/smoke_demo.py` against `http://127.0.0.1:8000`.

## Verification

```powershell
.\.venv\Scripts\pytest.exe -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check src tests scripts
node --check src/carenote/static/api.js
node --check src/carenote/static/app.js
node --check src/carenote/static/service-worker.js
.\.venv\Scripts\python.exe scripts\benchmark_glance.py --iterations 1000 --warmup 100
```

The current local baseline is 192 passing tests. The performance result is specific to the
single-patient in-memory adapter and is not a production capacity claim.

## Directory responsibilities

```text
src/carenote/
├── api/           HTTP/WebSocket routes and dependency wiring
├── core/          configuration, identity, encryption and safe logging
├── data/          deterministic synthetic seed data
├── domain/        models and request/response contracts
├── privacy/       PHI redaction boundary
├── repositories/  in-memory and encrypted PostgreSQL adapters
├── services/      RBAC, provenance, ranking, conflicts, AI and realtime behavior
└── static/        responsive PWA client
tests/             automated unit, policy, API and integration tests
migrations/        PostgreSQL schema and RLS policy
deploy/            TLS reverse-proxy baseline
docs/              technical brief, operations, demo and evidence
```
