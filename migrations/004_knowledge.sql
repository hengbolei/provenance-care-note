-- Reference knowledge base (design §19): clinic policies, guidelines and drug labels.
-- Payloads are encrypted by the application with the clinical data key. Both tables are
-- clinic-scoped by forced row-level security. Versions are immutable: there is no UPDATE or
-- DELETE policy on knowledge_versions, so such statements change no rows for the app role.
CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL,
    encrypted_payload bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_versions (
    document_id uuid NOT NULL REFERENCES knowledge_documents (document_id),
    version integer NOT NULL CHECK (version >= 1),
    clinic_id uuid NOT NULL,
    encrypted_payload bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, version)
);

ALTER TABLE knowledge_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_documents FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_versions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS knowledge_document_clinic_isolation ON knowledge_documents;
CREATE POLICY knowledge_document_clinic_isolation ON knowledge_documents
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);

DROP POLICY IF EXISTS knowledge_version_read ON knowledge_versions;
CREATE POLICY knowledge_version_read ON knowledge_versions FOR SELECT
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);

DROP POLICY IF EXISTS knowledge_version_insert ON knowledge_versions;
CREATE POLICY knowledge_version_insert ON knowledge_versions FOR INSERT
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);
