package pipeline

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"holodub/internal/agents"
	"holodub/internal/llm"
	"holodub/internal/ml"
	"holodub/internal/models"
	pipettstts "holodub/internal/pipeline/tts"
	"holodub/internal/store"

	"gorm.io/gorm"
)

// realDubbingTools is the production implementation of
// agents.DubbingTools. It is a thin adapter over s.ml and s.llm — every
// method does one delegation call and a 1:1 field copy. Behaviour
// (timeouts, retries, error classification) is owned entirely by the
// upstream client packages, not duplicated here.
//
// The adapter exists so the agent compiles against agents.DubbingTools
// (a narrow contract) instead of the full pipeline.Service surface,
// which would force tests to mock half the package.
type realDubbingTools struct {
	svc *Service
}

func (t *realDubbingTools) Synthesize(ctx context.Context, args agents.TTSArgs) (agents.TTSResult, error) {
	resp, err := t.svc.ml.RunTTS(ctx, ml.TTSRequest{
		Text:              args.Text,
		TargetDurationSec: args.TargetDurationSec,
		MaxAllowedSec:     args.MaxAllowedSec,
		VoiceConfig:       args.VoiceConfig,
		OutputRelPath:     args.OutputRelPath,
		PrevActualSec:     args.PrevActualSec,
		PrevTextChars:     args.PrevTextChars,
		DubbingMeta:       args.DubbingMeta,
	})
	if err != nil {
		return agents.TTSResult{}, err
	}
	return agents.TTSResult{
		AudioRelPath:     resp.AudioRelPath,
		ActualDurationMs: resp.ActualDurationMs,
		Diagnostics:      resp.Diagnostics,
	}, nil
}

func (t *realDubbingTools) RetranslateWithConstraint(ctx context.Context, args agents.RetranslateArgs) (agents.RetranslateResult, error) {
	newText, err := t.svc.llm.RetranslateWithConstraint(
		ctx,
		args.SourceLanguage, args.TargetLanguage,
		args.SourceText, args.CurrentTrans,
		args.TargetSec, args.ActualSec,
		args.Attempt, args.MaxAttempts,
		args.DriftThresholdPct,
		args.History,
		args.UseThinking,
		args.ObservedCharsPerSec,
		args.ContextBefore,
		args.NextSourceText,
		args.TranslationSummary,
	)
	if err != nil {
		return agents.RetranslateResult{}, err
	}
	return agents.RetranslateResult{Text: newText, UsedThinking: args.UseThinking}, nil
}

// RetranslateEnsemble (OPT-202) routes to llm.Client.RetranslateEnsemble
// using the operator-configured EnsembleModels + EnsembleJudgeModel.
// Returns agents.ErrEnsembleUnavailable when the operator has not opted
// in (enabled=false OR models list empty); the agent's Decide branch
// handles the fallback to a single-model retranslate. We intentionally
// do NOT enforce the gating decision here (the agent already decided
// "use ensemble" by the time we're called) — the executor only enforces
// the static configuration.
func (t *realDubbingTools) RetranslateEnsemble(ctx context.Context, args agents.RetranslateArgs) (agents.EnsembleResult, error) {
	if !t.svc.cfg.EnsembleRetranslateEnabled || len(t.svc.cfg.EnsembleModels) == 0 {
		return agents.EnsembleResult{}, agents.ErrEnsembleUnavailable
	}
	out, err := t.svc.llm.RetranslateEnsemble(ctx, llm.EnsembleArgs{
		SourceLanguage:      args.SourceLanguage,
		TargetLanguage:      args.TargetLanguage,
		SourceText:          args.SourceText,
		CurrentTrans:        args.CurrentTrans,
		TargetSec:           args.TargetSec,
		ActualSec:           args.ActualSec,
		Attempt:             args.Attempt,
		MaxAttempts:         args.MaxAttempts,
		DriftThresholdPct:   args.DriftThresholdPct,
		History:             args.History,
		ObservedCharsPerSec: args.ObservedCharsPerSec,
		ContextBefore:       args.ContextBefore,
		NextSourceText:      args.NextSourceText,
		TranslationSummary:  args.TranslationSummary,
		EpisodeSummary:      args.TranslationSummary,
	}, t.svc.cfg.EnsembleModels, t.svc.cfg.EnsembleJudgeModel)
	if err != nil {
		return agents.EnsembleResult{}, err
	}
	return agents.EnsembleResult{
		Text:           out.Best,
		Model:          out.BestModel,
		JudgeScore:     out.BestVerdict.OverallScore(),
		CandidateCount: len(out.Candidates),
	}, nil
}

