# Clinical Review Agent — Roadmap

Status tracker for building the agent described in [AGENT_DESIGN.md](AGENT_DESIGN.md). The
design says *what* and *why*; this file says *in what order* and *where we are*. Update it in the
same PR as the work it describes.

Last updated: 2026-10-18 · Current phase: **Phase 2 — Evaluation, self-correction and observability**

## How we work

1. **One branch per phase** (`feat/agent-<phase>`), cut from `main`. Small steps accumulate on
   it as separate commits; each step below is one commit.
2. **Confirm before committing.** After each step: tests and `ruff check` pass, the change is
   summarised, and it is committed only after review.
3. **One PR per phase** into `main`, opened when the phase's exit criteria are met. CI
   (Python 3.11 and 3.12) must be green; merging is a manual decision.
4. **Fixes found on the way** go on the current branch as their own `fix(...)` commit, with a
   regression test that fails without the fix.
5. **Keep this file current**: tick the step, add the commit hash, and record any decision that
   departs from the design under [Decisions](#decisions).

Step status: `[x]` done · `[ ]` to do · `[~]` in progress.

## Phase 0 — Foundations (no model calls) · ✅ done

Merged in PR #3 (`e62d79c`).

- [x] 0.1 Map citation offsets through PHI redaction: `RedactedSpan`, `to_original()`,
      `to_redacted()` (`0adee1c`)
- [x] fix: `labelled_id` redacted ordinary words such as "idiopathic" (`a4145b7`)
- [x] 0.2 Shared entity extraction with spans: `services/entities.py` (`b3fdd27`)
- [x] 0.3 `Span` model and citable keyword `search_entries()` (`0974d95`)
- [x] 0.4 `AgentActor` and `EntryRef`; SYSTEM writes rejected (`4ab4238`)
- [x] fix: patients could create internal sections (`96f6aff`)

## Phase 1 — Minimal end-to-end loop · ✅ done

Merged in PR #6 (`50572c9`). CI, including the Docker demo smoke test, passed on the merge.

Branch: `feat/agent-mvp`. Design §17 week 1. Build framework-free parts first, LangGraph last.

- [x] 1.1 `agent/state.py`: `Citation`, `Claim`, `DraftReview`, `CandidateHighlight`,
      `ClaimCheck`, `Budget`, `ReviewState` (`d5cedc1`)
- [x] 1.2 `agent/verify.py`: deterministic checks 1–6 from design §8, pure functions with dense
      unit tests (exists and in scope, in bounds, quote match with relocation, entity support,
      patient-source risk cap, conflict coverage) (`be586b8`)
- [x] 1.3 `agent/gateway.py`: `ModelGateway` protocol (`structured()`, `text()`);
      `OpenAIGateway` with JSON-schema output and mandatory redaction; `RecordedGateway` over
      `evals/cassettes/`; move `summarize` onto it without changing `services/ai.py` callers
      (`d33310c`)
- [x] 1.4 `agent/tools.py`: read tools that call `CareNoteService` as `agent.principal`;
      `propose_highlight` writes to run state only (`9fce3b0`)
- [x] 1.5 `agent/graph.py`: `load_context → check_conflicts → draft → verify →
      human_review (interrupt) → commit | discard`, `MemorySaver`; `langgraph` dependency
      (`c2a1900`)
- [x] fix: medication-state support check read "hold pending" as a drug (`8d55516`)
- [x] 1.6 `agent/runs.py` and API: endpoints from design §15, the nine `agent_*` audit
      operations, WebSocket `agent.step` / `agent.draft` / `agent.done` (`4c630cb`)
- [x] 1.7 Commit path: accepted claims become a clinician-authored revision of an internal
      `agent_review` section; accepted highlights go through `create_highlight` (`4ef472d`)
- [x] 1.8 Review panel in `static/app.js`: claims, clickable citations, accept / edit / reject,
      Commit (`2b698d4`)
- [x] 1.9 Offline demo: `RecordedGateway` by default with a seed cassette; `docker-compose.yml`
      with app and Postgres (`aa19648`)

**Exit criteria**: with no API key, "Prepare review" on the seed patient returns a draft whose
citations all pass verification; Commit creates the `agent_review` revision, highlights and an
intact audit chain; CI green.

## Phase 2 — Evaluation, self-correction and observability · ✅ done (pending merge)

Branch: `feat/agent-evals`. Design §17 week 2, plus the revise loop from week 3. Plan revised in
`c9d0169`.

No API key is available, so evaluation is split in two (see [Decisions](#decisions)): a
**pipeline** suite that needs no model and gates CI, and an **agent** suite that scores real
model drafts from cassettes and reports "not recorded" until they exist.

- [x] 2.1 Eval case format and at least 30 cases in `evals/cases/*.yaml` across the design §13
      families: each case is a synthetic record, the conflicts and identifiers it contains, the
      critical facts a review must cover, and probe claims with the verdict the verifier must
      reach (supported, fabricated citation, misquote, wrong dose, wrong polarity, patient-only
      overreach, injection-following) (`45187a6`)
- [x] 2.2 `evals/run_evals.py` runs both suites: pipeline (conflict recall, redaction and
      model-bound payload leakage, verifier verdict accuracy, key entries in context) and agent
      (citation validity, unsupported-claim rate, critical-finding recall, conflict coverage,
      injection resistance, tokens and cost from cassettes); generated `evals/REPORT.md` checked
      in CI; CI gate (`6a5991d`)
- [x] 2.3 Automatic `revise` node: unsupported claims and uncovered conflicts go back to the model
      with the verifier's reasons (versioned `revise_v1` prompt), at most `max_auto_revisions`
      loops, then remaining failures are shown as unsupported; the agent suite reports revision's
      effect (`92e38aa`)
- [x] 2.4 `migrations/002_agent_runs.sql` with RLS and a Postgres checkpointer so runs survive
      restarts, with encrypted checkpoints and clinic-scoped RLS; verified by Postgres tests and
      the CI Docker job (`9008b49`)
- [x] 2.5 OpenTelemetry tracing (Langfuse optional): a span per node, tool and model call; the
      trace ID is the run ID that audit events carry; run timeline in the panel (`603e3b7`)
- [x] Budgets (formerly 2.3): step, revision, token, cost and time ceilings with a partial draft
      and `agent_budget_exceeded` already shipped in Phase 1 (`c2a1900`, `4c630cb`)

**Exit criteria**: the pipeline suite gates CI; the agent suite runs end to end on recorded
cassettes and reports honestly when none are recorded; the revise loop is covered by tests and
measured once live cassettes exist; a run survives a restart in the Docker demo; a trace is
visible for a demo run.

## Phase 3 — Retrieval, safety and a second provider

Branch: `feat/agent-retrieval`. Design §17 week 3. Blocked on the storage decision below.

- [ ] 3.0 Decide the retrieval storage model (see [Open questions](#open-questions))
- [ ] 3.1 Embedding index with clinic scope; hybrid search with reciprocal rank fusion
- [ ] 3.2 `plan` and `retrieve` nodes
- [x] 3.3 `revise` loop: moved to step 2.3
- [ ] 3.4 Prompt-injection defences and eval cases
- [ ] 3.5 Second provider adapter with fallback

**Exit criteria**: 0 injection failures; critical-finding recall ≥ 90% on timelines of 50+
entries.

## Later

- Expose tools as an MCP server
- Streaming token output
- Per-claim LLM-judge flag in the review panel

## Decisions

Departures from or refinements of the design, newest first.

| Date | Decision | Why |
| --- | --- | --- |
| 2026-10-18 | Each run is one OpenTelemetry trace whose trace ID is the run ID | Spans from separate graph segments, and from before and after a restart, join one trace; audit events already carry the run ID, so they join without changing the hash-chained audit schema |
| 2026-10-18 | Spans carry IDs, node names, durations, model names, prompt versions and token counts only, never record, claim or quote text | Traces leave the clinical boundary to an external collector, so they must hold nothing that needs redaction |
| 2026-10-18 | The panel's run timeline comes from metadata-only steps stored on the run (`agent_runs.timeline`), not from the trace backend | It works with no collector configured and survives restarts with the run |
| 2026-10-18 | OTLP export needs the optional `otel` extra and `OTEL_EXPORTER_OTLP_ENDPOINT`; Langfuse is not bundled in Docker Compose | Any OpenTelemetry collector works, and the default demo stays small |
| 2026-10-18 | Checkpoints use LangGraph's `PostgresSaver` with every serialized value Fernet-encrypted by the clinical data key; loading an unencrypted value is refused | The rest of the database is encrypted at rest, and drafts, quotes and decisions are clinical text; LangGraph keeps only primitive values (status, prompt version, model) in clear |
| 2026-10-18 | `agent_runs` (migration 002) is clinic-scoped by forced RLS, and LangGraph's checkpoint tables are scoped through it (migration 003, applied after the saver creates them); connections set `app.current_clinic_id` when they open | Matches the existing per-clinic isolation; a checkpoint is visible only when its run belongs to the current clinic |
| 2026-10-18 | Postgres tests run against `CARENOTE_TEST_DATABASE_URL` in a CI service container, or a local throwaway server from the optional `pgserver` package, and skip otherwise | Real RLS, encryption and restart behaviour need a real Postgres |
| 2026-10-18 | Automatic revisions (`max_auto_revisions`, default 2) are budgeted separately from clinician-requested redrafts (`max_revisions`) | A run that used its automatic fixes can still be redrafted at the clinician's request |
| 2026-10-18 | Only automated nodes (load, conflicts, draft, verify, revise) count as steps; review, commit and discard do not | With two automatic and two clinician revisions the worst case is exactly the design's 12 steps |
| 2026-10-18 | The revise request sends the previous claims, citations and failure reasons as `DataTag`s with redacted text, and the state keeps the first draft's verification | Typed tags keep IDs and reasons out of free text; keeping the first verification lets the agent suite measure what revision changed |
| 2026-10-18 | `evals/REPORT.md` is generated, committed and checked: CI regenerates it and fails if it differs, so the README's metrics always match the code | A metrics table that can drift from the code is worse than none |
| 2026-10-18 | The pipeline suite also scores the exact model-bound payload, captured before the cassette lookup, so identifier leakage and context coverage are measured with no model | These are properties of what the agent sends, not of what the model answers |
| 2026-10-18 | A known gap that starts passing fails the gate as stale | Keeps the known-gap list honest; the marker must be removed so a later regression is caught |
| 2026-10-18 | Eval cases mark anything the system is known to get wrong with a `known_gap` reason; reports show known gaps separately and they do not fail the gate | The nine gaps found while writing the 32 cases (brand vs generic names; past-tense "stopped" and "takes"; "and 20 mg" and "at 10 mg" dose parsing; "allergy with"; invented diagnoses and invented undosed drugs passing; partial names and email addresses not redacted) stay visible instead of being worded around |
| 2026-10-18 | Evaluation has two suites: `pipeline` (no model; conflict detection, redaction and verifier accuracy on probe claims) gates CI, and `agent` scores model drafts from cassettes, reporting "not recorded" until live cassettes exist | Without an API key, scoring hand-written drafts would give good-looking numbers that measure nothing; the deterministic parts can be measured honestly now |
| 2026-10-18 | The automatic revise loop moves from Phase 3 (3.3) to Phase 2 (2.3) | It is the first step where the model reacts to feedback from its environment, which is what makes this an agent rather than a fixed workflow |
| 2026-10-18 | Budgets (old step 2.3) are marked done | They shipped with the graph and run service in Phase 1 |
| 2026-10-18 | The seed cassette is a curated, hand-written draft (`scripts/record_demo_cassette.py`), labelled `curated-demo` in the cassette and shown in the panel; `--source openai` re-records it live | No API key was available; the demo must never pass hand-written text off as model output |
| 2026-10-18 | CI gains a `demo` job: `docker compose up --wait`, then `scripts/smoke_demo.py` signs in, runs a review and commits it | Docker was not available locally, so the stack is verified in CI |
| 2026-10-18 | Checkpoints deserialize only the review state's own types (`allowed_msgpack_modules`) | LangGraph warns it will block unregistered types; an explicit allowlist also stops a tampered checkpoint instantiating anything else |
| 2026-10-18 | Prompts are loaded with newlines normalised to LF | A Windows checkout with `core.autocrlf=true` would otherwise change the prompt bytes and miss every cassette |
| 2026-10-18 | Review panel defaults: verified claims start as "include as written", unsupported ones as "leave out" with accept disabled; highlights start unchecked | One click commits what verified; anything unverified needs a deliberate edit |
| 2026-10-18 | The panel polls the run every second while it is running and also refreshes on `agent.*` socket events; the active run id is kept in `sessionStorage` | Polling keeps progress working if the socket drops; the run survives a page reload |
| 2026-10-18 | `agent_review` stays internal: it is a section, and sections are care-team only | Resolves the design §18 open question with its default, by construction rather than a flag |
| 2026-10-18 | A claim that failed verification can be edited or rejected but not accepted as written; a highlight whose citation did not resolve cannot be accepted (422) | Keeps unverified model text out of the record unless a clinician rewrites it |
| 2026-10-18 | Committed highlights are created as `moderate` and `needs_review`; the scorer sets priority | Design §10 keeps risk out of the model's hands; the scorer takes risk as input, so the clinician sets the final risk in the highlight workflow |
| 2026-10-18 | Each commit replaces the `agent_review` content as a new revision, listing accepted claims with their source spans | Revision history keeps earlier reviews; the section reads as the latest accepted review |
| 2026-10-18 | `langgraph` is a core dependency, not an optional `[agent]` extra | The API serves agent endpoints at startup; an optional extra would leave the app half-working without it |
| 2026-10-18 | Review endpoints split by action: `POST /agent-runs/{id}/review` takes only a revise instruction, `/commit` takes per-item decisions, `/discard` takes nothing | Each request has one meaning, and validation can be exact |
| 2026-10-18 | `/commit` returns 501 until step 1.7 wires a committer | Marking a run committed without persisting anything would mislead |
| 2026-10-18 | Start and revise run on a 4-worker thread pool and return 202 at once; commit and discard run inline | Drafting takes seconds; commit and discard are fast and their result belongs in the response |
| 2026-10-18 | Audit: run-level events use `resource=agent_run`; tool calls `agent_tool:{run}:{tool}`; claim decisions `agent_claim:{run}:{index}`; added `agent_run_failed` | `AuditEvent` has no metadata field, so the run id and item travel in `resource_id` |
| 2026-10-18 | Realtime events carry `audience: care_team` and counts only, never text; patient sockets skip them | Agent runs are internal |
| 2026-10-18 | Phase 1 graph supports clinician-requested revision (`ReviewDecisions.action = "revise"` routes back to `draft` with the instruction, within `max_revisions`); the automatic `revise` loop stays in step 3.3 | Design §11 lets the clinician ask for changes; reusing `draft` avoids a second prompt in Phase 1 |
| 2026-10-18 | Record data reaches the model inside `DataTag` markers (`<entry id=... type=... author=...>`) whose attributes must be IDs, enums, timestamps, booleans or integers | Frames untrusted text for injection resistance without letting free text bypass redaction; re-redacting framed text could mangle entry IDs that look like phone numbers |
| 2026-10-18 | The 60 s wall-clock budget restarts when a clinician answers `human_review` | The ceiling is for automated work; review time is unbounded |
| 2026-10-18 | Commit is an injected callable that receives the state and the reviewing clinician; the graph never writes to the record itself | Keeps step 1.7's persistence separate and testable |
| 2026-10-18 | Medication-state facts count in the support check only for drugs the record doses somewhere | The state pattern reads "hold pending review" as a drug named "pending" |
| 2026-10-18 | Tools read one patient-view snapshot per run, taken as the principal clinician; search, read and evidence results are redacted and cite offsets in the redacted text, with each entry's `RedactionResult` kept for verification | Every tool sees the same record, and citations stay mappable to the original |
| 2026-10-18 | `get_patient_overview` returns age in years and pronouns only, never name, date of birth or record number | Design §7 asks for redacted demographics; age is all the review needs |
| 2026-10-18 | Added `CareNoteService.search_entries` for care-team roles | Tools must read through the service policy boundary, not the repository |
| 2026-10-18 | Gateway requests take `RedactionResult` inputs only (plain strings raise `TypeError`); instructions are the only free text | Makes design §10 "refuses unredacted input by construction" concrete |
| 2026-10-18 | Cassettes are keyed by a SHA-256 of purpose, schema, instructions and redacted input, and store only the output, model and usage | Any prompt or input change forces a re-record; no record text is written to disk |
| 2026-10-18 | Cost is reported only when per-token prices are configured, otherwise `None` | Prices change per model; a guessed figure would mislead the budget |
| 2026-10-18 | Audio transcription stays on its direct HTTP call, outside the gateway | It sends audio, not redactable text; moving it is out of scope for the agent |
| 2026-10-18 | Check 5 phrasing is enforced: claims resting only on patient-sourced text (patient notes and AI patient-session summaries) must contain a reporting cue such as "reports" or "states", else `patient_source_phrasing` | Design §8 requires reported phrasing; a deterministic cue list is checkable, and the revise loop can fix misses |
| 2026-10-18 | Check 4 support: every medication, dose and allergy fact the claim states must appear in its cited text with the same dose and polarity; claims with no extractable facts pass on valid citations alone | Reuses `extract_entities`, so support is only as strong as extraction (see Known limitations) |
| 2026-10-18 | Verification results are a `VerificationReport` (per-claim `ClaimCheck` plus uncovered conflict IDs) instead of `list[ClaimCheck]` | Conflict coverage (check 6) belongs to the whole draft, not to one claim |
| 2026-10-18 | `CandidateHighlight` has no risk level or priority field, and model-output schemas forbid extra keys | Design §10: the deterministic scorer sets highlight risk and priority; the model cannot smuggle them in |
| 2026-10-18 | Default budget: 60k tokens and $0.50 per run, beside the design's 12 steps, 2 revisions and 60 s | The design names token and cost ceilings without values; revisit with Phase 2 trace data |
| 2026-10-17 | Agent tools **read as the principal clinician**; the `AgentActor` (Role.SYSTEM) is write-denied | Every service read already denies SYSTEM; reading as the clinician reuses their exact scope instead of adding agent-specific permissions |
| 2026-10-17 | Redaction offset mapping moved from a risk (design §18) to a Phase 0 prerequisite | Citations cannot be verified without it |

## Open questions

- **Retrieval storage (blocks Phase 3).** `PostgresCareNoteRepository` stores one encrypted
  snapshot per clinic and searches in memory, so the in-database `tsvector` / pgvector search in
  design §9 cannot index anything. Options: (a) move entries to per-row storage with encrypted
  content plus a redacted search column or embeddings; (b) keep the snapshot and run vector
  search in application memory.

## Known limitations

- The draft prompt receives only the 40 most recent entries. In 3 of the 4 long-timeline eval
  cases an earlier key entry never reaches the model (see `evals/REPORT.md`); Phase 3 retrieval
  addresses this.
- Commit writes the section, then each highlight; a failure part-way leaves the earlier writes in
  place and marks the run failed. Decisions are validated first to make this unlikely.
- Without `CARENOTE_DATABASE_URL`, runs and checkpoints live in memory and a restart loses them;
  with Postgres they persist (step 2.4).
- `OpenAIGateway` is covered by mocked HTTP tests only; strict structured output has not yet been
  run against the live API. The first `record_demo_cassette.py --source openai` run will show
  whether the provider rejects any schema keyword.
- The Docker demo is checked by the CI `demo` job (it passed on PR #6); it has not been run on a
  local Docker install.
- Entity extraction (`services/entities.py`) only recognises a medication as a word followed by
  a dose or an action verb, which will cap recall, and reads any word after "hold", "start" and
  similar verbs as a drug ("hold pending"). The verifier filters this; the conflict detector does
  not yet. Revisit in Phase 3 using eval results.
- `labelled_id` redaction now requires a digit in the identifier, so letter-only identifiers are
  not redacted by that rule.
