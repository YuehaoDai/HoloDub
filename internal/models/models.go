package models

import (
	"time"

	"gorm.io/datatypes"
)

type JobStage string

const (
	StageMedia       JobStage = "media"
	StageSeparate    JobStage = "separate"
	StageASRSmart    JobStage = "asr_smart"
	StageTranslate   JobStage = "translate"
	StageTTSDuration JobStage = "tts_duration"
	StageMerge       JobStage = "merge"
)

var StageOrder = []JobStage{
	StageMedia,
	StageSeparate,
	StageASRSmart,
	StageTranslate,
	StageTTSDuration,
	StageMerge,
}

type JobStatus string

const (
	JobStatusPending         JobStatus = "pending"
	JobStatusQueued          JobStatus = "queued"
	JobStatusRunning         JobStatus = "running"
	JobStatusFailed          JobStatus = "failed"
	JobStatusCompleted       JobStatus = "completed"
	JobStatusTimedOut        JobStatus = "timed_out"
	JobStatusCancelRequested JobStatus = "cancel_requested"
	JobStatusCancelled       JobStatus = "cancelled"
)

type Job struct {
	ID                uint              `json:"id" gorm:"primaryKey"`
	TenantKey         string            `json:"tenant_key" gorm:"size:128;index"`
	ExternalID        string            `json:"external_id" gorm:"size:128;index"`
	Name              string            `json:"name"`
	Status            JobStatus         `json:"status" gorm:"size:32;index"`
	CurrentStage      JobStage          `json:"current_stage" gorm:"size:32;index"`
	SourceLanguage    string            `json:"source_language" gorm:"size:16"`
	TargetLanguage    string            `json:"target_language" gorm:"size:16"`
	InputRelPath      string            `json:"input_relpath"`
	VocalsRelPath     string            `json:"vocals_relpath"`
	BgmRelPath        string            `json:"bgm_relpath"`
	OutputRelPath     string            `json:"output_relpath"`
	Config            datatypes.JSONMap `json:"config" gorm:"type:jsonb"`
	ErrorMessage      string            `json:"error_message" gorm:"type:text"`
	// TranslationSummary is a compact LLM-generated reference card produced after the
	// initial batch translation. It captures topic, characters, key terminology and
	// register so that later TTS retranslations maintain global coherence.
	TranslationSummary string            `json:"translation_summary" gorm:"type:text"`
	RetryCount        int               `json:"retry_count"`
	MaxRetries        int               `json:"max_retries"`
	WebhookURL        string            `json:"webhook_url"`
	WebhookSecret     string            `json:"-" gorm:"type:text"`
	HeartbeatAt       *time.Time        `json:"heartbeat_at"`
	StartedAt         *time.Time        `json:"started_at"`
	CompletedAt       *time.Time        `json:"completed_at"`
	DeadlineAt        *time.Time        `json:"deadline_at"`
	CancelRequestedAt *time.Time        `json:"cancel_requested_at"`
	CancelledAt       *time.Time        `json:"cancelled_at"`
	CreatedAt         time.Time         `json:"created_at"`
	UpdatedAt         time.Time         `json:"updated_at"`
	Segments          []Segment         `json:"segments,omitempty"`
	Speakers          []Speaker         `json:"speakers,omitempty"`
	StageRuns         []JobStageRun     `json:"stage_runs,omitempty"`
}

type VoiceProfile struct {
	ID                uint              `json:"id" gorm:"primaryKey"`
	TenantKey         string            `json:"tenant_key" gorm:"size:128;index"`
	Name              string            `json:"name" gorm:"index"`
	Mode              string            `json:"mode" gorm:"size:32"`
	Provider          string            `json:"provider" gorm:"size:64"`
	Language          string            `json:"language" gorm:"size:16"`
	SampleRelPaths    datatypes.JSON    `json:"sample_relpaths" gorm:"type:jsonb"`
	CheckpointRelPath string            `json:"checkpoint_relpath"`
	IndexRelPath      string            `json:"index_relpath"`
	ConfigRelPath     string            `json:"config_relpath"`
	InternalSpeakerID string            `json:"internal_speaker_id"`
	ValidationStatus  string            `json:"validation_status" gorm:"size:32"`
	ValidationError   string            `json:"validation_error" gorm:"type:text"`
	ValidatedAt       *time.Time        `json:"validated_at"`
	Meta              datatypes.JSONMap `json:"meta" gorm:"type:jsonb"`
	// EstCharsPerSec is an empirically calibrated speaking rate for the target language.
	// Populated automatically from TTS synthesis results via exponential moving average.
	// nil means no data yet; use language-based default instead.
	EstCharsPerSec    *float64          `json:"est_chars_per_sec,omitempty"`
	CreatedAt         time.Time         `json:"created_at"`
	UpdatedAt         time.Time         `json:"updated_at"`
}

type Speaker struct {
	ID        uint              `json:"id" gorm:"primaryKey"`
	JobID     uint              `json:"job_id" gorm:"uniqueIndex:idx_speaker_job_label"`
	Label     string            `json:"label" gorm:"size:64;uniqueIndex:idx_speaker_job_label"`
	Name      string            `json:"name"`
	Meta      datatypes.JSONMap `json:"meta" gorm:"type:jsonb"`
	CreatedAt time.Time         `json:"created_at"`
	UpdatedAt time.Time         `json:"updated_at"`
}