func (t *realDubbingTools) JudgeFidelity(ctx context.Context, args agents.JudgeArgs) (*agents.JudgeResult, error) {
	return t.svc.llm.JudgeFidelity(ctx, args)
}

// runSegmentAgentV2 is the OPT-201 SegmentAgent entry point that replaces
// the inline retry loop. It is gated behind cfg.SegmentAgentEnabled; the
// legacy path lives in processOneTTSSegment and is preserved verbatim
// until the 2-week soak after L4 default-on (see PR-6).
//
// Functional parity contract: given identical inputs (segment metadata,
// voice profile, ml/llm tool sequences), V2 MUST produce byte-identical
// TTSAudioRelPath / TargetText / TTSDurationMs / Status / Diagnostics
// to the legacy path. This is verified in PR-5 L2 staging (diff two job
// outputs after running with and without the flag).
//
// Side effects (in this order, matching the legacy path):
//
//  1. Resolve voice profile (same DB query, same fallback to job.VocalsRelPath).
//  2. Compute per-segment Config (target slot, gap, drift thresholds —
//     reuses the same tts.EffectiveDriftThreshold / AdaptiveMinDriftThreshold
//     helpers as the legacy code, so no parity divergence).
//  3. Drive agents.Agent.Run with realDubbingTools.
//  4. On every retranslate the agent ALSO persists the new TargetText
//     via store.UpdateSegmentTranslations — same legacy contract.
//  5. After the loop: persist seg.TTSAudioRelPath / TTSDurationMs / Status.
//  6. Fire-and-forget async judge call (unchanged from legacy).
//
// What V2 does NOT do (intentional):
//
//   - No new judge integration into the decision loop. judge stays
//     observe-only in this PR; OPT-002-followup-4 (PR-7) flips that.
//   - No new metrics beyond holodub_segment_agent_decisions_total
//     (added in PR-3). LLM / TTS metrics are still emitted by the
//     upstream packages.
//
// Returning a non-nil error fails the whole segment (same as legacy)
// and bubbles up to runTTSDuration, which records firstErr.
func (s *Service) runSegmentAgentV2(ctx context.Context, job *models.Job, segments []models.Segment, idx int, isInitial bool, translationSummary string) error {
	return s.runSegmentAgentV2WithHint(ctx, job, segments, idx, isInitial, translationSummary, nil)
}

