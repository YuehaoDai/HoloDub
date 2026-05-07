-- Baseline schema for HoloDub (documentation only).
--
-- This file mirrors the tables created by GORM AutoMigrate from
-- internal/models/models.go as of v0.1. It is NOT executed by the runtime;
-- it serves as a reference snapshot for future versioned migration tooling.
--
-- See migrations/README.md for the migration policy.

-- jobs ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_key          VARCHAR(128),
    external_id         VARCHAR(128),
    name                TEXT,
    status              VARCHAR(32),
    current_stage       VARCHAR(32),
    source_language     VARCHAR(16),
    target_language     VARCHAR(16),
    input_relpath       TEXT,
    vocals_relpath      TEXT,
    bgm_relpath         TEXT,
    output_relpath      TEXT,
    config              JSONB,
    error_message       TEXT,
    translation_summary TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 0,
    webhook_url         TEXT,
    webhook_secret      TEXT,
    heartbeat_at        TIMESTAMPTZ,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    deadline_at         TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_key    ON jobs (tenant_key);
CREATE INDEX IF NOT EXISTS idx_jobs_external_id   ON jobs (external_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status        ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_current_stage ON jobs (current_stage);

-- voice_profiles -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_profiles (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_key          VARCHAR(128),
    name                TEXT,
    mode                VARCHAR(32),
    provider            VARCHAR(64),
    language            VARCHAR(16),
    sample_relpaths     JSONB,
    checkpoint_relpath  TEXT,
    index_relpath       TEXT,
    config_relpath      TEXT,
    internal_speaker_id TEXT,
    validation_status   VARCHAR(32),
    validation_error    TEXT,
    validated_at        TIMESTAMPTZ,
    meta                JSONB,
    est_chars_per_sec   DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_profiles_tenant_key ON voice_profiles (tenant_key);
CREATE INDEX IF NOT EXISTS idx_voice_profiles_name       ON voice_profiles (name);

-- speakers -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS speakers (
    id        BIGSERIAL PRIMARY KEY,
    job_id    BIGINT NOT NULL,
    label     VARCHAR(64) NOT NULL,
    name      TEXT,
    meta      JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT idx_speaker_job_label UNIQUE (job_id, label)
);

-- speaker_voice_bindings ---------------------------------------------------
CREATE TABLE IF NOT EXISTS speaker_voice_bindings (
    id               BIGSERIAL PRIMARY KEY,
    job_id           BIGINT NOT NULL,
    speaker_id       BIGINT NOT NULL,
    voice_profile_id BIGINT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL,
    CONSTRAINT idx_binding_job_speaker UNIQUE (job_id, speaker_id)
);
CREATE INDEX IF NOT EXISTS idx_speaker_voice_bindings_voice_profile_id
    ON speaker_voice_bindings (voice_profile_id);

-- segments -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS segments (
    id                   BIGSERIAL PRIMARY KEY,
    job_id               BIGINT NOT NULL,
    speaker_id           BIGINT,
    speaker_label        VARCHAR(64),
    voice_profile_id     BIGINT,
    ordinal              INTEGER,
    start_ms             BIGINT,
    end_ms               BIGINT,
    original_duration_ms BIGINT,
    src_text             TEXT,
    tgt_text             TEXT,
    split_reason         VARCHAR(64),
    tts_audio_path       TEXT,
    tts_duration_ms      BIGINT,
    status               VARCHAR(32),
    meta                 JSONB,
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_job_id           ON segments (job_id);
CREATE INDEX IF NOT EXISTS idx_segments_speaker_id       ON segments (speaker_id);
CREATE INDEX IF NOT EXISTS idx_segments_speaker_label    ON segments (speaker_label);
CREATE INDEX IF NOT EXISTS idx_segments_voice_profile_id ON segments (voice_profile_id);
CREATE INDEX IF NOT EXISTS idx_segments_ordinal          ON segments (ordinal);
CREATE INDEX IF NOT EXISTS idx_segment_status            ON segments (status);

-- job_stage_runs -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_stage_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT NOT NULL,
    stage         VARCHAR(32),
    attempt       INTEGER,
    status        VARCHAR(32),
    requested_by  TEXT,
    reason        TEXT,
    worker_id     VARCHAR(128),
    segment_ids   JSONB,
    error_message TEXT,
    duration_ms   BIGINT,
    meta          JSONB,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_stage_runs_job_id     ON job_stage_runs (job_id);
CREATE INDEX IF NOT EXISTS idx_job_stage_runs_stage      ON job_stage_runs (stage);
CREATE INDEX IF NOT EXISTS idx_job_stage_runs_status     ON job_stage_runs (status);
CREATE INDEX IF NOT EXISTS idx_job_stage_runs_started_at ON job_stage_runs (started_at);

-- tenant_quotas ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_quotas (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_key          VARCHAR(128) UNIQUE,
    max_concurrent_jobs INTEGER,
    max_jobs_per_day    INTEGER,
    max_storage_gb      INTEGER,
    max_gpu_concurrency INTEGER,
    enabled             BOOLEAN,
    meta                JSONB,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);

-- segment_suggestions: see 003_segment_review.sql for the full definition.
