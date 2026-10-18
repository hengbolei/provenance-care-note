-- Registry of Clinical Review Agent runs. Graph state lives in LangGraph's checkpoint tables
-- (scoped through this table by 003_agent_checkpoint_rls.sql); this row ties a run to its clinic.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    started_by uuid NOT NULL,
    agent_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    error text,
    audit_version integer NOT NULL DEFAULT 0,
    tool_calls_audited integer NOT NULL DEFAULT 0
);

ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_run_clinic_isolation ON agent_runs;
CREATE POLICY agent_run_clinic_isolation ON agent_runs
    USING (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = nullif(current_setting('app.current_clinic_id', true), '')::uuid);