// runSegmentAgentV2WithHint is the rework-aware variant that accepts
// an OPT-407 ReworkHint. The hint tightens the drift threshold and
// adds rework_verdict / rework_reason fields to the agent's log lines.
// processOneTTSSegment delegates here when SegmentAgentEnabled.
func (s *Service) runSegmentAgentV2WithHint(ctx context.Context, job *models.Job, segments []models.Segment, idx int, isInitial bool, translationSummary string, hint *models.ReworkHint) error {
	seg := &segments[idx]

	voiceConfig, profile, err := s.resolveVoiceConfigForSegment(ctx, job, *seg)
	if err != nil {
		return err
	}

	targetMs := seg.DurationMs()
	targetSec := float64(targetMs) / 1000.0
	gapAfterMs := pipettstts.DefaultGapAfterMs
	if idx+1 < len(segments) {
		if gap := segments[idx+1].StartMs - seg.EndMs; gap >= 0 {
			gapAfterMs = gap
		} else {
			gapAfterMs = 0
		}
	}
	maxAllowedSec := pipettstts.MaxAllowedSec(targetSec, gapAfterMs)

	text := seg.TargetText
	if text == "" {
		text = seg.SourceText
	}

	var vpID uint
	if profile != nil {
		vpID = profile.ID
	}
	outputRelPath := fmt.Sprintf("jobs/%d/tts/vp%d/segment-%04d.wav", job.ID, vpID, seg.Ordinal)

	absMaxDriftSec := s.cfg.RetranslationAbsMaxDriftSec
	maxAttempts := s.cfg.RetranslationMaxAttempts
	if isInitial {
		absMaxDriftSec = s.cfg.RetranslationInitialAbsMaxDriftSec
		maxAttempts = s.cfg.RetranslationInitialMaxAttempts
	}
	effectiveFloor := pipettstts.AdaptiveMinDriftThreshold(
		s.cfg.RetranslationMinDriftThreshold, targetSec,
	)
	driftThreshold := pipettstts.EffectiveDriftThreshold(
		s.cfg.RetranslationDriftThreshold,
		absMaxDriftSec,
		effectiveFloor,
		targetSec,
	)
	maxBorrowDriftPct := pipettstts.EffectiveBorrowDriftPct(
		s.cfg.RetranslationMaxBorrowDriftPct, absMaxDriftSec, targetMs,
	)

	// OPT-407-followup-2: rework hint tightens the drift threshold.
	// The previous attempt already passed the looser bar but the judge
	// said retry — aim for the tighter target so we don't repeat the
	// same near-acceptance result. effectiveFloor is still respected
	// (we don't go BELOW the adaptive floor for long segments because
	// that's physically unreachable; the hint can only tighten down
	// to the floor).
	if hint != nil && hint.DriftThresholdHint > 0 && hint.DriftThresholdHint < driftThreshold {
		driftThreshold = hint.DriftThresholdHint
		if driftThreshold < effectiveFloor {
			driftThreshold = effectiveFloor
		}
	}

	cfg := agents.Config{
		TargetSec:            targetSec,
		TargetMs:             targetMs,
		GapAfterMs:           gapAfterMs,
		MaxAttempts:          maxAttempts,
		DriftThreshold:       driftThreshold,
		MaxBorrowDriftPct:    maxBorrowDriftPct,
		AbsMaxDriftSec:       absMaxDriftSec,
		StuckThreshold:       s.cfg.RetranslationStuckThreshold,
		NonConvergenceWindow: s.cfg.RetranslationNonConvergenceWindow,
		RetranslationEnabled: s.cfg.RetranslationEnabled,
		// OPT-002-followup-4 / OPT-FOLLOWUP-3(b): only honour VETO
		// when both the judge model is configured AND the operator
		// opted in. Defaults to the configured flag; production env
		// keeps this on by default (the followup has been observed-
		// only for months).
		JudgeVetoDriftRetry: s.cfg.JudgeVetoDriftRetry && s.cfg.JudgeModel != "",
		JudgeVetoMinScore:   s.cfg.JudgeVetoMinScore,

		// OPT-202 ensemble wiring. The agent's local guards (per-segment
		// cap + non-convergence trigger) keep cost predictable; the
		// global episode-level ceiling lives at the rework engine
		// layer (accumulated_cost_usd) and is enforced separately.
		EnsembleEnabled:               s.cfg.EnsembleRetranslateEnabled && len(s.cfg.EnsembleModels) > 0,
		EnsembleNonConvergenceTrigger: 2,
		EnsembleJudgeScoreTrigger:     0.7,
		EnsembleMaxPerSegment:         s.cfg.EnsembleMaxPerSegment,
		EnsembleImportant:             segmentImportant(seg),
	}

	// Local context window (prev 2 segments) and forward hint (next 1 source).
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

	// Wrap the existing tool stack. realDubbingTools is the thin
	// adapter at the top of this file; the agent treats it as a black
	// box.
	tools := &realDubbingTools{svc: s}

	// Wrap RetranslateWithConstraint so the agent's retranslate path
	// also persists the new TargetText — matches the legacy contract
	// (stage_tts.go:440 `s.store.UpdateSegmentTranslations`). Using a
	// wrapper rather than a method on realDubbingTools lets us thread
	// the live *seg pointer + job ID without touching the agent's
	// signature.
	persistingTools := &translationPersistingTools{
		inner: tools,
		store: s.store,
		seg:   seg,
		jobID: job.ID,
	}

	// OPT-407-followup-2: when a rework hint is present, build a
	// rework-aware logger that carries the rework verdict/reason on
	// every log line — operators can grep `rework_verdict=retry` to
	// see only agent activity that resulted from a closed-loop
	// dispatch.
	logger := slog.Default()
	if hint != nil {
		logger = logger.With(
			"rework_verdict", hint.PrevVerdict,
			"rework_reason", hint.PrevReason,
			"rework_drift_threshold_hint", hint.DriftThresholdHint,
		)
	}

	agent := agents.NewAgent(persistingTools,
		agents.WithLogger(logger),
		agents.WithObserver(agents.DefaultObservabilityObserver),
	)

	out, err := agent.Run(ctx, agents.RunInput{
		JobID:              job.ID,
		SegmentID:          seg.ID,
		SourceLanguage:     job.SourceLanguage,
		TargetLanguage:     job.TargetLanguage,
		SourceText:         seg.SourceText,
		InitialText:        text,
		VoiceConfig:        voiceConfig,
		OutputRelPath:      outputRelPath,
		MaxAllowedSec:      maxAllowedSec,
		ContextBefore:      contextBefore,
		NextSourceText:     nextSrcText,
		TranslationSummary: translationSummary,
		EpisodeSummary:     job.TranslationSummary,
		DubbingMeta:        extractDubbingMeta(seg),
	}, cfg)
	if err != nil {
		return fmt.Errorf("tts segment %d (agent): %w", seg.ID, err)
	}

	// Voice-profile speaking rate update: same as the legacy path,
	// runTTSDuration handles the post-batch ROLL-UP. Here we just
	// expose the per-segment observed rate via state for parity (the
	// caller computes the EMA across all segments in one batch).
	_ = out.State.ObservedCharsPerSec

	// Persist final accepted result.
	seg.TargetText = out.FinalText
	seg.TTSAudioRelPath = out.FinalAudioRelPath
	seg.TTSDurationMs = out.FinalActualMs
	seg.Status = models.SegmentStatusSynthesized

	if saveErr := s.store.UpdateSegmentTranslations(ctx, []models.Segment{*seg}); saveErr != nil {
		slog.Warn("segment_agent: failed to persist final translation",
			"job_id", job.ID, "segment_id", seg.ID, "error", saveErr,
		)
	}
	if saveErr := s.store.UpdateSegmentSynthResults(ctx, []models.Segment{*seg}); saveErr != nil {
		slog.Warn("segment_agent: failed to persist TTS result immediately; will retry at end",
			"job_id", job.ID, "segment_id", seg.ID, "error", saveErr,
		)
	}

	// OPT-002 async judge — unchanged from legacy (observe-only here;
	// OPT-002-followup-4 in PR-7 will give the agent judge access via
	// a Decide-time observation rather than this fire-and-forget call).
	s.maybeJudgeSegmentAsync(job, *seg, contextBefore)
	return nil
}

