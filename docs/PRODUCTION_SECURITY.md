# Production security boundary

The application refuses to start in `production` unless the token secret, PostgreSQL URL,
encryption key, and HTTPS public URL are explicitly configured. Store those values in a
managed secret/key service; do not commit a populated `.env` file.

Terminate TLS with the supplied reverse-proxy baseline or an equivalent managed ingress.
Rotate the signing and encryption keys under a documented key version. Existing encrypted
snapshots must be re-encrypted before retiring the previous encryption key.

Clinical snapshots use authenticated encryption before PostgreSQL storage. PostgreSQL RLS
is forced on clinic-scoped tables. The application sets `app.current_clinic_id` inside each
database transaction. Use a non-superuser application role that cannot bypass RLS.

Audit events form a SHA-256 hash chain and each PostgreSQL write records an external chain
anchor. This is tamper-evident, not WORM storage. Export anchors to immutable object storage
or a compliance log service for production non-repudiation.

Request logs contain method, route path, status, and duration only. Headers, cookies, query
values, request bodies, response bodies, names, MRNs, and clinical text are excluded. Apply
the configured retention period through a scheduled maintenance job and document legal-hold
exceptions before deleting any records.
