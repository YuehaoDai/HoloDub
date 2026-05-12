-- Down migration for 011_segments_split.sql.
--
-- Reverts the parent_segment_id column. Note: this destroys split
-- provenance — operators should snapshot the `segments` table before
-- running this if any segment has been split since OPT-407-followup-1
-- shipped. Required by .cursor/rules/incremental-evolution.mdc §5
-- (every up.sql MUST have a down.sql).

DROP INDEX IF EXISTS idx_segments_parent_segment_id;
ALTER TABLE segments DROP COLUMN IF EXISTS parent_segment_id;
