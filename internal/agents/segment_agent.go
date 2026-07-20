package agents

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"path"

	"holodub/internal/llm"
	"holodub/internal/observability"
	pipettstts "holodub/internal/pipeline/tts"
)

// AdaptiveMaxAcceptableDrift returns the maximum *absolute* drift (in
// seconds) we are willing to ship when the LLM judge gives the
// translation a perfect score. The bands mirror
// pipeline/tts.AdaptiveMinDriftThreshold but are LOOSER — the latter
// caps retranslation work, this one caps the VETO acceptance window.
//
// Bands (tightened in OPT-002-followup-5 after chapter 2 of job 154
// showed too many segments shipping with audible drift):
//
//	targetSec ≥ 20s → 8% (was 10%, e.g. 1.6s on a 20s segment)
//	5s ≤ targetSec <  20s → 5% (was 6%, e.g. 0.5s on a 10s segment)
//	targetSec <  5s → 2.5% (was 3%, e.g. 0.075s on a 3s segment)
//
// Why tighten: the OPT-002-followup-3 observe-only baseline accepted
// drift up to ~10% on long segments through the VETO branch even when
// retries could plausibly converge. The new bands force more retries
// (and, after PR-1 B2, more ensemble escalations) before falling back
// to VETO acceptance. Short segments stay strict because a 2.5%
// drift on a 2s clip is still ≤ 50ms — well below what TTS+LLM can
// routinely hit; a perfect-score 2s clip drifting 20% is almost
// certainly truncated audio, not just a long translation.
//
// The new abs-drift ensemble trigger in shouldUseEnsemble (PR-1 B2)
// uses these SAME bands, so tightening here also makes ensemble
// escalation more aggressive — by design.
func AdaptiveMaxAcceptableDrift(targetSec float64) float64 {
	switch {
	case targetSec >= 20.0:
		return targetSec * 0.08
	case targetSec >= 5.0:
		return targetSec * 0.05
	default:
		return targetSec * 0.025
	}
}

// Decide is the OPT-201 pure decision function. Given the cumulative
// State produced by all prior attempts on this segment plus the
// Observation from the most recent Synthesize call, it returns the
// Decision the agent should execute next.
//
// Decision priority (first match wins) — exactly mirrors the inline
// branching in internal/pipeline/stage_tts.go::processOneTTSSegment
// at the time of the OPT-201 refactor:
//
//  1. retranslation disabled OR last-attempt → DecisionAccept (legacy break).
//  2. overflow <= 0:
//     drift within threshold → DecisionAccept ("within_threshold")
//     else                   → DecisionRetranslate ("under_run_drift")
//  3. overflow > 0:
//     borrow case (fits gap, within borrow drift) → DecisionAccept ("borrow_from_gap")
//     gap too small / borrow drift exceeded      → DecisionRetranslate ("over_short_gap")
//
// Thinking-mode escalation on DecisionRetranslate is set independently:
// either ConsecutiveSameChars >= StuckThreshold or AttemptsWithoutImprovement
// >= NonConvergenceWindow triggers it (matches the inline
// `consecutiveSameChars >= stuckThreshold || attemptsWithoutImprovement
// >= nonConvergenceWindow` computation).
//
// No side effects, no logging, no time-of-day inputs — exhaustively
// unit-testable. The caller (Run) handles side effects (LLM call,
// TTS call, history append, prevActualSec feedback).
func Decide(state State, obs Observation, cfg Config) Decision {
	// Pre-compute the loop-exhausted predicate. attempt == MaxAttempts
	// is the LAST attempt the loop will run (matches `attempt ==
	// maxAttempts` short-circuit in the legacy code). Note State.Attempt
	// is the 0-based index of the attempt that just produced `obs`, NOT
	// the next-attempt index — the caller bumps Attempt AFTER Decide.
	isLastAttempt := state.Attempt >= cfg.MaxAttempts

	// OPT-201-followup-2: pre-compute the absolute hard cap check.
	// `cap.exceeded` is true when the observed drift breaks the
	// asymmetric over/under ceiling configured by the operator. When
	// not exceeded, every existing acceptance gate behaves as before;
	// when exceeded, acceptance gates downshift to retranslate so we
	// don't ship audio that violates the operator's hard target. The
	// explicit escape branches (last_attempt / stuck / disabled / etc.)
	// still accept but flag the decision via Decision.HardCapExceeded
	// so emit() can record it.
	cap := evalHardCap(obs, cfg)

	// Branch 1 / 2-fallback: retranslation disabled bypasses every
	// retry decision (accept whatever we just produced).
	if !cfg.RetranslationEnabled {
		switch {
		case obs.OverflowMs <= 0:
			return markHardCap(Decision{Kind: DecisionAccept, Reason: "retranslation_disabled"}, cap)
		default:
			// Over-run with retranslation off: if we can borrow we
			// prefer that label (more informative); otherwise the
			// merge stage will clip. Either way → DecisionAccept.
			if canBorrow(obs, cfg) {
				return markHardCap(Decision{Kind: DecisionAccept, Reason: "borrow_from_gap"}, cap)
			}
			return markHardCap(Decision{Kind: DecisionAccept, Reason: "clip_overflow"}, cap)
		}
	}

	// Branch: no overflow (under-run or exact).
	if obs.OverflowMs <= 0 {
		if obs.DriftPct <= cfg.DriftThreshold {
			// Percentage gate satisfied. Hard cap (if configured)
			// must ALSO be satisfied — otherwise the segment looks
			// fine on relative terms but ships >0.7s of audible
			// trailing silence on a long segment. Force another
			// retranslate unless we've run out of attempts.
			if !cap.exceeded {
				return Decision{Kind: DecisionAccept, Reason: "within_threshold"}
			}
			if isLastAttempt {
				return markHardCap(Decision{Kind: DecisionAccept, Reason: "within_threshold"}, cap)
			}
			// fall through to retranslate
		} else if isLastAttempt {
			return markHardCap(Decision{Kind: DecisionAccept, Reason: "no_more_attempts"}, cap)
		}
		// VETO branch (OPT-002-followup-4): a high-judge-score
		// segment with drift inside the adaptive acceptance band
		// short-circuits the retry — translation quality is already
		// at ceiling and additional retries would not improve it.
		// Hard cap (OPT-201-followup-2): VETO also respects the
		// absolute ceiling, so a "perfect translation, drifted 1.5s"
		// observation no longer ships — the agent keeps trying.
		if shouldVetoDriftRetry(obs, cfg) && !cap.exceeded {
			return Decision{Kind: DecisionAccept, Reason: "judge_veto_drift"}
		}
		// Under-run still outside threshold → retranslate.
		d := Decision{
			Kind:        DecisionRetranslate,
			Reason:      "under_run_drift",
			UseThinking: shouldUseThinking(state, cfg),
			UseEnsemble: shouldUseEnsemble(state, obs, cfg),
		}
		// Once ensemble fires we drop thinking — they are alternative
		// escalations, not stackable. The fan-out already includes
		// candidates strong enough that picking a thinking-class
		// single model on top would not pay for itself.
		if d.UseEnsemble {
			d.UseThinking = false
		}
		return d
	}

	// Branch: overflow > 0 — try borrow first. Hard cap blocks borrow
	// on cap violation (e.g. 0.8s over-run still fits a 1.2s gap but
	// the audio is audibly long).
	if canBorrow(obs, cfg) && !cap.exceeded {
		// Borrow path: fits in the gap AND drift is within borrow
		// tolerance (or we've exhausted retries / retranslation off).
		return Decision{Kind: DecisionAccept, Reason: "borrow_from_gap"}
	}

	// Overflow exceeds borrowable gap, or gap too short. Last attempt
	// → accept with clip; otherwise force retranslate.
	if isLastAttempt {
		return markHardCap(Decision{Kind: DecisionAccept, Reason: "clip_overflow"}, cap)
	}
	// VETO branch (OPT-002-followup-4) for over-run path too:
	// long-segment over-runs that the judge calls perfect are also
	// candidates for early acceptance. Bands are the same as the
	// under-run side (symmetrical). Hard cap (OPT-201-followup-2):
	// over-run VETO respects the (tighter) 0.3s cap so over-runs
	// that would otherwise ship as "perfect but 1s late" instead
	// force another retranslate.
	if shouldVetoDriftRetry(obs, cfg) && !cap.exceeded {
		return Decision{Kind: DecisionAccept, Reason: "judge_veto_drift"}
	}
	// OPT-202-followup-1 (B3): over_short_gap deadlock escape.
	//
	// Root cause from segment 10186 of job 154: a long segment that
	// can't borrow (GapAfterMs too small), fired ensemble once, the
	// ensemble winner also over-ran, and now we're stuck retranslating
	// over and over with no convergence. The retry loop would run all
	// MaxAttempts iterations producing identical "still over-runs by
	// 2-3 seconds" output, burning 14× the cost of a single segment.
	//
	// Heuristic: if AttemptsWithoutImprovement is high AND we're in
	// the tail of the retry window, the agent has demonstrated it
	// can't improve further — accept what we have (the merge stage
	// will clip the overflow). The thresholds (AwI >= 4, Attempt >=
	// MaxAttempts-3) are deliberately conservative so this only
	// triggers AFTER the agent has had a fair chance to converge.
	if state.AttemptsWithoutImprovement >= 4 && state.Attempt >= cfg.MaxAttempts-3 {
		return markHardCap(Decision{Kind: DecisionAccept, Reason: "over_short_gap_stuck"}, cap)
	}
	d := Decision{
		Kind:        DecisionRetranslate,
		Reason:      "over_short_gap",
		UseThinking: shouldUseThinking(state, cfg),
		UseEnsemble: shouldUseEnsemble(state, obs, cfg),
	}
	if d.UseEnsemble {
		d.UseThinking = false
	}
	return d
}

