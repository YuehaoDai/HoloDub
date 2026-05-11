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
	// JobStatusAwaitingChapterize is the transitional state OPT-403 puts chapter 1
	// into between "ASR/glossary done" and "fan-out chapter 2..N created". Chapter 1
	// stays here while runFanOutChapters slices media + reassigns segments + creates
	// sibling chapter Jobs; once fan-out commits, every chapter (including ch1) goes
	// back to JobStatusQueued and resumes from StageSegmentReview.
	JobStatusAwaitingChapterize JobStatus = "awaiting_chapterize"
	JobStatusFailed             JobStatus = "failed"
	JobStatusCompleted          JobStatus = "completed"
	JobStatusTimedOut           JobStatus = "timed_out"
	JobStatusCancelRequested    JobStatus = "cancel_requested"
	JobStatusCancelled          JobStatus = "cancelled"
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
	ChapterStartMs int64 `json:"chapter_start_ms"`
	ChapterEndMs   int64 `json:"chapter_end_ms"`
	// ChapterTitle / ChapterTitleTranslated / ChapterSummaryMD are written by OPT-403
	// chapterize after the LLM Pass 3 review (see internal/llm/chapter_review.go). Empty
	// strings on 1-chapter shortcut paths and on every historical Job. The translated
	// title is what the EpisodeDetail UI shows by default; the source title is preserved
	// for downstream search / human review.
	ChapterTitle           string        `json:"chapter_title" gorm:"size:256"`
	ChapterTitleTranslated string        `json:"chapter_title_translated" gorm:"size:256"`
	ChapterSummaryMD       string        `json:"chapter_summary_md" gorm:"type:text"`
	// ChapterJudgeScore + ChapterJudgeMeta are populated asynchronously by the
	// OPT-409 chapter-level judge that runs after runMerge completes a chapter.
	// Both nil when chapter judging is disabled (CHAPTER_JUDGE_MODEL="") or
	// when the judge call has not yet run for this chapter (including every
	// historical Job — chapter judge writes are gated behind the new env flag,
	// so OPT-401 back-filled rows naturally stay NULL until re-merged).
	// ChapterJudgeScore is the scalar overall score (0..1, currently equal to
	// ChapterJudgeResult.OverallFidelityChapter) suitable for the UI heat map;
	// ChapterJudgeMeta carries the full structured verdict (per-axis sub-scores,
	// top-3 weakest segments, observed glossary, verdict enum).
	ChapterJudgeScore *float64       `json:"chapter_judge_score,omitempty" gorm:"type:numeric"`
	ChapterJudgeMeta  datatypes.JSON `json:"chapter_judge_meta,omitempty" gorm:"type:jsonb"`
	RetryCount             int           `json:"retry_count"`
	MaxRetries             int           `json:"max_retries"`
	WebhookURL             string        `json:"webhook_url"`
	WebhookSecret          string        `json:"-" gorm:"type:text"`
	HeartbeatAt            *time.Time    `json:"heartbeat_at"`
	StartedAt              *time.Time    `json:"started_at"`
	CompletedAt            *time.Time    `json:"completed_at"`
	DeadlineAt             *time.Time    `json:"deadline_at"`
	CancelRequestedAt      *time.Time    `json:"cancel_requested_at"`
	CancelledAt            *time.Time    `json:"cancelled_at"`
	CreatedAt              time.Time     `json:"created_at"`
	UpdatedAt              time.Time     `json:"updated_at"`
	Segments               []Segment     `json:"segments,omitempty"`
	Speakers               []Speaker     `json:"speakers,omitempty"`
	StageRuns              []JobStageRun `json:"stage_runs,omitempty"`
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

	// OPT-402 episode-level dispatch. When EpisodeStage is non-empty the
	// worker routes the task through the episode-stage switch and uses
	// EpisodeID instead of JobID. Stage and EpisodeStage are MUTUALLY
	// EXCLUSIVE in any single payload — the worker enforces this and
	// rejects payloads that set both.
	EpisodeID    uint         `json:"episode_id,omitempty"`
	EpisodeStage EpisodeStage `json:"episode_stage,omitempty"`
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
	// VocalsRelPath / BgmRelPath are produced by the OPT-402 episode-level
	// `ep_separate` stage on the FULL video (so the GPU runs once per
	// episode, not once per chapter). 1-chapter shortcut also writes the
	// matching Job-level fields for backward compat.
	VocalsRelPath string `json:"vocals_relpath" gorm:"size:512"`
	BgmRelPath    string `json:"bgm_relpath" gorm:"size:512"`
	// ASRDoneAt / GlossaryDoneAt are simple progress timestamps for the
	// EpisodeDetail UI's "episode-level stages" tracker. Nil until that
	// episode-stage finishes successfully. They are not strict invariants
	// (don't gate logic on them) — the source of truth is Status.
	ASRDoneAt      *time.Time `json:"asr_done_at,omitempty"`
	GlossaryDoneAt *time.Time `json:"glossary_done_at,omitempty"`
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
	Status        EpisodeStatus `json:"status" gorm:"size:32;index"`
	OutputRelPath string        `json:"output_relpath"`
	// OutputLayoutVersion distinguishes pre-OPT-403 layout (1 = jobs/{id}/output/...)
	// from the unified OPT-403 layout (2 = episodes/{ep_id}/{chapters,output}/...).
	// New episodes default to 2; the cmd/migrate-output one-shot tool back-fills every
	// historical episode to 2 in lock-step with hard-linking the physical files. Code
	// reading episode artefacts MUST honour this field — never assume layout.
	OutputLayoutVersion int8 `json:"output_layout_version" gorm:"not null;default:1"`
	// ChaptersManifestRelPath points at episodes/{ep_id}/chapters.json (written by
	// stage_episode_merge after every chapter completes). Empty until episode-merge
	// runs, or until back-fill seeds it.
	ChaptersManifestRelPath string `json:"chapters_manifest_rel_path" gorm:"size:512"`
	// LoudnormStats records per-chapter measured EBU R128 LUFS / TP / LRA from
	// stage_merge plus the optional master-pass stats from stage_episode_merge. Shape:
	//   { "vp0_ch01": {...}, "vp0_ch02": {...}, "vp0_master": {...}, "vp1_...": {...} }
	// Used by chapters.json to surface per-chapter loudness, and by future OPT-406.
	LoudnormStats datatypes.JSON `json:"loudnorm_stats,omitempty" gorm:"type:jsonb"`
	// LLMChapters is the OPT-405 LLM-driven chapter plan emitted by
	// ep_glossary_extract (see internal/llm/glossary.go ExtractEpisodeGlossary).
	// Shape:
	//   [{"start_segment_idx": 0, "end_segment_idx": 17,
	//     "title_source": "...", "title_translated": "...",
	//     "summary_md": "..."}, ...]
	// Read by stage_chapterize.go: the LLM's segment-index boundaries get
	// snapped to the nearest silence midpoint, then the hard min/max
	// guardrails are applied. Empty / NULL means "fall back to the legacy
	// deterministic DP algorithm" — which happens when the LLM is disabled,
	// the call failed, or the episode is short enough to short-circuit.
	LLMChapters datatypes.JSON `json:"llm_chapters,omitempty" gorm:"type:jsonb"`
	ErrorMessage  string         `json:"error_message" gorm:"type:text"`
	CompletedAt   *time.Time     `json:"completed_at"`
	CreatedAt     time.Time      `json:"created_at"`
	UpdatedAt     time.Time      `json:"updated_at"`
	Chapters      []Job          `json:"chapters,omitempty" gorm:"foreignKey:EpisodeID"`
}

