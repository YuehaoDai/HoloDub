package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"holodub/internal/config"
	"holodub/internal/llm"
	"holodub/internal/media"
	"holodub/internal/ml"
	"holodub/internal/models"
	"holodub/internal/observability"
	"holodub/internal/queue"
	"holodub/internal/rework"
	"holodub/internal/storage"
	"holodub/internal/store"
	"holodub/internal/webhook"

	"gorm.io/datatypes"
)

type Service struct {
	cfg      config.Config
	store    *store.Store
	queue    *queue.Queue
	ml       *ml.Client
	llm      *llm.Client
	notifier *webhook.Notifier
	// rework is the OPT-407 closed-loop rework engine. Always non-nil after
	// NewService; gates its own dispatches on cfg.ReworkEngineLevel so a
	// caller never has to nil-check before calling MaybeRework*.
	rework *rework.Engine
}

func NewService(cfg config.Config, st *store.Store, q *queue.Queue, mlClient *ml.Client, llmClient *llm.Client) *Service {
	svc := &Service{
		cfg:      cfg,
		store:    st,
		queue:    q,
		ml:       mlClient,
		llm:      llmClient,
		notifier: webhook.New(cfg),
	}
	// Engine depends on Service via the narrow rework.RetryJobAPI interface
	// (RetryJob + EnqueueEpisodeStage). Wired in after construction so the
	// circular reference (Service → Engine → Service) is captured by the
	// interface, NOT by an import cycle.
	svc.rework = rework.NewEngine(cfg, st, svc)
	return svc
}

func (s *Service) MLHealth(ctx context.Context) (map[string]any, error) {
	return s.ml.Health(ctx)
}

// PingQueue verifies Redis connectivity for /readyz.
func (s *Service) PingQueue(ctx context.Context) error {
	return s.queue.Ping(ctx)
}

// MLReadiness reports whether the ML service is ready to serve TTS/ASR
// requests. It returns the upstream raw payload and an error categorising
// the failure mode (network unreachable, TTS warmup not ready, etc.).
func (s *Service) MLReadiness(ctx context.Context) (map[string]any, bool, error) {
	resp, err := s.ml.Health(ctx)
	if err != nil {
		return nil, false, err
	}
	// IndexTTS2 inline mode reports tts_warmup_status; "ready" means the
	// model is loaded into VRAM and TTS requests will not block on cold
	// start. Other backends report "idle" which is also acceptable.
	if status, ok := resp["tts_warmup_status"].(string); ok {
		if status == "loading" {
			return resp, false, nil
		}
		if status == "error" {
			return resp, false, fmt.Errorf("ml service tts warmup status=error")
		}
	}
	return resp, true, nil
}

func (s *Service) EnqueueStage(ctx context.Context, task models.TaskPayload) error {
	if task.Attempt < 0 {
		task.Attempt = 0
	}
	if err := s.store.UpdateJobState(ctx, task.JobID, models.JobStatusQueued, task.Stage, "", false); err != nil {
		return err
	}
	return s.queue.Enqueue(ctx, task)
}

// EnqueueEpisodeStage is the OPT-402 entry point for episode-level
// pipeline tasks (currently: ep_glossary_extract). It deliberately does
// NOT touch the chapter-level Job state machine — episode stages run
// in parallel with the chapter pipeline (e.g. glossary extracted
// between asr_smart and segment_review of the same 1-chapter episode).
//
// The TaskPayload uses EpisodeID + EpisodeStage; Stage / JobID are
// left zero so the worker dispatch can unambiguously route via
// EpisodeStage != "".
func (s *Service) EnqueueEpisodeStage(ctx context.Context, episodeID uint, stage models.EpisodeStage, requestedBy, reason string) error {
	if episodeID == 0 || stage == "" {
		return fmt.Errorf("EnqueueEpisodeStage requires non-zero episodeID and non-empty stage")
	}
	return s.queue.Enqueue(ctx, models.TaskPayload{
		EpisodeID:    episodeID,
		EpisodeStage: stage,
		Attempt:      0,
		RequestedBy:  requestedBy,
		Reason:       reason,
	})
}

func (s *Service) StartJob(ctx context.Context, jobID uint, requestedBy string) error {
	return s.EnqueueStage(ctx, models.TaskPayload{
		JobID:       jobID,
		Stage:       models.StageMedia,
		Attempt:     0,
		RequestedBy: requestedBy,
		Reason:      "initial_start",
	})
}

func (s *Service) RetryJob(ctx context.Context, jobID uint, stage models.JobStage, segmentIDs []uint, requestedBy string) error {
	return s.retryJobWithHint(ctx, jobID, stage, segmentIDs, requestedBy, nil)
}

// DispatchSegmentRework satisfies rework.RetryJobAPI. The hint is
// carried verbatim into the queue payload; when nil the behaviour is
// identical to RetryJob.
func (s *Service) DispatchSegmentRework(ctx context.Context, jobID uint, stage models.JobStage, segmentIDs []uint, requestedBy string, hint *models.ReworkHint) error {
	return s.retryJobWithHint(ctx, jobID, stage, segmentIDs, requestedBy, hint)
}

func (s *Service) retryJobWithHint(ctx context.Context, jobID uint, stage models.JobStage, segmentIDs []uint, requestedBy string, hint *models.ReworkHint) error {
	if stage == "" {
		job, err := s.store.GetJob(ctx, jobID)
		if err != nil {
			return err
		}
		stage = job.CurrentStage
	}
	if stage == models.StageTTSDuration && len(segmentIDs) > 0 {
		if err := s.store.ResetSegmentsForRerun(ctx, segmentIDs); err != nil {
			return err
		}
	}
	if err := s.store.UpdateJobState(ctx, jobID, models.JobStatusQueued, stage, "", true); err != nil {
		return err
	}
	reason := "manual_retry"
	if hint != nil {
		reason = "rework_engine"
	}
	return s.queue.Enqueue(ctx, models.TaskPayload{
		JobID:           jobID,
		Stage:           stage,
		Attempt:         0,
		SegmentIDs:      store.UniqueUint(segmentIDs),
		RequestedBy:     requestedBy,
		Reason:          reason,
		SkipAutoAdvance: true,
		ReworkHint:      hint,
	})
}

// ConfirmSegmentation advances a job that is blocked in awaiting_review status
// to the translate stage.  It is called when the user clicks "Confirm segmentation"
// in the UI after reviewing (and optionally adjusting) the ASR segments.
func (s *Service) ConfirmSegmentation(ctx context.Context, jobID uint, requestedBy string) error {
	job, err := s.store.GetJob(ctx, jobID)
	if err != nil {
		return err
	}
	if job.Status != models.JobStatusAwaitingReview {
		return fmt.Errorf("job %d is not in awaiting_review state (current: %s)", jobID, job.Status)
	}
	return s.EnqueueStage(ctx, models.TaskPayload{
		JobID:       jobID,
		Stage:       models.StageTranslate,
		Attempt:     0,
		RequestedBy: requestedBy,
		Reason:      "segmentation_confirmed",
	})
}

// RetryASR re-runs the asr_smart stage for a job that is in awaiting_review.
// It clears all existing segments and suggestions so the new ASR run starts
// from a clean slate and will produce fresh segment_review suggestions.
func (s *Service) RetryASR(ctx context.Context, jobID uint, requestedBy string) error {
	job, err := s.store.GetJob(ctx, jobID)
	if err != nil {
		return err
	}
	if job.Status != models.JobStatusAwaitingReview {
		return fmt.Errorf("job %d is not in awaiting_review state (current: %s)", jobID, job.Status)
	}
	// Clear suggestions and segments atomically before re-queuing
	if err := s.store.DeleteSuggestionsForJob(ctx, jobID); err != nil {
		return err
	}
	if err := s.store.ReplaceSegments(ctx, jobID, nil); err != nil {
		return err
	}
	return s.EnqueueStage(ctx, models.TaskPayload{
		JobID:       jobID,
		Stage:       models.StageASRSmart,
		Attempt:     0,
		RequestedBy: requestedBy,
		Reason:      "asr_retry_from_review",
	})
}

