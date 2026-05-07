// Package tts contains pure decision functions extracted from the TTS stage.
// They have no dependencies on store, queue, ml client or external state, so
// they can be exhaustively unit-tested.
//
// The TTS stage in pipeline.go used to be a single ~350 line function that
// mixed:
//
//   - Token / duration budgeting
//   - Drift threshold computation
//   - Overflow policy (accept / borrow gap / re-translate)
//   - Adaptive char-rate blending
//   - LLM re-translation orchestration
//   - Database persistence
//
// Mixing these made the code untestable. By extracting the pure decisions
// here, the orchestration layer in pipeline.go becomes a thin shell that
// reads inputs, calls these helpers, and applies the resulting effects.
package tts

import "math"

// Constants shared across the TTS budgeting code. They originally lived as
// magic numbers inside processOneTTSSegment.
const (
	// BreathMarginMs is the minimum silence we preserve between sentences,
	// even when borrowing from a trailing gap. Below this threshold the
	// dub feels rushed and unnatural.
	BreathMarginMs int64 = 300

	// ShortGapThresholdMs marks a gap so tight that we should not even
	// attempt to borrow on overflow — instead we go straight to forced
	// re-translation.
	ShortGapThresholdMs int64 = 1000

	// DefaultGapAfterMs is used for the final segment in a job (no
	// successor). 30 seconds is large enough that the borrow-vs-retranslate
	// path will never trigger on the trailing gap alone.
	DefaultGapAfterMs int64 = 30_000
)

// EffectiveDriftThreshold returns the per-segment drift threshold (a
// fraction, e.g. 0.06 = 6 %) after blending the relative threshold, the
// absolute-seconds cap, and the relative floor.
//
// Inputs:
//   - relThreshold:    base drift threshold (e.g. cfg.RetranslationDriftThreshold)
//   - absMaxDriftSec:  absolute drift cap in seconds (0 disables)
//   - minRelThreshold: floor for the relative threshold; prevents impossibly
//     strict targets when targetSec is large
//   - targetSec:       segment target duration in seconds
//
// Behaviour:
//   - if absMaxDriftSec > 0 and targetSec > 0, the absolute cap is converted
//     to a relative threshold and only applied if it is *stricter* than the
//     base relative threshold.
//   - the result is then clamped from below by minRelThreshold (when > 0).
func EffectiveDriftThreshold(relThreshold, absMaxDriftSec, minRelThreshold, targetSec float64) float64 {
	threshold := relThreshold
	if absMaxDriftSec > 0 && targetSec > 0 {
		absThreshold := absMaxDriftSec / targetSec
		if absThreshold < threshold {
			threshold = absThreshold
		}
	}
	if minRelThreshold > 0 && threshold < minRelThreshold {
		threshold = minRelThreshold
	}
	return threshold
}

// EffectiveBorrowDriftPct returns the maximum overflow drift (relative to
// targetMs) that is still acceptable for "borrow from trailing gap" without
// triggering re-translation. It applies the absolute drift cap to the
// configured borrow percentage so long segments are held to the same
// absolute ceiling as short ones.
func EffectiveBorrowDriftPct(maxBorrowDriftPct, absMaxDriftSec float64, targetMs int64) float64 {
	pct := maxBorrowDriftPct
	if absMaxDriftSec > 0 && targetMs > 0 {
		absCap := absMaxDriftSec / (float64(targetMs) / 1000.0)
		if absCap < pct {
			pct = absCap
		}
	}
	return pct
}

// MaxAllowedSec returns the hard ceiling for a segment's TTS audio length:
// the target duration plus the entire trailing gap. The TTS adapter uses
// this as the upper bound on its mel-token budget; physical clipping during
// merge is handled separately.
func MaxAllowedSec(targetSec float64, gapAfterMs int64) float64 {
	if gapAfterMs < 0 {
		gapAfterMs = 0
	}
	return targetSec + float64(gapAfterMs)/1000.0
}