// extractDubbingMeta pulls the OPT-204 structured prosody plan out of
// seg.Meta["dubbing"] if present. Returns nil when the segment was
// translated without OPT-204 (legacy path), when the key is missing,
// or when the value is not a map — defensive against operator-edited
// meta blobs.
//
// Returns a shallow copy so callers can pass the map across the
// pipeline / ml boundary without worrying about concurrent mutation.
func extractDubbingMeta(seg *models.Segment) map[string]any {
	if seg == nil || seg.Meta == nil {
		return nil
	}
	raw, ok := seg.Meta["dubbing"]
	if !ok {
		return nil
	}
	m, ok := raw.(map[string]any)
	if !ok {
		return nil
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// segmentImportant reads seg.Meta["important"] and returns true when
// it is the boolean true OR the string "true". Anything else (missing,
// false, non-bool) returns false. The lenient string handling lets
// curl-driven manual seg.meta edits work without requiring strict JSON
// boolean syntax (operators sometimes set "true" by hand).
//
// Used by OPT-202: when set, the SegmentAgent enables the EnsembleImportant
// trigger for this segment so every retranslate goes through the
// multi-model fan-out (still capped by EnsembleMaxPerSegment).
func segmentImportant(seg *models.Segment) bool {
	if seg == nil || seg.Meta == nil {
		return false
	}
	v, ok := seg.Meta["important"]
	if !ok {
		return false
	}
	switch x := v.(type) {
	case bool:
		return x
	case string:
		return x == "true"
	default:
		return false
	}
}

// resolveVoiceConfigForSegment is the small shared helper that both
// processOneTTSSegment (legacy) and runSegmentAgentV2 use to resolve
// the voice profile + build the voice_config map. Pulled out so the
// resolution logic stays in one place — diverging copies have caused
// production bugs in the past (see lessons-learned.mdc#1).
func (s *Service) resolveVoiceConfigForSegment(ctx context.Context, job *models.Job, seg models.Segment) (map[string]any, *models.VoiceProfile, error) {
	voiceConfig := map[string]any{}
	profile, err := s.store.ResolveVoiceProfileForSegment(ctx, job.ID, seg)
	if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, nil, fmt.Errorf("resolve voice profile for segment %d: %w", seg.ID, err)
	}
	if profile != nil {
		voiceConfig, err = buildVoiceConfig(*profile)
		if err != nil {
			return nil, nil, fmt.Errorf("build voice config for segment %d: %w", seg.ID, err)
		}
	} else if job.VocalsRelPath != "" {
		voiceConfig = map[string]any{
			"sample_relpaths": []string{job.VocalsRelPath},
		}
	}
	return voiceConfig, profile, nil
}

// translationPersistingTools wraps a real DubbingTools and writes the
// new TargetText to the DB after every successful retranslate. Matches
// the legacy contract (stage_tts.go:440) so a worker SIGTERM mid-loop
// leaves the segment with the latest translation persisted — the next
// retry picks up where this one left off.
//
// All other methods delegate transparently to inner.
type translationPersistingTools struct {
	inner agents.DubbingTools
	store *store.Store
	seg   *models.Segment
	jobID uint
}

func (t *translationPersistingTools) Synthesize(ctx context.Context, args agents.TTSArgs) (agents.TTSResult, error) {
	return t.inner.Synthesize(ctx, args)
}

func (t *translationPersistingTools) RetranslateWithConstraint(ctx context.Context, args agents.RetranslateArgs) (agents.RetranslateResult, error) {
	res, err := t.inner.RetranslateWithConstraint(ctx, args)
	if err != nil {
		return res, err
	}
	if t.seg != nil && t.store != nil {
		t.seg.TargetText = res.Text
		if saveErr := t.store.UpdateSegmentTranslations(ctx, []models.Segment{*t.seg}); saveErr != nil {
			slog.Warn("segment_agent: failed to persist re-translated text",
				"job_id", t.jobID, "segment_id", t.seg.ID, "error", saveErr,
			)
		}
	}
	return res, nil
}

// RetranslateEnsemble proxies the inner call and persists the winning
// candidate's text the same way RetranslateWithConstraint does. The
// agent's Decide treats ensemble winners as the new working
// translation, so the DB must reflect that to maintain the legacy
// crash-recovery contract.
func (t *translationPersistingTools) RetranslateEnsemble(ctx context.Context, args agents.RetranslateArgs) (agents.EnsembleResult, error) {
	res, err := t.inner.RetranslateEnsemble(ctx, args)
	if err != nil {
		return res, err
	}
	if t.seg != nil && t.store != nil && res.Text != "" {
		t.seg.TargetText = res.Text
		if saveErr := t.store.UpdateSegmentTranslations(ctx, []models.Segment{*t.seg}); saveErr != nil {
			slog.Warn("segment_agent: failed to persist ensemble winner text",
				"job_id", t.jobID, "segment_id", t.seg.ID,
				"winner_model", res.Model, "judge_score", res.JudgeScore,
				"error", saveErr,
			)
		}
	}
	return res, nil
}

func (t *translationPersistingTools) JudgeFidelity(ctx context.Context, args agents.JudgeArgs) (*agents.JudgeResult, error) {
	return t.inner.JudgeFidelity(ctx, args)
}