// ErrSegmentTranscriptionEmpty is returned by RetrySegmentASR when the
// ml-service successfully ran ASR over the requested window but produced
// no text (e.g. the window is pure silence or noise).  Callers should
// surface this to the user as a hint to edit the transcript manually
// rather than treating it as a hard failure.
var ErrSegmentTranscriptionEmpty = errors.New("segment transcription returned empty text")

// RetrySegmentASR re-runs ASR on a single segment without disturbing the
// rest of the job.  Unlike RetryASR (job-level), this preserves all
// existing segments, suggestions, manual merges and split decisions.
//
// Preconditions enforced:
//   - the parent job is in JobStatusAwaitingReview
//   - the segment exists AND belongs to that job
//
// On success the segment's source_text is replaced via
// store.UpdateSegmentSourceText (timing / status / target_text untouched).
// On empty transcription the function returns ErrSegmentTranscriptionEmpty
// without writing anything to the database, so the caller can prompt the
// user to edit manually.
func (s *Service) RetrySegmentASR(ctx context.Context, jobID uint, segmentID uint, requestedBy string) (string, error) {
	job, err := s.store.GetJob(ctx, jobID)
	if err != nil {
		return "", err
	}
	if job.Status != models.JobStatusAwaitingReview {
		return "", fmt.Errorf("job %d is not in awaiting_review state (current: %s)", jobID, job.Status)
	}
	seg, err := s.store.GetSegment(ctx, segmentID)
	if err != nil {
		return "", err
	}
	if seg.JobID != jobID {
		return "", fmt.Errorf("segment %d does not belong to job %d", segmentID, jobID)
	}
	if seg.EndMs <= seg.StartMs {
		return "", fmt.Errorf("segment %d has invalid time range %d..%d", segmentID, seg.StartMs, seg.EndMs)
	}

	audioRelPath := job.VocalsRelPath
	if audioRelPath == "" {
		audioRelPath = job.InputRelPath
	}
	if audioRelPath == "" {
		return "", fmt.Errorf("job %d has no audio path (vocals or input)", jobID)
	}

	slog.Info("retry_segment_asr starting",
		"job_id", jobID,
		"segment_id", segmentID,
		"audio_relpath", audioRelPath,
		"start_ms", seg.StartMs,
		"end_ms", seg.EndMs,
		"requested_by", requestedBy,
	)
	resp, err := s.ml.TranscribeSegment(ctx, ml.TranscribeSegmentRequest{
		AudioRelPath:   audioRelPath,
		SourceLanguage: job.SourceLanguage,
		StartMs:        seg.StartMs,
		EndMs:          seg.EndMs,
	})
	if err != nil {
		return "", fmt.Errorf("ml transcribe_segment: %w", err)
	}

	text := strings.TrimSpace(resp.Text)
	if text == "" {
		slog.Warn("retry_segment_asr returned empty transcript",
			"job_id", jobID, "segment_id", segmentID,
			"diagnostics", resp.Diagnostics)
		return "", ErrSegmentTranscriptionEmpty
	}

	if err := s.store.UpdateSegmentSourceText(ctx, jobID, segmentID, text); err != nil {
		return "", fmt.Errorf("persist source_text: %w", err)
	}
	slog.Info("retry_segment_asr completed",
		"job_id", jobID, "segment_id", segmentID, "text_chars", len(text))
	return text, nil
}

func (s *Service) HandleTask(ctx context.Context, task models.TaskPayload) error {
	// OPT-402 episode-stage routing. Episode-level tasks (ep_glossary_extract,
	// future ep_chapterize, ep_episode_merge, ep_episode_judge) carry
	// EpisodeStage != "" and operate on EpisodeID, NOT JobID. They bypass
	// the chapter Job state machine so episode-level work can run in
	// parallel with chapter pipelines (e.g. glossary extract runs while a
	// 1-chapter episode is between asr_smart and segment_review).
	if task.EpisodeStage != "" {
		return s.handleEpisodeStage(ctx, task)
	}

	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}
	if job.DeadlineAt != nil && time.Now().After(*job.DeadlineAt) {
		_ = s.store.UpdateJobState(ctx, job.ID, models.JobStatusTimedOut, task.Stage, "job deadline exceeded", false)
		return fmt.Errorf("job %d deadline exceeded", job.ID)
	}
	if job.Status == models.JobStatusCancelRequested {
		_ = s.store.UpdateJobState(ctx, job.ID, models.JobStatusCancelled, task.Stage, "cancel requested", false)
		_ = s.notifyJobEvent(ctx, *job, "job.cancelled", task, nil)
		return nil
	}

	leaseAcquired, err := s.queue.AcquireStageLease(ctx, task.JobID, task.Stage, s.cfg.StageLeaseTTL)
	if err != nil {
		return fmt.Errorf("acquire stage lease: %w", err)
	}
	if !leaseAcquired {
		slog.Info("stage lease already held, re-queuing for retry",
			"job_id", task.JobID,
			"stage", task.Stage,
			"attempt", task.Attempt,
		)
		// Re-queue with delay so another worker (or lease expiry) can pick it up.
		delay := 60 * time.Second
		if err := s.queue.EnqueueWithDelay(ctx, task, delay); err != nil {
			slog.Warn("re-queue task failed", "job_id", task.JobID, "stage", task.Stage, "error", err)
		}
		return nil
	}
	defer func() {
		if releaseErr := s.queue.ReleaseStageLease(context.Background(), task.JobID, task.Stage); releaseErr != nil {
			slog.Warn("release stage lease failed",
				"job_id", task.JobID,
				"stage", task.Stage,
				"error", releaseErr,
			)
		}
	}()

	startedAt := time.Now()
	if err := s.store.UpdateJobState(ctx, task.JobID, models.JobStatusRunning, task.Stage, "", false); err != nil {
		return err
	}
	stageRun, err := s.createStageRun(ctx, task)
	if err != nil {
		return err
	}
	_ = s.store.TouchJobHeartbeat(ctx, task.JobID)
	_ = s.notifyJobEvent(ctx, *job, "stage.started", task, nil)

	stageTimeout := s.stageTimeoutForJob(*job)
	stageCtx, cancel := context.WithTimeout(ctx, stageTimeout)
	defer cancel()

	var stageErr error
	switch task.Stage {
	case models.StageMedia:
		stageErr = s.runMedia(stageCtx, task)
	case models.StageSeparate:
		stageErr = s.runSeparate(stageCtx, task)
	case models.StageASRSmart:
		stageErr = s.runASRSmart(stageCtx, task)
	case models.StageSegmentReview:
		stageErr = s.runSegmentReview(stageCtx, task)
	case models.StageTranslate:
		stageErr = s.runTranslate(stageCtx, task)
	case models.StageTTSDuration:
		stageErr = s.runTTSDuration(stageCtx, task)
	case models.StageMerge:
		stageErr = s.runMerge(stageCtx, task)
	default:
		stageErr = fmt.Errorf("unsupported stage %q", task.Stage)
	}
	duration := time.Since(startedAt)

	if stageErr != nil {
		finalStatus := models.JobStatusFailed
		if errors.Is(stageCtx.Err(), context.DeadlineExceeded) {
			finalStatus = models.JobStatusTimedOut
		}
		if cancelRequested, cancelErr := s.store.IsCancelRequested(ctx, task.JobID); cancelErr == nil && cancelRequested {
			finalStatus = models.JobStatusCancelled
		}

		if retryErr := s.handleStageFailure(ctx, *job, task, stageRun.ID, finalStatus, stageErr, duration); retryErr != nil {
			return retryErr
		}
		return stageErr
	}
	observability.ObserveStageRun(string(task.Stage), "completed", duration)
	if err := s.store.FinishStageRun(ctx, stageRun.ID, "completed", "", duration.Milliseconds(), map[string]any{
		"attempt": task.Attempt,
	}); err != nil {
		return err
	}
	_ = s.notifyJobEvent(ctx, *job, "stage.completed", task, map[string]any{
		"duration_ms": duration.Milliseconds(),
	})

	nextStage, hasNext := task.Stage.Next()
	if !hasNext || task.SkipAutoAdvance {
		job.OutputRelPath = s.outputRelPathForJob(ctx, task.JobID)
		if err := s.store.UpdateJobState(ctx, task.JobID, models.JobStatusCompleted, task.Stage, "", false); err != nil {
			return err
		}
		// OPT-401 1-chapter shortcut: keep the parent Episode's status in
		// sync with its only chapter so the EpisodeDetail UI doesn't show
		// "pending" while the chapter has actually finished. Multi-chapter
		// episodes are intentionally left to OPT-404 / OPT-407 which know
		// how to fan-in across chapters.
		s.maybeShortcutEpisodeCompleted(ctx, *job)
		_ = s.notifyJobEvent(ctx, *job, "job.completed", task, map[string]any{
			"output_relpath": job.OutputRelPath,
		})
		return nil
	}
	// Blocking stage: if the stage set job status to awaiting_review
	// (segment_review waiting for user confirmation) or
	// JobStatusAwaitingChapterize (asr_smart on a long video deferring to
	// the OPT-403 ep_chapterize fan-out decision), do not auto-advance.
	// In both cases another agent — the user, or runEpisodeChapterize —
	// will re-enqueue the job.
	refreshedJob, refreshErr := s.store.GetJob(ctx, task.JobID)
	if refreshErr == nil && (refreshedJob.Status == models.JobStatusAwaitingReview ||
		refreshedJob.Status == models.JobStatusAwaitingChapterize) {
		return nil
	}
	return s.EnqueueStage(ctx, models.TaskPayload{
		JobID:       task.JobID,
		Stage:       nextStage,
		Attempt:     0,
		RequestedBy: "pipeline",
		Reason:      string(task.Stage) + "_completed",
	})
}

