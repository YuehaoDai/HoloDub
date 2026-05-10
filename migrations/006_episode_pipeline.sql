-- Migration: OPT-402 Episode-level pipeline columns
--
-- Adds the four columns the OPT-402 episode-level pipeline needs to write
-- without (a) breaking 1-chapter back-compat or (b) widening the chapter
-- (jobs) row. The corresponding Go fields live in internal/models/models.go
-- under Episode.{VocalsRelPath, BgmRelPath, ASRDoneAt, GlossaryDoneAt}.
--
-- Applied automatically by GORM AutoMigrate at API startup. Run this file
-- by hand only if AUTO_MIGRATE_ON_START=false. Every statement is
-- idempotent (IF NOT EXISTS guard) so re-running on a partially migrated
-- DB is safe.
--
-- Forward-compat: this migration deliberately ships ONLY the columns that
-- OPT-402 actually populates. OPT-403 (chapterize), OPT-404 (episode_merge)
-- and OPT-406 (episode_judge) will introduce their own ALTER statements in
-- migrations/007+ to keep blast radius small per OPT.

-- ── 1. episode-level "separate" output (vocals + BGM on full video) ──────────
-- Written by stage_episode_separate.go after the OPT-402 ep_separate stage
-- runs ONCE on the whole episode (vs the legacy per-chapter rerun that
-- wasted GPU). 1-chapter shortcut also writes the matching jobs.* fields
-- so historical readers continue to work.
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS vocals_rel_path  VARCHAR(512);
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS bgm_rel_path     VARCHAR(512);

-- ── 2. episode-level progress timestamps ─────────────────────────────────────
-- Pure UI breadcrumbs: the EpisodeDetail "episode-level stages" tracker
-- needs a clickable timestamp per stage. Logic NEVER reads these — they
-- are descriptive only, the source of truth is Episode.Status.
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS asr_done_at      TIMESTAMPTZ;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS glossary_done_at TIMESTAMPTZ;