// GetEpisodeOutputRelPath returns the relpath of the episode-level final video for
// the given voice profile. Format: episodes/{ep_id}/output/vp{vpID}/final.mp4
//
// All path generation for episode artefacts MUST go through these four helpers
// (do not fmt.Sprintf in handlers) — single source of truth for the OPT-403 output
// layout, and the only reason lessons-learned.mdc §1's "always read DB, never
// reconstruct path by convention" rule does not get repeatedly violated.
func (e *Episode) GetEpisodeOutputRelPath(vpID uint) string {
	return fmt.Sprintf("episodes/%d/output/vp%d/final.mp4", e.ID, vpID)
}

// GetChapterOutputRelPath returns the relpath of one chapter's final video.
// Format: episodes/{ep_id}/chapters/vp{vpID}/ch{ordinal:02d}.mp4
//
// Used by chapter-level stage_merge as the OUTPUT and by stage_episode_merge as
// the INPUT it concats from.
func (e *Episode) GetChapterOutputRelPath(ordinal int, vpID uint) string {
	return fmt.Sprintf("episodes/%d/chapters/vp%d/ch%02d.mp4", e.ID, vpID, ordinal)
}

// GetEpisodeSeparateRelPath returns the relpath of the episode-level master vocals
// or BGM track. Track must be one of "vocals" or "bgm".
// Format: episodes/{ep_id}/separate/{track}.wav
func (e *Episode) GetEpisodeSeparateRelPath(track string) string {
	return fmt.Sprintf("episodes/%d/separate/%s.wav", e.ID, track)
}

