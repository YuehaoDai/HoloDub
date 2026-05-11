-- Migration: OPT-407 Closed-loop Rework Engine.
--
-- Adds three nullable columns to the episodes table that drive the OPT-407
-- closed-loop rework engine (see internal/rework/). All three are added in
-- a single migration because they are written together every time the
-- rework engine acts and reading them as one consistent snapshot is what
-- the Decide() pure function expects.
--
-- All columns are NULL by default — pre-existing episodes stay untouched,
-- and the rework engine short-circuits at the top when REWORK_ENGINE_LEVEL
-- (default "none") is configured to skip its level. So this migration is
-- forward AND backward compatible: an old worker writing to a new schema
-- simply leaves all three columns NULL.
--
-- rework_attempts is the JSONB history of every rework decision recorded
-- on this episode (one ReworkAttempt per call, see internal/rework/types.go).
-- It is read every time the engine evaluates a new verdict so it can detect
-- oscillation and enforce per-level retry caps.
--
-- rework_status is the escalation / halt flag. Non-empty values block
-- further auto-rework dispatch until an operator clears the column. The
-- partial index keeps the index small even when many episodes never get
-- escalated.
--
-- accumulated_cost_usd is the running LLM cost ledger compared against
-- EPISODE_REWORK_COST_CEILING_USD before each dispatch.
--
-- Applied automatically by GORM AutoMigrate when AUTO_MIGRATE_ON_START=true.
-- Run manually only if auto-migrate is disabled.

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS rework_attempts      JSONB NULL;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS rework_status        TEXT NULL;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS accumulated_cost_usd NUMERIC NULL;

-- Partial index: only episodes that have been escalated. Operators
-- routinely query "show me everything halted / awaiting human review";
-- we want that to be O(escalated_episode_count), not O(total_episode_count).
CREATE INDEX IF NOT EXISTS idx_episodes_rework_status
    ON episodes(rework_status)
    WHERE rework_status IS NOT NULL;