// hardCapResult captures whether the current observation breaks the
// per-direction absolute drift ceiling. Computed once per Decide()
// call and reused across every acceptance branch so the gating logic
// stays uniform.
type hardCapResult struct {
	exceeded  bool
	direction string  // "over" / "under", empty when not exceeded
	absDrift  float64 // copied from obs for logging convenience
}

// evalHardCap returns the cap-violation status for the current
// observation. A direction's cap of 0 (or negative) means "disabled"
// — legacy percentage-only behaviour stays in effect for that side.
// The asymmetric design (over vs under) is documented on
// Config.HardMaxOverDriftSec / HardMaxUnderDriftSec.
//
// The direction is decided by OverflowMs sign: > 0 over-run, <= 0
// under-run (including the edge "exact" case which carries no drift
// to cap).
func evalHardCap(obs Observation, cfg Config) hardCapResult {
	if obs.OverflowMs > 0 {
		if cfg.HardMaxOverDriftSec <= 0 {
			return hardCapResult{}
		}
		if obs.AbsDrift-cfg.HardMaxOverDriftSec > 1e-9 {
			return hardCapResult{exceeded: true, direction: "over", absDrift: obs.AbsDrift}
		}
		return hardCapResult{}
	}
	// under-run path
	if cfg.HardMaxUnderDriftSec <= 0 {
		return hardCapResult{}
	}
	if obs.AbsDrift-cfg.HardMaxUnderDriftSec > 1e-9 {
		return hardCapResult{exceeded: true, direction: "under", absDrift: obs.AbsDrift}
	}
	return hardCapResult{}
}

// markHardCap stamps the Decision with HardCapExceeded / HardCapDirection
// when the supplied hardCapResult says we shipped audio outside the
// operator's absolute ceiling. Used only on escape branches that have
// no further retry option — the regular acceptance branches refuse
// the accept outright instead of falling through to mark.
func markHardCap(d Decision, cap hardCapResult) Decision {
	if cap.exceeded {
		d.HardCapExceeded = true
		d.HardCapDirection = cap.direction
	}
	return d
}

// shouldVetoDriftRetry returns true when the OPT-002-followup-4 VETO
// branch should override a drift-driven retranslate. Triggers ONLY when:
//
//  1. cfg.JudgeVetoDriftRetry is enabled.
//  2. The judge actually produced a verdict (Observation.JudgeVerdict
//     != "" — the agent left it empty when the judge was unavailable).
//  3. Judge verdict is "accept" (judge thinks translation is shippable).
//  4. Judge score ≥ cfg.JudgeVetoMinScore (default 0.95).
//  5. Absolute drift is within AdaptiveMaxAcceptableDrift(targetSec).
//
// Why every check matters: we only want to skip the retry when the
// LLM has explicitly endorsed the translation AND the drift is small
// enough to be acceptable for THIS segment length. Skipping retry on
// a perfect-score 2s segment that's drifted 30% is wrong — likely
// truncated audio.
func shouldVetoDriftRetry(obs Observation, cfg Config) bool {
	if !cfg.JudgeVetoDriftRetry {
		return false
	}
	if obs.JudgeVerdict == "" {
		return false
	}
	if obs.JudgeVerdict != "accept" {
		return false
	}
	minScore := cfg.JudgeVetoMinScore
	if minScore <= 0 {
		minScore = 0.95
	}
	if obs.JudgeScore < minScore {
		return false
	}
	if obs.AbsDrift > AdaptiveMaxAcceptableDrift(cfg.TargetSec) {
		return false
	}
	return true
}

// canBorrow returns true when the overflow audio fits inside the
// trailing silence (minus a breath margin) AND either the over-run
// drift is within the borrow tolerance, retranslation is disabled,
// or we've exhausted retries. Mirrors the borrow predicate from
// stage_tts.go:330.
func canBorrow(obs Observation, cfg Config) bool {
	if obs.OverflowMs <= 0 {
		return false
	}
	if cfg.GapAfterMs <= pipettstts.ShortGapThresholdMs {
		return false
	}
	borrowableMs := cfg.GapAfterMs - pipettstts.BreathMarginMs
	if obs.OverflowMs > borrowableMs {
		return false
	}
	if cfg.TargetMs <= 0 {
		return false
	}
	overDrift := float64(obs.OverflowMs) / float64(cfg.TargetMs)
	return overDrift <= cfg.MaxBorrowDriftPct
}