// GapAfter returns the gap (in milliseconds) between segment idx and the
// next segment in the slice. If idx is the last segment, DefaultGapAfterMs
// is returned. Negative observed gaps (overlapping segments) are clamped
// to zero.
//
// Generic over a minimal interface so callers can pass a slice of structs
// without copying their fields out.
func GapAfter[T interface {
	GetStartMs() int64
	GetEndMs() int64
}](segments []T, idx int) int64 {
	if idx+1 >= len(segments) {
		return DefaultGapAfterMs
	}
	gap := segments[idx+1].GetStartMs() - segments[idx].GetEndMs()
	if gap < 0 {
		return 0
	}
	return gap
}

// OverflowAction encodes what should happen when TTS audio for a segment
// is longer than the target slot.
type OverflowAction int

const (
	// OverflowAccept means the actual duration is within drift tolerance
	// (or under-runs the target); take the result as-is.
	OverflowAccept OverflowAction = iota

	// OverflowBorrow means the overflow fits inside the trailing gap and
	// satisfies the borrow drift cap. The merge stage will clip it later.
	OverflowBorrow

	// OverflowRetranslate means the overflow exceeds what the gap can
	// absorb (or the gap is too short); ask the LLM for a tighter
	// translation and try again.
	OverflowRetranslate
)

// OverflowDecisionInput collects the inputs to DecideOverflow.
type OverflowDecisionInput struct {
	ActualMs            int64
	TargetMs            int64
	GapAfterMs          int64
	MaxBorrowDriftPct   float64
	AbsMaxDriftSec      float64
	DriftThreshold      float64 // for under-run / overall-drift acceptance
	RetranslationOn     bool
	IsLastAttempt       bool
}

// DecideOverflow encodes the overflow policy that previously lived inline
// in processOneTTSSegment. See OverflowAction for the three possible
// outcomes. The decision is a pure function of the inputs.
//
// Policy summary:
//
//   - actual <= target: accept if drift is within threshold OR retranslation
//     is disabled OR we've exhausted attempts; otherwise re-translate.
//   - actual >  target:
//   - if overflow fits within (gap - breathMargin) AND drift is within
//     borrow tolerance (or we've exhausted attempts): borrow.
//   - if gap is too short to borrow: re-translate (or accept if no more
//     attempts).
func DecideOverflow(in OverflowDecisionInput) OverflowAction {
	overflow := in.ActualMs - in.TargetMs
	if overflow <= 0 {
		actualSec := float64(in.ActualMs) / 1000.0
		targetSec := float64(in.TargetMs) / 1000.0
		drift := math.Abs(actualSec-targetSec) / targetSec
		if drift <= in.DriftThreshold || !in.RetranslationOn || in.IsLastAttempt {
			return OverflowAccept
		}
		return OverflowRetranslate
	}

	borrowableMs := in.GapAfterMs - BreathMarginMs
	overDrift := float64(overflow) / float64(in.TargetMs)
	borrowPct := EffectiveBorrowDriftPct(in.MaxBorrowDriftPct, in.AbsMaxDriftSec, in.TargetMs)
	withinBorrowDrift := overDrift <= borrowPct

	if overflow <= borrowableMs && in.GapAfterMs > ShortGapThresholdMs &&
		(withinBorrowDrift || !in.RetranslationOn || in.IsLastAttempt) {
		return OverflowBorrow
	}
	if !in.RetranslationOn || in.IsLastAttempt {
		return OverflowAccept
	}
	return OverflowRetranslate
}

// BlendCharsPerSec returns an updated estimate of the TTS speaking rate
// (chars per second) from a running total of observed chars and seconds.
// Returns 0 if there is no data yet.
func BlendCharsPerSec(totalChars int, totalSec float64) float64 {
	if totalChars <= 0 || totalSec <= 0 {
		return 0
	}
	return float64(totalChars) / totalSec
}

// CountNonWhitespaceRunes returns the number of non-whitespace code points
// in s. Used as the "chars" for adaptive token-budget feedback.
func CountNonWhitespaceRunes(s string) int {
	count := 0
	for _, r := range s {
		switch r {
		case ' ', '\t', '\n', '\r':
			continue
		default:
			count++
		}
	}
	return count
}
