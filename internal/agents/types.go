package agents

import (
	"math"

	"holodub/internal/llm"
)

// DecisionKind is the discrete set of next-step actions the SegmentAgent
// can take after observing a TTS attempt. The set was distilled from the
// original 180-line for-loop in internal/pipeline/stage_tts.go::processOneTTSSegment;
// every break / continue / fall-through in that loop maps to one of these
// kinds plus a free-form Reason string.
type DecisionKind int

const (
	// DecisionUnknown is the zero value; reading it from a Decide result
	// indicates a programming error (caller forgot to map a branch).
	DecisionUnknown DecisionKind = iota

	// DecisionAccept means: keep the current text and audio, exit the
	// retry loop. The Reason field disambiguates why we accepted
	// (within_threshold / under_run / borrow_from_gap / clip_overflow /
	// no_more_attempts / retranslation_disabled / retranslate_failed /
	// judge_veto_skipped).
	//
	// Borrow-from-gap and clip-overflow are bundled under DecisionAccept
	// because the audio handling downstream is identical: keep this
	// attempt's WAV, let the merge stage clip / pad as needed. The
	// distinction lives in Reason so observability can still see it.
	DecisionAccept

	// DecisionRetranslate means: ask the LLM for a new translation and
	// then re-synthesize. UseThinking on the Decision indicates whether
	// the agent wants the (slower, smarter) reasoning model.
	DecisionRetranslate
)

func (k DecisionKind) String() string {
	switch k {
	case DecisionAccept:
		return "accept"
	case DecisionRetranslate:
		return "retranslate"
	default:
		return "unknown"
	}
}

// Decision is the result of one Decide() call. It is a small flat value
// type so tests can compare with == and so the agent loop can pass it
// through slog without further marshalling.
//
// Reason is a stable, low-cardinality string suitable for both log
// messages and the holodub_segment_agent_decisions_total{reason}
// Prometheus label.
type Decision struct {
	Kind        DecisionKind
	Reason      string
	UseThinking bool // only meaningful when Kind == DecisionRetranslate

	// UseEnsemble (OPT-202) escalates a normal retranslate to a
	// parallel multi-model fan-out (RetranslateEnsemble). Only meaningful
	// when Kind == DecisionRetranslate. The Run executor dispatches to
	// the ensemble tool; if the tool returns ErrEnsembleUnavailable the
	// executor falls back to a single-model retranslate so the loop
	// always makes forward progress.
	//
	// UseThinking and UseEnsemble are NOT mutually exclusive on the
	// decision struct, but the production executor ignores UseThinking
	// when UseEnsemble is true (the ensemble path runs all configured
	// models in parallel; "thinking" is a single-model concept that
	// would only confuse the fan-out cost accounting).
	UseEnsemble bool
}

// Observation is what one Synthesize call produced. Computed from the
// raw TTSResult plus the per-segment Config (target_sec, target_ms) so
// the agent's Decide function doesn't have to recompute drift signs
// repeatedly across branches.
type Observation struct {
	// ActualDurationMs is the TTS adapter's reported audio length.
	ActualDurationMs int64

	// ActualSec is ActualDurationMs / 1000.0 (cached so Decide stays branch-free).
	ActualSec float64

	// OverflowMs = ActualDurationMs - TargetMs. Positive = audio overran
	// the slot, negative = under-run, zero = exact.
	OverflowMs int64

	// AbsDrift = |ActualSec - TargetSec|, always non-negative. Used for
	// best-result tracking + drift threshold comparison.
	AbsDrift float64

	// DriftPct = AbsDrift / TargetSec, the relative drift compared to
	// DriftThreshold. Zero when targetSec == 0 (defensive).
	DriftPct float64

	// JudgeVerdict / JudgeScore are the OPT-002 LLM-as-judge signals
	// for the most-recent attempt's (source, target) pair. Populated
	// by Run() only when the agent was about to retranslate due to
	// drift AND Config.JudgeVetoDriftRetry is true; the synchronous
	// judge call is the cost the agent pays to short-circuit a wasteful
	// retry chain (see OPT-002-followup-4 / OPT-FOLLOWUP-3(b)).
	//
	// Empty Verdict means "judge was not consulted on this attempt"
	// (either disabled, observation unavailable, or judge failed).
	JudgeVerdict string
	JudgeScore   float64
}