// shouldUseThinking returns true when the agent has either stalled
// (same character count for StuckThreshold consecutive attempts) or
// failed to make progress (AttemptsWithoutImprovement reached
// NonConvergenceWindow). Mirrors the inline `useThinking := ...`
// expression in the legacy code.
func shouldUseThinking(state State, cfg Config) bool {
	stuck := cfg.StuckThreshold
	if stuck <= 0 {
		stuck = 2
	}
	window := cfg.NonConvergenceWindow
	if window <= 0 {
		window = 3
	}
	if state.ConsecutiveSameChars >= stuck {
		return true
	}
	if state.AttemptsWithoutImprovement >= window {
		return true
	}
	return false
}

// shouldUseEnsemble (OPT-202) returns true when the next retranslate
// should be a multi-model fan-out. Three independent triggers, ANY
// hit is sufficient:
//
//  1. cfg.EnsembleImportant — operator-flagged "this segment matters".
//  2. state.AttemptsWithoutImprovement ≥ EnsembleNonConvergenceTrigger
//     — single-model retranslate has demonstrably stopped converging.
//  3. obs.JudgeVerdict is non-empty AND obs.JudgeScore < cfg.EnsembleJudgeScoreTrigger
//     AND state.Attempt ≥ 1 — the judge has weighed in and the score
//     is in the "weak but not abandon-able" band.
//
// The per-segment cap (EnsembleMaxPerSegment) is enforced last: once
// state.EnsembleCallsThisSegment ≥ the cap, no further ensemble
// triggers fire. This is the budget guard — without it a chronically
// non-converging segment could fire ensemble every attempt and burn
// $1+ on its own.
//
// The function takes Observation by value so an empty JudgeVerdict
// trigger evaluates to false naturally (no judge consulted yet).
func shouldUseEnsemble(state State, obs Observation, cfg Config) bool {
	if !cfg.EnsembleEnabled {
		return false
	}
	// Cap default raised from 1 → 2 in OPT-202-followup-1 (B3). The
	// chapter 2 incident showed segments that fire ensemble once,
	// don't converge, then get stuck in over_short_gap retranslate
	// loops because the cap blocked a second ensemble attempt. The
	// over_short_gap_stuck escape in Decide() is the second safety
	// net for the same problem; both work together.
	cap := cfg.EnsembleMaxPerSegment
	if cap <= 0 {
		cap = 2
	}
	if state.EnsembleCallsThisSegment >= cap {
		return false
	}
	if cfg.EnsembleImportant {
		return true
	}
	nonConv := cfg.EnsembleNonConvergenceTrigger
	if nonConv <= 0 {
		nonConv = 2
	}
	if state.AttemptsWithoutImprovement >= nonConv {
		return true
	}
	scoreCut := cfg.EnsembleJudgeScoreTrigger
	if scoreCut <= 0 {
		scoreCut = 0.7
	}
	if obs.JudgeVerdict != "" && obs.JudgeScore < scoreCut && state.Attempt >= 1 {
		return true
	}
	// OPT-202-followup-1 (B2): abs-drift fallback trigger.
	//
	// Root cause from chapter 2 of job 154: long segments converge
	// linearly (best_drift improves a little every retry) so the
	// AttemptsWithoutImprovement counter stays at 0/1 and the existing
	// non-convergence trigger never fires; the judge rarely scores
	// below 0.7 either; nobody flags segments as Important. Net effect:
	// `shouldUseEnsemble` returns false even when single-model
	// retranslate has obviously plateaued well outside the acceptance
	// band. This new trigger says: after we've already retranslated
	// at least twice (state.Attempt >= 2) and the drift is STILL
	// outside the adaptive acceptance band, the single model has
	// demonstrated it can't close the gap on its own — escalate to
	// the multi-model fan-out.
	//
	// The adaptive band reuses AdaptiveMaxAcceptableDrift because that
	// is exactly the band beyond which we'd otherwise spend more
	// retries; pulling the trigger at the same threshold makes the
	// transition operator-comprehensible ("we tried 2x, still outside
	// VETO band → ensemble").
	if state.Attempt >= 2 && obs.AbsDrift > AdaptiveMaxAcceptableDrift(cfg.TargetSec) {
		return true
	}
	return false
}

// ShouldRestoreBest returns true when the loop ended with a worse
// result than the best mid-loop attempt (by more than 0.1 s of
// absolute drift). When true the caller should use the saved
// BestAudioRelPath rather than re-running TTS, because the synthesizer
// is not guaranteed to produce the same duration for the same text.
// Mirrors the legacy `bestAbsDrift < currentAbsDrift - 0.1` guard in
// stage_tts.go:454.
//
// Returns false when BestText is empty (no observation ever improved).
// Same-text attempts are still eligible when BestAudioRelPath is present:
// the TTS backend is not deterministic, so a later attempt can regress in
// duration even if the retranslation text did not change.
func ShouldRestoreBest(state State, currentAbsDrift float64) bool {
	if state.BestText == "" {
		return false
	}
	if state.BestText == state.Text && state.BestAudioRelPath == "" {
		return false
	}
	if state.BestAbsDrift >= currentAbsDrift {
		return false
	}
	return (currentAbsDrift - state.BestAbsDrift) > 0.1
}

// ApplyObservation updates the running State with the latest
// Observation. This is the *post*-Synthesize bookkeeping that lives
// alongside Decide but separately so tests can step through a
// trajectory deterministically:
//
//	state = ApplyObservation(state, obs)
//	d := Decide(state, obs, cfg)
//	if d.Kind == DecisionAccept { break }
//	state = ApplyRetranslate(state, newText, obs, cfg)
//
// Returns the updated state by value (no aliasing of slices that
// would otherwise let callers mutate state.History under our feet).
func ApplyObservation(state State, obs Observation, result ...TTSResult) State {
	// Update running speaking rate.
	obsChars := len([]rune(state.Text))
	if obsChars > 0 && obs.ActualSec > 0 {
		state.TotalObsChars += obsChars
		state.TotalObsSec += obs.ActualSec
		state.ObservedCharsPerSec = float64(state.TotalObsChars) / state.TotalObsSec
	}

	// Update best-result tracking.
	if obs.AbsDrift < state.BestAbsDrift {
		state.BestAbsDrift = obs.AbsDrift
		state.BestText = state.Text
		state.BestActualMs = obs.ActualDurationMs
		if len(result) > 0 {
			state.BestAudioRelPath = result[0].AudioRelPath
			state.BestDiagnostics = append([]string(nil), result[0].Diagnostics...)
			state.BestLoraCandidateRuntime = copyAgentAnyMap(result[0].LoraCandidateRuntime)
		}
		state.AttemptsWithoutImprovement = 0
	} else {
		state.AttemptsWithoutImprovement++
	}
	return state
}

// AttemptOutputRelPath derives a stable, per-attempt audio destination.
// The caller persists the selected attempt's path as the final segment
// audio, which prevents later retries from overwriting the best file.
func AttemptOutputRelPath(base string, attempt int) string {
	if base == "" {
		return ""
	}
	ext := path.Ext(base)
	stem := base
	if ext != "" {
		stem = base[:len(base)-len(ext)]
	}
	return fmt.Sprintf("%s.agent_attempt%02d%s", stem, attempt, ext)
}

