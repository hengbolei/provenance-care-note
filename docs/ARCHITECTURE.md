# Architecture

Requests enter through the API layer, which parses HTTP input before calling the service layer.
The service layer enforces RBAC, clinic scope, section ownership, concurrency, and provenance
rules. Repository adapters handle persistence. The UI is never treated as a security boundary.
Signed, expiring sessions bind actor, role, clinic, and a revocable session ID. PostgreSQL-backed
accounts and active clinic memberships are checked at login; `system` cannot receive an
interactive session. Legacy actor/role/clinic request headers are ignored.

Glance priorities come from deterministic visible factors rather than a model-reported score. A
bounded reviewed-outcome factor activates only after three human decisions and shrinks according
to review/exposure coverage. A narrow conflict detector covers medication state, dose, and allergy
polarity. Provenance is
two-hop: a Highlight resolves to an exact Timeline span, and the Timeline entry resolves to an
exact span in a source artifact. Comments can target entries, sections, or exact character spans;
Comment and Highlight revisions store complete snapshots separately from metadata-only audit logs.

`InMemoryCareNoteRepository` is intended only for rapid development and unit tests. The optional
PostgreSQL adapter encrypts clinical snapshots and cold archives before storage. Forced RLS uses
the transaction-local `app.current_clinic_id`; account, membership, session, snapshot, archive,
and audit-anchor tables are clinic-scoped.

The optional OpenAI adapter sits behind `PHIRedactionGateway`, requests `store=false`, hashes its
safety identifier, and fails closed when no key is configured. Original source text remains in the
encrypted clinical repository and generated summaries remain unconfirmed. Audio transcription
preserves unknown speaker/confidence values instead of inventing them. Logs contain only method,
path, status, and duration; they exclude headers, query values, bodies, cookies, and clinical text.

WebSocket sessions use the same signed cookie and patient/clinic authorization. Presence and
patient audit generation drive online state and automatic refresh. Audit events are hash chained;
PostgreSQL anchors make changes detectable, while production non-repudiation still requires
external immutable/WORM export.

See `TECHNICAL_BRIEF.md` for the full schema, safety decisions, performance evidence, trade-offs,
and environment-gated production gaps.
