package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"strings"
	"sync"
	"time"

	"holodub/internal/llm"
	"holodub/internal/ml"
	"holodub/internal/models"
	pipettstts "holodub/internal/pipeline/tts"

	"gorm.io/gorm"
)

// runTTSDuration executes the tts_duration stage: synthesise audio for every
// segment that is not already marked synthesized, with up to
// cfg.TTSConcurrency concurrent ML calls. Per-segment retry / re-translation
// logic lives in processOneTTSSegment.
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

	// OPT-402: prepend episode-level glossary + reference card to the
	// translation summary that retranslate sees, so a long-segment retry
	// stays terminologically consistent with the rest of the episode.
	// effectiveSummary is computed ONCE per stage (not per segment) so the
	// system prompt sent to the LLM stays byte-stable per job and the
	// OPT-001 prefix cache continues to hit.
	effectiveSummary := s.buildEpisodeAwareSummary(ctx, job)

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
		if segments[idx].Status == models.SegmentStatusSynthesized {
			continue
		}
		idx := idx
		wg.Add(1)
		go func() {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			if err := s.processOneTTSSegmentWithHint(ctx, job, segments, idx, isInitial, effectiveSummary, task.ReworkHint); err != nil {
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

// processOneTTSSegment runs the per-segment TTS synthesis loop with adaptive
// duration alignment. The pure decision helpers used here live in
// internal/pipeline/tts and are unit-tested independently.
//
// translationSummary is the OPT-402 episode-aware summary (job's own
// summary prepended with the episode glossary + reference card). It is
// passed through to RetranslateWithConstraint verbatim so the LLM sees
// canonical terminology even on a single isolated retry.
//
// OPT-201 dual-path: when cfg.SegmentAgentEnabled is true, the per-
// segment loop is delegated to runSegmentAgentV2 (internal/agents.Agent).
// The legacy inline loop below is preserved verbatim until the 2-week
// soak after L4 default-on (PR-6 deletes it). The two paths produce
// byte-identical outputs given the same tool sequences — verified in
// PR-5 L2 staging.
func (s *Service) processOneTTSSegment(ctx context.Context, job *models.Job, segments []models.Segment, idx int, isInitial bool, translationSummary string) error {
	return s.processOneTTSSegmentWithHint(ctx, job, segments, idx, isInitial, translationSummary, nil)
}

// processOneTTSSegmentWithHint is the rework-aware variant; the
// SegmentAgent path threads `hint` through to V2WithHint, and the
// legacy path simply ignores it (hint is only honoured by the agent).
func (s *Service) processOneTTSSegmentWithHint(ctx context.Context, job *models.Job, segments []models.Segment, idx int, isInitial bool, translationSummary string, hint *models.ReworkHint) error {
	if s.cfg.SegmentAgentEnabled {
		return s.runSegmentAgentV2WithHint(ctx, job, segments, idx, isInitial, translationSummary, hint)
	}
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
	} else if job.VocalsRelPath != "" {
		// No voice profile assigned: fall back to the current job's separated
		// vocals so TTS clones the actual speaker from this video, not the
		// global test_ref.wav which belongs to an unrelated previous recording.
		voiceConfig = map[string]any{
			"sample_relpaths": []string{job.VocalsRelPath},
		}
	}

	targetMs := seg.DurationMs()
	targetSec := float64(targetMs) / 1000.0

	// Gap between this segment's end and the next segment's start.
	// Used for overflow policy: TTS audio that overruns the target slot can
	// "borrow" from the trailing silence up to (gap - breathMargin).
	gapAfterMs := pipettstts.DefaultGapAfterMs
	if idx+1 < len(segments) {
		if gap := segments[idx+1].StartMs - seg.EndMs; gap >= 0 {
			gapAfterMs = gap
		} else {
			gapAfterMs = 0
		}
	}

	// maxAllowedSec caps the token budget inside the TTS model. It is the full
	// slot: target + entire gap. Physical playback clipping is handled later
	// in the merge stage via ffmpeg atrim.
	maxAllowedSec := pipettstts.MaxAllowedSec(targetSec, gapAfterMs)

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

	// Effective threshold: stricter of the relative % or the absolute-seconds
	// cap, but never below the (adaptive) minimum relative floor.
	//
	// OPT-FOLLOWUP-3: AdaptiveMinDriftThreshold relaxes the floor for long
	// segments (≥10s, ≥20s) where the global 0.03 default required absolute
	// precision the TTS+LLM stack cannot reach, causing retry oscillation
	// on the 10 min baseline. Short segments stay at the user-configured
	// floor; users who set a stricter global value (e.g. 0.05) are NEVER
	// silently relaxed because AdaptiveMinDriftThreshold is monotonic in
	// baseFloor.
	effectiveFloor := pipettstts.AdaptiveMinDriftThreshold(
		s.cfg.RetranslationMinDriftThreshold, targetSec,
	)
	driftThreshold := pipettstts.EffectiveDriftThreshold(
		s.cfg.RetranslationDriftThreshold,
		absMaxDriftSec,
		effectiveFloor,
		targetSec,
	)

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
		// Respect cancellation between attempts so a SIGTERM on the worker or
		// a user-issued cancel propagates promptly even if every individual
		// retry attempt would still complete.
		if err := ctx.Err(); err != nil {
			return fmt.Errorf("tts segment %d: %w", seg.ID, err)
		}
		response, err = s.ml.RunTTS(ctx, ml.TTSRequest{
			Text:              text,
			TargetDurationSec: targetSec,
			MaxAllowedSec:     maxAllowedSec,
			VoiceConfig:       voiceConfig,
			OutputRelPath:     outputRelPath,
			PrevActualSec:     prevActualSec,
			PrevTextChars:     prevTextChars,
			DubbingMeta:       extractDubbingMeta(seg),
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
			borrowableMs := gapAfterMs - pipettstts.BreathMarginMs
			overDrift := float64(actualMs-targetMs) / float64(targetMs)
			// Apply the absolute-seconds cap to the borrow threshold so that
			// long segments (e.g. 78s) are held to the same absolute ceiling
			// as the retranslation threshold.
			maxBorrowDriftPct := pipettstts.EffectiveBorrowDriftPct(
				s.cfg.RetranslationMaxBorrowDriftPct, absMaxDriftSec, targetMs,
			)
			withinBorrowDrift := overDrift <= maxBorrowDriftPct
			if overflowMs <= borrowableMs && gapAfterMs > pipettstts.ShortGapThresholdMs && (withinBorrowDrift || !s.cfg.RetranslationEnabled || attempt == maxAttempts) {
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
			translationSummary,
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
			prevTextChars = pipettstts.CountNonWhitespaceRunes(text)
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
				DubbingMeta:       extractDubbingMeta(seg),
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
	seg.Status = models.SegmentStatusSynthesized

	if saveErr := s.store.UpdateSegmentSynthResults(ctx, []models.Segment{*seg}); saveErr != nil {
		slog.Warn("failed to persist TTS result immediately; will retry at end",
			"job_id", job.ID,
			"segment_id", seg.ID,
			"error", saveErr,
		)
	}

	// OPT-002: async judge call. Observe-only by default — never blocks
	// synthesis, never rolls back the segment, never affects retry decisions.
	// Decision integration is OPT-201 (SegmentAgent ReAct refactor).
	s.maybeJudgeSegmentAsync(job, *seg, contextBefore)

	return nil
}

// buildEpisodeAwareSummary returns the translation summary that retranslate
// should see, augmented with the OPT-402 episode glossary + reference card.
//
// Format (markdown, injected verbatim into the LLM prompt):
//
//	[Episode reference card]
//	<reference_card markdown ...>
//	[End of reference card]
//
//	[Episode glossary — use these translations verbatim]
//	- <source>: <target> (<note>)
//	- ...
//	[End of glossary]
//
//	[Translation summary from job]
//	<job.TranslationSummary>
//
// The function is intentionally tolerant of missing pieces:
//   - no episode → return job.TranslationSummary unchanged (== legacy behaviour)
//   - episode exists but glossary empty → return job.TranslationSummary unchanged
//   - episode lookup error → log + fall back to job.TranslationSummary
//
// This is the OPT-402 contract: glossary failure NEVER blocks the pipeline.
func (s *Service) buildEpisodeAwareSummary(ctx context.Context, job *models.Job) string {
	if job == nil || job.EpisodeID == 0 {
		return job.TranslationSummary
	}
	ep, err := s.store.GetEpisode(ctx, job.EpisodeID)
	if err != nil || ep == nil {
		// Best-effort. Glossary unavailability should not break TTS.
		return job.TranslationSummary
	}

	var glossaryEntries []llm.GlossaryEntry
	if len(ep.Glossary) > 0 {
		if err := json.Unmarshal(ep.Glossary, &glossaryEntries); err != nil {
			slog.Warn("episode glossary unmarshal failed; falling back to legacy summary",
				"episode_id", ep.ID, "job_id", job.ID, "error", err)
			glossaryEntries = nil
		}
	}

	if len(glossaryEntries) == 0 && ep.ReferenceCard == "" {
		return job.TranslationSummary
	}

	var b strings.Builder
	if ep.ReferenceCard != "" {
		b.WriteString("[Episode reference card]\n")
		b.WriteString(ep.ReferenceCard)
		b.WriteString("\n[End of reference card]\n\n")
	}
	if len(glossaryEntries) > 0 {
		b.WriteString("[Episode glossary — use these translations verbatim]\n")
		for _, g := range glossaryEntries {
			if g.Source == "" || g.Target == "" {
				continue
			}
			if g.Note != "" {
				fmt.Fprintf(&b, "- %s: %s (%s)\n", g.Source, g.Target, g.Note)
			} else {
				fmt.Fprintf(&b, "- %s: %s\n", g.Source, g.Target)
			}
		}
		b.WriteString("[End of glossary]\n\n")
	}
	if job.TranslationSummary != "" {
		b.WriteString("[Translation summary from job]\n")
		b.WriteString(job.TranslationSummary)
	}
	return strings.TrimRight(b.String(), "\n")
}

// maybeJudgeSegmentAsync fires off a background judge call for the just-
// synthesised segment when JUDGE_MODEL is configured. The function returns
// immediately; the goroutine has its own deadline + writes results via
// store.UpdateSegmentJudgeResult on a fresh background context so a worker
// SIGTERM cancelling the synthesis ctx does NOT silently drop the verdict.
//
// Failure modes (network / parse / provider error) are logged and dropped:
// observability dashboards should monitor holodub_llm_strict_parse_failed_total
// {operation="judge"} to detect sustained issues.
func (s *Service) maybeJudgeSegmentAsync(job *models.Job, segCopy models.Segment, contextBefore []llm.ContextSegment) {
	if s.cfg.JudgeModel == "" {
		return
	}
	if segCopy.SourceText == "" || segCopy.TargetText == "" {
		return
	}
	go func() {
		// Detached background context so a worker shutdown signal does
		// not silently lose the verdict mid-flight. 30s ceiling is
		// generous for a single short LLM call but bounds the goroutine's
		// max in-flight time.
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		result, err := s.llm.JudgeFidelity(ctx, llm.JudgeArgs{
			SrcText:        segCopy.SourceText,
			TgtText:        segCopy.TargetText,
			SrcLang:        job.SourceLanguage,
			TgtLang:        job.TargetLanguage,
			EpisodeSummary: job.TranslationSummary,
			PrevContext:    contextBefore,
		})
		if err != nil {
			slog.Warn("judge call failed",
				"job_id", job.ID,
				"segment_id", segCopy.ID,
				"error", err,
			)
			return
		}
		if result == nil {
			return // judging disabled or empty inputs — should not reach here
		}

		metaJSON, err := json.Marshal(result)
		if err != nil {
			slog.Warn("judge result marshal failed",
				"job_id", job.ID,
				"segment_id", segCopy.ID,
				"error", err,
			)
			return
		}
		if err := s.store.UpdateSegmentJudgeResult(ctx, segCopy.ID, result.OverallScore(), metaJSON); err != nil {
			slog.Warn("judge result persist failed",
				"job_id", job.ID,
				"segment_id", segCopy.ID,
				"error", err,
			)
			return
		}
		slog.Info("judge result recorded",
			"job_id", job.ID,
			"segment_id", segCopy.ID,
			"verdict", result.Verdict,
			"fidelity", result.Fidelity,
			"fluency", result.Fluency,
			"coherence", result.Coherence,
		)

		// OPT-407 closed-loop rework hook. Always called — the engine
		// decides internally whether to dispatch (REWORK_ENGINE_LEVEL gate)
		// or just record the attempt as observe-only. Engine swallows its
		// own errors, so this is fire-and-forget. EpisodeID == 0 (older
		// jobs without an episode) is handled inside MaybeReworkSegment.
		//
		// OPT-407-followup-6: pass signed drift in seconds so the engine's
		// drift hard guard can override a high-LLM-score verdict when the
		// audio length is well outside the configured tolerance. driftSec
		// is "actual TTS audio - target slot" (positive = audio overflowed).
		// Zero target (corrupt segment metadata) collapses to driftSec=0
		// which silently disables the guard for that segment.
		var driftSec float64
		targetMs := segCopy.EndMs - segCopy.StartMs
		if targetMs > 0 && segCopy.TTSDurationMs > 0 {
			driftSec = float64(int64(segCopy.TTSDurationMs)-int64(targetMs)) / 1000.0
		}
		s.rework.MaybeReworkSegment(
			ctx,
			job.ID,
			job.EpisodeID,
			segCopy.ID,
			result.Verdict,
			result.OverallScore(),
			driftSec,
		)
	}()
}

// maybeJudgeChapterAsync fires off a background chapter-level judge call
// for the just-merged chapter when CHAPTER_JUDGE_MODEL is configured. The
// function returns immediately; the goroutine has its own deadline + writes
// results via store.UpdateChapterJudgeResult on a fresh background context
// so a worker SIGTERM cancelling the merge ctx does NOT silently lose the
// verdict (mirrors maybeJudgeSegmentAsync's contract).
//
// Failure modes (network / parse / provider error / DB write) are logged
// and dropped — chapter judging is observe-only in the OPT-409 MVP and
// must never fail the chapter or the downstream episode merge. The DB
// UPDATE only touches chapter_judge_score / chapter_judge_meta, so it is
// safe against concurrent writes that may rewrite chapter_title or
// output_relpath later.
//
// Why we accept []models.Segment by value: SaveJob just persisted the Job
// and runMerge holds the loaded segments slice already; reusing it spares
// a redundant DB read. The judge goroutine treats the slice as immutable
// (only reads).
func (s *Service) maybeJudgeChapterAsync(job *models.Job, segments []models.Segment) {
	if s.cfg.ChapterJudgeModel == "" {
		return
	}
	if len(segments) == 0 {
		return
	}
	// Snapshot fields the goroutine needs so callers can mutate the Job
	// after we return without racing the async judge call.
	jobCopy := *job
	chapterOrdinal := jobCopy.ChapterOrdinal
	// Build the LLM-side segment slice with text + per-segment judge hint
	// (when available) so the chapter judge can correlate with OPT-002
	// signals. Skip empty pairs — they would dilute the chapter score
	// without carrying useful information.
	chapterSegs := make([]llm.ChapterJudgeSegment, 0, len(segments))
	for _, seg := range segments {
		if seg.SourceText == "" || seg.TargetText == "" {
			continue
		}
		var segScore *float64
		if seg.JudgeScore != nil {
			v := *seg.JudgeScore
			segScore = &v
		}
		chapterSegs = append(chapterSegs, llm.ChapterJudgeSegment{
			Ordinal:       seg.Ordinal,
			StartMs:       seg.StartMs,
			EndMs:         seg.EndMs,
			SourceText:    seg.SourceText,
			TargetText:    seg.TargetText,
			SegJudgeScore: segScore,
		})
	}
	if len(chapterSegs) == 0 {
		return
	}
	go func() {
		// Detached background context: a SIGTERM during the chapter judge
		// must not silently swallow the verdict. 60s ceiling — chapter
		// judge prompts are larger than segment judge prompts (≤8k tokens
		// in/300 out vs ≤500/30) and reasoning models can take 10-15s.
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()

		result, err := s.llm.JudgeChapter(ctx, llm.ChapterJudgeArgs{
			SourceLang:     jobCopy.SourceLanguage,
			TargetLang:     jobCopy.TargetLanguage,
			ChapterOrdinal: chapterOrdinal,
			ChapterTitle:   jobCopy.ChapterTitle,
			EpisodeSummary: jobCopy.TranslationSummary,
			Segments:       chapterSegs,
		})
		if err != nil {
			slog.Warn("chapter judge call failed",
				"job_id", jobCopy.ID,
				"chapter_ordinal", chapterOrdinal,
				"error", err,
			)
			return
		}
		if result == nil {
			return // judging disabled or empty inputs — should not reach here
		}

		metaJSON, err := json.Marshal(result)
		if err != nil {
			slog.Warn("chapter judge result marshal failed",
				"job_id", jobCopy.ID,
				"chapter_ordinal", chapterOrdinal,
				"error", err,
			)
			return
		}
		if err := s.store.UpdateChapterJudgeResult(ctx, jobCopy.ID, result.OverallScore(), metaJSON); err != nil {
			slog.Warn("chapter judge result persist failed",
				"job_id", jobCopy.ID,
				"chapter_ordinal", chapterOrdinal,
				"error", err,
			)
			return
		}
		slog.Info("chapter judge result recorded",
			"job_id", jobCopy.ID,
			"chapter_ordinal", chapterOrdinal,
			"verdict", result.Verdict,
			"overall_fidelity", result.OverallFidelityChapter,
			"narrative_coherence", result.NarrativeCoherenceWithinChapter,
			"speaker_voice_stability", result.SpeakerVoiceStabilityWithinChapter,
			"terminology_consistency", result.TerminologyConsistencyWithinChapter,
			"register_consistency", result.RegisterConsistencyWithinChapter,
			"weakest_count", len(result.Top3WeakestSegments),
		)

		// OPT-407 closed-loop rework hook (chapter level). Engine resolves
		// 1-indexed in-chapter ordinals to real DB segment IDs and gates
		// dispatch on REWORK_ENGINE_LEVEL >= chapter. Same fire-and-forget
		// contract as the segment hook above.
		weakestOrdinals := make([]int, 0, len(result.Top3WeakestSegments))
		for _, ws := range result.Top3WeakestSegments {
			if ws.Ordinal > 0 {
				weakestOrdinals = append(weakestOrdinals, ws.Ordinal)
			}
		}
		s.rework.MaybeReworkChapter(
			ctx,
			jobCopy.ID,
			jobCopy.EpisodeID,
			result.Verdict,
			result.OverallScore(),
			weakestOrdinals,
		)
	}()
}