func (s *Service) runMedia(ctx context.Context, task models.TaskPayload) error {
	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}
	inputPath := storage.ResolveDataPath(s.cfg.DataRoot, job.InputRelPath)
	if _, err := os.Stat(inputPath); err != nil {
		return fmt.Errorf("input media not found at %s: %w", inputPath, err)
	}
	jobDir := filepath.Join(s.cfg.DataRoot, "jobs", fmt.Sprintf("%d", job.ID))
	return os.MkdirAll(jobDir, 0o755)
}

func (s *Service) runSeparate(ctx context.Context, task models.TaskPayload) error {
	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}

	response, err := s.ml.Separate(ctx, ml.SeparateRequest{
		InputRelPath:        job.InputRelPath,
		VocalsOutputRelPath: fmt.Sprintf("jobs/%d/separate/vocals.wav", job.ID),
		BgmOutputRelPath:    fmt.Sprintf("jobs/%d/separate/bgm.wav", job.ID),
	})
	if err != nil {
		return err
	}

	job.VocalsRelPath = response.VocalsRelPath
	job.BgmRelPath = response.BgmRelPath
	return s.store.SaveJob(ctx, job)
}

func (s *Service) runASRSmart(ctx context.Context, task models.TaskPayload) error {
	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}

	audioRelPath := job.VocalsRelPath
	if audioRelPath == "" {
		audioRelPath = job.InputRelPath
	}
	slog.Info("asr_smart starting",
		"job_id", task.JobID,
		"audio_relpath", audioRelPath,
		"source_language", job.SourceLanguage,
	)

	response, err := s.ml.SmartSplit(ctx, ml.SmartSplitRequest{
		AudioRelPath:      audioRelPath,
		SourceLanguage:    job.SourceLanguage,
		MinSegmentSec:     jobConfigFloat(job.Config, "min_segment_sec", 4.0),
		MaxSegmentSec:     jobConfigFloat(job.Config, "max_segment_sec", 20.0),
		HardMaxSegmentSec: jobConfigFloat(job.Config, "hard_max_segment_sec", s.cfg.HardMaxSegmentSec),
		CloseGapMs:        jobConfigInt(job.Config, "close_gap_ms", s.cfg.CloseGapMs),
	})
	if err != nil {
		slog.Error("asr_smart ml.SmartSplit failed", "job_id", task.JobID, "error", err)
		return err
	}
	if len(response.Segments) == 0 {
		slog.Warn("asr_smart returned 0 segments", "job_id", task.JobID)
		return errors.New("smart split returned no segments")
	}
	slog.Info("asr_smart completed", "job_id", task.JobID, "segment_count", len(response.Segments))

	drafts := make([]models.SegmentDraft, 0, len(response.Segments))
	for _, segment := range response.Segments {
		drafts = append(drafts, models.SegmentDraft{
			StartMs:      segment.StartMs,
			EndMs:        segment.EndMs,
			Text:         segment.Text,
			SpeakerLabel: segment.SpeakerLabel,
			SplitReason:  segment.SplitReason,
		})
	}
	if err := s.store.ReplaceSegments(ctx, job.ID, drafts); err != nil {
		return err
	}

	// OPT-402 double-write: mirror the chapter Job's vocals/bgm into
	// the parent Episode row and stamp asr_done_at. 1-chapter shortcut
	// path; multi-chapter episodes will get vocals from a dedicated
	// ep_separate stage in OPT-403.
	if job.EpisodeID != 0 {
		if err := s.store.UpdateEpisodeMediaFromChapter(ctx, job.EpisodeID, job.VocalsRelPath, job.BgmRelPath); err != nil {
			slog.Warn("opt-402 double-write episode media failed; continuing",
				"job_id", job.ID, "episode_id", job.EpisodeID, "error", err)
		}
		// Fire-and-forget enqueue of the OPT-402 glossary stage. If
		// GlossaryEnabled=false the handler short-circuits at the top.
		// Non-fatal: queue failure logs but never blocks the chapter
		// pipeline (translate falls back to the legacy summary if
		// glossary doesn't materialise in time).
		if s.cfg.GlossaryEnabled {
			if err := s.EnqueueEpisodeStage(ctx,
				job.EpisodeID,
				models.EpisodeStageGlossaryExtract,
				"pipeline",
				"asr_smart_completed",
			); err != nil {
				slog.Warn("opt-402 enqueue ep_glossary_extract failed; continuing",
					"job_id", job.ID, "episode_id", job.EpisodeID, "error", err)
			}
		}

		// OPT-403 gating: if chapterize is enabled AND the episode is long
		// enough that fan-out is plausible, park chapter 1 in
		// JobStatusAwaitingChapterize. The auto-advance machinery in
		// HandleTask checks this status and skips its EnqueueStage call,
		// leaving runEpisodeChapterize as the sole agent that resumes the
		// chapter pipeline (either by short-circuiting or by fan-out).
		// We use the last segment's EndMs as the canonical episode duration
		// because ASR has just produced it and it is exact (no ffprobe
		// round-trip needed).
		if s.cfg.ChapterizeEnabled {
			lastEndMs := int64(0)
			if n := len(drafts); n > 0 {
				lastEndMs = drafts[n-1].EndMs
			}
			if lastEndMs > s.cfg.ChapterizeMaxChapterMs {
				if err := s.store.UpdateJobState(ctx, job.ID,
					models.JobStatusAwaitingChapterize, models.StageASRSmart,
					"long video; awaiting OPT-403 chapterize decision", false); err != nil {
					slog.Warn("opt-403 set chapter 1 awaiting_chapterize failed; continuing",
						"job_id", job.ID, "error", err)
				} else {
					slog.Info("opt-403 chapter 1 parked awaiting chapterize",
						"job_id", job.ID,
						"episode_id", job.EpisodeID,
						"episode_duration_ms", lastEndMs,
						"max_chapter_ms", s.cfg.ChapterizeMaxChapterMs,
					)
				}
			}
		}
	}
	return nil
}

