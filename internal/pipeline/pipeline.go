package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"os"
	"path/filepath"
	"time"

	"holodub/internal/config"
	"holodub/internal/llm"
	"holodub/internal/media"
	"holodub/internal/ml"
	"holodub/internal/models"
	"holodub/internal/observability"
	"holodub/internal/queue"
	"holodub/internal/storage"
	"holodub/internal/store"
	"holodub/internal/webhook"

	"gorm.io/datatypes"
	"gorm.io/gorm"
)

type Service struct {
	cfg      config.Config
	store    *store.Store
	queue    *queue.Queue
	ml       *ml.Client
	llm      *llm.Client
	notifier *webhook.Notifier
}

func NewService(cfg config.Config, st *store.Store, q *queue.Queue, mlClient *ml.Client, llmClient *llm.Client) *Service {
	return &Service{
		cfg:      cfg,
		store:    st,
		queue:    q,
		ml:       mlClient,
		llm:      llmClient,
		notifier: webhook.New(cfg),
	}
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
	return s.queue.Enqueue(ctx, models.TaskPayload{
		JobID:       jobID,
		Stage:       stage,
		Attempt:     0,
		SegmentIDs:  store.UniqueUint(segmentIDs),
		RequestedBy: requestedBy,
		Reason:      "manual_retry",
	})
}

