package agents

import (
	"context"
	"errors"
	"strings"
	"testing"
)

// baseEnsembleCfg returns a Config with ensemble enabled and the
// trigger thresholds matching the OPT-202 plan defaults. Tests
// override individual fields as needed; collected here so a test
// reads as "this case with EnsembleEnabled flipped off" rather than
// re-declaring 12 unrelated fields.
func baseEnsembleCfg() Config {
	return Config{
		TargetSec:                      10.0,
		TargetMs:                       10_000,
		GapAfterMs:                     500,
		MaxAttempts:                    5,
		DriftThreshold:                 0.06,
		MaxBorrowDriftPct:              0.10,
		AbsMaxDriftSec:                 2.0,
		StuckThreshold:                 2,
		NonConvergenceWindow:           3,
		RetranslationEnabled:           true,
		EnsembleEnabled:                true,
		EnsembleNonConvergenceTrigger:  2,
		EnsembleJudgeScoreTrigger:      0.7,
		EnsembleMaxPerSegment:          1,
	}
}

// TestShouldUseEnsemble_DisabledByDefault: when EnsembleEnabled=false,
// no trigger should ever fire. Cheap safety net for the rollout — a
// misconfigured prod flag must NOT silently activate ensemble.
func TestShouldUseEnsemble_DisabledByDefault(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.EnsembleEnabled = false
	state := State{AttemptsWithoutImprovement: 5}
	obs := Observation{JudgeVerdict: "retry", JudgeScore: 0.3}
	if shouldUseEnsemble(state, obs, cfg) {
		t.Fatal("ensemble must stay off when EnsembleEnabled=false")
	}
}

// TestShouldUseEnsemble_NonConvergenceTrigger: AttemptsWithoutImprovement
// >= cfg.EnsembleNonConvergenceTrigger should fire ensemble (the
// primary OPT-202 entry condition).
func TestShouldUseEnsemble_NonConvergenceTrigger(t *testing.T) {
	cfg := baseEnsembleCfg()
	cases := []struct {
		attemptsWithoutImpr int
		want                bool
	}{
		{0, false},
		{1, false},
		{2, true},
		{5, true},
	}
	for _, tc := range cases {
		state := State{AttemptsWithoutImprovement: tc.attemptsWithoutImpr}
		obs := Observation{}
		got := shouldUseEnsemble(state, obs, cfg)
		if got != tc.want {
			t.Errorf("AttemptsWithoutImprovement=%d: want=%v got=%v",
				tc.attemptsWithoutImpr, tc.want, got)
		}
	}
}

// TestShouldUseEnsemble_JudgeScoreTrigger: a low judge score AFTER
// state.Attempt >= 1 should fire ensemble (plan: "judge_score < 0.7
// && attempts >= 1"). attempt=0 without a judge verdict is the very
// first decision and ensemble would not yet have evidence of failure.
func TestShouldUseEnsemble_JudgeScoreTrigger(t *testing.T) {
	cfg := baseEnsembleCfg()
	cases := []struct {
		name     string
		attempt  int
		verdict  string
		score    float64
		want     bool
	}{
		{"first-attempt-low-score-no-trigger", 0, "retry", 0.5, false},
		{"second-attempt-low-score-triggers", 1, "retry", 0.5, true},
		{"second-attempt-high-score-no-trigger", 1, "accept", 0.85, false},
		{"second-attempt-empty-verdict-no-trigger", 1, "", 0.5, false},
		{"second-attempt-borderline-just-under-triggers", 1, "retry", 0.69, true},
		{"second-attempt-borderline-just-at-no-trigger", 1, "retry", 0.7, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			state := State{Attempt: tc.attempt}
			obs := Observation{JudgeVerdict: tc.verdict, JudgeScore: tc.score}
			if got := shouldUseEnsemble(state, obs, cfg); got != tc.want {
				t.Fatalf("want=%v got=%v", tc.want, got)
			}
		})
	}
}

// TestShouldUseEnsemble_ImportantBypassesTriggerCount: when the
// segment is operator-marked important, ensemble fires immediately
// (no non-convergence wait, no judge score required).
func TestShouldUseEnsemble_ImportantBypassesTriggerCount(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.EnsembleImportant = true
	state := State{Attempt: 0, AttemptsWithoutImprovement: 0}
	obs := Observation{}
	if !shouldUseEnsemble(state, obs, cfg) {
		t.Fatal("important segment should fire ensemble immediately")
	}
}