// runSegmentReview is the pipeline stage that sits between asr_smart and translate.
// It runs the LLM segmentation-review agent and stores suggestions, then sets the
// job status to awaiting_review so HandleTask does NOT auto-advance to translate.
// The job resumes when the user calls POST /jobs/:id/confirm-segmentation.
func (s *Service) runSegmentReview(ctx context.Context, task models.TaskPayload) error {
	if !s.cfg.SegmentReviewEnabled {
		slog.Info("segment_review disabled by config, skipping", "job_id", task.JobID)
		return nil // HandleTask will auto-advance to translate
	}

	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}

	segments, err := s.store.ListSegments(ctx, task.JobID, nil)
	if err != nil {
		return err
	}
	if len(segments) == 0 {
		slog.Warn("segment_review: no segments found, skipping", "job_id", task.JobID)
		return nil
	}

	// Build SegmentInfo list for the LLM
	segInfos := make([]llm.SegmentInfo, len(segments))
	for i, seg := range segments {
		var gapAfterMs int64
		if i+1 < len(segments) {
			gapAfterMs = segments[i+1].StartMs - seg.EndMs
			if gapAfterMs < 0 {
				gapAfterMs = 0
			}
		}
		segInfos[i] = llm.SegmentInfo{
			Ordinal:     seg.Ordinal,
			Text:        seg.SourceText,
			StartMs:     seg.StartMs,
			EndMs:       seg.EndMs,
			GapAfterMs:  gapAfterMs,
			SplitReason: seg.SplitReason,
		}
	}

	slog.Info("segment_review starting LLM review", "job_id", task.JobID, "segment_count", len(segments))
	suggestions, err := s.llm.ReviewSegmentation(ctx, job.SourceLanguage, segInfos)
	if err != nil {
		// Non-fatal: log and continue with zero suggestions so the user still gets
		// the manual-review UI even without LLM suggestions.
		slog.Warn("segment_review LLM call failed, continuing without suggestions",
			"job_id", task.JobID, "error", err)
		suggestions = nil
	}
	slog.Info("segment_review LLM review done", "job_id", task.JobID, "suggestion_count", len(suggestions))

	// Build an ordinal→segment mapping to resolve real segment IDs
	ordinalToSegment := make(map[int]models.Segment, len(segments))
	for _, seg := range segments {
		ordinalToSegment[seg.Ordinal] = seg
	}

	// Convert to models.SegmentSuggestion with real segment IDs
	dbSuggestions := make([]models.SegmentSuggestion, 0, len(suggestions))
	for i, sug := range suggestions {
		realIDs := make([]uint, 0, len(sug.SegmentIDs))
		valid := true
		for _, ordinal := range sug.SegmentIDs {
			seg, ok := ordinalToSegment[int(ordinal)]
			if !ok {
				valid = false
				break
			}
			realIDs = append(realIDs, seg.ID)
		}
		if !valid || len(realIDs) < 2 {
			continue
		}
		dbSuggestions = append(dbSuggestions, models.SegmentSuggestion{
			Ordinal:    i,
			Action:     "merge",
			SegmentIDs: realIDs,
			Reason:     sug.Reason,
			Confidence: sug.Confidence,
			Status:     "pending",
		})
	}

	if err := s.store.CreateSuggestions(ctx, task.JobID, dbSuggestions); err != nil {
		return fmt.Errorf("segment_review: save suggestions: %w", err)
	}

	// Mark the job as awaiting_review — HandleTask will see this and skip auto-advance.
	if err := s.store.UpdateJobState(ctx, task.JobID, models.JobStatusAwaitingReview, models.StageSegmentReview, "", false); err != nil {
		return fmt.Errorf("segment_review: set awaiting_review: %w", err)
	}
	slog.Info("segment_review: job is now awaiting user confirmation",
		"job_id", task.JobID, "suggestions", len(dbSuggestions))
	return nil
}

func (s *Service) runTranslate(ctx context.Context, task models.TaskPayload) error {
	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}
	segments, err := s.store.ListSegments(ctx, job.ID, task.SegmentIDs)
	if err != nil {
		return err
	}

	// Determine the voice-profile speaking-rate hint to pass to the LLM.
	charsPerSecHint := s.voiceRateHintForJob(ctx, job.ID, job.TargetLanguage)

	// Two-pass translation strategy:
	//   Pass 1 — translate the first ~20% of segments (seed pass).
	//   Between passes — generate an episode reference card (terminology, register, style)
	//                    from the seed translations.
	//   Pass 2 — translate the remaining segments with the reference card injected,
	//             plus a rolling window of the last 2 translated segments as local context.
	//
	// This fixes the "blind translation" problem where every segment was translated in
	// isolation, causing inconsistent terminology (e.g. "Raft"→"筏", "term"→"段", mixed
	// "leader"/"领导者"/"领袖") and unwanted elaborations.

	const seedFraction = 0.20   // translate this fraction first to bootstrap the summary
	const contextWindowSize = 2 // preceding segments to pass as local context

	total := len(segments)
	seedCount := int(math.Ceil(float64(total) * seedFraction))
	if seedCount < 5 {
		seedCount = 5 // always seed at least 5 segments
	}
	if seedCount > total {
		seedCount = total
	}

	// translationSummary starts empty; will be populated after the seed pass.
	var translationSummary string

	// translateOne translates a single segment and updates segments[idx] in place.
	// contextWindow is the slice of the last N successfully translated segments.
	//
	// OPT-204: when s.cfg.DubbingPlanEnabled is true, the call goes
	// through the strict-tool TranslateWithDubbingPlan and the
	// structured prosody output is persisted on seg.Meta["dubbing"]
	// so the IndexTTS2 adapter (PR-13) can consume it. On any
	// dubbing-plan failure we fall back to plain-text translate
	// rather than fail the segment — OPT-204 is a quality-of-life
	// improvement, never a correctness lever.
	translateOne := func(idx int, contextWindow []llm.ContextSegment, summary string) error {
		targetSec := float64(segments[idx].DurationMs()) / 1000.0
		if s.cfg.DubbingPlanEnabled {
			plan, err := s.llm.TranslateWithDubbingPlan(
				ctx,
				job.SourceLanguage,
				job.TargetLanguage,
				segments[idx].SourceText,
				targetSec,
				charsPerSecHint,
				contextWindow,
				summary,
			)
			if err == nil {
				segments[idx].TargetText = plan.Translation
				segments[idx].Status = models.SegmentStatusTranslated
				if segments[idx].Meta == nil {
					segments[idx].Meta = map[string]any{}
				}
				// Persist the full plan as a sub-key on seg.Meta so
				// the TTS adapter can read it without depending on
				// the llm package; using a JSON-marshallable map
				// matches the datatypes.JSONMap semantics.
				segments[idx].Meta["dubbing"] = map[string]any{
					"emotion": map[string]any{
						"valence": plan.Emotion.Valence,
						"arousal": plan.Emotion.Arousal,
						"label":   plan.Emotion.Label,
					},
					"pacing":         plan.Pacing,
					"emphasis_words": plan.EmphasisWords,
					"pause_after_ms": plan.PauseAfterMs,
				}
				return nil
			}
			// Dubbing-plan call failed — log + fall through to
			// plain-text path. Logged at WARN because consistent
			// failures indicate the provider does not support the
			// strict tool call (or the schema is being rejected),
			// which an operator should see in dashboards.
			slog.Warn("translate: dubbing-plan call failed, falling back to plain text",
				"job_id", job.ID, "segment_id", segments[idx].ID, "error", err,
			)
		}
		translated, err := s.llm.TranslateTextWithDuration(
			ctx,
			job.SourceLanguage,
			job.TargetLanguage,
			segments[idx].SourceText,
			targetSec,
			charsPerSecHint,
			contextWindow,
			summary,
		)
		if err != nil {
			return fmt.Errorf("translate segment %d: %w", segments[idx].ID, err)
		}
		segments[idx].TargetText = translated
		segments[idx].Status = models.SegmentStatusTranslated
		return nil
	}

	// buildContext returns the last min(contextWindowSize, n) translated segments as
	// ContextSegment pairs, suitable for passing to TranslateTextWithDuration.
	buildContext := func(upToIdx int) []llm.ContextSegment {
		start := upToIdx - contextWindowSize
		if start < 0 {
			start = 0
		}
		window := make([]llm.ContextSegment, 0, upToIdx-start)
		for i := start; i < upToIdx; i++ {
			if segments[i].TargetText != "" {
				window = append(window, llm.ContextSegment{
					SrcText: segments[i].SourceText,
					TgtText: segments[i].TargetText,
				})
			}
		}
		return window
	}

	// ── Pass 1: seed translation (no summary yet, no context for very first segment) ──
	for idx := 0; idx < seedCount; idx++ {
		if err := translateOne(idx, buildContext(idx), ""); err != nil {
			return err
		}
	}

	// Save seed translations so they're not lost if the summary call fails.
	if err := s.store.UpdateSegmentTranslations(ctx, segments[:seedCount]); err != nil {
		return err
	}

	// Generate episode reference card from the seed translations.
	seedSample := buildTranslationSample(segments[:seedCount], 20)
	if len(seedSample) > 0 {
		summary, sumErr := s.llm.SummarizeTranslation(ctx, job.SourceLanguage, job.TargetLanguage, seedSample)
		if sumErr != nil {
			slog.Warn("failed to generate seed translation summary; continuing without it",
				"job_id", job.ID, "error", sumErr)
		} else {
			translationSummary = summary
			slog.Info("seed translation summary generated",
				"job_id", job.ID, "seed_count", seedCount, "summary_len", len(summary))
		}
	}

	// ── Pass 2: translate remaining segments with summary + rolling context ──
	for idx := seedCount; idx < total; idx++ {
		if err := translateOne(idx, buildContext(idx), translationSummary); err != nil {
			return err
		}
	}

	// Save all translations (pass 1 already saved its slice; saving again is idempotent).
	if err := s.store.UpdateSegmentTranslations(ctx, segments); err != nil {
		return err
	}

	// Final summary refresh: regenerate from the full set for higher quality.
	// This replaces the seed-only summary and is what retranslation will use.
	finalSample := buildTranslationSample(segments, 30)
	if len(finalSample) > 0 {
		summary, sumErr := s.llm.SummarizeTranslation(ctx, job.SourceLanguage, job.TargetLanguage, finalSample)
		if sumErr != nil {
			slog.Warn("failed to generate final translation summary",
				"job_id", job.ID, "error", sumErr)
		} else if summary != "" {
			if storeErr := s.store.UpdateJobTranslationSummary(ctx, job.ID, summary); storeErr != nil {
				slog.Warn("failed to store translation summary",
					"job_id", job.ID, "error", storeErr)
			} else {
				slog.Info("final translation summary generated",
					"job_id", job.ID, "summary_len", len(summary))
			}
		}
	}
	return nil
}

