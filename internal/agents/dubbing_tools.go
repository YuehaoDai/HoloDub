// Package agents — OPT-201 SegmentAgent ReAct refactor.
//
// This package extracts the 180+ line `for attempt` retry loop in
// internal/pipeline/stage_tts.go::processOneTTSSegment (and its surrounding
// best-result tracking / borrow-from-gap / thinking-escalate decisions) into
// an explicit ReAct-style agent split between:
//
//   - a pure decision function (segment_agent.go::Decide) covering the entire
//     "given the current state + the latest observation, what should we do
//     next?" space, and
//
//   - an executor that translates Decisions into side effects via the
//     DubbingTools interface defined in this file. The interface lets the
//     agent be unit-tested with deterministic FakeTools that pre-program a
//     sequence of TTSResult / RetranslateResult / JudgeResult, eliminating
//     the need for live ml-service / LLM calls in tests.
//
// The contract is intentionally narrow: only the calls the segment loop
// actually makes (Synthesize / Retranslate / JudgeFidelity) appear here.
// Adding a tool means adding a method to the interface plus a fake
// implementation; the agent's Decide loop never sees the implementations
// directly.
//
// Why a separate package (not pipeline/): keeping the agent in its own
// package
//   - prevents the import cycle pipeline → agents → pipeline that would
//     otherwise emerge once the agent calls back into store / queue;
//   - matches the existing layout of internal/rework/ (decision package,
//     thin engine that depends on a narrow RetryJobAPI), keeping the two
//     decision systems structurally symmetrical for future maintainers.
package agents

import (
	"context"

	"holodub/internal/llm"
)

// TTSArgs is the input bundle for one Synthesize call. It mirrors the
// fields the pipeline currently fills in ml.TTSRequest, but is decoupled
// so the agent compiles without depending on internal/ml — the real
// implementation in pipeline.realDubbingTools translates between the two.
//
// All durations are seconds (float) to match the existing TTS adapter
// contract; output paths are repository-relative (no leading slash, see
// lessons-learned.mdc#2).
type TTSArgs struct {
	// Text is the translation text the TTS model should speak.
	Text string

	// TargetDurationSec is the slot length the dub should aim for. The
	// agent reads ActualDurationSec - TargetDurationSec as the per-attempt
	// drift signal.
	TargetDurationSec float64

	// MaxAllowedSec is the hard upper bound on the TTS output (target +
	// trailing gap). Mirrors ml.TTSRequest.MaxAllowedSec.
	MaxAllowedSec float64

	// VoiceConfig is the opaque map forwarded verbatim to the ml-service
	// /tts/run endpoint. The agent does not interpret it.
	VoiceConfig map[string]any

	// OutputRelPath is the repository-relative WAV destination. The
	// pipeline computes it from job.ID / voice profile / segment ordinal
	// so the same retry attempt always overwrites the same file (tool
	// idempotency, see agent-design.mdc#6).
	OutputRelPath string

	// PrevActualSec and PrevTextChars carry adaptive token-budget feedback
	// from the previous attempt to the TTS adapter. Both zero on the
	// first attempt; intentionally kept opaque to the agent — only the
	// executor decides when to populate them (over-run feeds back, under-
	// run zeros out, see processOneTTSSegment's "scheme 2" comment).
	PrevActualSec float64
	PrevTextChars int

	// DubbingMeta is the OPT-204 structured prosody plan. Opaque to the
	// agent (the agent only knows "Synthesize takes whatever the pipeline
	// gave it"); the executor pulls it from seg.Meta["dubbing"] and
	// forwards it to ml.TTSRequest.DubbingMeta. nil = legacy path.
	DubbingMeta map[string]any
}

// TTSResult is what Synthesize returns. Mirrors ml.TTSResponse so the
// real implementation is a 1:1 field copy. Diagnostics are kept as a
// flat string slice so the agent can surface them in slog without
// depending on ml types.
type TTSResult struct {
	AudioRelPath     string
	ActualDurationMs int64
	Diagnostics      []string
}

// RetranslateArgs is the input bundle for one RetranslateWithConstraint
// call. Mirrors the long parameter list of llm.Client.RetranslateWithConstraint
// (see internal/llm/client.go) but as a struct so future field additions
// (e.g. OPT-204 prosody hints) don't break every test.
//
// Field naming deliberately matches the source pipeline call site so a
// reader of stage_tts.go can locate the equivalent agent field in
// O(1) time.
type RetranslateArgs struct {
	SourceLanguage string
	TargetLanguage string
	SourceText     string
	CurrentTrans   string
	TargetSec      float64
	ActualSec      float64
	Attempt        int
	MaxAttempts    int

	// DriftThresholdPct is the drift threshold the LLM should aim for
	// (e.g. 0.06 = 6%). The agent passes its computed effective threshold
	// (which already blends absolute caps + adaptive floor) so the LLM
	// sees the actual target.
	DriftThresholdPct float64

	// History is the list of previous (text, actualSec) pairs from this
	// segment's retry loop. The LLM uses it to learn the chars→duration
	// mapping and avoid repeating tried-and-failed candidates.
	History []llm.RetranslationAttempt

	// UseThinking switches to the (slower, smarter) reasoning model. The
	// agent flips this on when StuckCount or NonConvergenceCount triggers.
	UseThinking bool

	// ObservedCharsPerSec is the calibrated TTS speaking rate (0 when no
	// data). Lets the LLM compute a tighter character ceiling from the
	// actual voice's measured speed.
	ObservedCharsPerSec float64

	// ContextBefore is the 1-2 immediately preceding (src, tgt) pairs for
	// local coherence. Empty for the first segment of a chapter.
	ContextBefore []llm.ContextSegment

	// NextSourceText is the next segment's source text. Lets the LLM
	// adjust register / connectives so the retranslated line still flows
	// into the next one.
	NextSourceText string

	// TranslationSummary is the OPT-402 episode-aware summary (job summary
	// + episode glossary + reference card). Passed through verbatim so
	// the LLM sees canonical terminology even on a single isolated retry.
	TranslationSummary string
}

