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
	"sort"
	"strings"
	"sync"
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

func (s *Service) MLHealth(ctx context.Context) (map[string]any, error) {
	return s.ml.Health(ctx)
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
		JobID:           jobID,
		Stage:           stage,
		Attempt:         0,
		SegmentIDs:      store.UniqueUint(segmentIDs),
		RequestedBy:     requestedBy,
		Reason:          "manual_retry",
		SkipAutoAdvance: true,
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

	// Determine the voice-profile speaking-rate hint to pass to the LLM.
	// Query distinct VPs already assigned to this job's segments; if any have
	// a calibrated EstCharsPerSec, use their average as the translation hint.
	charsPerSecHint := s.voiceRateHintForJob(ctx, job.ID, job.TargetLanguage)

	for idx := range segments {
		targetSec := float64(segments[idx].DurationMs()) / 1000.0
		translated, err := s.llm.TranslateTextWithDuration(ctx, job.SourceLanguage, job.TargetLanguage, segments[idx].SourceText, targetSec, charsPerSecHint)
		if err != nil {
			return fmt.Errorf("translate segment %d: %w", segments[idx].ID, err)
		}
		segments[idx].TargetText = translated
		segments[idx].Status = "translated"
	}
	if err := s.store.UpdateSegmentTranslations(ctx, segments); err != nil {
		return err
	}

	// After all translations are saved, generate a compact episode reference card.
	// The summary is stored on the job and injected into every subsequent TTS
	// retranslation prompt to maintain global coherence (terminology, register, etc.).
	// We sample up to 30 segments spread evenly across the episode.
	sample := buildTranslationSample(segments, 30)
	if len(sample) > 0 {
		summary, sumErr := s.llm.SummarizeTranslation(ctx, job.SourceLanguage, job.TargetLanguage, sample)
		if sumErr != nil {
			// Non-fatal: log and continue without summary.
			slog.Warn("failed to generate translation summary",
				"job_id", job.ID, "error", sumErr)
		} else if summary != "" {
			if storeErr := s.store.UpdateJobTranslationSummary(ctx, job.ID, summary); storeErr != nil {
				slog.Warn("failed to store translation summary",
					"job_id", job.ID, "error", storeErr)
			} else {
				slog.Info("translation summary generated",
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

	// Pipeline-triggered synthesis uses stricter thresholds and more attempts.
	isInitial := task.Reason == "translate_completed"

	concurrency := s.cfg.TTSConcurrency
	if concurrency < 1 {
		concurrency = 1
	}
	sem := make(chan struct{}, concurrency)
	var wg sync.WaitGroup
	var firstErr error
	var errMu sync.Mutex
	var processedIdx []int
	var processedMu sync.Mutex

	for idx := range segments {
		if segments[idx].Status == "synthesized" {
			continue
		}
		idx := idx
		wg.Add(1)
		go func() {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			if err := s.processOneTTSSegment(ctx, job, segments, idx, isInitial); err != nil {
				errMu.Lock()
				if firstErr == nil {
					firstErr = err
				}
				errMu.Unlock()
			} else {
				processedMu.Lock()
				processedIdx = append(processedIdx, idx)
				processedMu.Unlock()
			}
		}()
	}
	wg.Wait()

	if firstErr != nil {
		return firstErr
	}
	if len(processedIdx) == 0 {
		return nil
	}
	processed := make([]models.Segment, 0, len(processedIdx))
	for _, idx := range processedIdx {
		processed = append(processed, segments[idx])
	}

	// Update each voice profile's empirical speaking rate from this batch.
	// Group synthesized segments by VP and compute the average chars/sec.
	vpRateAccum := map[uint][]float64{}
	for _, idx := range processedIdx {
		seg := segments[idx]
		if seg.TTSDurationMs <= 0 || seg.TargetText == "" {
			continue
		}
		chars := len([]rune(seg.TargetText))
		if chars == 0 {
			continue
		}
		durationSec := float64(seg.TTSDurationMs) / 1000.0
		rate := float64(chars) / durationSec
		var vpID uint = 0
		if seg.VoiceProfileID != nil {
			vpID = *seg.VoiceProfileID
		}
		vpRateAccum[vpID] = append(vpRateAccum[vpID], rate)
	}
	for vpID, rates := range vpRateAccum {
		if vpID == 0 {
			continue // skip default (nil) voice — no VP record to update
		}
		var sum float64
		for _, r := range rates {
			sum += r
		}
		avgRate := sum / float64(len(rates))
		if updateErr := s.store.UpdateVoiceProfileSpeakingRate(context.Background(), vpID, avgRate, s.cfg.VoiceProfileRateAlpha); updateErr != nil {
			slog.Warn("failed to update voice profile speaking rate",
				"vp_id", vpID, "avg_rate", avgRate, "error", updateErr)
		}
	}

	return s.store.UpdateSegmentSynthResults(ctx, processed)
}

func (s *Service) processOneTTSSegment(ctx context.Context, job *models.Job, segments []models.Segment, idx int, isInitial bool) error {
	seg := &segments[idx]
	voiceConfig := map[string]any{}
	profile, err := s.store.ResolveVoiceProfileForSegment(ctx, job.ID, *seg)
	if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
		return fmt.Errorf("resolve voice profile for segment %d: %w", seg.ID, err)
	}
	if profile != nil {
		voiceConfig, err = buildVoiceConfig(*profile)
		if err != nil {
			return fmt.Errorf("build voice config for segment %d: %w", seg.ID, err)
		}
	}

	targetMs := seg.DurationMs()
	targetSec := float64(targetMs) / 1000.0

	// Gap between this segment's end and the next segment's start.
	// Used for overflow policy: TTS audio that overruns the target slot can
	// "borrow" from the trailing silence up to (gap - breathMarginMs).
	var gapAfterMs int64 = 30_000
	if idx+1 < len(segments) {
		if gap := segments[idx+1].StartMs - seg.EndMs; gap >= 0 {
			gapAfterMs = gap
		} else {
			gapAfterMs = 0
		}
	}

	// breathMarginMs: minimum silence to preserve between sentences even when
	// borrowing from the gap.  300 ms is the shortest perceptible breath pause.
	const breathMarginMs int64 = 300
	// shortGapThresholdMs: when the gap is at or below this value the TTS slot
	// is already dangerously tight — skip straight to forced re-translation on
	// any overflow instead of trying to borrow.
	const shortGapThresholdMs int64 = 1000
	// maxAllowedSec caps the token budget inside the TTS model.  It is the full
	// slot: target + entire gap.  Physical playback clipping is handled later in
	// the merge stage via ffmpeg atrim.
	maxAllowedSec := targetSec + float64(gapAfterMs)/1000.0

	text := seg.TargetText
	if text == "" {
		text = seg.SourceText
	}

	var vpID uint = 0
	if profile != nil {
		vpID = profile.ID
	}
	outputRelPath := fmt.Sprintf("jobs/%d/tts/vp%d/segment-%04d.wav", job.ID, vpID, seg.Ordinal)

	// isInitial uses a stricter drift ceiling and more retranslation attempts;
	// manual retries keep the original (looser) settings for quick tweaks.
	absMaxDriftSec := s.cfg.RetranslationAbsMaxDriftSec
	maxAttempts := s.cfg.RetranslationMaxAttempts
	if isInitial {
		absMaxDriftSec = s.cfg.RetranslationInitialAbsMaxDriftSec
		maxAttempts = s.cfg.RetranslationInitialMaxAttempts
	}

	driftThreshold := s.cfg.RetranslationDriftThreshold
	// Effective threshold: stricter of the relative % or the absolute-seconds cap,
	// but never below the minimum relative floor (prevents impossibly strict targets
	// for very long segments, e.g. 0.8s / 105.9s = 0.76% is unreachable).
	if absMaxDriftSec > 0 && targetSec > 0 {
		absThreshold := absMaxDriftSec / targetSec
		if absThreshold < driftThreshold {
			driftThreshold = absThreshold
		}
	}
	if s.cfg.RetranslationMinDriftThreshold > 0 && driftThreshold < s.cfg.RetranslationMinDriftThreshold {
		driftThreshold = s.cfg.RetranslationMinDriftThreshold
	}

	// Voice-adaptive speaking rate: start from VP's calibrated rate if available,
	// then update from empirical TTS results within this segment's retry loop.
	var observedCharsPerSec float64
	if profile != nil && profile.EstCharsPerSec != nil && *profile.EstCharsPerSec > 0 {
		observedCharsPerSec = *profile.EstCharsPerSec
	}
	var totalObsChars int
	var totalObsSec float64

	// Build the local context window (prev 2 segments) and forward hint (next 1 source).
	// These are passed to every retranslation call to improve coherence and natural flow.
	var contextBefore []llm.ContextSegment
	for i := max(0, idx-2); i < idx; i++ {
		if segments[i].SourceText != "" && segments[i].TargetText != "" {
			contextBefore = append(contextBefore, llm.ContextSegment{
				SrcText: segments[i].SourceText,
				TgtText: segments[i].TargetText,
			})
		}
	}
	var nextSrcText string
	if idx+1 < len(segments) {
		nextSrcText = segments[idx+1].SourceText
	}

	// Non-convergence and best-result tracking.
	var bestAbsDrift float64 = math.MaxFloat64
	var bestText string
	var bestActualMs int64
	var attemptsWithoutImprovement int
	nonConvergenceWindow := s.cfg.RetranslationNonConvergenceWindow
	if nonConvergenceWindow <= 0 {
		nonConvergenceWindow = 3
	}

	var response *ml.TTSResponse
	var retryHistory []llm.RetranslationAttempt
	var prevActualSec float64
	var prevTextChars int
	consecutiveSameChars := 0
	stuckThreshold := s.cfg.RetranslationStuckThreshold
	if stuckThreshold <= 0 {
		stuckThreshold = 2
	}
	for attempt := 0; attempt <= maxAttempts; attempt++ {
		response, err = s.ml.RunTTS(ctx, ml.TTSRequest{
			Text:              text,
			TargetDurationSec: targetSec,
			MaxAllowedSec:     maxAllowedSec,
			VoiceConfig:       voiceConfig,
			OutputRelPath:     outputRelPath,
			PrevActualSec:     prevActualSec,
			PrevTextChars:     prevTextChars,
		})
		if err != nil {
			return fmt.Errorf("tts segment %d (attempt %d): %w", seg.ID, attempt, err)
		}

		actualMs := response.ActualDurationMs
		actualSec := float64(actualMs) / 1000.0
		overflowMs := actualMs - targetMs

		// Update observed speaking rate with this run's data.
		obsChars := len([]rune(text))
		if obsChars > 0 && actualSec > 0 {
			totalObsChars += obsChars
			totalObsSec += actualSec
			observedCharsPerSec = float64(totalObsChars) / totalObsSec
		}

		// Update non-convergence and best-result tracking.
		absDrift := math.Abs(actualSec - targetSec)
		if absDrift < bestAbsDrift {
			bestAbsDrift = absDrift
			bestText = text
			bestActualMs = actualMs
			attemptsWithoutImprovement = 0
		} else {
			attemptsWithoutImprovement++
		}

		// --- Overflow policy ---
		// Case 1: no overflow, or overflow within drift threshold — accept.
		if overflowMs <= 0 {
			drift := math.Abs(actualSec-targetSec) / targetSec
			if drift <= driftThreshold || !s.cfg.RetranslationEnabled || attempt == maxAttempts {
				break
			}
			// Under-run: use normal re-translation path below.
		} else {
			// Case 2: overflow exists — decide whether to borrow gap or re-translate.
			borrowableMs := gapAfterMs - breathMarginMs
			overDrift := float64(actualMs-targetMs) / float64(targetMs)
			// Apply the absolute-seconds cap to the borrow threshold so that long segments
			// (e.g. 78s) are held to the same absolute ceiling as the retranslation threshold.
			maxBorrowDriftPct := s.cfg.RetranslationMaxBorrowDriftPct
			if absMaxDriftSec > 0 && targetMs > 0 {
				absCap := absMaxDriftSec / (float64(targetMs) / 1000.0)
				if absCap < maxBorrowDriftPct {
					maxBorrowDriftPct = absCap
				}
			}
			withinBorrowDrift := overDrift <= maxBorrowDriftPct
			if overflowMs <= borrowableMs && gapAfterMs > shortGapThresholdMs && (withinBorrowDrift || !s.cfg.RetranslationEnabled || attempt == maxAttempts) {
				// Overflow fits within the available gap AND drift is within the borrow
				// tolerance (or we've exhausted retries).  Accept; merge stage will clip.
				slog.Info("tts overflow: borrowing from gap",
					"job_id", job.ID,
					"segment_id", seg.ID,
					"target_ms", targetMs,
					"actual_ms", actualMs,
					"overflow_ms", overflowMs,
					"gap_after_ms", gapAfterMs,
					"borrowed_ms", overflowMs,
				)
				break
			}
			// Overflow exceeds borrowable gap or gap is too short — must re-translate.
			if !s.cfg.RetranslationEnabled || attempt == maxAttempts {
				slog.Warn("tts overflow: gap exhausted, accepting with clip",
					"job_id", job.ID,
					"segment_id", seg.ID,
					"overflow_ms", overflowMs,
					"gap_after_ms", gapAfterMs,
				)
				break
			}
			slog.Info("tts overflow: gap too small, forcing re-translation",
				"job_id", job.ID,
				"segment_id", seg.ID,
				"target_ms", targetMs,
				"actual_ms", actualMs,
				"overflow_ms", overflowMs,
				"gap_after_ms", gapAfterMs,
				"attempt", attempt+1,
				"max_attempts", maxAttempts,
			)
		}

		// --- Re-translation ---
		drift := math.Abs(actualSec-targetSec) / targetSec
		if overflowMs <= 0 {
			// Under-run path: only log when not already at max attempts.
			slog.Info("tts drift exceeds threshold, re-translating",
				"job_id", job.ID,
				"segment_id", seg.ID,
				"target_sec", targetSec,
				"actual_sec", actualSec,
				"drift_pct", drift*100,
				"threshold_pct", driftThreshold*100,
				"attempt", attempt+1,
				"max_attempts", maxAttempts,
			)
		}

		// thinking is triggered either by consecutive same-char stall OR by
		// non-convergence (N attempts without improving best drift).
		useThinking := consecutiveSameChars >= stuckThreshold || attemptsWithoutImprovement >= nonConvergenceWindow

		newText, retErr := s.llm.RetranslateWithConstraint(
			ctx,
			job.SourceLanguage, job.TargetLanguage,
			seg.SourceText, text,
			targetSec, actualSec,
			attempt+1, maxAttempts,
			driftThreshold,
			retryHistory,
			useThinking,
			observedCharsPerSec,
			contextBefore,
			nextSrcText,
			job.TranslationSummary,
		)
		if retErr != nil {
			slog.Warn("re-translation failed, accepting current result",
				"job_id", job.ID,
				"segment_id", seg.ID,
				"error", retErr,
			)
			break
		}

		slog.Info("retranslation result",
			"job_id", job.ID,
			"segment_id", seg.ID,
			"attempt", attempt+1,
			"prev_chars", len([]rune(text)),
			"new_chars", len([]rune(newText)),
			"prev_actual_sec", actualSec,
			"use_thinking", useThinking,
			"obs_chars_per_sec", observedCharsPerSec,
		)
		retryHistory = append(retryHistory, llm.RetranslationAttempt{Text: text, ActualSec: actualSec})
		if len([]rune(newText)) == len([]rune(text)) {
			consecutiveSameChars++
		} else {
			consecutiveSameChars = 0
		}
		// Only feed adaptive token-budget feedback when the previous attempt
		// was an over-run.  For under-runs the observed tokens/char is
		// artificially low (TTS stopped early or text was already sparse), and
		// blending it into the prior would make the next budget even tighter —
		// exactly the wrong direction.  Reset to zero so the adapter uses its
		// default prior instead.
		if actualSec >= targetSec {
			prevActualSec = actualSec
			prevTextChars = 0
			for _, r := range text {
				if r != ' ' && r != '\t' && r != '\n' && r != '\r' {
					prevTextChars++
				}
			}
		} else {
			prevActualSec = 0
			prevTextChars = 0
		}
		text = newText
		seg.TargetText = newText
		if saveErr := s.store.UpdateSegmentTranslations(ctx, []models.Segment{*seg}); saveErr != nil {
			slog.Warn("failed to persist re-translated text",
				"job_id", job.ID,
				"segment_id", seg.ID,
				"error", saveErr,
			)
		}
	}

	// Best-result restoration: if the loop exit left us with a worse result than
	// the best seen mid-loop (by more than 0.1 s), re-run TTS with the best text
	// so the stored audio matches the optimal translation found.
	if bestText != "" && bestText != text {
		currentAbsDrift := math.Abs(float64(response.ActualDurationMs)/1000.0 - targetSec)
		if bestAbsDrift < currentAbsDrift-0.1 {
			slog.Info("tts best-result restore",
				"job_id", job.ID,
				"segment_id", seg.ID,
				"best_drift_sec", bestAbsDrift,
				"final_drift_sec", currentAbsDrift,
				"best_actual_ms", bestActualMs,
			)
			bestResp, bestErr := s.ml.RunTTS(ctx, ml.TTSRequest{
				Text:              bestText,
				TargetDurationSec: targetSec,
				MaxAllowedSec:     maxAllowedSec,
				VoiceConfig:       voiceConfig,
				OutputRelPath:     outputRelPath,
			})
			if bestErr == nil {
				response = bestResp
			}
			seg.TargetText = bestText
			if saveErr := s.store.UpdateSegmentTranslations(ctx, []models.Segment{*seg}); saveErr != nil {
				slog.Warn("failed to persist best-attempt text",
					"job_id", job.ID,
					"segment_id", seg.ID,
					"error", saveErr,
				)
			}
		}
	}

	seg.TTSAudioRelPath = response.AudioRelPath
	seg.TTSDurationMs = response.ActualDurationMs
	seg.Status = "synthesized"

	if saveErr := s.store.UpdateSegmentSynthResults(ctx, []models.Segment{*seg}); saveErr != nil {
		slog.Warn("failed to persist TTS result immediately; will retry at end",
			"job_id", job.ID,
			"segment_id", seg.ID,
			"error", saveErr,
		)
	}
	return nil
}

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

	dubTrackRelPath := fmt.Sprintf("jobs/%d/output/%s/dub_track.wav", job.ID, mergeVoiceKey(segments))
	if err := media.RenderDubTrack(s.cfg.DataRoot, s.cfg.FFmpegBin, dubTrackRelPath, totalDurationMs, job.BgmRelPath, overlays); err != nil {
		return fmt.Errorf("render dub track: %w", err)
	}

	if media.IsVideoFile(job.InputRelPath) {
		outputRelPath := fmt.Sprintf("jobs/%d/output/%s/final.mp4", job.ID, mergeVoiceKey(segments))
		if err := media.MuxVideo(s.cfg.DataRoot, s.cfg.FFmpegBin, job.InputRelPath, dubTrackRelPath, outputRelPath); err != nil {
			return fmt.Errorf("mux final video: %w", err)
		}
		job.OutputRelPath = outputRelPath
	} else {
		job.OutputRelPath = dubTrackRelPath
	}
	return s.store.SaveJob(ctx, job)
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