// buildTranslationSample returns up to maxSamples (source, translation) pairs
// sampled evenly from segments that have a non-empty TargetText.
func buildTranslationSample(segments []models.Segment, maxSamples int) []llm.ContextSegment {
	// Collect only segments with translations.
	var translated []models.Segment
	for _, seg := range segments {
		if seg.TargetText != "" && seg.SourceText != "" {
			translated = append(translated, seg)
		}
	}
	if len(translated) == 0 {
		return nil
	}
	if len(translated) <= maxSamples {
		result := make([]llm.ContextSegment, len(translated))
		for i, seg := range translated {
			result[i] = llm.ContextSegment{SrcText: seg.SourceText, TgtText: seg.TargetText}
		}
		return result
	}
	// Evenly spaced sampling.
	result := make([]llm.ContextSegment, maxSamples)
	for i := 0; i < maxSamples; i++ {
		idx := i * (len(translated) - 1) / (maxSamples - 1)
		result[i] = llm.ContextSegment{SrcText: translated[idx].SourceText, TgtText: translated[idx].TargetText}
	}
	return result
}

// voiceRateHintForJob returns the average EstCharsPerSec across all voice profiles
// currently assigned to segments of the given job. Returns 0 if no calibrated
// rate is available, in which case the LLM falls back to language defaults.
func (s *Service) voiceRateHintForJob(ctx context.Context, jobID uint, targetLang string) float64 {
	type vpIDRow struct {
		VoiceProfileID *uint
	}
	var rows []vpIDRow
	if err := s.store.DB().WithContext(ctx).
		Table("segments").
		Select("DISTINCT voice_profile_id").
		Where("job_id = ? AND voice_profile_id IS NOT NULL", jobID).
		Scan(&rows).Error; err != nil || len(rows) == 0 {
		return 0
	}
	ids := make([]uint, 0, len(rows))
	for _, r := range rows {
		if r.VoiceProfileID != nil {
			ids = append(ids, *r.VoiceProfileID)
		}
	}
	if len(ids) == 0 {
		return 0
	}
	var profiles []models.VoiceProfile
	if err := s.store.DB().WithContext(ctx).
		Where("id IN ? AND est_chars_per_sec IS NOT NULL", ids).
		Find(&profiles).Error; err != nil || len(profiles) == 0 {
		return 0
	}
	var sum float64
	var count int
	for _, vp := range profiles {
		if vp.EstCharsPerSec != nil && *vp.EstCharsPerSec > 0 {
			sum += *vp.EstCharsPerSec
			count++
		}
	}
	if count == 0 {
		return 0
	}
	return sum / float64(count)
}

// TTS stage handlers (runTTSDuration, processOneTTSSegment) live in stage_tts.go.
// They are package-level methods on *Service so file boundary is purely organisational.

