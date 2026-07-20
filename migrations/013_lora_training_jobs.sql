-- LoRA adapter training workbench.
--
-- Runtime schema is still created by GORM AutoMigrate from
-- internal/models.LoraTrainingJob. This file documents the intended
-- PostgreSQL shape for future versioned migrations.

CREATE TABLE IF NOT EXISTS lora_training_jobs (
    id BIGSERIAL PRIMARY KEY,
    tenant_key VARCHAR(128),
    name VARCHAR(256),
    status VARCHAR(32),
    run_id VARCHAR(256),
    source_tier VARCHAR(32),
    train_features_rel_path VARCHAR(512),
    val_features_rel_path VARCHAR(512),
    canary_features_rel_path VARCHAR(512),
    output_root_rel_path VARCHAR(512),
    output_dir_rel_path VARCHAR(512),
    adapter_rel_path VARCHAR(512),
    voice_prompt_rel_path VARCHAR(512),
    rank BIGINT,
    alpha DOUBLE PRECISION,
    dropout DOUBLE PRECISION,
    learning_rate DOUBLE PRECISION,
    max_steps BIGINT,
    save_every BIGINT,
    eval_interval BIGINT,
    text_loss_weight DOUBLE PRECISION,
    mel_loss_weight DOUBLE PRECISION,
    duration_control BOOLEAN,
    duration_source VARCHAR(16),
    duration_dropout DOUBLE PRECISION,
    mel_stop_loss_weight DOUBLE PRECISION,
    mel_end_loss_weight DOUBLE PRECISION,
    mel_end_window BIGINT,
    duration_stop_bce_weight DOUBLE PRECISION,
    duration_position_loss_weight DOUBLE PRECISION,
    duration_position_loss_window BIGINT,
    gradient_checkpointing BOOLEAN,
    precision VARCHAR(16),
    execution_mode VARCHAR(32),
    commands JSONB,
    gate_config JSONB,
    artifacts JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_lora_training_jobs_tenant_key ON lora_training_jobs (tenant_key);
CREATE INDEX IF NOT EXISTS idx_lora_training_jobs_status ON lora_training_jobs (status);
CREATE INDEX IF NOT EXISTS idx_lora_training_jobs_run_id ON lora_training_jobs (run_id);
CREATE INDEX IF NOT EXISTS idx_lora_training_jobs_source_tier ON lora_training_jobs (source_tier);
CREATE INDEX IF NOT EXISTS idx_lora_training_jobs_output_dir_rel_path ON lora_training_jobs (output_dir_rel_path);
CREATE INDEX IF NOT EXISTS idx_lora_training_jobs_adapter_rel_path ON lora_training_jobs (adapter_rel_path);
CREATE INDEX IF NOT EXISTS idx_lora_training_jobs_execution_mode ON lora_training_jobs (execution_mode);
