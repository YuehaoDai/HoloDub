-- OPT-204 PR-12 down migration.
--
-- The up migration adds no schema; this down migration is therefore a
-- no-op marker for symmetry (incremental-evolution.mdc §5 requires a
-- paired down.sql for every up.sql, even when the up is empty).
--
-- If an operator needs to PURGE existing meta.dubbing data after a
-- rollback (rare; the sub-key is optional and the legacy translate path
-- ignores it), they can run:
--   UPDATE segments SET meta = meta - 'dubbing' WHERE meta ? 'dubbing';
-- This is NOT performed automatically because it is destructive and
-- the OPT-204 plan ships everything behind a feature flag — disabling
-- DUBBING_PLAN_ENABLED is sufficient to stop new writes, and the
-- existing data is safe to keep around for offline analysis.

SELECT 1 AS opt_204_pr12_down_marker;