// mergeVoiceKey returns a deterministic directory key that reflects which
// voice profile(s) were used to synthesise the segments being merged.
// Single profile → "vp2"; mixed → "vp1_vp2"; all default → "vp0".
// This ensures re-running merge with a different voice profile produces a
// separate output file instead of overwriting the previous result.
func mergeVoiceKey(segments []models.Segment) string {
	seen := map[uint]struct{}{}
	for _, seg := range segments {
		if seg.VoiceProfileID != nil {
			seen[*seg.VoiceProfileID] = struct{}{}
		} else {
			seen[0] = struct{}{}
		}
	}
	ids := make([]uint, 0, len(seen))
	for id := range seen {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	parts := make([]string, len(ids))
	for i, id := range ids {
		parts[i] = fmt.Sprintf("vp%d", id)
	}
	return strings.Join(parts, "_")
}

func (s *Service) runMerge(ctx context.Context, task models.TaskPayload) error {
	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}
	segments, err := s.store.ListSegmentsForMerge(ctx, job.ID)
	if err != nil {
		return err
	}
	if len(segments) == 0 {
		return errors.New("no synthesized segments to merge")
	}

	var totalDurationMs int64
	overlays := make([]media.AudioOverlay, 0, len(segments))
	for i, segment := range segments {
		if segment.EndMs > totalDurationMs {
			totalDurationMs = segment.EndMs
		}
		// MaxDurationMs = available slot from this segment's start to the next
		// segment's start, minus a 300ms breath margin.
		// ffmpeg atrim clips to this value so borrowed-gap audio plays fully but
		// never bleeds into the following sentence.
		const breathMarginMs int64 = 300
		var maxDurationMs int64
		if i+1 < len(segments) {
			if slotMs := segments[i+1].StartMs - segment.StartMs - breathMarginMs; slotMs > 0 {
				maxDurationMs = slotMs
			}
		}
		overlays = append(overlays, media.AudioOverlay{
			RelPath:       segment.TTSAudioRelPath,
			DelayMs:       segment.StartMs,
			DurationMs:    segment.TTSDurationMs,
			MaxDurationMs: maxDurationMs,
		})
	}
	if job.BgmRelPath != "" {
		if bgmDurationMs, err := media.ProbeDurationMs(s.cfg.DataRoot, s.cfg.FFprobeBin, job.BgmRelPath); err == nil && bgmDurationMs > totalDurationMs {
			totalDurationMs = bgmDurationMs
		}
	}
	if inputDurationMs, err := media.ProbeDurationMs(s.cfg.DataRoot, s.cfg.FFprobeBin, job.InputRelPath); err == nil && inputDurationMs > totalDurationMs {
		totalDurationMs = inputDurationMs
	}

	// OPT-403: route output paths through the unified layout
	// (episodes/{ep_id}/chapters/vp{vp}/ch{ord:02d}.{wav,mp4}) when the
	// parent Episode is on layout v2. Falls back to the legacy
	// jobs/{id}/output/... layout for v1 episodes (the 138 historical rows
	// pre-back-fill).
	dubRelPath, finalRelPath := s.chapterMergeOutputPaths(ctx, job, segments)
	if err := media.RenderDubTrack(s.cfg.DataRoot, s.cfg.FFmpegBin, dubRelPath, totalDurationMs, job.BgmRelPath, overlays); err != nil {
		return fmt.Errorf("render dub track: %w", err)
	}

	// OPT-403 chapter-level EBU R128 normalisation. Runs on the dub track
	// BEFORE mux so the final.mp4 carries already-normalised audio. The
	// normalised file replaces the raw dub track on success; on failure
	// the raw dub track stays in place so the mux step still produces a
	// usable final.mp4 (logged as a soft warning).
	if s.cfg.LoudnormChapterEnabled {
		if stats, err := s.runChapterLoudnorm(ctx, dubRelPath); err != nil {
			slog.Warn("opt-403 chapter loudnorm failed; using un-normalised dub track",
				"job_id", job.ID, "error", err)
		} else {
			s.persistChapterLoudnormStats(ctx, job, stats)
		}
	}

	if media.IsVideoFile(job.InputRelPath) {
		if err := media.MuxVideo(s.cfg.DataRoot, s.cfg.FFmpegBin, job.InputRelPath, dubRelPath, finalRelPath); err != nil {
			return fmt.Errorf("mux final video: %w", err)
		}
		job.OutputRelPath = finalRelPath
	} else {
		job.OutputRelPath = dubRelPath
	}
	if err := s.store.SaveJob(ctx, job); err != nil {
		return err
	}
	// OPT-409 chapter-level judge. Asynchronous, observe-only, never blocks
	// or fails the pipeline. Uses a detached background context internally
	// so a worker shutdown signal does not silently lose the verdict mid-
	// flight (mirrors maybeJudgeSegmentAsync). MUST run BEFORE
	// maybeEnqueueEpisodeMerge so its goroutine starts even when the
	// episode merge is enqueued in the same task batch.
	s.maybeJudgeChapterAsync(job, segments)
	// OPT-404 trigger: chapter just finished merging. If every chapter under
	// this episode is completed, kick off ep_episode_merge so the unified-
	// layout episode-level final + chapters.json get written. The handler
	// is idempotent if already enqueued.
	s.maybeEnqueueEpisodeMerge(ctx, job.EpisodeID)
	return nil
}

// chapterMergeOutputPaths returns (dubTrackRelPath, finalVideoRelPath) for
// runMerge, picking the OPT-403 unified layout when the parent Episode is on
// OutputLayoutVersion=2 and the legacy jobs/{id}/output/{voiceKey}/... layout
// otherwise. Single source of truth so future layout changes only touch
// here, not the runMerge body.
func (s *Service) chapterMergeOutputPaths(
	ctx context.Context,
	job *models.Job,
	segments []models.Segment,
) (dubRelPath, finalRelPath string) {
	voiceKey := mergeVoiceKey(segments)
	primaryVP := primaryVoiceProfileID(segments)

	if job.EpisodeID != 0 {
		ep, err := s.store.GetEpisode(ctx, job.EpisodeID)
		if err == nil && ep != nil && ep.OutputLayoutVersion >= 2 {
			finalRelPath = ep.GetChapterOutputRelPath(job.ChapterOrdinal, primaryVP)
			// dub_track sits next to the final mp4 in the unified layout.
			dubRelPath = filepath.ToSlash(filepath.Join(
				filepath.Dir(finalRelPath),
				fmt.Sprintf("ch%02d.dub_track.wav", job.ChapterOrdinal),
			))
			return dubRelPath, finalRelPath
		}
	}
	// Legacy layout (output_layout_version=1 episodes + standalone jobs).
	dubRelPath = fmt.Sprintf("jobs/%d/output/%s/dub_track.wav", job.ID, voiceKey)
	finalRelPath = fmt.Sprintf("jobs/%d/output/%s/final.mp4", job.ID, voiceKey)
	return dubRelPath, finalRelPath
}

// primaryVoiceProfileID picks the dominant voice profile across the segments.
// Uses the lowest non-zero ID when multiple appear (deterministic, matches
// mergeVoiceKey ordering); falls back to 0 ("default voice") when no segment
// has a voice profile assigned.
func primaryVoiceProfileID(segments []models.Segment) uint {
	seen := map[uint]int{}
	for _, seg := range segments {
		if seg.VoiceProfileID != nil {
			seen[*seg.VoiceProfileID]++
		} else {
			seen[0]++
		}
	}
	var best uint
	var bestCount int
	for id, count := range seen {
		if count > bestCount || (count == bestCount && id < best) {
			best = id
			bestCount = count
		}
	}
	return best
}

// runChapterLoudnorm runs LoudnormTwoPass on the dub track and replaces the
// original wav with the normalised version. Returns the measured stats so
// the caller can persist them to Episode.LoudnormStats.
//
// On any failure the original dub track is left untouched (so the mux step
// still produces a usable final.mp4) and the error is returned for logging.
func (s *Service) runChapterLoudnorm(
	ctx context.Context,
	dubRelPath string,
) (media.LoudnormStats, error) {
	dubAbs := storage.ResolveDataPath(s.cfg.DataRoot, dubRelPath)
	tmpAbs := dubAbs + ".loudnorm.m4a"
	stats, err := media.LoudnormTwoPass(ctx, s.cfg.FFmpegBin, dubAbs, tmpAbs,
		s.cfg.LoudnormTargetI, s.cfg.LoudnormTargetTP, s.cfg.LoudnormTargetLRA)
	if err != nil {
		_ = os.Remove(tmpAbs)
		return stats, err
	}
	// Convert the normalised m4a back to the same wav format runMerge
	// expects downstream (24 kHz mono, matches RenderDubTrack output) so
	// MuxVideo + ffmpeg consumers don't need to know loudnorm happened.
	if err := convertM4AToWav(ctx, s.cfg.FFmpegBin, tmpAbs, dubAbs); err != nil {
		_ = os.Remove(tmpAbs)
		return stats, fmt.Errorf("re-encode normalised dub track to wav: %w", err)
	}
	_ = os.Remove(tmpAbs)
	return stats, nil
}

// convertM4AToWav re-encodes an AAC m4a back to PCM wav at the dub track's
// target sample rate / channel count. Used after the loudnorm pass to keep
// the rest of the merge pipeline format-agnostic.
func convertM4AToWav(ctx context.Context, ffmpegBin, inAbs, outAbs string) error {
	args := []string{
		"-y",
		"-i", inAbs,
		"-ar", "24000",
		"-ac", "1",
		"-acodec", "pcm_s16le",
		outAbs,
	}
	out, err := exec.CommandContext(ctx, ffmpegBin, args...).CombinedOutput()
	if err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return ctxErr
		}
		return fmt.Errorf("%w\n%s", err, string(out))
	}
	return nil
}

