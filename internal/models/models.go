package models

import (
	"fmt"
	"time"

	"gorm.io/datatypes"
)

type JobStage string

const (
	StageMedia          JobStage = "media"
	StageSeparate       JobStage = "separate"
	StageASRSmart       JobStage = "asr_smart"
	StageSegmentReview  JobStage = "segment_review"
	StageTranslate      JobStage = "translate"
	StageTTSDuration    JobStage = "tts_duration"
	StageMerge          JobStage = "merge"
)

var StageOrder = []JobStage{
	StageMedia,
	StageSeparate,
	StageASRSmart,
	StageSegmentReview,
	StageTranslate,
	StageTTSDuration,
	StageMerge,
}

type JobStatus string

const (
	JobStatusPending         JobStatus = "pending"
	JobStatusQueued          JobStatus = "queued"
	JobStatusRunning         JobStatus = "running"
	JobStatusAwaitingReview  JobStatus = "awaiting_review"
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
	TranslationSummary string `json:"translation_summary" gorm:"type:text"`
	// EpisodeID groups one or more chapter Jobs into a single Episode. OPT-401 introduced
	// the three-tier model (Episode -> Chapter -> Segment); for backward compatibility every
	// historical Job is back-filled to a 1-chapter Episode with EpisodeID = Job.ID.
	// New rows are auto-created via Store.CreateJob's transaction when EpisodeID == 0.
	EpisodeID uint `json:"episode_id" gorm:"index:idx_jobs_episode_chapter,priority:1"`
	// ChapterOrdinal is 1-indexed within an Episode. 1-chapter Episodes always have ordinal=1.
	ChapterOrdinal int `json:"chapter_ordinal" gorm:"index:idx_jobs_episode_chapter,priority:2;default:1"`
	// ChapterStartMs / ChapterEndMs mark the chapter's time window inside the source video,
	// in milliseconds. Both default to 0 (entire video) until OPT-403 chapterize fills them in.
	ChapterStartMs    int64         `json:"chapter_start_ms"`
	ChapterEndMs      int64         `json:"chapter_end_ms"`
	RetryCount        int           `json:"retry_count"`
	MaxRetries        int           `json:"max_retries"`
	WebhookURL        string        `json:"webhook_url"`
	WebhookSecret     string        `json:"-" gorm:"type:text"`
	HeartbeatAt       *time.Time    `json:"heartbeat_at"`
	StartedAt         *time.Time    `json:"started_at"`
	CompletedAt       *time.Time    `json:"completed_at"`
	DeadlineAt        *time.Time    `json:"deadline_at"`
	CancelRequestedAt *time.Time    `json:"cancel_requested_at"`
	CancelledAt       *time.Time    `json:"cancelled_at"`
	CreatedAt         time.Time     `json:"created_at"`
	UpdatedAt         time.Time     `json:"updated_at"`
	Segments          []Segment     `json:"segments,omitempty"`
	Speakers          []Speaker     `json:"speakers,omitempty"`
	StageRuns         []JobStageRun `json:"stage_runs,omitempty"`
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

// SegmentStatus is the typed lifecycle state of a Segment row.
//
// State machine (see Transition for the full rule set):
//
//	"" / pending  --(translate stage writes target_text)-->  translated
//	translated    --(tts stage writes audio path)----------> synthesized
//	synthesized   --(retry / edit clears tts result)--------> pending
//	translated    --(retry of asr stage)------------------->  pending
//
// Any other transition is a programming error and is logged via
// SegmentStatus.Transition.
type SegmentStatus string

const (
	SegmentStatusPending     SegmentStatus = "pending"
	SegmentStatusTranslated  SegmentStatus = "translated"
	SegmentStatusSynthesized SegmentStatus = "synthesized"
)

// Transition validates a segment status change. It returns the next state
// when the transition is allowed, or an error describing the invalid
// transition. The function is a pure look-up; callers typically still
// persist the new status themselves once Transition succeeds.
func (s SegmentStatus) Transition(to SegmentStatus) (SegmentStatus, error) {
	allowed := map[SegmentStatus]map[SegmentStatus]bool{
		"":                       {SegmentStatusPending: true, SegmentStatusTranslated: true},
		SegmentStatusPending:     {SegmentStatusPending: true, SegmentStatusTranslated: true},
		SegmentStatusTranslated:  {SegmentStatusPending: true, SegmentStatusTranslated: true, SegmentStatusSynthesized: true},
		SegmentStatusSynthesized: {SegmentStatusPending: true, SegmentStatusSynthesized: true, SegmentStatusTranslated: true},
	}
	next, ok := allowed[s]
	if !ok || !next[to] {
		return s, fmt.Errorf("invalid segment status transition: %q -> %q", s, to)
	}
	return to, nil
}

// IsTerminal reports whether no further automatic stage will modify the
// segment. Used by reset/retry helpers.
func (s SegmentStatus) IsTerminal() bool {
	return s == SegmentStatusSynthesized
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
	Status             SegmentStatus     `json:"status" gorm:"size:32;index:idx_segment_status"`
	Meta               datatypes.JSONMap `json:"meta" gorm:"type:jsonb"`
	// JudgeScore + JudgeMeta are populated asynchronously by the OPT-002
	// LLM-as-Judge MVP. Both nil when judging is disabled (JUDGE_MODEL="")
	// or when the judge call has not yet run for this segment.
	// JudgeScore is the overall scalar (currently equal to Fidelity 0..1);
	// JudgeMeta carries the full structured verdict (issues, sub-scores).
	JudgeScore         *float64          `json:"judge_score,omitempty" gorm:"type:numeric"`
	JudgeMeta          datatypes.JSON    `json:"judge_meta,omitempty" gorm:"type:jsonb"`
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

// SegmentSuggestion represents a single merge or split recommendation produced
// by the LLM segmentation-review agent during the segment_review stage.
// Actions are applied by the user (accept / reject) through the UI before
// the pipeline advances to translate.
type SegmentSuggestion struct {
	ID             uint                     `json:"id" gorm:"primaryKey"`
	JobID          uint                     `json:"job_id" gorm:"index"`
	Ordinal        int                      `json:"ordinal"`
	Action         string                   `json:"action" gorm:"size:32"` // "merge" | "split"
	SegmentIDs     datatypes.JSONSlice[uint] `json:"segment_ids" gorm:"type:jsonb"`
	SplitCharIndex int                      `json:"split_char_index"`
	Reason         string                   `json:"reason" gorm:"type:text"`
	Confidence     float64                  `json:"confidence"`
	Status         string                   `json:"status" gorm:"size:32"` // "pending" | "accepted" | "rejected"
	CreatedAt      time.Time                `json:"created_at"`
	UpdatedAt      time.Time                `json:"updated_at"`
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

// IsActive returns true for statuses where the job is alive and may still make progress
// (either running automatically or waiting for user input).
func (status JobStatus) IsActive() bool {
	switch status {
	case JobStatusQueued, JobStatusRunning, JobStatusAwaitingReview:
		return true
	default:
		return false
	}
}

// EpisodeStatus is the typed lifecycle state of an Episode row (the "long-form
// container" introduced by OPT-401: one Episode owns 1..N chapter Jobs).
//
// State machine (see Transition for the full rule set):
//
//	"" / pending --(POST /jobs, 1-chapter shortcut)----------> running
//	pending      --(OPT-403 chapterize starts)---------------> chaptering
//	chaptering   --(OPT-403 fan-out enqueued)----------------> dispatched
//	dispatched   --(first chapter accepted by worker)--------> running
//	running      --(OPT-404 episode_merge stage)-------------> merging
//	merging      --(OPT-406 episode judge stage)-------------> judging
//	judging      --(OPT-407 verdict=rework)------------------> reworking
//	reworking    --(OPT-407 issued chapter retries)----------> running
//	judging      --(OPT-406 verdict=accept)------------------> completed
//	running      --(1-chapter shortcut, OPT-401)-------------> completed
//	any-active   --(unrecoverable error)--------------------> failed
//
// OPT-401 only triggers pending/running/completed/failed; the seven judging-
// related states are pre-declared here so OPT-402..408 can land incrementally
// without further model migrations.
type EpisodeStatus string

const (
	EpisodeStatusPending    EpisodeStatus = "pending"
	EpisodeStatusChaptering EpisodeStatus = "chaptering"
	EpisodeStatusDispatched EpisodeStatus = "dispatched"
	EpisodeStatusRunning    EpisodeStatus = "running"
	EpisodeStatusMerging    EpisodeStatus = "merging"
	EpisodeStatusJudging    EpisodeStatus = "judging"
	EpisodeStatusReworking  EpisodeStatus = "reworking"
	EpisodeStatusCompleted  EpisodeStatus = "completed"
	EpisodeStatusFailed     EpisodeStatus = "failed"
)

// Transition validates an episode status change. It returns the next state
// when the transition is allowed, or an error describing the invalid
// transition. Mirrors the SegmentStatus/JobStatus pattern for consistency.
func (s EpisodeStatus) Transition(to EpisodeStatus) (EpisodeStatus, error) {
	allowed := map[EpisodeStatus]map[EpisodeStatus]bool{
		"": {
			EpisodeStatusPending: true,
		},
		EpisodeStatusPending: {
			EpisodeStatusChaptering: true,
			EpisodeStatusRunning:    true,
			EpisodeStatusFailed:     true,
		},
		EpisodeStatusChaptering: {
			EpisodeStatusDispatched: true,
			EpisodeStatusFailed:     true,
		},
		EpisodeStatusDispatched: {
			EpisodeStatusRunning: true,
			EpisodeStatusFailed:  true,
		},
		EpisodeStatusRunning: {
			EpisodeStatusMerging:   true,
			EpisodeStatusCompleted: true,
			EpisodeStatusFailed:    true,
		},
		EpisodeStatusMerging: {
			EpisodeStatusJudging: true,
			EpisodeStatusFailed:  true,
		},
		EpisodeStatusJudging: {
			EpisodeStatusReworking: true,
			EpisodeStatusCompleted: true,
			EpisodeStatusFailed:    true,
		},
		EpisodeStatusReworking: {
			EpisodeStatusRunning: true,
			EpisodeStatusFailed:  true,
		},
	}
	next, ok := allowed[s]
	if !ok || !next[to] {
		return s, fmt.Errorf("invalid episode status transition: %q -> %q", s, to)
	}
	return to, nil
}

// IsTerminal reports whether the episode has reached a final resting state and
// no further pipeline work will be performed on it.
func (s EpisodeStatus) IsTerminal() bool {
	return s == EpisodeStatusCompleted || s == EpisodeStatusFailed
}

// IsActive reports whether the episode is alive and may still make progress
// (either running automatically or waiting for user/judge input).
func (s EpisodeStatus) IsActive() bool {
	switch s {
	case EpisodeStatusPending,
		EpisodeStatusChaptering,
		EpisodeStatusDispatched,
		EpisodeStatusRunning,
		EpisodeStatusMerging,
		EpisodeStatusJudging,
		EpisodeStatusReworking:
		return true
	default:
		return false
	}
}

// Episode is the "long-form container" introduced by OPT-401. It groups one
// or more Jobs (each Job is a chapter) so that long videos can be split,
// translated and re-merged with episode-level coherence guarantees.
//
// Backwards compatibility: every historical Job is back-filled to a single
// 1-chapter Episode whose ID equals the Job ID (see migrations/005_episodes.sql
// and Store.RunBackfillIfNeeded). The handful of episode-level columns that
// later OPTs populate (glossary, reference card, episode-judge meta, output
// path) are pre-declared here so subsequent migrations stay additive.
type Episode struct {
	ID                 uint              `json:"id" gorm:"primaryKey"`
	TenantKey          string            `json:"tenant_key" gorm:"size:128;index"`
	Name               string            `json:"name"`
	SourceVideoRelPath string            `json:"source_video_relpath"`
	SourceLanguage     string            `json:"source_language" gorm:"size:16"`
	TargetLanguage     string            `json:"target_language" gorm:"size:16"`
	DurationMs         int64             `json:"duration_ms"`
	TotalChapters      int               `json:"total_chapters" gorm:"default:1"`
	// Glossary is the canonical episode-level term sheet produced by OPT-402's
	// glossary_extract stage. Empty until that stage runs.
	Glossary datatypes.JSON `json:"glossary,omitempty" gorm:"type:jsonb"`
	// ReferenceCard is the episode-level prose summary (topic, characters,
	// register) injected into per-chapter translate/TTS prompts for cross-
	// chapter coherence. Empty until OPT-402 runs.
	ReferenceCard string `json:"reference_card" gorm:"type:text"`
	// EpisodeJudgeScore + EpisodeJudgeMeta are populated by the OPT-406
	// episode-level judge stage. Both nil until that stage runs.
	EpisodeJudgeScore *float64       `json:"episode_judge_score,omitempty" gorm:"type:numeric"`
	EpisodeJudgeMeta  datatypes.JSON `json:"episode_judge_meta,omitempty" gorm:"type:jsonb"`
	Status            EpisodeStatus  `json:"status" gorm:"size:32;index"`
	OutputRelPath     string         `json:"output_relpath"`
	ErrorMessage      string         `json:"error_message" gorm:"type:text"`
	CompletedAt       *time.Time     `json:"completed_at"`
	CreatedAt         time.Time      `json:"created_at"`
	UpdatedAt         time.Time      `json:"updated_at"`
	Chapters          []Job          `json:"chapters,omitempty" gorm:"foreignKey:EpisodeID"`
}