// State accumulates everything the agent needs to know across attempts
// of the same segment. It is intentionally a flat struct (no pointers)
// so tests can construct expected states by literal assignment.
//
// BestAbsDrift is initialised to math.MaxFloat64 by NewState so the
// first observation always wins the "best so far" comparison.
type State struct {
	// Attempt is the 0-based index of the *next* attempt the agent will
	// run. attempt == 0 before the first Synthesize call, attempt == 1
	// before the first retranslate-then-resynthesize, etc.
	Attempt int

	// Text is the translation the next Synthesize call will speak.
	Text string

	// BestText / BestActualMs / BestAbsDrift track the lowest-drift
	// attempt seen so far. Used by ShouldRestoreBest after the loop ends.
	BestText     string
	BestActualMs int64
	BestAbsDrift float64

	// History is the sequence of (text, actualSec) pairs the LLM has
	// already produced and we've already synthesized. Threaded into
	// every retranslate call so the LLM can learn the chars→duration
	// mapping and avoid repeating tried-and-failed candidates.
	History []llm.RetranslationAttempt

	// ObservedCharsPerSec is the running average of chars/sec the TTS
	// model has been producing on this segment. Threaded back into both
	// the retranslate prompt (so the LLM uses a calibrated ceiling) and
	// the TTS budget feedback (PrevActualSec / PrevTextChars below).
	ObservedCharsPerSec float64
	TotalObsChars       int
	TotalObsSec         float64

	// PrevActualSec / PrevTextChars are the adaptive-token-budget
	// feedback signals fed into the *next* TTS call. Populated only
	// after over-run attempts (under-run feedback would push the next
	// budget tighter — exactly the wrong direction, see "scheme 2"
	// comment in stage_tts.go).
	PrevActualSec float64
	PrevTextChars int

	// ConsecutiveSameChars counts how many consecutive retranslate
	// attempts have produced the same character count as the previous
	// text. Triggers thinking-mode escalation at StuckThreshold.
	ConsecutiveSameChars int

	// AttemptsWithoutImprovement counts how many attempts have passed
	// since BestAbsDrift was last improved. Triggers thinking-mode
	// escalation at NonConvergenceWindow.
	AttemptsWithoutImprovement int

	// EnsembleCallsThisSegment is the running count of how many times
	// the agent has dispatched a RetranslateEnsemble for THIS segment.
	// Capped at Config.EnsembleMaxPerSegment by Decide so a single
	// segment cannot drain the global LLM budget on a long
	// non-converging chain. Bookkeeping lives on State (not on Agent)
	// because the per-segment lifecycle ends with Run; storing it on
	// the Agent would leak state across segments.
	EnsembleCallsThisSegment int
}

// NewState returns a fresh State seeded with the initial translation
// text and the math.MaxFloat64 sentinel for BestAbsDrift (so the first
// observation always wins the "best so far" comparison without needing
// a separate "first-seen" boolean).
func NewState(initialText string) State {
	return State{
		Text:         initialText,
		BestAbsDrift: math.MaxFloat64,
	}
}