// persistChapterLoudnormStats merges the chapter's measured stats into
// Episode.LoudnormStats under vp{primary}/ch{ordinal:02d}. Best-effort —
// stats are descriptive only (UI / future chapter judge surface), the
// pipeline never reads them back.
func (s *Service) persistChapterLoudnormStats(
	ctx context.Context,
	job *models.Job,
	stats media.LoudnormStats,
) {
	if job.EpisodeID == 0 {
		return
	}
	// Flatten the (vp, chapter) key into a single top-level entry so the
	// shallow `||` merge in store.UpdateLoudnormStats never collides with
	// the episode-merge master pass (which writes "vp{N}_master").
	chapterKey := fmt.Sprintf("vp%d_ch%02d", primaryVoiceProfileIDOnJob(job), job.ChapterOrdinal)
	statsJSON, err := json.Marshal(map[string]any{chapterKey: stats})
	if err != nil {
		slog.Warn("persistChapterLoudnormStats: marshal stats failed",
			"job_id", job.ID, "error", err)
		return
	}
	if err := s.store.UpdateLoudnormStats(ctx, job.EpisodeID, statsJSON, true); err != nil {
		slog.Warn("persistChapterLoudnormStats: persist stats failed",
			"job_id", job.ID, "episode_id", job.EpisodeID, "error", err)
	}
}

// primaryVoiceProfileIDOnJob is a Job-level analogue of primaryVoiceProfileID.
// Scans the segments lazily through the store to keep the loudnorm path
// independent of runMerge's already-loaded segments slice.
func primaryVoiceProfileIDOnJob(job *models.Job) uint {
	// Heuristic: most jobs have a single voice profile. Sniff the job's
	// first segment via the preloaded slice if present; fall back to 0.
	if len(job.Segments) > 0 {
		return primaryVoiceProfileID(job.Segments)
	}
	return 0
}

// PreviewVoice synthesizes a single segment with the specified voice profile
// without persisting any results to the database. The output is written to
// data/preview/job_{jobID}_seg_{segID}_vp_{vpID}.wav and the relpath is returned.
func (s *Service) PreviewVoice(ctx context.Context, jobID uint, seg models.Segment, profile models.VoiceProfile) (audioRelPath string, actualDurationMs int64, err error) {
	voiceConfig, err := buildVoiceConfig(profile)
	if err != nil {
		return "", 0, fmt.Errorf("build voice config: %w", err)
	}

	previewDir := filepath.Join(s.cfg.DataRoot, "preview")
	if mkErr := os.MkdirAll(previewDir, 0755); mkErr != nil {
		return "", 0, fmt.Errorf("create preview dir: %w", mkErr)
	}

	outputRelPath := fmt.Sprintf("preview/job_%d_seg_%d_vp_%d.wav", jobID, seg.ID, profile.ID)

	targetMs := seg.DurationMs()
	targetSec := float64(targetMs) / 1000.0
	maxAllowedSec := targetSec + 5.0

	text := seg.TargetText
	if text == "" {
		text = seg.SourceText
	}

	resp, ttsErr := s.ml.RunTTS(ctx, ml.TTSRequest{
		Text:              text,
		TargetDurationSec: targetSec,
		MaxAllowedSec:     maxAllowedSec,
		VoiceConfig:       voiceConfig,
		OutputRelPath:     outputRelPath,
	})
	if ttsErr != nil {
		return "", 0, fmt.Errorf("preview tts: %w", ttsErr)
	}
	return outputRelPath, resp.ActualDurationMs, nil
}

func buildVoiceConfig(profile models.VoiceProfile) (map[string]any, error) {
	config := map[string]any{
		"name":                profile.Name,
		"mode":                profile.Mode,
		"provider":            profile.Provider,
		"language":            profile.Language,
		"checkpoint_relpath":  profile.CheckpointRelPath,
		"index_relpath":       profile.IndexRelPath,
		"config_relpath":      profile.ConfigRelPath,
		"internal_speaker_id": profile.InternalSpeakerID,
	}
	if len(profile.Meta) > 0 {
		config["meta"] = profile.Meta
	}
	if len(profile.SampleRelPaths) > 0 {
		var samples []string
		if err := json.Unmarshal(profile.SampleRelPaths, &samples); err != nil {
			return nil, err
		}
		config["sample_relpaths"] = samples
	}
	return config, nil
}

func jobConfigFloat(configMap map[string]any, key string, fallback float64) float64 {
	if configMap == nil {
		return fallback
	}
	raw, ok := configMap[key]
	if !ok {
		return fallback
	}
	switch value := raw.(type) {
	case float64:
		return value
	case int:
		return float64(value)
	case int64:
		return float64(value)
	default:
		return fallback
	}
}

func jobConfigInt(configMap map[string]any, key string, fallback int) int {
	if configMap == nil {
		return fallback
	}
	raw, ok := configMap[key]
	if !ok {
		return fallback
	}
	switch value := raw.(type) {
	case float64:
		return int(value)
	case int:
		return value
	case int64:
		return int(value)
	default:
		return fallback
	}
}

func (s *Service) stageTimeoutForJob(job models.Job) time.Duration {
	overrideSeconds := jobConfigInt(job.Config, "stage_timeout_sec", 0)
	if overrideSeconds > 0 {
		return time.Duration(overrideSeconds) * time.Second
	}
	return s.cfg.StageTimeout
}

func (s *Service) effectiveMaxRetries(job models.Job) int {
	if job.MaxRetries > 0 {
		return job.MaxRetries
	}
	return s.cfg.MaxJobRetries
}

func (s *Service) createStageRun(ctx context.Context, task models.TaskPayload) (*models.JobStageRun, error) {
	segmentIDs, err := json.Marshal(task.SegmentIDs)
	if err != nil {
		return nil, err
	}
	run := &models.JobStageRun{
		JobID:       task.JobID,
		Stage:       task.Stage,
		Attempt:     task.Attempt,
		Status:      "running",
		RequestedBy: task.RequestedBy,
		Reason:      task.Reason,
		WorkerID:    s.cfg.WorkerID,
		SegmentIDs:  datatypes.JSON(segmentIDs),
		StartedAt:   time.Now().UTC(),
	}
	if err := s.store.CreateStageRun(ctx, run); err != nil {
		return nil, err
	}
	return run, nil
}

func (s *Service) handleStageFailure(ctx context.Context, job models.Job, task models.TaskPayload, stageRunID uint, finalStatus models.JobStatus, stageErr error, duration time.Duration) error {
	observability.ObserveStageRun(string(task.Stage), string(finalStatus), duration)
	_ = s.store.FinishStageRun(ctx, stageRunID, string(finalStatus), stageErr.Error(), duration.Milliseconds(), map[string]any{
		"attempt": task.Attempt,
	})

	if finalStatus == models.JobStatusCancelled {
		_ = s.store.UpdateJobState(ctx, task.JobID, models.JobStatusCancelled, task.Stage, stageErr.Error(), false)
		_ = s.notifyJobEvent(ctx, job, "job.cancelled", task, map[string]any{"error": stageErr.Error()})
		return nil
	}

	if task.Attempt < s.effectiveMaxRetries(job) {
		delay := s.retryDelay(task.Attempt)
		retryTask := task
		retryTask.Attempt++
		if err := s.store.UpdateJobState(ctx, task.JobID, models.JobStatusQueued, task.Stage, stageErr.Error(), true); err != nil {
			return err
		}
		if err := s.queue.EnqueueWithDelay(ctx, retryTask, delay); err != nil {
			return err
		}
		_ = s.notifyJobEvent(ctx, job, "stage.retry_scheduled", retryTask, map[string]any{
			"delay_ms": delay.Milliseconds(),
			"error":    stageErr.Error(),
		})
		return nil
	}

	if err := s.store.UpdateJobState(ctx, task.JobID, finalStatus, task.Stage, stageErr.Error(), false); err != nil {
		return err
	}
	// OPT-401 1-chapter shortcut: mirror the only chapter's terminal failure
	// up to the parent Episode so dashboards / GET /episodes reflect the
	// real state. Multi-chapter episodes are deliberately skipped: OPT-407
	// rework engine decides episode-wide outcomes from per-chapter signals.
	s.maybeShortcutEpisodeFailed(ctx, job, stageErr.Error())
	if err := s.queue.EnqueueDeadLetter(ctx, task, stageErr.Error()); err == nil {
		observability.IncDeadLetters()
	}
	_ = s.notifyJobEvent(ctx, job, "stage.failed", task, map[string]any{
		"error": stageErr.Error(),
	})
	return nil
}

