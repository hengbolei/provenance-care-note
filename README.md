# Provenance Care Note

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/hengbolei/provenance-care-note/actions/workflows/ci.yml/badge.svg)](https://github.com/hengbolei/provenance-care-note/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A provenance-first longitudinal care note prototype for a single synthetic patient. The project
helps clinical staff understand urgent information quickly while keeping AI-generated content,
human-authored notes, source evidence, and access boundaries explicit.

> This repository contains synthetic demonstration data only. It is not a medical device and is
> not ready for production use.

## Current capabilities

- A Clinical Review Agent (LangGraph) that drafts a patient review in which every claim cites an
  exact span of the record, verifies each citation deterministically, and stops for a clinician
  to accept, edit or reject each claim before anything is saved. See
  [the design](docs/AGENT_DESIGN.md) and [the roadmap](docs/AGENT_ROADMAP.md).
- A 10-second Glance view with deterministic explainable priority factors, risk reasons,
  suggested actions, and review states.
- Narrow medication, dose, and allergy conflict detection with explicit clinician precedence or
  needs-review states.
- An actionable highlight workflow with care-role assignment, open/in-progress/completed states,
  disposition notes, optimistic concurrency checks, and metadata-only audit events.
- A longitudinal timeline containing patient, staff, clinician, and three distinct AI-scribed
  entry types.
- Role-authored manual timeline notes whose server-generated author and entry type cannot be
  forged by the browser.
- Clinician-created highlights from an exact selected source span, with risk reason, suggested
  action, priority, assignment, and audit metadata.
- Exact highlight provenance pointers that resolve to an entry and character span, then continue
  to an original synthetic message or transcript span.
- Exact source-span highlighting in the timeline and signed clinician/staff/patient demo sessions.
- Server-side clinic scope, role ownership, and patient self-access checks.
- Patient-facing filtering that excludes internal highlights and raw AI-scribed entries.
- A patient-facing dashboard with explicitly published clinician-confirmed guidance, action
  progress, and a form for adding patient-authored symptom or question updates.
- Deterministic section versioning, audit metadata, revert behavior, and optimistic concurrency
  checks.
- Internal care-team comments bound to entries, sections, or exact spans, with reply links, role
  mentions, assignment, resolve/reopen, optimistic locking, and complete revision snapshots.
- Editable clinician plan with API-backed revision history, arbitrary-version comparison, and
  revert controls, plus complete Highlight revision snapshots.
- An in-page metadata-only audit trail for note creation, comments/replies, resolve/reopen,
  highlight assignment/completion/review, plan edits, and reverts.
- A tested PHI redaction gateway used by the optional external text-summary adapter.
- Deterministic synthetic data for repeatable demonstrations and tests.
- Responsive, English-only web interface with loading, empty, forbidden, not-found, and error
  states.
- Signed, expiring sessions with HttpOnly cookies, logout revocation, PostgreSQL-backed accounts
  and clinic memberships, and no interactive `system` identity.
- Optional encrypted PostgreSQL snapshots with forced clinic RLS, encrypted cold-entry archives,
  hash-chained audit events, request-log minimization, and a TLS reverse-proxy baseline.
- WebSocket presence and automatic record refresh, arbitrary Comment/Highlight snapshot A/B
  comparison, and a bounded reviewed-outcome priority adjustment that controls for exposure.
- Optional OpenAI Responses API summary ingestion and audio transcription. The gateway redacts
  known identifiers, requests non-storage, preserves original sources internally, and fails closed
  when no API key is configured.

## Technology

- Python 3.11+
- FastAPI and Uvicorn
- Pydantic domain models
- Vanilla HTML, CSS, and JavaScript
- Pytest and Ruff
- PostgreSQL/psycopg and Fernet authenticated encryption (optional production adapter)

Development defaults to an in-memory repository. Setting `CARENOTE_DATABASE_URL` selects the
encrypted PostgreSQL adapter and runs the RLS migration.

## Quick start

Create a virtual environment and install the application with development dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Start the development server with automatic reload:

```powershell
.\.venv\Scripts\python.exe -m uvicorn carenote.main:app --reload
```

Open:

- Application: `http://127.0.0.1:8000`
- Interactive API documentation: `http://127.0.0.1:8000/docs`
- Health endpoint: `http://127.0.0.1:8000/api/health`

## Offline demo with Docker

```bash
docker compose up --build
```

This starts the app on encrypted PostgreSQL with the synthetic seed patient. Sign in as
`clinician` / `clinician-demo-2026` and use **Prepare review** in the care-team workspace. The
review agent replays a recorded draft from `evals/cassettes/`, so no API key or model call is
needed; the panel names the draft's source. The recorded seed draft is hand-written to show the
workflow and is labelled as such, not presented as model output. Set
`CARENOTE_AGENT_GATEWAY=openai` and `OPENAI_API_KEY` to draft with a live model instead.

`python scripts/smoke_demo.py` signs in, runs a review and commits it against a running demo;
CI runs it against the Docker stack on every change.

## Synthetic demo identity

The UI uses a server login and a signed, expiring HttpOnly session cookie. Development-only
accounts are `patient`, `staff`, `clinician`, and `admin`; their passwords follow
`<username>-demo-2026`. Actor IDs and role/clinic headers are never accepted from the browser.
When PostgreSQL is configured, accounts, active memberships, and hashed session IDs are persisted.

## Verification

Run the complete automated test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run static and formatting checks:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check src tests scripts
node --check src/carenote/static/api.js
node --check src/carenote/static/app.js
node --check src/carenote/static/service-worker.js
.\.venv\Scripts\python.exe scripts\benchmark_glance.py --iterations 1000 --warmup 100
```

The required acceptance scenarios are organized in:

- `tests/test_rbac_scope.py`
- `tests/test_revision_history.py`
- `tests/test_highlight_provenance.py`
- `tests/test_concurrent_edits.py`

Additional tests cover the synthetic dataset, patient API, and frontend/API contract.
The current local baseline is 45 passing tests.

## Architecture and trust boundaries

HTTP requests enter through the API layer and then pass to the service layer, where clinic scope,
role ownership, authorship, concurrency, and provenance rules are enforced. Repository adapters
handle persistence. The browser UI is not a security boundary.

Highlights contain a `risk_reason`, `suggested_action`, review status, and a
`provenance_pointer`. The repository rejects pointers that do not resolve to the same patient's
timeline or whose character span lies outside the source entry.

See `docs/ARCHITECTURE.md` for the current architecture and production direction.

## Security and privacy status

Implemented in the prototype:

- Synthetic data only.
- Server-side clinic scope and role ownership checks.
- Patient self-access restriction.
- Patient response filtering for internal and raw AI content.
- Full section snapshots and diffs, with a separate metadata-only audit event stream.
- Deterministic PHI redaction gateway used before optional external text-summary requests.
- Signed session tokens, expiration/revocation, PostgreSQL account/membership schema, and forced
  clinic RLS policies.
- Authenticated encryption of PostgreSQL clinical snapshots and cold archives; hash-chained audit
  events with database anchors; metadata-only request logs and retention maintenance.
- Optional, fail-closed external model and transcription adapters; no call occurs without an
  explicitly configured project API key.
- Reproducible Glance benchmark: 1.535 ms warm-path P95 in the latest recorded local run.

Required before production:

- Validate the PostgreSQL adapter and RLS policies against the target managed PostgreSQL service.
- Connect TLS certificates, a managed key/secret service, immutable audit export, legal holds,
  backup/restore, and operational retention scheduling.
- Replace the local password provisioning workflow with the organization's identity provider,
  MFA/SSO, recovery, and account lifecycle process.
- Repeat Glance load testing with a production-like database, network, concurrency, and
  representative dataset; the local in-memory result is not a capacity claim.

No external LLM call occurs by default. `OPENAI_API_KEY` explicitly enables the model boundary;
real clinical use still requires organizational privacy, retention, and validation approval.

## Project layout

```text
src/carenote/
├── api/           HTTP routes and response handling
├── core/          Application configuration
├── data/          Deterministic synthetic seed data
├── domain/        Domain models and API contracts
├── privacy/       PHI redaction boundary
├── repositories/  Persistence adapters
├── services/      Use cases and policy enforcement
└── static/        Patient web interface
tests/             Automated acceptance and integration tests
docs/              Architecture and development documentation
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md): layers, trust boundaries and production direction
- [Technical brief](docs/TECHNICAL_BRIEF.md): product thesis, scope and design choices
- [Development](docs/DEVELOPMENT.md): requirements, setup, tests and operation
- [Production security](docs/PRODUCTION_SECURITY.md): required configuration and secret handling
- [Performance](docs/PERFORMANCE.md): Glance warm-path measurements
- [Clinical Review Agent design](docs/AGENT_DESIGN.md): proposed multi-step review agent
- [Agent roadmap](docs/AGENT_ROADMAP.md): build phases, progress and decisions
- [Attribution](ATTRIBUTION.txt): third-party licences

## License

Licensed under the MIT License. See `LICENSE`.
