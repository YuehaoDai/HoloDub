-- Migration: OPT-407-followup-1 segment split parentage.
--
-- Adds a nullable parent_segment_id column to the segments table so the
-- OPT-407 closed-loop rework engine (and the OPT-201 SegmentAgent's
-- split_segment decision) can record provenance when one source segment
-- is split into two or more child segments.
--
-- Why nullable: pre-existing segments stay untouched; new top-level
-- segments (the common case) also leave it NULL. Only segments produced
-- by a split operation get the parent ID populated.
--
-- Why a real FK: catches dangling references at write time (the worker
-- can't accidentally write parent_segment_id=999999 if no such row
-- exists). ON DELETE SET NULL means deleting the parent segment doesn't
-- cascade-delete the children — operators may need to audit historical
-- splits even after the original segment is purged.
--
-- The split operation itself stores additional metadata on the parent
-- row's Meta JSONB (key `split_into`) so the agent can find children
-- when re-evaluating the parent. The column here is for SQL-side
-- reverse lookups ("give me every child of parent X").
--
-- Applied automatically by GORM AutoMigrate when AUTO_MIGRATE_ON_START=true.

ALTER TABLE segments
    ADD COLUMN IF NOT EXISTS parent_segment_id BIGINT NULL
        REFERENCES segments(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_segments_parent_segment_id
    ON segments(parent_segment_id)
    WHERE parent_segment_id IS NOT NULL;