func (s *Service) HandleTask(ctx context.Context, task models.TaskPayload) error {
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
		slog.Info("stage lease already held",
			"job_id", task.JobID,
			"stage", task.Stage,
			"attempt", task.Attempt,
		)
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
	if !hasNext {
		job.OutputRelPath = s.outputRelPathForJob(ctx, task.JobID)
		if err := s.store.UpdateJobState(ctx, task.JobID, models.JobStatusCompleted, task.Stage, "", false); err != nil {
			return err
		}
		_ = s.notifyJobEvent(ctx, *job, "job.completed", task, map[string]any{
			"output_relpath": job.OutputRelPath,
		})
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

	response, err := s.ml.SmartSplit(ctx, ml.SmartSplitRequest{
		AudioRelPath:   audioRelPath,
		SourceLanguage: job.SourceLanguage,
		MinSegmentSec:  jobConfigFloat(job.Config, "min_segment_sec", 4.0),
		MaxSegmentSec:  jobConfigFloat(job.Config, "max_segment_sec", 20.0),
	})
	if err != nil {
		return err
	}
	if len(response.Segments) == 0 {
		return errors.New("smart split returned no segments")
	}

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
	return s.store.ReplaceSegments(ctx, job.ID, drafts)
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
	for idx := range segments {
		targetSec := float64(segments[idx].DurationMs()) / 1000.0
		translated, err := s.llm.TranslateTextWithDuration(ctx, job.SourceLanguage, job.TargetLanguage, segments[idx].SourceText, targetSec)
		if err != nil {
			return fmt.Errorf("translate segment %d: %w", segments[idx].ID, err)
		}
		segments[idx].TargetText = translated
		segments[idx].Status = "translated"
	}
	return s.store.UpdateSegmentTranslations(ctx, segments)
}

func (s *Service) runTTSDuration(ctx context.Context, task models.TaskPayload) error {
	job, err := s.store.GetJob(ctx, task.JobID)
	if err != nil {
		return err
	}
	segments, err := s.store.ListSegments(ctx, job.ID, task.SegmentIDs)
	if err != nil {
		return err
	}
	if len(segments) == 0 {
		return errors.New("no segments available for tts stage")
	}

	for idx := range segments {
		voiceConfig := map[string]any{}
		profile, err := s.store.ResolveVoiceProfileForSegment(ctx, job.ID, segments[idx])
		if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
			return fmt.Errorf("resolve voice profile for segment %d: %w", segments[idx].ID, err)
		}
		if profile != nil {
			voiceConfig, err = buildVoiceConfig(*profile)
			if err != nil {
				return fmt.Errorf("build voice config for segment %d: %w", segments[idx].ID, err)
			}
		}

		targetMs := segments[idx].DurationMs()
		targetSec := float64(targetMs) / 1000.0

		text := segments[idx].TargetText
		if text == "" {
			text = segments[idx].SourceText
		}

		outputRelPath := fmt.Sprintf("jobs/%d/tts/segment-%04d.wav", job.ID, segments[idx].Ordinal)
		maxAttempts := s.cfg.RetranslationMaxAttempts

		var response *ml.TTSResponse
		for attempt := 0; attempt <= maxAttempts; attempt++ {
			response, err = s.ml.RunTTS(ctx, ml.TTSRequest{
				Text:              text,
				TargetDurationSec: targetSec,
				VoiceConfig:       voiceConfig,
				OutputRelPath:     outputRelPath,
			})
			if err != nil {
				return fmt.Errorf("tts segment %d (attempt %d): %w", segments[idx].ID, attempt, err)
			}

			// Check whether re-translation is warranted.
			if !s.cfg.RetranslationEnabled || attempt == maxAttempts {
				break
			}
			drift := math.Abs(float64(response.ActualDurationMs-targetMs)) / float64(targetMs) //nolint:govet
			if drift <= s.cfg.RetranslationDriftThreshold {
				break // Within tolerance — accept result.
			}

			actualMs := response.ActualDurationMs
			slog.Info("tts duration drift exceeds threshold, re-translating",
				"job_id", job.ID,
				"segment_id", segments[idx].ID,
				"target_ms", targetMs,
				"actual_ms", actualMs,
				"drift_pct", fmt.Sprintf("%.1f%%", drift*100),
				"attempt", attempt+1,
				"max_attempts", maxAttempts,
			)

			newText, retErr := s.llm.RetranslateWithConstraint(
				ctx,
				job.SourceLanguage, job.TargetLanguage,
				segments[idx].SourceText, text,
				targetSec, float64(actualMs)/1000.0,
				attempt+1, maxAttempts,
			)
			if retErr != nil {
				// Non-fatal: log and accept the TTS result with atempo fallback.
				slog.Warn("re-translation failed, keeping current result",
					"job_id", job.ID,
					"segment_id", segments[idx].ID,
					"error", retErr,
				)
				break
			}

			text = newText
			segments[idx].TargetText = newText
			// Persist the improved translation so the segment history is accurate.
			if saveErr := s.store.UpdateSegmentTranslations(ctx, []models.Segment{segments[idx]}); saveErr != nil {
				slog.Warn("failed to persist re-translated text",
					"job_id", job.ID,
					"segment_id", segments[idx].ID,
					"error", saveErr,
				)
			}
		}

		segments[idx].TTSAudioRelPath = response.AudioRelPath
		segments[idx].TTSDurationMs = response.ActualDurationMs
		segments[idx].Status = "synthesized"
	}

	return s.store.UpdateSegmentSynthResults(ctx, segments)
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
	for _, segment := range segments {
		if segment.EndMs > totalDurationMs {
			totalDurationMs = segment.EndMs
		}
		overlays = append(overlays, media.AudioOverlay{
			RelPath:    segment.TTSAudioRelPath,
			DelayMs:    segment.StartMs,
			DurationMs: segment.TTSDurationMs,
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

	dubTrackRelPath := fmt.Sprintf("jobs/%d/output/dub_track.wav", job.ID)
	if err := media.RenderDubTrack(s.cfg.DataRoot, s.cfg.FFmpegBin, dubTrackRelPath, totalDurationMs, job.BgmRelPath, overlays); err != nil {
		return fmt.Errorf("render dub track: %w", err)
	}

	if media.IsVideoFile(job.InputRelPath) {
		outputRelPath := fmt.Sprintf("jobs/%d/output/final.mp4", job.ID)
		if err := media.MuxVideo(s.cfg.DataRoot, s.cfg.FFmpegBin, job.InputRelPath, dubTrackRelPath, outputRelPath); err != nil {
			return fmt.Errorf("mux final video: %w", err)
		}
		job.OutputRelPath = outputRelPath
	} else {
		job.OutputRelPath = dubTrackRelPath
	}
	return s.store.SaveJob(ctx, job)
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
	if err := s.queue.EnqueueDeadLetter(ctx, task, stageErr.Error()); err == nil {
		observability.IncDeadLetters()
	}
	_ = s.notifyJobEvent(ctx, job, "stage.failed", task, map[string]any{
		"error": stageErr.Error(),
	})
	return nil
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
