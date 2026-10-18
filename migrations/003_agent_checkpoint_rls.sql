-- Clinic isolation for LangGraph's checkpoint tables, applied after the checkpointer creates them.
-- A checkpoint row is visible only when its thread (the run id) belongs to a run the current
-- clinic can see; agent_runs is itself clinic-scoped, so the policy inherits that scope.
DO $$
DECLARE
    tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['checkpoints', 'checkpoint_blobs', 'checkpoint_writes'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS agent_checkpoint_clinic_isolation ON %I', tbl);
        EXECUTE format(
            'CREATE POLICY agent_checkpoint_clinic_isolation ON %I '
            'USING (EXISTS (SELECT 1 FROM agent_runs r WHERE r.run_id::text = %I.thread_id)) '
            'WITH CHECK (EXISTS (SELECT 1 FROM agent_runs r WHERE r.run_id::text = %I.thread_id))',
            tbl, tbl, tbl
        );
    END LOOP;
END
$$;