// TestShouldUseEnsemble_PerSegmentCap: once EnsembleCallsThisSegment
// reaches EnsembleMaxPerSegment, no further triggers fire even if
// AttemptsWithoutImprovement keeps climbing. Verifies the budget guard
// — without this a non-converging segment could spend 5× cost.
func TestShouldUseEnsemble_PerSegmentCap(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.EnsembleMaxPerSegment = 1
	state := State{AttemptsWithoutImprovement: 5, EnsembleCallsThisSegment: 1}
	obs := Observation{}
	if shouldUseEnsemble(state, obs, cfg) {
		t.Fatal("ensemble must respect EnsembleMaxPerSegment cap")
	}
}

// TestDecide_EnsembleEscalationDropsThinking: when the decision sets
// UseEnsemble=true, it must clear UseThinking. The two are mutually
// exclusive escalations in the executor; leaving both true would
// confuse the realDubbingTools branch.
func TestDecide_EnsembleEscalationDropsThinking(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.StuckThreshold = 1
	cfg.EnsembleNonConvergenceTrigger = 2
	state := State{
		Attempt:                    3,
		ConsecutiveSameChars:       2, // would trigger thinking on its own
		AttemptsWithoutImprovement: 3, // also triggers ensemble
	}
	obs := Observation{
		ActualDurationMs: 8_000,
		ActualSec:        8.0,
		OverflowMs:       -2_000,
		AbsDrift:         2.0,
		DriftPct:         0.20,
	}
	d := Decide(state, obs, cfg)
	if d.Kind != DecisionRetranslate {
		t.Fatalf("want DecisionRetranslate, got %v", d.Kind)
	}
	if !d.UseEnsemble {
		t.Fatalf("want UseEnsemble=true, got false: %+v", d)
	}
	if d.UseThinking {
		t.Fatalf("ensemble escalation must drop UseThinking, got: %+v", d)
	}
}

// TestAgentRun_EnsembleHappyPath: agent fans out to ensemble when
// triggered, accepts the winner. Models a stuck single-model
// trajectory (each retranslate yields WORSE drift, not better) so
// AttemptsWithoutImprovement climbs and the ensemble trigger fires.
//
// Trajectory:
//
//	attempt 0: tts 11s   (drift 1.0 → 10%, new best)       → retranslate
//	            normal retranslate              → text_v2
//	attempt 1: tts 11.5s (drift 1.5 → 15%, no improvement) → retranslate
//	            normal retranslate              → text_v3
//	attempt 2: tts 11.4s (drift 1.4 → 14%, AwI=2)          → ENSEMBLE
//	            ensemble winner                            → text_ensemble
//	attempt 3: tts 10.1s (drift 0.1 → 1%)                  → accept
//
// MaxAttempts must be ≥ 3. AwI = AttemptsWithoutImprovement.
func TestAgentRun_EnsembleHappyPath(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.MaxAttempts = 5
	cfg.EnsembleNonConvergenceTrigger = 2

	ft := newFakeTools().
		WantSynthesize(TTSResult{AudioRelPath: "a0.wav", ActualDurationMs: 11_000}, nil).
		WantRetranslate("text_v2", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "a1.wav", ActualDurationMs: 11_500}, nil).
		WantRetranslate("text_v3", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "a2.wav", ActualDurationMs: 11_400}, nil).
		WantEnsemble(EnsembleResult{
			Text:           "text_ensemble",
			Model:          "qwen-plus",
			JudgeScore:     0.91,
			CandidateCount: 2,
		}, nil).
		// Final attempt under-runs slightly so it cleanly accepts
		// via the (OverflowMs <= 0 && DriftPct < threshold) branch.
		WantSynthesize(TTSResult{AudioRelPath: "a3.wav", ActualDurationMs: 9_950}, nil)

	a := NewAgent(ft)
	out, err := a.Run(context.Background(), RunInput{
		JobID:       1,
		SegmentID:   42,
		InitialText: "initial",
		SourceText:  "Hello",
	}, cfg)
	if err != nil {
		t.Fatalf("agent run: %v", err)
	}
	if ft.Calls("ensemble") != 1 {
		t.Fatalf("expected 1 ensemble call, got %d", ft.Calls("ensemble"))
	}
	if ft.Calls("retranslate") != 2 {
		t.Fatalf("expected 2 plain retranslate calls (before ensemble), got %d", ft.Calls("retranslate"))
	}
	if ft.Calls("tts") != 4 {
		t.Fatalf("expected 4 tts calls, got %d", ft.Calls("tts"))
	}
	if out.FinalText != "text_ensemble" {
		t.Fatalf("expected final text=text_ensemble, got %q", out.FinalText)
	}
	if out.FinalActualMs != 9_950 {
		t.Fatalf("expected FinalActualMs=9950, got %d", out.FinalActualMs)
	}
	if out.State.EnsembleCallsThisSegment != 1 {
		t.Fatalf("expected EnsembleCallsThisSegment=1, got %d", out.State.EnsembleCallsThisSegment)
	}
}