func copyAgentAnyMap(in map[string]any) map[string]any {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// ApplyRetranslate updates the State after a successful retranslate
// call: appends history, updates stuck/improvement counters, swaps
// in the new text, and recomputes the adaptive token-budget feedback.
//
// The adaptive feedback rule (mirrors stage_tts.go:431):
//   - actualSec >= targetSec (over-run): feed back into next TTS call.
//   - actualSec <  targetSec (under-run): zero out (under-run feedback
//     would tighten the budget further, the opposite of what we want).
func ApplyRetranslate(state State, newText string, obs Observation, cfg Config) State {
	state.History = append(state.History, llm.RetranslationAttempt{
		Text:      state.Text,
		ActualSec: obs.ActualSec,
	})
	if len([]rune(newText)) == len([]rune(state.Text)) {
		state.ConsecutiveSameChars++
	} else {
		state.ConsecutiveSameChars = 0
	}
	if obs.ActualSec >= cfg.TargetSec {
		state.PrevActualSec = obs.ActualSec
		state.PrevTextChars = pipettstts.CountNonWhitespaceRunes(state.Text)
	} else {
		state.PrevActualSec = 0
		state.PrevTextChars = 0
	}
	state.Text = newText
	state.Attempt++
	return state
}

// ObserveResult builds an Observation from a raw TTSResult plus the
// per-segment Config. Pulled out as a helper so Run() and tests share
// the same drift computation.
func ObserveResult(res TTSResult, cfg Config) Observation {
	actualSec := float64(res.ActualDurationMs) / 1000.0
	overflow := res.ActualDurationMs - cfg.TargetMs
	absDrift := math.Abs(actualSec - cfg.TargetSec)
	driftPct := 0.0
	if cfg.TargetSec > 0 {
		driftPct = absDrift / cfg.TargetSec
	}
	return Observation{
		ActualDurationMs: res.ActualDurationMs,
		ActualSec:        actualSec,
		OverflowMs:       overflow,
		AbsDrift:         absDrift,
		DriftPct:         driftPct,
	}
}

// RunInput collects the per-segment inputs that Run() needs but Decide
// does NOT (Run uses them to issue actual tool calls). All fields are
// immutable across the retry loop.
type RunInput struct {
	JobID                   uint
	SegmentID               uint
	SourceLanguage          string
	TargetLanguage          string
	SourceText              string
	InitialText             string
	CharsPerSecHint         float64
	PriorLoraCandidateCount int

	// VoiceConfig / OutputRelPath / MaxAllowedSec are passed through to
	// every Synthesize call. Idempotency requires OutputRelPath include
	// all distinguishing dimensions (job_id / vp_id / segment_ordinal)
	// so repeated calls overwrite the same file — see agent-design.mdc#6.
	VoiceConfig   map[string]any
	OutputRelPath string
	MaxAllowedSec float64

	// ContextBefore / NextSourceText / TranslationSummary are passed
	// verbatim to RetranslateWithConstraint. Empty values are valid
	// (legacy behaviour: missing context just doesn't help the LLM).
	ContextBefore      []llm.ContextSegment
	NextSourceText     string
	TranslationSummary string

	// EpisodeSummary is the (smaller) summary used by the async judge
	// call. Distinct from TranslationSummary because the judge prompt
	// is fixed-budget while retranslation can include the full glossary.
	EpisodeSummary string

	// DubbingMeta (OPT-204) is the per-segment structured prosody plan
	// pulled from seg.Meta["dubbing"]. Threaded into every Synthesize
	// call via TTSArgs.DubbingMeta. nil for segments translated before
	// OPT-204 / without DUBBING_PLAN_ENABLED.
	DubbingMeta map[string]any
}

// RunOutput is what Run returns. Captures both the final accepted
// audio and the State of the agent at exit so callers (and tests) can
// inspect attempt counts, best tracking, retry history, etc.
type RunOutput struct {
	FinalText                    string
	FinalAudioRelPath            string
	FinalActualMs                int64
	FinalDecision                Decision
	FinalLoraCandidateRuntime    map[string]any
	State                        State
	RestoredFromBest             bool
	RestoredBestHardCapExceeded  bool
	RestoredBestHardCapDirection string

	// JudgeResult (when non-nil) is the verdict the optional async
	// judge produced. Run captures it but does NOT block on its result;
	// see Agent.Run for the contract.
	JudgeResult *JudgeResult
}

// Agent is the OPT-201 SegmentAgent. It owns no state of its own
// (the per-segment State lives in RunOutput); the struct exists to
// bundle the tools + config + structured logger so Run() doesn't need
// 10 positional arguments.
type Agent struct {
	tools  DubbingTools
	logger *slog.Logger

	// observer is invoked once per Decide() call so callers (production
	// pipeline) can emit Prometheus counters without the agent
	// depending on observability. Optional — nil = noop.
	observer DecisionObserver
}

// DecisionObserver is the structural callback the agent fires every
// time it makes a decision. The production wiring routes this to
// observability.IncSegmentAgentDecision (added in PR-3); tests leave
// it nil so they can assert decisions via the returned RunOutput.
type DecisionObserver func(d Decision, state State, obs Observation)

// AgentOption follows the functional-options pattern so future fields
// (e.g. metrics tags) can be added without breaking call sites.
type AgentOption func(*Agent)

// WithLogger plumbs a structured slog logger into the agent. The
// logger is used to emit one INFO line per decision with the standard
// observability fields (segment_id / attempt / decision / reason /
// best_drift_sec / use_thinking).
func WithLogger(l *slog.Logger) AgentOption {
	return func(a *Agent) { a.logger = l }
}

// WithObserver registers a DecisionObserver. See its docstring.
func WithObserver(o DecisionObserver) AgentOption {
	return func(a *Agent) { a.observer = o }
}

// DefaultObservabilityObserver is the production wiring: every decision
// increments the holodub_segment_agent_decisions_total Prometheus
// counter (labels: decision, reason, use_thinking). Tests construct
// agents without WithObserver so they can assert decisions directly
// from RunOutput without touching the global metric registry.
//
// Keep this as a freestanding func (not auto-attached in NewAgent) so
// the package's behaviour stays explicit: tests opt OUT of metrics,
// production opts IN by passing WithObserver(DefaultObservabilityObserver).
func DefaultObservabilityObserver(d Decision, _ State, _ Observation) {
	observability.IncSegmentAgentDecision(d.Kind.String(), d.Reason, d.UseThinking, d.UseEnsemble)
	// OPT-201-followup-2: when the agent ships audio over the absolute
	// drift cap (only happens on escape branches), record it on a
	// dedicated counter so operators can see "how many segments
	// bypassed the cap because we ran out of retries" without parsing
	// agent_decision log lines.
	if d.HardCapExceeded {
		observability.IncHardDriftCapViolation(d.HardCapDirection, d.Reason)
	}
}

// NewAgent constructs a SegmentAgent over the provided DubbingTools.
// Callers MUST pass real tools or fakeTools — a nil tools value is a
// programming error and is caught at construction time so production
// crashes happen at boot rather than mid-segment.
func NewAgent(tools DubbingTools, opts ...AgentOption) *Agent {
	if tools == nil {
		panic("agents.NewAgent: tools is nil")
	}
	a := &Agent{tools: tools, logger: slog.Default()}
	for _, opt := range opts {
		opt(a)
	}
	if a.logger == nil {
		a.logger = slog.Default()
	}
	return a
}

// Run drives the per-segment ReAct loop:
//
//	for attempt := 0; attempt <= cfg.MaxAttempts; attempt++ {
//	    res = tools.Synthesize(...)
//	    state = ApplyObservation(state, ObserveResult(res, cfg))
//	    d = Decide(state, obs, cfg)
//	    if d == DecisionAccept { break }
//	    newText = tools.RetranslateWithConstraint(...)
//	    state = ApplyRetranslate(state, newText, obs, cfg)
//	}
//
// After the loop, Run optionally re-synthesizes the best-text-so-far
// (ShouldRestoreBest) and runs the optional async judge.
//
// Cancellation: ctx is checked at the top of every iteration AND on
// the way out of every tool call (the tools themselves propagate ctx).
// A worker SIGTERM mid-loop returns ctx.Err() wrapped with the segment
// ID so logs surface "which segment was in flight when we shut down".
//
// Errors: tool errors propagate up to the caller (the pipeline wrapper
// converts them to stage failures). A retranslate failure that comes
// AFTER at least one successful synth is treated as
// DecisionAccept(reason=retranslate_failed) instead — we have a usable
// audio in hand, we just couldn't improve it, so the segment ships at
// whatever quality the loop reached. This matches the legacy
// `if retErr != nil { ... break }` behaviour.
func (a *Agent) Run(ctx context.Context, in RunInput, cfg Config) (RunOutput, error) {
	if in.InitialText == "" {
		return RunOutput{}, errors.New("agents.Run: InitialText is empty")
	}
	state := NewState(in.InitialText)
	var (
		lastResult              TTSResult
		lastObs                 Observation
		finalDecision           Decision
		priorLoraCandidateCount = in.PriorLoraCandidateCount
	)

	maxIter := cfg.MaxAttempts + 1 // legacy loop is `attempt <= MaxAttempts`
	if maxIter < 1 {
		maxIter = 1
	}
	for i := 0; i < maxIter; i++ {
		if err := ctx.Err(); err != nil {
			return RunOutput{State: state}, fmt.Errorf("segment_agent run cancelled (segment %d, attempt %d): %w", in.SegmentID, state.Attempt, err)
		}

		res, err := a.tools.Synthesize(ctx, TTSArgs{
			JobID:                   in.JobID,
			SegmentID:               in.SegmentID,
			Attempt:                 state.Attempt,
			PriorLoraCandidateCount: priorLoraCandidateCount,
			Text:                    state.Text,
			TargetLanguage:          in.TargetLanguage,
			CharsPerSecHint:         in.CharsPerSecHint,
			TargetDurationSec:       cfg.TargetSec,
			MaxAllowedSec:           in.MaxAllowedSec,
			VoiceConfig:             in.VoiceConfig,
			PrevActualSec:           state.PrevActualSec,
			PrevTextChars:           state.PrevTextChars,
			DubbingMeta:             in.DubbingMeta,
			OutputRelPath:           AttemptOutputRelPath(in.OutputRelPath, state.Attempt),
		})
		if err != nil {
			return RunOutput{State: state}, fmt.Errorf("segment_agent synthesize (segment %d, attempt %d): %w", in.SegmentID, state.Attempt, err)
		}

		lastResult = res
		lastObs = ObserveResult(res, cfg)
		state = ApplyObservation(state, lastObs, res)
		if loraCandidateAllRejected(res.LoraCandidateRuntime) {
			priorLoraCandidateCount++
		}

		decision := Decide(state, lastObs, cfg)
		if decision.Kind == DecisionRetranslate && loraCandidateGateSelected(res, lastObs, cfg) {
			decision = Decision{Kind: DecisionAccept, Reason: "lora_candidate_gate"}
		}

		// OPT-002-followup-4: if the first pass decided to
		// retranslate due to drift AND JudgeVetoDriftRetry is on,
		// consult the judge before paying for another LLM
		// retranslate + TTS round. The judge call is ~$0.001 with
		// qwen-turbo; we only do it when we'd otherwise spend ~$0.01
		// retranslating + re-synthesizing → strictly cheaper if
		// the VETO triggers more than ~10% of the time.
		if decision.Kind == DecisionRetranslate && cfg.JudgeVetoDriftRetry {
			lastObs = a.maybeAttachJudge(ctx, in, lastObs, state)
			decision = Decide(state, lastObs, cfg)
		}

		if decision.Kind == DecisionAccept {
			decision = markHardCap(decision, evalHardCap(lastObs, cfg))
		}
		if decision.Kind == DecisionAccept && maxImpliedFitDelta(cfg) > 0 && impliedFitDelta(lastObs, cfg) > maxImpliedFitDelta(cfg) {
			if state.Attempt >= cfg.MaxAttempts || !cfg.RetranslationEnabled {
				finalDecision = decision
				a.emit(decision, state, lastObs, in)
				return RunOutput{
					FinalText:                 state.Text,
					FinalAudioRelPath:         lastResult.AudioRelPath,
					FinalActualMs:             lastResult.ActualDurationMs,
					FinalDecision:             finalDecision,
					FinalLoraCandidateRuntime: copyAgentAnyMap(lastResult.LoraCandidateRuntime),
					State:                     state,
				}, fmt.Errorf("segment_agent fit delta rejected (segment %d, attempt %d, reason %s, implied_fit_delta %.4f, max %.4f)", in.SegmentID, state.Attempt, decision.Reason, impliedFitDelta(lastObs, cfg), maxImpliedFitDelta(cfg))
			}
			decision = Decision{
				Kind:        DecisionRetranslate,
				Reason:      "high_implied_fit_delta",
				UseThinking: shouldUseThinking(state, cfg),
				UseEnsemble: shouldUseEnsemble(state, lastObs, cfg),
			}
			if decision.UseEnsemble {
				decision.UseThinking = false
			}
		}
		if decision.Kind == DecisionAccept && decision.HardCapExceeded && loraCandidateNeedsTextCompression(lastResult.LoraCandidateRuntime) {
			if state.Attempt >= cfg.MaxAttempts {
				finalDecision = decision
				a.emit(decision, state, lastObs, in)
				return RunOutput{
					FinalText:                 state.Text,
					FinalAudioRelPath:         lastResult.AudioRelPath,
					FinalActualMs:             lastResult.ActualDurationMs,
					FinalDecision:             finalDecision,
					FinalLoraCandidateRuntime: copyAgentAnyMap(lastResult.LoraCandidateRuntime),
					State:                     state,
				}, fmt.Errorf("segment_agent lora candidate requires text compression; refusing hard-cap accept (segment %d, attempt %d, reason %s, direction %s, drift %.3fs)", in.SegmentID, state.Attempt, decision.Reason, decision.HardCapDirection, lastObs.AbsDrift)
			}
			decision = Decision{
				Kind:        DecisionRetranslate,
				Reason:      "lora_candidate_text_compression_hard_cap",
				UseThinking: true,
				UseEnsemble: shouldUseEnsemble(state, lastObs, cfg),
			}
			if decision.UseEnsemble {
				decision.UseThinking = false
			}
		}
		if decision.Kind == DecisionAccept && decision.HardCapExceeded && cfg.RejectHardCapEscape {
			finalDecision = decision
			a.emit(decision, state, lastObs, in)
			return RunOutput{
				FinalText:                 state.Text,
				FinalAudioRelPath:         lastResult.AudioRelPath,
				FinalActualMs:             lastResult.ActualDurationMs,
				FinalDecision:             finalDecision,
				FinalLoraCandidateRuntime: copyAgentAnyMap(lastResult.LoraCandidateRuntime),
				State:                     state,
			}, fmt.Errorf("segment_agent hard-cap escape rejected (segment %d, attempt %d, reason %s, direction %s, drift %.3fs)", in.SegmentID, state.Attempt, decision.Reason, decision.HardCapDirection, lastObs.AbsDrift)
		}
		finalDecision = decision
		a.emit(decision, state, lastObs, in)

		if decision.Kind == DecisionAccept {
			break
		}

		// DecisionRetranslate: ask LLM for new text, then retry.
		retranslateObs := lastObs
		compressionHint := loraCandidateCompressionHint(lastResult.LoraCandidateRuntime, state.Text, cfg)
		if adjustedObs, ok := loraCandidateTextCompressionObservation(lastResult.LoraCandidateRuntime, lastObs, cfg); ok {
			retranslateObs = adjustedObs
			a.logger.Info("segment_agent: using lora candidate text-compression timing",
				"segment_id", in.SegmentID,
				"job_id", in.JobID,
				"fallback_actual_sec", lastObs.ActualSec,
				"candidate_actual_sec", adjustedObs.ActualSec,
				"target_sec", cfg.TargetSec,
			)
		}

		// When the decision flagged UseEnsemble (OPT-202), try the
		// multi-model fan-out first; on ErrEnsembleUnavailable fall
		// back to the single-model path so the loop always makes
		// progress. ErrEnsembleUnavailable is NOT a segment failure.
		retranslateArgs := RetranslateArgs{
			SourceLanguage:      in.SourceLanguage,
			TargetLanguage:      in.TargetLanguage,
			SourceText:          in.SourceText,
			CurrentTrans:        state.Text,
			TargetSec:           cfg.TargetSec,
			ActualSec:           retranslateObs.ActualSec,
			Attempt:             state.Attempt + 1,
			MaxAttempts:         cfg.MaxAttempts,
			DriftThresholdPct:   cfg.DriftThreshold,
			History:             append([]llm.RetranslationAttempt(nil), state.History...),
			UseThinking:         decision.UseThinking,
			ObservedCharsPerSec: state.ObservedCharsPerSec,
			ContextBefore:       in.ContextBefore,
			NextSourceText:      in.NextSourceText,
			TranslationSummary:  in.TranslationSummary,
			CompressionHint:     compressionHint,
		}

		var newText RetranslateResult
		var retErr error
		if decision.UseEnsemble {
			ens, ensErr := a.tools.RetranslateEnsemble(ctx, retranslateArgs)
			if ensErr == nil {
				newText = RetranslateResult{Text: ens.Text, UsedThinking: false}
				state.EnsembleCallsThisSegment++
				a.logger.Info("segment_agent: ensemble winner",
					"segment_id", in.SegmentID,
					"job_id", in.JobID,
					"attempt", state.Attempt,
					"winner_model", ens.Model,
					"judge_score", ens.JudgeScore,
					"candidate_count", ens.CandidateCount,
				)
			} else if errors.Is(ensErr, ErrEnsembleUnavailable) {
				// Operator hasn't configured ensemble; degrade to
				// single-model with the standard thinking-mode
				// decision so a non-converging segment still gets
				// the stronger model. NOT logged as a warning —
				// ErrEnsembleUnavailable is expected during rollout.
				retranslateArgs.UseThinking = shouldUseThinking(state, cfg)
				newText, retErr = a.tools.RetranslateWithConstraint(ctx, retranslateArgs)
			} else {
				// Real ensemble failure (network / all candidates
				// rejected). Fall back to single-model retranslate
				// but log because operators should see this in
				// production — repeated failures could indicate
				// one of the configured ensemble models is broken.
				a.logger.Warn("segment_agent: ensemble failed, falling back to single-model retranslate",
					"segment_id", in.SegmentID,
					"job_id", in.JobID,
					"attempt", state.Attempt,
					"error", ensErr,
				)
				retranslateArgs.UseThinking = shouldUseThinking(state, cfg)
				newText, retErr = a.tools.RetranslateWithConstraint(ctx, retranslateArgs)
			}
		} else {
			newText, retErr = a.tools.RetranslateWithConstraint(ctx, retranslateArgs)
		}
		if retErr != nil {
			// Context cancellation is NOT a retranslate failure; it's
			// the operator killing the job. Propagate immediately so
			// the caller doesn't think we shipped a degraded result —
			// the segment must be retried later, not accepted as-is.
			if errors.Is(retErr, context.Canceled) || errors.Is(retErr, context.DeadlineExceeded) {
				return RunOutput{State: state}, fmt.Errorf("segment_agent retranslate (segment %d, attempt %d): %w", in.SegmentID, state.Attempt, retErr)
			}
			// Legacy contract: retranslate failure with at least one
			// successful synth → accept current result and exit.
			finalDecision = markHardCap(Decision{Kind: DecisionAccept, Reason: "retranslate_failed"}, evalHardCap(lastObs, cfg))
			a.emit(finalDecision, state, lastObs, in)
			a.logger.Warn("segment_agent: retranslate failed, accepting current result",
				"segment_id", in.SegmentID,
				"job_id", in.JobID,
				"attempt", state.Attempt,
				"error", retErr,
			)
			break
		}
		state = ApplyRetranslate(state, newText.Text, retranslateObs, cfg)
	}

	out := RunOutput{
		FinalText:                 state.Text,
		FinalAudioRelPath:         lastResult.AudioRelPath,
		FinalActualMs:             lastResult.ActualDurationMs,
		FinalDecision:             finalDecision,
		FinalLoraCandidateRuntime: copyAgentAnyMap(lastResult.LoraCandidateRuntime),
		State:                     state,
	}

	// Best-result restore: re-synth with bestText if the loop ended
	// worse than the best mid-loop attempt by > 0.1 s.
	currentAbsDrift := math.Abs(float64(lastResult.ActualDurationMs)/1000.0 - cfg.TargetSec)
	currentCap := evalHardCap(lastObs, cfg)
	if ShouldRestoreBest(state, currentAbsDrift) {
		if state.BestAudioRelPath != "" {
			bestObs := ObserveResult(TTSResult{ActualDurationMs: state.BestActualMs}, cfg)
			bestCap := evalHardCap(bestObs, cfg)
			leastBadHardCapRestore := bestCap.exceeded &&
				currentCap.exceeded &&
				withinLeastBadRestoreCap(state.BestAbsDrift, currentAbsDrift, bestCap, cfg)
			if bestCap.exceeded && !leastBadHardCapRestore {
				a.logger.Warn("segment_agent: best-result restore skipped; best audio exceeds hard drift cap",
					"segment_id", in.SegmentID,
					"job_id", in.JobID,
					"best_drift_sec", state.BestAbsDrift,
					"final_drift_sec", currentAbsDrift,
					"best_actual_ms", state.BestActualMs,
					"best_audio_relpath", state.BestAudioRelPath,
					"hard_cap_direction", bestCap.direction,
				)
			} else {
				out.FinalText = state.BestText
				out.FinalAudioRelPath = state.BestAudioRelPath
				out.FinalActualMs = state.BestActualMs
				out.FinalLoraCandidateRuntime = copyAgentAnyMap(state.BestLoraCandidateRuntime)
				out.RestoredFromBest = true
				if bestCap.exceeded {
					out.FinalDecision = markHardCap(out.FinalDecision, bestCap)
					out.RestoredBestHardCapExceeded = true
					out.RestoredBestHardCapDirection = bestCap.direction
					a.logger.Warn("segment_agent: restored least-bad hard-cap-violating best attempt",
						"segment_id", in.SegmentID,
						"job_id", in.JobID,
						"best_drift_sec", state.BestAbsDrift,
						"final_drift_sec", currentAbsDrift,
						"best_actual_ms", state.BestActualMs,
						"best_audio_relpath", state.BestAudioRelPath,
						"hard_cap_direction", bestCap.direction,
					)
				} else {
					a.logger.Info("segment_agent: restored best mid-loop attempt",
						"segment_id", in.SegmentID,
						"job_id", in.JobID,
						"best_drift_sec", state.BestAbsDrift,
						"final_drift_sec", currentAbsDrift,
						"best_actual_ms", state.BestActualMs,
						"best_audio_relpath", state.BestAudioRelPath,
					)
				}
			}
		} else {
			a.logger.Warn("segment_agent: best-result restore skipped; best audio path missing",
				"segment_id", in.SegmentID,
				"job_id", in.JobID,
				"best_drift_sec", state.BestAbsDrift,
				"final_drift_sec", currentAbsDrift,
			)
		}
	}

	if out.FinalDecision.HardCapExceeded && loraCandidateNeedsTextCompression(out.FinalLoraCandidateRuntime) {
		return out, fmt.Errorf("segment_agent lora candidate requires text compression; refusing restored hard-cap output (segment %d, reason %s, direction %s)", in.SegmentID, out.FinalDecision.Reason, out.FinalDecision.HardCapDirection)
	}
	if out.FinalDecision.HardCapExceeded && cfg.RejectHardCapEscape {
		return out, fmt.Errorf("segment_agent hard-cap escape rejected after best-restore check (segment %d, reason %s, direction %s)", in.SegmentID, out.FinalDecision.Reason, out.FinalDecision.HardCapDirection)
	}
	return out, nil
}

func withinLeastBadRestoreCap(bestAbsDriftSec float64, currentAbsDriftSec float64, bestCap hardCapResult, cfg Config) bool {
	if !bestCap.exceeded {
		return false
	}
	if currentAbsDriftSec-bestAbsDriftSec <= 0.1 {
		return false
	}
	cap := cfg.AbsMaxDriftSec
	if cfg.HardMaxUnderDriftSec > cap {
		cap = cfg.HardMaxUnderDriftSec
	}
	if doubledOver := cfg.HardMaxOverDriftSec * 2; doubledOver > cap {
		cap = doubledOver
	}
	if cap <= 0 {
		return false
	}
	return bestAbsDriftSec-cap <= 1e-9
}

func loraCandidateGateSelected(res TTSResult, obs Observation, cfg Config) bool {
	if evalHardCap(obs, cfg).exceeded {
		return false
	}
	runtime := res.LoraCandidateRuntime
	if len(runtime) == 0 {
		return false
	}
	if stringValue(runtime["audio_selection_mode"]) != "candidate_audio" {
		return false
	}
	if stringValue(runtime["fallback_reason"]) != "" {
		return false
	}
	gate, ok := runtime["gate_decision"].(map[string]any)
	if !ok {
		return false
	}
	if stringValue(gate["status"]) != "selected" {
		return false
	}
	if loraCandidateSelectedHasHardFitFailure(runtime, maxImpliedFitDelta(cfg)) {
		return false
	}
	return true
}

func maxImpliedFitDelta(cfg Config) float64 {
	if cfg.MaxImpliedFitDelta > 0 {
		return cfg.MaxImpliedFitDelta
	}
	return 0
}

func impliedFitDelta(obs Observation, cfg Config) float64 {
	if cfg.TargetSec <= 0 {
		return 0
	}
	return math.Abs(obs.ActualSec/cfg.TargetSec - 1.0)
}

func loraCandidateSelectedHasHardFitFailure(runtime map[string]any, maxFitDelta float64) bool {
	if len(runtime) == 0 {
		return false
	}
	gate, _ := runtime["gate_decision"].(map[string]any)
	if hasRejectReason(gate["reject_reasons"], "high_fit_delta") {
		return true
	}
	selected := selectedCandidateRuntime(runtime)
	if len(selected) == 0 {
		return false
	}
	if hasRejectReason(selected["reject_reasons"], "high_fit_delta") {
		return true
	}
	if stringValue(selected["fit_reject_reason"]) == "high_fit_delta" {
		return true
	}
	if maxFitDelta > 0 && floatValue(selected["fit_tempo_delta"])-maxFitDelta > 1e-9 {
		return true
	}
	return false
}

func selectedCandidateRuntime(runtime map[string]any) map[string]any {
	if selected, _ := runtime["selected_candidate"].(map[string]any); len(selected) > 0 {
		return selected
	}
	selectedFamily := stringValue(runtime["selected_family"])
	selectedStrategy := stringValue(runtime["selected_split_strategy"])
	if selectedFamily == "" {
		return nil
	}
	raw, ok := runtime["candidates"].([]any)
	if !ok {
		return nil
	}
	var fallback map[string]any
	for _, item := range raw {
		row, _ := item.(map[string]any)
		if len(row) == 0 || stringValue(row["family"]) != selectedFamily {
			continue
		}
		if stringValue(row["split_strategy"]) == selectedStrategy {
			return row
		}
		if fallback == nil {
			fallback = row
		}
	}
	return fallback
}

func hasRejectReason(raw any, reason string) bool {
	switch values := raw.(type) {
	case []any:
		for _, value := range values {
			if stringValue(value) == reason {
				return true
			}
		}
	case []string:
		for _, value := range values {
			if value == reason {
				return true
			}
		}
	}
	return false
}

func loraCandidateAllRejected(runtime map[string]any) bool {
	if len(runtime) == 0 {
		return false
	}
	if loraCandidateNeedsTextCompression(runtime) {
		return false
	}
	return stringValue(runtime["fallback_reason"]) == "all_candidates_rejected"
}

func loraCandidateTextCompressionObservation(runtime map[string]any, fallbackObs Observation, cfg Config) (Observation, bool) {
	if len(runtime) == 0 {
		return Observation{}, false
	}
	gate, _ := runtime["gate_decision"].(map[string]any)
	if gate != nil && gate["needs_text_compression"] != true {
		return Observation{}, false
	}
	recommendation, _ := runtime["text_compression_recommendation"].(map[string]any)
	if recommendation == nil || recommendation["needed"] != true {
		return Observation{}, false
	}
	driftMs := floatValue(recommendation["best_drift_ms"])
	if driftMs <= 0 {
		requiredCPS := floatValue(recommendation["max_required_chars_per_sec"])
		allowedCPS := floatValue(recommendation["max_allowed_chars_per_sec"])
		if requiredCPS > allowedCPS && allowedCPS > 0 {
			driftMs = cfg.TargetSec * (requiredCPS/allowedCPS - 1.0) * 1000.0
		}
	}
	if driftMs <= 0 || cfg.TargetSec <= 0 {
		return Observation{}, false
	}
	actualSec := cfg.TargetSec + driftMs/1000.0
	actualMs := int64(math.Round(actualSec * 1000.0))
	obs := fallbackObs
	obs.ActualSec = actualSec
	obs.ActualDurationMs = actualMs
	obs.OverflowMs = actualMs - cfg.TargetMs
	obs.AbsDrift = math.Abs(actualSec - cfg.TargetSec)
	obs.DriftPct = obs.AbsDrift / cfg.TargetSec
	return obs, true
}

func loraCandidateCompressionHint(runtime map[string]any, currentText string, cfg Config) *llm.DurationCompressionHint {
	if len(runtime) == 0 || cfg.TargetSec <= 0 {
		return nil
	}
	recommendation, _ := runtime["text_compression_recommendation"].(map[string]any)
	if recommendation == nil || recommendation["needed"] != true {
		return nil
	}
	allowedCPS := floatValue(recommendation["max_allowed_chars_per_sec"])
	requiredCPS := floatValue(recommendation["max_required_chars_per_sec"])
	recommendedMaxChars := int(math.Floor(cfg.TargetSec * allowedCPS))
	if recommendedMaxChars <= 0 {
		return nil
	}
	riskIndexes := []int{}
	switch raw := recommendation["risk_part_indexes"].(type) {
	case []any:
		for _, item := range raw {
			if value := int(floatValue(item)); value > 0 {
				riskIndexes = append(riskIndexes, value)
			}
		}
	case []int:
		riskIndexes = append(riskIndexes, raw...)
	}
	return &llm.DurationCompressionHint{
		Reason:                 stringValue(recommendation["reason"]),
		MaxAllowedCharsPerSec:  allowedCPS,
		MaxRequiredCharsPerSec: requiredCPS,
		RecommendedMaxChars:    recommendedMaxChars,
		CurrentChars:           len([]rune(currentText)),
		RiskPartIndexes:        riskIndexes,
	}
}

func loraCandidateNeedsTextCompression(runtime map[string]any) bool {
	if len(runtime) == 0 {
		return false
	}
	gate, _ := runtime["gate_decision"].(map[string]any)
	if gate != nil && gate["needs_text_compression"] == true {
		return true
	}
	recommendation, _ := runtime["text_compression_recommendation"].(map[string]any)
	if recommendation != nil && recommendation["needed"] == true {
		return true
	}
	if stringValue(runtime["stop_reason"]) == "needs_text_compression_speech_rate" {
		return true
	}
	previous, _ := runtime["previous_candidate_runtime"].(map[string]any)
	return loraCandidateNeedsTextCompression(previous)
}

func stringValue(v any) string {
	switch x := v.(type) {
	case string:
		return x
	default:
		return ""
	}
}

func floatValue(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case float32:
		return float64(x)
	case int:
		return float64(x)
	case int64:
		return float64(x)
	case json.Number:
		f, _ := x.Float64()
		return f
	default:
		return 0
	}
}