// maybeShortcutEpisodeCompleted is called from the chapter HandleTask end
// when a chapter Job reaches Completed. It is the convergence point for
// both Episode lifecycle paths:
//
//   - 1-chapter episode: transition pending → running (if needed), then
//     enqueue ep_episode_merge so the unified-layout final.mp4 +
//     chapters.json get materialised. Episode → Completed happens inside
//     runEpisodeMerge after the disk I/O succeeds.
//   - N-chapter episode: only fire when EVERY chapter is completed —
//     maybeEnqueueEpisodeMerge enforces that check internally.
//
// When EpisodeMergeEnabled=false (legacy compatibility), the function
// falls back to the OPT-401 behaviour: directly stamp the 1-chapter
// Episode completed without producing the unified manifest.
//
// Errors are swallowed throughout — the parent job-completion path stays
// the source of truth; episode bookkeeping must never fail the job.
func (s *Service) maybeShortcutEpisodeCompleted(ctx context.Context, job models.Job) {
	if job.EpisodeID == 0 {
		return
	}
	ep, err := s.store.GetEpisode(ctx, job.EpisodeID)
	if err != nil || ep == nil || ep.Status.IsTerminal() {
		return
	}

	// Pending → Running on first chapter completion (regardless of count).
	if ep.Status == models.EpisodeStatusPending {
		if err := s.store.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusRunning, ""); err != nil {
			slog.Warn("episode shortcut pending->running failed",
				"episode_id", ep.ID, "error", err)
			// Do NOT bail; we still want to enqueue merge if every chapter
			// is done.
		}
	}

	// OPT-403/404: route through ep_episode_merge so 1-chapter and N-chapter
	// episodes both end up with episodes/{ep_id}/output/vp{vp}/final.mp4 +
	// chapters.json on disk. The merge handler is the sole agent that flips
	// Episode → Completed once the disk write succeeds.
	if s.cfg.EpisodeMergeEnabled {
		s.maybeEnqueueEpisodeMerge(ctx, ep.ID)
		return
	}

	// Legacy fall-back when ep_episode_merge is disabled: replicate the
	// OPT-401 behaviour for 1-chapter episodes only. Multi-chapter episodes
	// stay in Running until the operator manually re-enables merging.
	if ep.TotalChapters == 1 {
		if err := s.store.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusCompleted, ""); err != nil {
			slog.Warn("episode shortcut ->completed failed",
				"episode_id", ep.ID, "error", err)
		}
	}
}

// maybeShortcutEpisodeFailed mirrors the chapter Job's terminal failure
// into the parent 1-chapter Episode. Like the completed counterpart, it
// is best-effort and never propagates an error to the caller so worker
// retry / dead-letter logic stays unchanged.
func (s *Service) maybeShortcutEpisodeFailed(ctx context.Context, job models.Job, errMsg string) {
	if job.EpisodeID == 0 {
		return
	}
	ep, err := s.store.GetEpisode(ctx, job.EpisodeID)
	if err != nil || ep == nil || ep.TotalChapters > 1 || ep.Status.IsTerminal() {
		return
	}
	// pending -> failed and running -> failed are both legal.
	if err := s.store.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusFailed, errMsg); err != nil {
		slog.Warn("episode shortcut ->failed failed",
			"episode_id", ep.ID, "error", err)
	}
}

func (s *Service) retryDelay(attempt int) time.Duration {
	multiplier := 1 << attempt
	return time.Duration(multiplier) * s.cfg.RetryBaseDelay
}

func (s *Service) notifyJobEvent(ctx context.Context, job models.Job, event string, task models.TaskPayload, meta map[string]any) error {
	status := job.Status
	switch event {
	case "stage.started":
		status = models.JobStatusRunning
	case "stage.retry_scheduled":
		status = models.JobStatusQueued
	case "stage.failed":
		status = models.JobStatusFailed
	case "job.completed":
		status = models.JobStatusCompleted
	case "job.cancelled":
		status = models.JobStatusCancelled
	}
	payload := webhook.EventPayload{
		Event:         event,
		JobID:         job.ID,
		TenantKey:     job.TenantKey,
		Status:        status,
		Stage:         task.Stage,
		Attempt:       task.Attempt,
		OutputRelPath: job.OutputRelPath,
		ErrorMessage:  job.ErrorMessage,
		Timestamp:     time.Now().UTC(),
		Meta:          meta,
	}
	if err := s.notifier.Notify(ctx, job, payload); err != nil {
		slog.Warn("webhook notification failed",
			"job_id", job.ID,
			"event", event,
			"error", err,
		)
		return err
	}
	return nil
}

func (s *Service) outputRelPathForJob(ctx context.Context, jobID uint) string {
	job, err := s.store.GetJob(ctx, jobID)
	if err != nil {
		return ""
	}
	return job.OutputRelPath
}

// handleEpisodeStage dispatches OPT-402 episode-level tasks to their
// stage handlers. Compared to chapter HandleTask this path is
// deliberately leaner: no per-stage lease, no JobStatus mutations, no
// stage-run rows. Episode stages are short, idempotent and best-effort
// (a glossary failure leaves the column empty and the chapter pipeline
// keeps running with the legacy summary).
//
// We DO record metrics + log so dashboards can spot episode-stage
// regressions independently from chapter stage_runs. If/when OPT-403
// fan-out makes episode stages long-running, lift this path back into
// the same lease/stage_run scaffolding chapter stages use.
func (s *Service) handleEpisodeStage(ctx context.Context, task models.TaskPayload) error {
	startedAt := time.Now()
	stageCtx, cancel := context.WithTimeout(ctx, s.cfg.StageTimeout)
	defer cancel()

	var stageErr error
	switch task.EpisodeStage {
	case models.EpisodeStageGlossaryExtract:
		stageErr = s.runEpisodeGlossaryExtract(stageCtx, task)
	case models.EpisodeStageChapterize:
		stageErr = s.runEpisodeChapterize(stageCtx, task)
	case models.EpisodeStageEpisodeMerge:
		stageErr = s.runEpisodeMerge(stageCtx, task)
	case models.EpisodeStageGlossaryBroadcast:
		// OPT-407 episode-level rework action. Triggered on demand by
		// rework.Engine, NEVER part of the canonical EpisodeStageOrder.
		stageErr = s.runEpisodeGlossaryBroadcast(stageCtx, task)
	default:
		stageErr = fmt.Errorf("unsupported episode stage %q", task.EpisodeStage)
	}
	duration := time.Since(startedAt)
	observability.ObserveStageRun(string(task.EpisodeStage), statusFromErr(stageErr), duration)
	if stageErr != nil {
		slog.Warn("episode-stage failed",
			"episode_id", task.EpisodeID,
			"stage", task.EpisodeStage,
			"duration_ms", duration.Milliseconds(),
			"error", stageErr,
		)
		return stageErr
	}
	slog.Info("episode-stage completed",
		"episode_id", task.EpisodeID,
		"stage", task.EpisodeStage,
		"duration_ms", duration.Milliseconds(),
	)
	return nil
}

// statusFromErr converts a stage error to the status label used by
// holodub_stage_run_total. nil → "completed"; ctx-deadline → "timed_out";
// everything else → "failed". Matches the chapter-stage convention.
func statusFromErr(err error) string {
	if err == nil {
		return "completed"
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timed_out"
	}
	return "failed"
}