// GetChaptersJSONRelPath returns the relpath of the chapter manifest JSON.
// Format: episodes/{ep_id}/chapters.json
//
// Written by stage_episode_merge / cmd/migrate-output. Read by the UI's "下载
// 章节清单" button and by any external CLI that needs the bilingual chapter
// list without going through the API.
func (e *Episode) GetChaptersJSONRelPath() string {
	return fmt.Sprintf("episodes/%d/chapters.json", e.ID)
}

// EpisodeStage is the typed pipeline stage for the episode-level pre-fan-out
// pipeline introduced by OPT-402. These stages run ONCE per episode on the
// full video before any chapter-level work begins (separate vocals from BGM,
// ASR the whole soundtrack, derive a canonical glossary, then chapterize).
//
// Distinguished from JobStage (which is per-chapter) so worker dispatch can
// route a single redis queue item unambiguously: TaskPayload carries either
// Stage or EpisodeStage, never both.
//
// OPT-403 / 404 / 406 episode-level stages (chapterize / episode_merge /
// episode_judge) are pre-declared here so the corresponding fan-out work
// can land incrementally without further enum churn.
type EpisodeStage string

const (
	EpisodeStageMedia           EpisodeStage = "ep_media"
	EpisodeStageSeparate        EpisodeStage = "ep_separate"
	EpisodeStageASRSmart        EpisodeStage = "ep_asr_smart"
	EpisodeStageGlossaryExtract EpisodeStage = "ep_glossary_extract"
	// OPT-403 placeholder — the chapterize stage will turn the full ASR text
	// into chapter ranges and fan out chapter Jobs.
	EpisodeStageChapterize EpisodeStage = "ep_chapterize"
	// OPT-404 placeholder — final episode-level merge after every chapter
	// completes (concatenation + cross-chapter loudness normalisation).
	EpisodeStageEpisodeMerge EpisodeStage = "ep_episode_merge"
	// OPT-406 placeholder — episode-level judge that scores the full output.
	EpisodeStageEpisodeJudge EpisodeStage = "ep_episode_judge"
)

// EpisodeStageOrder is the canonical sequence the OPT-402 worker walks when
// processing an episode. Each finished stage enqueues the next via
// EpisodeStage.Next(). The chapter fan-out (post-chapterize) is NOT in this
// slice — once chapterize is done, control transfers to per-chapter Job
// stages (StageOrder).
//
// Invariant: every stage in this slice has a registered handler in
// pipeline.HandleTask's episode-stage switch. New stages must be added
// to both places in the same commit.
var EpisodeStageOrder = []EpisodeStage{
	EpisodeStageMedia,
	EpisodeStageSeparate,
	EpisodeStageASRSmart,
	EpisodeStageGlossaryExtract,
	EpisodeStageChapterize,
	// EpisodeStageEpisodeMerge is intentionally NOT in this list. It runs at a
	// non-deterministic time AFTER all chapter Jobs reach JobStatusCompleted, so
	// chapter-merge code triggers it via pipeline.maybeEnqueueEpisodeMerge instead
	// of EpisodeStageChapterize.Next() returning it. Same reasoning will apply to
	// EpisodeStageEpisodeJudge once OPT-406 lands.
}

// Next returns the stage that should follow the receiver in the episode-
// level pipeline, or "", false if the receiver is the last (or unknown).
// Mirrors JobStage.Next().
func (stage EpisodeStage) Next() (EpisodeStage, bool) {
	for idx, current := range EpisodeStageOrder {
		if current == stage && idx+1 < len(EpisodeStageOrder) {
			return EpisodeStageOrder[idx+1], true
		}
	}
	return "", false
}