// Config bundles the per-segment immutable parameters. All values are
// computed once at the start of Run() and threaded through every
// Decide() call so Decide can stay pure.
//
// Defaults: StuckThreshold falls back to 2 and NonConvergenceWindow to 3
// (matches the inline `if x <= 0 { x = N }` defaults from
// processOneTTSSegment).
type Config struct {
	TargetSec  float64
	TargetMs   int64
	GapAfterMs int64

	// MaxAttempts is the maximum number of TTS attempts allowed. attempt
	// == MaxAttempts is the LAST attempt (matches the inline `attempt ==
	// maxAttempts` check; the loop uses `for attempt := 0; attempt <=
	// maxAttempts; attempt++` so MaxAttempts+1 total iterations are
	// theoretically possible).
	MaxAttempts int

	// DriftThreshold is the *effective* per-segment drift threshold
	// (already blended via tts.EffectiveDriftThreshold + adaptive floor).
	DriftThreshold float64

	// MaxBorrowDriftPct is the maximum over-run drift (as a fraction of
	// target) that is still acceptable for "borrow from trailing gap"
	// without forcing re-translation. Already adjusted for absolute cap
	// via tts.EffectiveBorrowDriftPct.
	MaxBorrowDriftPct float64

	// AbsMaxDriftSec is the absolute drift cap (seconds). Threaded through
	// Decide for parity with the borrow-pct calculation, although Decide
	// uses MaxBorrowDriftPct (which is already adjusted) directly.
	AbsMaxDriftSec float64

	// StuckThreshold is the number of consecutive same-char retranslate
	// outputs that trigger thinking-mode escalation. Default 2.
	StuckThreshold int

	// NonConvergenceWindow is the number of attempts without improving
	// BestAbsDrift that triggers thinking-mode escalation. Default 3.
	NonConvergenceWindow int

	// RetranslationEnabled gates the entire retry loop. When false the
	// agent always accepts the first attempt's audio (legacy behaviour
	// when env RETRANSLATION_ENABLED=false).
	RetranslationEnabled bool

	// JudgeVetoDriftRetry: OPT-002-followup-4 / OPT-FOLLOWUP-3(b).
	// When true the agent calls the judge synchronously before
	// retranslating due to drift, and accepts the current attempt if
	// the judge gave verdict=accept + score ≥ JudgeVetoMinScore AND
	// the absolute drift is within AdaptiveMaxAcceptableDrift(targetSec).
	//
	// Why this matters: long segments (≥ 20s) routinely produce 7-11%
	// drift because LLM+TTS together cannot hit sub-second timing on
	// 30s utterances. The legacy code would retranslate up to 50 times
	// in pursuit of < 6% drift, burning $0.10+ on each segment, even
	// though the judge consistently scored those segments 1.0 (perfect
	// fidelity). VETO lets the judge override the drift retry: a
	// perfect translation that's 10% long is shipping-ready.
	//
	// Default in production: true (the followup has been observed-only
	// for months — see CHANGELOG OPT-002-followup-3 — the metric data
	// supports flipping the lever on).
	JudgeVetoDriftRetry bool

	// JudgeVetoMinScore is the minimum judge score required to honour a
	// VETO. Default 0.95 — the judge schema permits 0..1 and 0.95+
	// means "near-flawless fidelity"; using a stricter cut-off keeps
	// false-positive VETOs out of the cheap-but-wrong band.
	JudgeVetoMinScore float64

	// EnsembleEnabled (OPT-202) is the master gate for promoting a
	// normal retranslate to a multi-model fan-out. Default false. When
	// false, EnsembleNonConvergenceTrigger / EnsembleJudgeScoreTrigger /
	// EnsembleImportant are ignored and Decide never sets
	// Decision.UseEnsemble. The executor's tool implementation may
	// ALSO refuse (ErrEnsembleUnavailable) for operational reasons —
	// the agent treats that as a clean fall-through to a single-model
	// retranslate so the segment still makes forward progress.
	EnsembleEnabled bool

	// EnsembleNonConvergenceTrigger: when state.AttemptsWithoutImprovement
	// >= this and EnsembleEnabled, the next retranslate uses ensemble.
	// Default 2 (so the agent gives single-model retranslate one full
	// "improvement window" before paying for N-way fan-out). Match the
	// OPT-202 plan: "attempts_without_improvement >= 2".
	EnsembleNonConvergenceTrigger int

	// EnsembleJudgeScoreTrigger: when the most-recent judge call (if any)
	// scored BELOW this AND state.Attempt >= 1, the next retranslate
	// uses ensemble. Default 0.7 (matches plan). Combined with the
	// judge VETO branch (which short-circuits ACCEPT at score ≥ 0.95)
	// this band leaves a "weak translation, keep trying" window of
	// 0.70..0.94 where ensemble is the natural escalation.
	EnsembleJudgeScoreTrigger float64

	// EnsembleImportant signals "this segment is important enough that
	// ensemble should fire on every retranslate decision, regardless
	// of the convergence/judge triggers". Threaded from seg.meta.important
	// (when set by the operator). The MVP rollout leaves it false for
	// every segment; once the L3 baseline confirms the cost envelope,
	// pipeline code can flip it for chapter-opener / chapter-closer
	// segments where translation quality matters most.
	EnsembleImportant bool

	// EnsembleMaxPerSegment is the per-segment ensemble call cap.
	// Default 1 (matches the plan's "ensemble triggers at most once
	// before falling back to thinking-mode"). The agent counts ensemble
	// uses in state.EnsembleCallsThisSegment; once the cap is hit,
	// Decide downgrades subsequent ensemble triggers to plain
	// UseThinking=true. This is the local cost ceiling; the global
	// per-episode cost ledger lives at the rework engine layer.
	EnsembleMaxPerSegment int
}
