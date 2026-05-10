-- Migration: OPT-401 Episode / Chapter data model
--
-- Introduces the long-form three-tier hierarchy (Episode → Chapter → Segment)
-- by adding the `episodes` table and four new columns to `jobs`. Every
-- historical Job is back-filled to a 1-chapter Episode whose id equals the
-- Job id so external references and existing routes keep working unchanged.
--
-- Applied automatically by GORM AutoMigrate + Store.RunBackfillIfNeeded
-- when AUTO_MIGRATE_ON_START=true. Run manually only if auto-migrate is
-- disabled. Both sides are idempotent (IF NOT EXISTS / WHERE NULL guards).

-- ── 1. episodes table ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS episodes (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_key            VARCHAR(128),
    name                  TEXT,
    source_video_rel_path TEXT,
    source_language       VARCHAR(16),
    target_language       VARCHAR(16),
    duration_ms           BIGINT,
    total_chapters        INTEGER NOT NULL DEFAULT 1,
    glossary              JSONB,            -- written by OPT-402 glossary_extract
    reference_card        TEXT,             -- written by OPT-402 glossary_extract
    episode_judge_score   NUMERIC,          -- written by OPT-406 episode judge
    episode_judge_meta    JSONB,            -- written by OPT-406 episode judge
    status                VARCHAR(32),
    output_relpath        TEXT,             -- written by OPT-404 episode merge
    error_message         TEXT,
    completed_at          TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_episodes_tenant_key ON episodes(tenant_key);
CREATE INDEX IF NOT EXISTS idx_episodes_status     ON episodes(status);

-- ── 2. jobs new columns (chapter pointer) ─────────────────────────────────────
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS episode_id        BIGINT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_ordinal   INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_start_ms  BIGINT NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_end_ms    BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_jobs_episode_chapter
    ON jobs(episode_id, chapter_ordinal);

-- ── 3. back-fill 1:1 episode for every historical job ────────────────────────
-- The Job.id is reused as the Episode.id so that any external reference like
-- /jobs/123 → /episodes/123 stays trivially mappable. Status is collapsed
-- via mapLegacyJobStatusToEpisode (see internal/store/store.go).
INSERT INTO episodes (
    id, tenant_key, name, source_video_rel_path,
    source_language, target_language,
    total_chapters, status, output_relpath,
    created_at, updated_at, completed_at
)
SELECT id,
       COALESCE(tenant_key, 'default'),
       name,
       input_rel_path,
       source_language,
       target_language,
       1,
       CASE
         WHEN status IN ('completed', 'cancelled') THEN 'completed'
         WHEN status IN ('failed',    'timed_out') THEN 'failed'
         ELSE 'running'
       END,
       output_rel_path,
       created_at,
       updated_at,
       completed_at
FROM jobs
WHERE id NOT IN (SELECT id FROM episodes);

-- Advance the episodes id sequence past the highest back-filled id so
-- subsequently auto-allocated episodes do not collide.
SELECT setval(
    'episodes_id_seq',
    GREATEST((SELECT COALESCE(MAX(id), 0) FROM episodes), 1)
);

UPDATE jobs SET episode_id = id      WHERE episode_id IS NULL OR episode_id = 0;
UPDATE jobs SET chapter_ordinal = 1  WHERE chapter_ordinal IS NULL OR chapter_ordinal = 0;

-- ── 4. enforce non-null on episode_id once back-fill is done ─────────────────
-- (Repeating ALTER TABLE ... SET NOT NULL is a no-op when the column is
-- already NOT NULL, so the migration stays idempotent.)
ALTER TABLE jobs ALTER COLUMN episode_id SET NOT NULL;