type SpeakerVoiceBinding struct {
	ID             uint         `json:"id" gorm:"primaryKey"`
	JobID          uint         `json:"job_id" gorm:"uniqueIndex:idx_binding_job_speaker"`
	SpeakerID      uint         `json:"speaker_id" gorm:"uniqueIndex:idx_binding_job_speaker"`
	VoiceProfileID uint         `json:"voice_profile_id" gorm:"index"`
	CreatedAt      time.Time    `json:"created_at"`
	UpdatedAt      time.Time    `json:"updated_at"`
	Speaker        Speaker      `json:"speaker,omitempty"`
	VoiceProfile   VoiceProfile `json:"voice_profile,omitempty"`
}

type Segment struct {
	ID                 uint              `json:"id" gorm:"primaryKey"`
	JobID              uint              `json:"job_id" gorm:"index"`
	SpeakerID          *uint             `json:"speaker_id" gorm:"index"`
	SpeakerLabel       string            `json:"speaker_label" gorm:"size:64;index"`
	// VoiceProfileID is an optional per-segment voice override.
	// When set, it takes priority over the speaker-level SpeakerVoiceBinding.
	VoiceProfileID     *uint             `json:"voice_profile_id,omitempty" gorm:"index"`
	Ordinal            int               `json:"ordinal" gorm:"index"`
	StartMs            int64             `json:"start_ms"`
	EndMs              int64             `json:"end_ms"`
	OriginalDurationMs int64             `json:"original_duration_ms"`
	SourceText         string            `json:"src_text" gorm:"type:text"`
	TargetText         string            `json:"tgt_text" gorm:"type:text"`
	SplitReason        string            `json:"split_reason" gorm:"size:64"`
	TTSAudioRelPath    string            `json:"tts_audio_path"`
	TTSDurationMs      int64             `json:"tts_duration_ms"`
	Status             string            `json:"status" gorm:"size:32"`
	Meta               datatypes.JSONMap `json:"meta" gorm:"type:jsonb"`
	CreatedAt          time.Time         `json:"created_at"`
	UpdatedAt          time.Time         `json:"updated_at"`
}

type JobStageRun struct {
	ID           uint              `json:"id" gorm:"primaryKey"`
	JobID        uint              `json:"job_id" gorm:"index"`
	Stage        JobStage          `json:"stage" gorm:"size:32;index"`
	Attempt      int               `json:"attempt"`
	Status       string            `json:"status" gorm:"size:32;index"`
	RequestedBy  string            `json:"requested_by"`
	Reason       string            `json:"reason"`
	WorkerID     string            `json:"worker_id" gorm:"size:128"`
	SegmentIDs   datatypes.JSON    `json:"segment_ids" gorm:"type:jsonb"`
	ErrorMessage string            `json:"error_message" gorm:"type:text"`
	DurationMs   int64             `json:"duration_ms"`
	Meta         datatypes.JSONMap `json:"meta" gorm:"type:jsonb"`
	StartedAt    time.Time         `json:"started_at" gorm:"index"`
	FinishedAt   *time.Time        `json:"finished_at"`
	CreatedAt    time.Time         `json:"created_at"`
	UpdatedAt    time.Time         `json:"updated_at"`
}

type TenantQuota struct {
	ID                uint              `json:"id" gorm:"primaryKey"`
	TenantKey         string            `json:"tenant_key" gorm:"uniqueIndex"`
	MaxConcurrentJobs int               `json:"max_concurrent_jobs"`
	MaxJobsPerDay     int               `json:"max_jobs_per_day"`
	MaxStorageGB      int               `json:"max_storage_gb"`
	MaxGPUConcurrency int               `json:"max_gpu_concurrency"`
	Enabled           bool              `json:"enabled"`
	Meta              datatypes.JSONMap `json:"meta" gorm:"type:jsonb"`
	CreatedAt         time.Time         `json:"created_at"`
	UpdatedAt         time.Time         `json:"updated_at"`
}

type TaskPayload struct {
	JobID           uint     `json:"job_id"`
	Stage           JobStage `json:"stage"`
	Attempt         int      `json:"attempt"`
	SegmentIDs      []uint   `json:"segment_ids,omitempty"`
	RequestedBy     string   `json:"requested_by,omitempty"`
	Reason          string   `json:"reason,omitempty"`
	// SkipAutoAdvance prevents HandleTask from automatically enqueueing the next
	// pipeline stage when this task succeeds.  Set to true for manual retries so
	// the user controls when to proceed to merge.
	SkipAutoAdvance bool `json:"skip_auto_advance,omitempty"`
}

type SegmentDraft struct {
	StartMs      int64
	EndMs        int64
	Text         string
	SpeakerLabel string
	SplitReason  string
}

func (stage JobStage) Next() (JobStage, bool) {
	for idx, current := range StageOrder {
		if current == stage && idx+1 < len(StageOrder) {
			return StageOrder[idx+1], true
		}
	}
	return "", false
}

func (segment Segment) DurationMs() int64 {
	if segment.OriginalDurationMs > 0 {
		return segment.OriginalDurationMs
	}
	return segment.EndMs - segment.StartMs
}

func (status JobStatus) IsTerminal() bool {
	switch status {
	case JobStatusCompleted, JobStatusFailed, JobStatusTimedOut, JobStatusCancelled:
		return true
	default:
		return false
	}
}
