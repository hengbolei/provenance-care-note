# Clinical Review Agent — Roadmap

Status tracker for building the agent described in [AGENT_DESIGN.md](AGENT_DESIGN.md). The
design says *what* and *why*; this file says *in what order* and *where we are*. Update it in the
same PR as the work it describes.

Last updated: 2026-10-18 · Current phase: **Phase 1 — Minimal end-to-end loop**

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

## Phase 1 — Minimal end-to-end loop · 🚧 in progress

Branch: `feat/agent-mvp`. Design §17 week 1. Build framework-free parts first, LangGraph last.

- [x] 1.1 `agent/state.py`: `Citation`, `Claim`, `DraftReview`, `CandidateHighlight`,
      `ClaimCheck`, `Budget`, `ReviewState`
- [x] 1.2 `agent/verify.py`: deterministic checks 1–6 from design §8, pure functions with dense
      unit tests (exists and in scope, in bounds, quote match with relocation, entity support,
      patient-source risk cap, conflict coverage)
- [x] 1.3 `agent/gateway.py`: `ModelGateway` protocol (`structured()`, `text()`);
      `OpenAIGateway` with JSON-schema output and mandatory redaction; `RecordedGateway` over
      `evals/cassettes/`; move `summarize` onto it without changing `services/ai.py` callers
- [x] 1.4 `agent/tools.py`: read tools that call `CareNoteService` as `agent.principal`;
      `propose_highlight` writes to run state only
- [x] 1.5 `agent/graph.py`: `load_context → check_conflicts → draft → verify →
      human_review (interrupt) → commit | discard`, `MemorySaver`; `langgraph` as optional
      extra `[agent]`
- [ ] 1.6 `agent/runs.py` and API: endpoints from design §15, the nine `agent_*` audit
      operations, WebSocket `agent.step` / `agent.draft` / `agent.done`
- [ ] 1.7 Commit path: accepted claims become a clinician-authored revision of an internal
      `agent_review` section; accepted highlights go through `create_highlight`
- [ ] 1.8 Review panel in `static/app.js`: claims, clickable citations, accept / edit / reject,
      Commit
- [ ] 1.9 Offline demo: `RecordedGateway` by default with a seed cassette; `docker-compose.yml`
      with app and Postgres

**Exit criteria**: with no API key, "Prepare review" on the seed patient returns a draft whose
citations all pass verification; Commit creates the `agent_review` revision, highlights and an
intact audit chain; CI green.

## Phase 2 — Evaluation and observability

Branch: `feat/agent-evals`. Design §17 week 2.

- [ ] 2.1 `evals/cases/*.yaml`: at least 30 golden cases across the families in design §13
- [ ] 2.2 `evals/run_evals.py --mode recorded`, Markdown report, CI job that fails on regression
- [ ] 2.3 Budgets: 12 steps, 2 revise loops, token and cost ceilings, 60 s; partial draft and
      `agent_budget_exceeded` on breach
- [ ] 2.4 `migrations/002_agent_runs.sql` with RLS; Postgres checkpointer so runs survive
      restarts
- [ ] 2.5 OpenTelemetry tracing (Langfuse optional); trace ID on audit events; run timeline
      view

**Exit criteria**: metrics table linked from the README; CI eval gate active; trace visible for
a demo run.

## Phase 3 — Retrieval, safety and a second provider

Branch: `feat/agent-retrieval`. Design §17 week 3. Blocked on the storage decision below.

- [ ] 3.0 Decide the retrieval storage model (see [Open questions](#open-questions))
- [ ] 3.1 Embedding index with clinic scope; hybrid search with reciprocal rank fusion
- [ ] 3.2 `plan` and `retrieve` nodes
- [ ] 3.3 `revise` loop, at most 2 iterations
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

- **`agent_review` visibility (needed by step 1.7).** Design §18 defaults to internal after
  commit. Proposed: keep it internal; patient-facing guidance goes through the existing flow.
- **Retrieval storage (blocks Phase 3).** `PostgresCareNoteRepository` stores one encrypted
  snapshot per clinic and searches in memory, so the in-database `tsvector` / pgvector search in
  design §9 cannot index anything. Options: (a) move entries to per-row storage with encrypted
  content plus a redacted search column or embeddings; (b) keep the snapshot and run vector
  search in application memory.

## Known limitations

- `OpenAIGateway` is covered by mocked HTTP tests only; strict structured output has not yet been
  run against the live API, so schema keywords the provider rejects may surface at step 1.9.
- Entity extraction (`services/entities.py`) only recognises a medication as a word followed by
  a dose or an action verb, which will cap recall, and reads any word after "hold", "start" and
  similar verbs as a drug ("hold pending"). The verifier filters this; the conflict detector does
  not yet. Revisit in Phase 3 using eval results.
- `labelled_id` redaction now requires a digit in the identifier, so letter-only identifiers are
  not redacted by that rule.
