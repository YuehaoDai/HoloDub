-- Migration: segment review stage
-- Applied automatically by GORM AutoMigrate when AUTO_MIGRATE_ON_START=true.
-- Run manually only if auto-migrate is disabled.

-- New table for LLM-generated merge/split suggestions produced during segment_review stage.
CREATE TABLE IF NOT EXISTS segment_suggestions (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL DEFAULT 0,
    action          VARCHAR(32) NOT NULL,          -- "merge" | "split"
    segment_ids     JSONB NOT NULL DEFAULT '[]',   -- array of segment IDs
    split_char_index INTEGER NOT NULL DEFAULT 0,   -- only for split actions
    reason          TEXT NOT NULL DEFAULT '',
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 0,
    status          VARCHAR(32) NOT NULL DEFAULT 'pending', -- "pending" | "accepted" | "rejected"
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_segment_suggestions_job_id ON segment_suggestions(job_id);

-- Note: job_status is VARCHAR(32), so the new "awaiting_review" value needs no DDL change.
