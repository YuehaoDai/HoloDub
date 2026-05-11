-- Migration: OPT-409 Chapter-level Judge (observe-only).
--
-- Adds two nullable columns to jobs so a chapter judge LLM can score every
-- merged chapter along multiple cross-segment axes (narrative coherence,
-- terminology consistency, register stability, character voice, fidelity,
-- fluency) plus a top-3-weakest-segments list — dimensions that segment-
-- level OPT-002 judge cannot see (single segment in isolation) and that
-- episode-level OPT-406 judge sees too late (after final mux).
--
-- Both columns are NULL by default — pre-existing chapter Jobs stay
-- untouched and the judge dispatch site short-circuits when
-- CHAPTER_JUDGE_MODEL is empty (env-disable path), so this migration is
-- forward- AND backward-compatible: an old worker writing to a new schema
-- simply leaves both columns NULL.
--
-- chapter_judge_score is the scalar overall score (0..1, currently
-- == ChapterJudgeResult.OverallFidelityChapter) suitable for an at-a-
-- glance heat map in the EpisodeDetail UI; chapter_judge_meta carries the
-- full structured verdict (per-axis sub-scores + weakest segments + verdict
-- enum + observed glossary).
--
-- Applied automatically by GORM AutoMigrate when AUTO_MIGRATE_ON_START=true.
-- Run manually only if auto-migrate is disabled.

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_judge_score NUMERIC NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chapter_judge_meta JSONB NULL;

-- Partial index: only chapters that have been judged. Keeps the index
-- small even when chapter judging is enabled later for fresh episodes
-- only (the 100+ historical 1-chapter Jobs back-filled by OPT-401 will
-- never get a chapter_judge_score because runMerge ran before OPT-409).
CREATE INDEX IF NOT EXISTS idx_jobs_chapter_judge_score
    ON jobs(chapter_judge_score)
    WHERE chapter_judge_score IS NOT NULL;
