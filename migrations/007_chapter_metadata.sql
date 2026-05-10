-- Migration: OPT-403 Chapterize fan-out + unified output layout
--
-- Adds the chapter metadata Job columns plus the Episode columns required by
-- the OPT-403 unified output layout. The new layout puts every episode
-- artefact under one directory tree:
--
--   episodes/{ep_id}/chapters/vp{vp}/ch{ord:02d}.mp4   chapter videos
--   episodes/{ep_id}/output/vp{vp}/final.mp4           episode final video
--   episodes/{ep_id}/chapters.json                     bilingual manifest
--   episodes/{ep_id}/separate/{vocals,bgm}.wav         master tracks
--
-- All existing OPT-401 back-filled episodes (~138 of them) keep the legacy
-- jobs/{id}/output/... layout (output_layout_version=1) until the operator
-- runs cmd/migrate-output to flip them to v2 in lock-step with hard-linking
-- the physical files. New episodes created by store.CreateEpisode start at 2.
--
-- Idempotent: every statement uses IF NOT EXISTS so re-running on a partial
-- migration is safe. Applied automatically by GORM AutoMigrate at API
-- startup unless AUTO_MIGRATE_ON_START=false.
--
-- Corresponding Go fields live in internal/models/models.go under Job
-- (ChapterTitle / ChapterTitleTranslated / ChapterSummaryMD) and Episode
-- (OutputLayoutVersion / ChaptersManifestRelPath / LoudnormStats).

-- ── 1. chapter metadata on the chapter Job rows ──────────────────────────────
-- Written by stage_chapterize.go after the LLM Pass 3 review (see
-- internal/llm/chapter_review.go). Empty on every 1-chapter shortcut Job
-- and on every historical Job back-filled by OPT-401.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_title            TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_title_translated TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_summary_md       TEXT NOT NULL DEFAULT '';

-- ── 2. unified output layout columns on episodes ─────────────────────────────
-- output_layout_version
--   1 = legacy jobs/{id}/output/... layout (every historical row defaults here)
--   2 = OPT-403 unified episodes/{ep_id}/... layout (new episodes; back-fill
--       flips historical rows AFTER physical files are in place).
-- chapters_manifest_rel_path holds episodes/{ep_id}/chapters.json once
-- stage_episode_merge writes it (or back-fill seeds it for v1->v2 migration).
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS output_layout_version      SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS chapters_manifest_rel_path TEXT     NOT NULL DEFAULT '';

-- ── 3. EBU R128 loudness measurements per chapter + master pass ──────────────
-- Shape (filled progressively as each chapter finishes its merge stage):
--
--   { "vp0": { "ch01":   {"measured_i": -19.5, "measured_tp": -3.2, ...},
--              "ch02":   {...},
--              "master": {...} },
--     "vp1": { ... } }
--
-- Pipeline logic NEVER reads this column — it is purely descriptive (the UI
-- surfaces it via EpisodeDetail.vue, and OPT-405 chapter-judge will read it
-- for severity weighting once that lands).
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS loudnorm_stats JSONB;
