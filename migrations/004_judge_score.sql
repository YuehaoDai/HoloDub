-- Migration: OPT-002 LLM-as-Judge MVP (observe-only)
--
-- Adds two nullable columns to segments so a judge LLM can score every
-- synthesised segment along multiple axes (fidelity / fluency / coherence)
-- without changing the existing pipeline contract.
--
-- Both columns are NULL by default — pre-existing segments stay untouched
-- and the judge call site short-circuits when JUDGE_MODEL is empty
-- (which is the default), so this migration is forward- AND backward-
-- compatible: an old worker writing to a new schema simply leaves both
-- columns NULL.
--
-- Applied automatically by GORM AutoMigrate when AUTO_MIGRATE_ON_START=true.
-- Run manually only if auto-migrate is disabled.

ALTER TABLE segments ADD COLUMN IF NOT EXISTS judge_score NUMERIC NULL;
ALTER TABLE segments ADD COLUMN IF NOT EXISTS judge_meta JSONB NULL;

-- Partial index: only segments that have been judged. Keeps the index
-- small even when judging is enabled later for fresh jobs only.
CREATE INDEX IF NOT EXISTS idx_segments_judge_score
    ON segments(judge_score)
    WHERE judge_score IS NOT NULL;