// maybeAttachJudge calls the judge tool synchronously and writes the
// verdict + score back into the Observation. Used by Run() when the
// VETO branch (OPT-002-followup-4) is enabled and the agent is about
// to retranslate due to drift. Returns the original observation
// unchanged on any failure — VETO is a best-effort optimization,
// never a correctness lever.
//
// Cost-control invariant: at most ONE judge call per attempt; the
// loop's RecordedJudge count stays bounded by maxIter even on the
// worst-case (judge fails every attempt, drift never improves) path.
func (a *Agent) maybeAttachJudge(ctx context.Context, in RunInput, obs Observation, state State) Observation {
	if a.tools == nil {
		return obs
	}
	if in.SourceText == "" || state.Text == "" {
		return obs
	}
	result, err := a.tools.JudgeFidelity(ctx, llm.JudgeArgs{
		SrcText:        in.SourceText,
		TgtText:        state.Text,
		SrcLang:        in.SourceLanguage,
		TgtLang:        in.TargetLanguage,
		EpisodeSummary: in.EpisodeSummary,
		PrevContext:    in.ContextBefore,
	})
	if err != nil || result == nil {
		// Judge unavailable / parse failure — keep flying with the
		// drift-based retry path. Log so operators can spot a noisy
		// upstream without paging anyone (judge is best-effort).
		if err != nil {
			a.logger.Warn("segment_agent: judge call failed during veto path",
				"segment_id", in.SegmentID,
				"job_id", in.JobID,
				"attempt", state.Attempt,
				"error", err,
			)
		}
		return obs
	}
	obs.JudgeVerdict = result.Verdict
	obs.JudgeScore = result.OverallScore()
	return obs
}

