CREATE TABLE IF NOT EXISTS care_note_snapshots (
    clinic_id uuid PRIMARY KEY,
    encrypted_payload bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clinic_memberships (
    actor_id uuid NOT NULL,
    clinic_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('patient', 'staff', 'clinician', 'admin')),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (actor_id, clinic_id)
);

CREATE TABLE IF NOT EXISTS authenticated_sessions (
    session_hash text PRIMARY KEY,
    actor_id uuid NOT NULL,
    clinic_id uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY (actor_id, clinic_id) REFERENCES clinic_memberships(actor_id, clinic_id)
);

CREATE TABLE IF NOT EXISTS user_accounts (
    username text PRIMARY KEY,
    actor_id uuid NOT NULL,
    clinic_id uuid NOT NULL,
    password_salt bytea NOT NULL,
    password_hash bytea NOT NULL,
    active boolean NOT NULL DEFAULT true,
    FOREIGN KEY (actor_id, clinic_id) REFERENCES clinic_memberships(actor_id, clinic_id)
);

CREATE TABLE IF NOT EXISTS audit_chain_anchors (
    clinic_id uuid NOT NULL,
    anchored_at timestamptz NOT NULL DEFAULT now(),
    event_hash text NOT NULL,
    PRIMARY KEY (clinic_id, anchored_at)
);

CREATE TABLE IF NOT EXISTS cold_entry_archives (
    clinic_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    entry_id uuid PRIMARY KEY,
    compressed_summary text NOT NULL,
    encrypted_payload bytea NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE care_note_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_note_snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE authenticated_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE authenticated_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE user_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_chain_anchors ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_chain_anchors FORCE ROW LEVEL SECURITY;
ALTER TABLE cold_entry_archives ENABLE ROW LEVEL SECURITY;
ALTER TABLE cold_entry_archives FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS clinic_snapshot_isolation ON care_note_snapshots;
CREATE POLICY clinic_snapshot_isolation ON care_note_snapshots
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);

DROP POLICY IF EXISTS membership_clinic_isolation ON clinic_memberships;
CREATE POLICY membership_clinic_isolation ON clinic_memberships
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);

DROP POLICY IF EXISTS session_clinic_isolation ON authenticated_sessions;
CREATE POLICY session_clinic_isolation ON authenticated_sessions
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);

DROP POLICY IF EXISTS user_account_clinic_isolation ON user_accounts;
CREATE POLICY user_account_clinic_isolation ON user_accounts
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);

DROP POLICY IF EXISTS audit_anchor_clinic_isolation ON audit_chain_anchors;
CREATE POLICY audit_anchor_clinic_isolation ON audit_chain_anchors
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);

DROP POLICY IF EXISTS cold_archive_clinic_isolation ON cold_entry_archives;
CREATE POLICY cold_archive_clinic_isolation ON cold_entry_archives
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);