// RetranslateResult is what RetranslateWithConstraint returns. The text
// is what the LLM produced; UsedThinking echoes back the model selection
// so the agent can record the actual decision in slog (the LLM client
// might silently fall back when the thinking model is unavailable).
type RetranslateResult struct {
	Text         string
	UsedThinking bool
}

// JudgeArgs is the input bundle for one JudgeFidelity call. Wraps
// llm.JudgeArgs so the agent's dependency on the llm package stays in
// types only (no behaviour).
type JudgeArgs = llm.JudgeArgs

// JudgeResult is what JudgeFidelity returns. Re-exported for symmetry
// with JudgeArgs and to keep the FakeTools API self-contained inside
// internal/agents.
type JudgeResult = llm.JudgeResult

// DubbingTools is the narrow interface every per-segment decision in the
// agent loop talks to. Pipeline wiring constructs a real implementation
// that delegates to internal/ml + internal/llm; tests pass a fakeTools
// that returns pre-programmed sequences (see fake_tools_test.go).
//
// Idempotency contract (see agent-design.mdc#6):
//
//   - Synthesize: callers must populate OutputRelPath with all distinguishing
//     dimensions (job_id / vp_id / segment_ordinal); repeated calls with the
//     same OutputRelPath overwrite the same file → equivalent to one call.
//   - RetranslateWithConstraint: pure (no side effects beyond LLM cost).
//   - JudgeFidelity: pure (LLM cost only).
//
// Cancellation: every method takes a context.Context. The agent loop
// checks ctx.Err() between tool calls (see agent-design.mdc#5); tool
// implementations should also bail promptly when ctx fires so a worker
// SIGTERM cancels everything in flight within ~1 round-trip.
//
// Error handling: tool errors propagate up to the agent loop, which
// decides whether to surface them (terminating the segment) or treat
// them as a transient observation (e.g. one judge failure should not
// fail the segment — see maybeJudgeSegmentAsync's contract). The
// decision is left to the agent so the tool layer stays mechanical.
type DubbingTools interface {
	// Synthesize calls the ml-service TTS endpoint and returns the
	// produced WAV's path and actual duration.
	Synthesize(ctx context.Context, args TTSArgs) (TTSResult, error)

	// RetranslateWithConstraint re-translates with drift-rate feedback,
	// retry history, and (optionally) the reasoning model.
	RetranslateWithConstraint(ctx context.Context, args RetranslateArgs) (RetranslateResult, error)

	// RetranslateEnsemble (OPT-202) fans the same retranslate input out
	// to multiple models in parallel and returns the candidate with the
	// highest judge OverallScore. Used by the agent only when single-
	// model retranslate has demonstrably failed to converge (stuck /
	// important segment / low judge score) — see Decision.RetryEnsemble.
	//
	// Returns ErrEnsembleUnavailable when the operator has not configured
	// any ensemble models; callers should treat this as a non-fatal
	// signal to fall back to RetranslateWithConstraint, NOT as a segment
	// failure. Returning a typed sentinel (instead of nil text + nil err)
	// keeps the agent's branch logic crisp.
	RetranslateEnsemble(ctx context.Context, args RetranslateArgs) (EnsembleResult, error)

	// JudgeFidelity scores one (src, tgt) pair on fidelity / fluency /
	// coherence and returns a verdict. Returns (nil, nil) when judging
	// is disabled at the provider level (the agent treats that as
	// "verdict observation unavailable" — same semantics as the existing
	// observe-only path).
	JudgeFidelity(ctx context.Context, args JudgeArgs) (*JudgeResult, error)
}

// EnsembleResult is the agent-side view of llm.EnsembleResult. The
// agent only needs the winner (text + score + model name) to make a
// decision; the per-candidate breakdown stays in the llm package so
// the executor can dump it into slog without exposing every llm
// type at this layer.
type EnsembleResult struct {
	// Text is the winning candidate's translation (already judged best
	// by pairwise OverallScore).
	Text string

	// Model is the model name that produced the winning candidate.
	// Logged so observability can track which models actually win in
	// production — useful input to a future cost/quality tuning pass.
	Model string

	// JudgeScore is the winner's OverallScore (Fidelity-dominant in
	// llm.JudgeResult.OverallScore). The agent records this alongside
	// the segment so OPT-202's L3 regression has a per-segment delta
	// vs the single-model baseline.
	JudgeScore float64

	// CandidateCount is len(EnsembleModels) — recorded for cost
	// attribution; the cost ledger uses this to detect a fanout outlier
	// before it spirals.
	CandidateCount int
}

// ErrEnsembleUnavailable signals the operator has not configured any
// ensemble models. Returned by RetranslateEnsemble (both the real and
// fake implementations) so the agent's RetryEnsemble branch can fall
// back to RetryNormal without treating the gap as a segment-fatal
// error. A typed sentinel (vs nil text + nil err) keeps the branch
// explicit at every call site.
var ErrEnsembleUnavailable = errEnsembleUnavailable{}

type errEnsembleUnavailable struct{}

func (errEnsembleUnavailable) Error() string {
	return "ensemble unavailable: no models configured"
}