// emit fires the optional observer callback and writes a structured
// slog INFO line for the decision. Kept as a helper so Run reads
// linearly. Logging deliberately uses the same key names as the
// legacy code (segment_id / job_id / attempt / stage) so existing log
// queries keep working unchanged, and adds OPT-201-specific fields
// (decision / reason / use_thinking / best_drift_sec /
// attempts_without_improvement / consecutive_same_chars) so a future
// LangFuse / Phoenix exporter can build a span tree without re-deriving
// state from raw events.
//
// Mandatory fields enforced by observability-and-cost.mdc §4:
//   - job_id (when in a job context)
//   - stage  (always "tts_duration" — the agent only runs in that stage)
func (a *Agent) emit(d Decision, state State, obs Observation, in RunInput) {
	if a.observer != nil {
		a.observer(d, state, obs)
	}
	if a.logger == nil {
		return
	}
	// Pre-compute display-friendly fields. drift_pct rounded to 4
	// decimal places (= 1bp) so log lines stay short. best_drift_sec
	// uses math.MaxFloat64 as the "no data" sentinel; converting it
	// to a finite value here keeps log JSON valid (some sinks reject
	// raw inf/NaN).
	bestDrift := state.BestAbsDrift
	if math.IsInf(bestDrift, 0) || math.IsNaN(bestDrift) || bestDrift > 1e6 {
		bestDrift = -1
	}
	// OPT-201-followup-2: emit the hard-cap violation flag inline so a
	// single agent_decision log line lets operators grep
	// `hard_cap_exceeded=true` to see every segment we shipped over
	// the operator's absolute drift ceiling. The field is always
	// present (false when the cap is satisfied) so log queries can
	// filter without missing entries from older releases.
	a.logger.Info("agent_decision",
		"segment_id", in.SegmentID,
		"job_id", in.JobID,
		"stage", "tts_duration",
		"attempt", state.Attempt,
		"decision", d.Kind.String(),
		"reason", d.Reason,
		"use_thinking", d.UseThinking,
		"use_ensemble", d.UseEnsemble,
		"ensemble_calls", state.EnsembleCallsThisSegment,
		"actual_sec", obs.ActualSec,
		"drift_pct", math.Round(obs.DriftPct*10000)/10000,
		"best_drift_sec", bestDrift,
		"attempts_without_improvement", state.AttemptsWithoutImprovement,
		"consecutive_same_chars", state.ConsecutiveSameChars,
		"overflow_ms", obs.OverflowMs,
		"hard_cap_exceeded", d.HardCapExceeded,
		"hard_cap_direction", d.HardCapDirection,
	)
}