// TestAgentRun_EnsembleUnavailableFallsBack: when the tool returns
// ErrEnsembleUnavailable, the agent must transparently fall through
// to a single-model retranslate. The Decide function still picks
// UseEnsemble=true but the executor handles the fallback. This is
// the "production rollout, ensemble flag flipped off mid-flight"
// scenario.
//
// Trajectory: attempt 0 establishes a best (drift 1.0); attempt 1
// regresses (drift 1.5 → AwI=1), triggering ensemble on the next
// retranslate; ensemble returns ErrEnsembleUnavailable; agent falls
// back to plain retranslate; attempt 2 accepts.
func TestAgentRun_EnsembleUnavailableFallsBack(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.MaxAttempts = 3
	cfg.EnsembleNonConvergenceTrigger = 1 // force trigger on AwI=1

	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 11_000}, nil). // drift 1.0 best
		WantRetranslate("v2", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 11_500}, nil). // drift 1.5 AwI=1
		// Ensemble triggers next retranslate.
		WantEnsemble(EnsembleResult{}, ErrEnsembleUnavailable).
		// Fallback: single-model retranslate.
		WantRetranslate("fallback_text", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 9_900}, nil) // under-run, accept

	a := NewAgent(ft)
	out, err := a.Run(context.Background(), RunInput{
		JobID: 1, SegmentID: 50, InitialText: "initial",
	}, cfg)
	if err != nil {
		t.Fatalf("agent run: %v", err)
	}
	if ft.Calls("ensemble") != 1 {
		t.Fatalf("ensemble must be attempted once, got %d", ft.Calls("ensemble"))
	}
	// 2 retranslate calls expected: 1 before ensemble trigger (attempt 0→1),
	// 1 as fallback after ensemble unavailable (attempt 1→2).
	if ft.Calls("retranslate") != 2 {
		t.Fatalf("expected 2 plain retranslate calls (initial + fallback), got %d", ft.Calls("retranslate"))
	}
	if out.FinalText != "fallback_text" {
		t.Fatalf("expected final text=fallback_text, got %q", out.FinalText)
	}
	if out.State.EnsembleCallsThisSegment != 0 {
		t.Fatalf("ErrEnsembleUnavailable must NOT increment EnsembleCallsThisSegment, got %d",
			out.State.EnsembleCallsThisSegment)
	}
}

// TestAgentRun_EnsembleFailureFallsBackButLogs: when ensemble returns
// a real error (not ErrEnsembleUnavailable), the agent also falls
// back, but the error should bubble through the warning channel.
// We assert behaviour only — log capture lives in higher-level tests.
//
// Same drift trajectory shape as the unavailable test: attempt 0 is
// best, attempt 1 regresses, ensemble fires and fails with a 500,
// agent falls back to plain retranslate, attempt 2 accepts.
func TestAgentRun_EnsembleFailureFallsBackButLogs(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.MaxAttempts = 3
	cfg.EnsembleNonConvergenceTrigger = 1

	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 11_000}, nil).
		WantRetranslate("v2", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 11_500}, nil).
		WantEnsemble(EnsembleResult{}, errors.New("provider 500")).
		WantRetranslate("recovered_text", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 9_900}, nil) // under-run, accept

	a := NewAgent(ft)
	_, err := a.Run(context.Background(), RunInput{
		JobID: 1, SegmentID: 51, InitialText: "initial",
	}, cfg)
	if err != nil {
		t.Fatalf("agent run must tolerate ensemble failure: %v", err)
	}
	if ft.Calls("retranslate") != 2 {
		t.Fatalf("expected 2 retranslate calls (pre-ensemble + post-failure fallback), got %d", ft.Calls("retranslate"))
	}
}

// TestAgentRun_EnsembleCapBlocksSecondFanout: a segment that keeps
// failing must NOT keep firing ensemble. The cap forces subsequent
// retranslates back to single-model (still potentially thinking),
// preventing one segment from draining the cost budget alone.
//
// Trajectory: attempt 0 establishes best (drift 1.0); attempt 1
// regresses (drift 1.5, AwI=1) → ensemble fires; ensemble winner
// also regresses (drift 1.4, AwI=2 but cap blocks); next retranslate
// is plain (NOT ensemble); attempt 3 accepts.
func TestAgentRun_EnsembleCapBlocksSecondFanout(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.MaxAttempts = 5
	cfg.EnsembleMaxPerSegment = 1
	cfg.EnsembleNonConvergenceTrigger = 1 // fire fast

	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 11_000}, nil). // best=1.0
		WantRetranslate("v2", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 11_500}, nil). // AwI=1 → ensemble next
		WantEnsemble(EnsembleResult{Text: "ens1", JudgeScore: 0.5, CandidateCount: 2}, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 11_400}, nil). // AwI=2 cap blocks
		// Cap is hit — must use plain retranslate, NOT ensemble.
		WantRetranslate("post_cap", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 9_900}, nil) // under-run, accept

	a := NewAgent(ft)
	out, err := a.Run(context.Background(), RunInput{
		JobID: 1, SegmentID: 52, InitialText: "initial",
	}, cfg)
	if err != nil {
		t.Fatalf("agent run: %v", err)
	}
	if ft.Calls("ensemble") != 1 {
		t.Fatalf("ensemble must fire at most once (cap=1), got %d", ft.Calls("ensemble"))
	}
	if ft.Calls("retranslate") < 1 {
		t.Fatalf("post-cap retranslate expected at least 1, got %d", ft.Calls("retranslate"))
	}
	if out.State.EnsembleCallsThisSegment != 1 {
		t.Fatalf("EnsembleCallsThisSegment should be capped at 1, got %d",
			out.State.EnsembleCallsThisSegment)
	}
}

// TestAgentRun_EnsembleHistoryIncludesAllPriorAttempts: the args
// passed to RetranslateEnsemble must include the full retry history
// the agent has accumulated so each ensemble candidate model can
// learn from prior failed attempts. Without this, the ensemble would
// not be a strict improvement over a single-model retranslate (each
// candidate would be re-translating from scratch).
//
// Trajectory: drift regresses then plateaus so AwI=2 triggers
// ensemble on the third retranslate decision.
func TestAgentRun_EnsembleHistoryIncludesAllPriorAttempts(t *testing.T) {
	cfg := baseEnsembleCfg()
	cfg.MaxAttempts = 5
	cfg.EnsembleNonConvergenceTrigger = 2

	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 11_000}, nil). // best=1.0
		WantRetranslate("v2", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 11_500}, nil). // AwI=1
		WantRetranslate("v3", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 11_400}, nil). // AwI=2 → ensemble
		WantEnsemble(EnsembleResult{Text: "v_ens", JudgeScore: 0.9, CandidateCount: 2}, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 9_950}, nil) // under-run, accept

	a := NewAgent(ft)
	if _, err := a.Run(context.Background(), RunInput{
		JobID: 1, SegmentID: 60, InitialText: "v1",
	}, cfg); err != nil {
		t.Fatalf("agent run: %v", err)
	}
	recs := ft.RecordedEnsemble()
	if len(recs) != 1 {
		t.Fatalf("expected 1 recorded ensemble call, got %d", len(recs))
	}
	// History should contain the 2 previously-tried texts (v1 first
	// because that was the initial; v2 after first retranslate).
	if got := len(recs[0].History); got < 2 {
		t.Fatalf("expected at least 2 entries in ensemble History (v1, v2), got %d", got)
	}
	wantTexts := []string{"v1", "v2"}
	for i, w := range wantTexts {
		if recs[0].History[i].Text != w {
			t.Errorf("History[%d].Text: want %q, got %q", i, w, recs[0].History[i].Text)
		}
	}
	// CurrentTrans should be the most recent (v3, the one the
	// ensemble is supposed to improve).
	if !strings.Contains(recs[0].CurrentTrans, "v3") {
		t.Fatalf("expected CurrentTrans to contain v3, got %q", recs[0].CurrentTrans)
	}
}
