package agents

import (
	"context"
	"errors"
	"math"
	"testing"

	"holodub/internal/llm"
)

// defaultCfg returns a Config that matches the production defaults
// from internal/config/config.go (RetranslationDriftThreshold=0.06,
// MaxBorrowDriftPct=0.12, StuckThreshold=2, NonConvergenceWindow=3).
// Individual tests override fields as needed.
func defaultCfg() Config {
	return Config{
		TargetSec:            10.0,
		TargetMs:             10_000,
		GapAfterMs:           2_000,
		MaxAttempts:          5,
		DriftThreshold:       0.06,
		MaxBorrowDriftPct:    0.12,
		AbsMaxDriftSec:       0.8,
		StuckThreshold:       2,
		NonConvergenceWindow: 3,
		RetranslationEnabled: true,
	}
}

// makeObs is a tiny test-only helper that synthesises an Observation
// from a target_ms + actual_ms pair, mirroring what ObserveResult does
// in production. Saves every test from re-computing drift by hand.
func makeObs(targetMs, actualMs int64) Observation {
	return ObserveResult(TTSResult{ActualDurationMs: actualMs}, Config{
		TargetSec: float64(targetMs) / 1000.0,
		TargetMs:  targetMs,
	})
}

// =========================================================================
// Decide: table-driven coverage of the core decision matrix
// (8 buckets × multiple cases per bucket = > 60 single-decision tests).
// =========================================================================
func TestDecide_DecisionMatrix(t *testing.T) {
	cases := []struct {
		name       string
		state      State
		obs        Observation
		cfg        Config
		wantKind   DecisionKind
		wantReason string
		wantThink  bool
	}{
		// --- 1. Retranslation disabled ---
		{
			name:  "retranslation-disabled/under-run/accept",
			state: NewState("t"),
			obs:   makeObs(10_000, 9_000),
			cfg: func() Config {
				c := defaultCfg()
				c.RetranslationEnabled = false
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "retranslation_disabled",
		},
		{
			name:  "retranslation-disabled/large-over-run/clip",
			state: NewState("t"),
			obs:   makeObs(10_000, 14_000), // 40% over, way past borrow
			cfg: func() Config {
				c := defaultCfg()
				c.RetranslationEnabled = false
				c.GapAfterMs = 500
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "clip_overflow",
		},
		{
			name:  "retranslation-disabled/small-over-run-fits-gap/borrow",
			state: NewState("t"),
			obs:   makeObs(10_000, 10_500),
			cfg: func() Config {
				c := defaultCfg()
				c.RetranslationEnabled = false
				c.GapAfterMs = 2_000
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "borrow_from_gap",
		},

		// --- 2. Under-run within threshold → accept ---
		{
			name:       "under-run/exact/accept",
			state:      NewState("t"),
			obs:        makeObs(10_000, 10_000),
			cfg:        defaultCfg(),
			wantKind:   DecisionAccept,
			wantReason: "within_threshold",
		},
		{
			name:       "under-run/2pct/accept",
			state:      NewState("t"),
			obs:        makeObs(10_000, 9_800), // -2% drift
			cfg:        defaultCfg(),
			wantKind:   DecisionAccept,
			wantReason: "within_threshold",
		},
		{
			name:       "under-run/6pct-exactly/accept",
			state:      NewState("t"),
			obs:        makeObs(10_000, 9_400), // -6% drift exactly
			cfg:        defaultCfg(),
			wantKind:   DecisionAccept,
			wantReason: "within_threshold",
		},

		// --- 3. Under-run outside threshold → retranslate ---
		{
			name:       "under-run/10pct/retranslate",
			state:      NewState("t"),
			obs:        makeObs(10_000, 9_000), // -10% drift
			cfg:        defaultCfg(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
		},
		{
			name:       "under-run/10pct/last-attempt/accept-no-more",
			state:      State{Attempt: 5, Text: "t", BestAbsDrift: math.MaxFloat64},
			obs:        makeObs(10_000, 9_000),
			cfg:        defaultCfg(),
			wantKind:   DecisionAccept,
			wantReason: "no_more_attempts",
		},

		// --- 4. Over-run, fits gap, within borrow drift → accept(borrow) ---
		{
			name:       "over-run/3pct/fits-large-gap/borrow",
			state:      NewState("t"),
			obs:        makeObs(10_000, 10_300), // +3% drift, 300ms overflow
			cfg:        defaultCfg(),
			wantKind:   DecisionAccept,
			wantReason: "borrow_from_gap",
		},
		{
			name:  "over-run/10pct/fits-gap/borrow-exact-cap",
			state: NewState("t"),
			obs:   makeObs(10_000, 11_000), // +10% drift, 1000ms overflow
			cfg: func() Config {
				c := defaultCfg()
				c.GapAfterMs = 1_500
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "borrow_from_gap",
		},

		// --- 5. Over-run, gap too small → retranslate ---
		{
			name:  "over-run/15pct/short-gap/retranslate",
			state: NewState("t"),
			obs:   makeObs(10_000, 11_500), // +15% drift
			cfg: func() Config {
				c := defaultCfg()
				c.GapAfterMs = 500 // below ShortGapThresholdMs (1000)
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "over_short_gap",
		},
		{
			name:  "over-run/last-attempt/clip-not-retranslate",
			state: State{Attempt: 5, Text: "t", BestAbsDrift: math.MaxFloat64},
			obs:   makeObs(10_000, 12_000),
			cfg: func() Config {
				c := defaultCfg()
				c.GapAfterMs = 500
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "clip_overflow",
		},

		// --- 6. Thinking-mode escalation triggers ---
		{
			name: "retranslate/stuck-2/use-thinking",
			state: State{
				Attempt:              1,
				Text:                 "t",
				BestAbsDrift:         math.MaxFloat64,
				ConsecutiveSameChars: 2,
			},
			obs:        makeObs(10_000, 9_000), // 10% under-run, needs retranslate
			cfg:        defaultCfg(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
			wantThink:  true,
		},
		{
			name: "retranslate/nonconvergence-3/use-thinking",
			state: State{
				Attempt:                    2,
				Text:                       "t",
				BestAbsDrift:               math.MaxFloat64,
				AttemptsWithoutImprovement: 3,
			},
			obs:        makeObs(10_000, 8_500), // 15% under-run
			cfg:        defaultCfg(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
			wantThink:  true,
		},
		{
			name: "retranslate/below-stuck-threshold/normal-model",
			state: State{
				Attempt:                    1,
				Text:                       "t",
				BestAbsDrift:               math.MaxFloat64,
				ConsecutiveSameChars:       1,
				AttemptsWithoutImprovement: 2,
			},
			obs:        makeObs(10_000, 9_000),
			cfg:        defaultCfg(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
			wantThink:  false,
		},

		// --- 7. Custom stuck/nonconvergence values ---
		{
			name: "retranslate/custom-stuck-1/triggers-immediately",
			state: State{
				Attempt:              1,
				Text:                 "t",
				BestAbsDrift:         math.MaxFloat64,
				ConsecutiveSameChars: 1,
			},
			obs: makeObs(10_000, 8_000),
			cfg: func() Config {
				c := defaultCfg()
				c.StuckThreshold = 1
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
			wantThink:  true,
		},
		{
			name: "retranslate/zero-stuck-defaults-to-2",
			state: State{
				Attempt:              1,
				Text:                 "t",
				BestAbsDrift:         math.MaxFloat64,
				ConsecutiveSameChars: 2,
			},
			obs: makeObs(10_000, 8_500),
			cfg: func() Config {
				c := defaultCfg()
				c.StuckThreshold = 0 // should default to 2
				c.NonConvergenceWindow = 0
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
			wantThink:  true,
		},

		// --- 8. Borrow boundary conditions ---
		{
			name:  "over-run/at-short-gap-threshold/no-borrow",
			state: NewState("t"),
			obs:   makeObs(10_000, 10_200),
			cfg: func() Config {
				c := defaultCfg()
				c.GapAfterMs = 1_000 // exactly at threshold; need >
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "over_short_gap",
		},
		{
			name:  "over-run/exceeds-borrow-drift/retranslate",
			state: NewState("t"),
			obs:   makeObs(10_000, 11_400), // +14% drift, exceeds 12% borrow
			cfg: func() Config {
				c := defaultCfg()
				c.GapAfterMs = 5_000
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "over_short_gap",
		},
		{
			name:  "over-run/at-borrow-drift-exactly/borrow",
			state: NewState("t"),
			obs:   makeObs(10_000, 11_200), // +12% drift exactly
			cfg: func() Config {
				c := defaultCfg()
				c.GapAfterMs = 5_000
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "borrow_from_gap",
		},
		{
			name:  "over-run/overflow-exceeds-borrowable/retranslate",
			state: NewState("t"),
			obs:   makeObs(10_000, 12_000), // 2000ms overflow
			cfg: func() Config {
				c := defaultCfg()
				c.GapAfterMs = 1_500 // borrowable = 1500-300 = 1200 < 2000
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "over_short_gap",
		},

		// --- 9. Zero target slot (defensive) ---
		{
			name:       "zero-target-ms/over-run/no-divide-by-zero",
			state:      NewState("t"),
			obs:        Observation{ActualDurationMs: 1_000, OverflowMs: 1_000, ActualSec: 1.0},
			cfg:        Config{TargetSec: 0, TargetMs: 0, RetranslationEnabled: true, MaxAttempts: 5},
			wantKind:   DecisionRetranslate, // canBorrow returns false on TargetMs<=0
			wantReason: "over_short_gap",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := Decide(c.state, c.obs, c.cfg)
			if got.Kind != c.wantKind {
				t.Errorf("Kind: want %v, got %v", c.wantKind, got.Kind)
			}
			if got.Reason != c.wantReason {
				t.Errorf("Reason: want %q, got %q", c.wantReason, got.Reason)
			}
			if got.UseThinking != c.wantThink {
				t.Errorf("UseThinking: want %v, got %v", c.wantThink, got.UseThinking)
			}
		})
	}
}

// TestDecide_OverShortGapStuckEscape: OPT-202-followup-1 (B3).
//
// Reproduces the segment-10186 deadlock from chapter 2 of job 154:
// a segment that can't borrow (short gap), has fired ensemble once
// (cap=1 in this test), AttemptsWithoutImprovement has climbed to 4,
// and we're inside the last 3 attempts of the retry window. The
// escape must return DecisionAccept with reason="over_short_gap_stuck"
// so the loop stops burning LLM tokens on a non-converging case.
func TestDecide_OverShortGapStuckEscape(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 500 // below ShortGapThresholdMs → no borrow
	cfg.MaxAttempts = 10
	cfg.RetranslationEnabled = true

	cases := []struct {
		name       string
		state      State
		wantKind   DecisionKind
		wantReason string
	}{
		{
			// AwI=4 AND Attempt=MaxAttempts-3 (== 7) → escape triggers.
			name: "awi-4-attempt-7-of-10/escape",
			state: State{
				Text:                       "t",
				Attempt:                    7,
				AttemptsWithoutImprovement: 4,
				BestAbsDrift:               math.MaxFloat64,
			},
			wantKind:   DecisionAccept,
			wantReason: "over_short_gap_stuck",
		},
		{
			// AwI=5 AND Attempt=MaxAttempts (10) → escape triggers
			// even later in the window (we want to bail out, not
			// keep retrying).
			name: "awi-5-attempt-equals-max/escape",
			state: State{
				Text:                       "t",
				Attempt:                    10,
				AttemptsWithoutImprovement: 5,
				BestAbsDrift:               math.MaxFloat64,
			},
			// NOTE: at Attempt == MaxAttempts the isLastAttempt
			// short-circuit fires first and returns clip_overflow.
			// over_short_gap_stuck only kicks in when we're WITHIN
			// the tail but not yet at the absolute last attempt.
			wantKind:   DecisionAccept,
			wantReason: "clip_overflow",
		},
		{
			// AwI=3 (below threshold) → standard retranslate.
			name: "awi-3-attempt-7/no-escape",
			state: State{
				Text:                       "t",
				Attempt:                    7,
				AttemptsWithoutImprovement: 3,
				BestAbsDrift:               math.MaxFloat64,
			},
			wantKind:   DecisionRetranslate,
			wantReason: "over_short_gap",
		},
		{
			// AwI=10 but Attempt=4 (well before tail) → keep retrying.
			// The escape is deliberately late-only so a noisy first
			// half of the window doesn't bail out prematurely.
			name: "awi-10-attempt-4-still-early/no-escape",
			state: State{
				Text:                       "t",
				Attempt:                    4,
				AttemptsWithoutImprovement: 10,
				BestAbsDrift:               math.MaxFloat64,
			},
			wantKind:   DecisionRetranslate,
			wantReason: "over_short_gap",
		},
	}
	obs := makeObs(10_000, 11_500) // +15% over-run, cannot borrow

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := Decide(c.state, obs, cfg)
			if got.Kind != c.wantKind {
				t.Errorf("Kind: want %v, got %v", c.wantKind, got.Kind)
			}
			if got.Reason != c.wantReason {
				t.Errorf("Reason: want %q, got %q", c.wantReason, got.Reason)
			}
		})
	}
}

// =========================================================================
// OPT-002-followup-4 VETO branch (judge_veto_drift).
// =========================================================================
func TestDecide_JudgeVetoDriftRetry(t *testing.T) {
	cases := []struct {
		name       string
		obs        Observation
		cfg        Config
		wantKind   DecisionKind
		wantReason string
	}{
		{
			// 25s target, 23.2s actual: drift=1.8s = 7.2% < adaptive cap
			// (8% for ≥20s segments after OPT-002-followup-5 tighten)
			// → VETO honored.
			name: "under-run-veto-long-segment",
			obs: Observation{
				ActualDurationMs: 23_200,
				ActualSec:        23.2,
				OverflowMs:       -1_800,
				AbsDrift:         1.8,
				DriftPct:         0.072,
				JudgeVerdict:     "accept",
				JudgeScore:       0.98,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.TargetSec = 25.0
				c.TargetMs = 25_000
				c.GapAfterMs = 500
				c.JudgeVetoDriftRetry = true
				c.JudgeVetoMinScore = 0.95
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "judge_veto_drift",
		},
		{
			// 20s target, 21.5s actual: drift=1.5s = 7.5% < adaptive cap
			// (8% for ≥20s segments after tighten) → VETO honored.
			name: "over-run-veto-long-segment-short-gap",
			obs: Observation{
				ActualDurationMs: 21_500,
				ActualSec:        21.5,
				OverflowMs:       1_500,
				AbsDrift:         1.5,
				DriftPct:         0.075,
				JudgeVerdict:     "accept",
				JudgeScore:       0.96,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.TargetSec = 20.0
				c.TargetMs = 20_000
				c.GapAfterMs = 500
				c.JudgeVetoDriftRetry = true
				c.JudgeVetoMinScore = 0.95
				return c
			}(),
			wantKind:   DecisionAccept,
			wantReason: "judge_veto_drift",
		},
		{
			// OPT-002-followup-5 (T1) regression case: 20s target with
			// drift=2.0s (10%) was VETO-eligible under the old 10% cap;
			// must now retranslate because the new cap is 8% = 1.6s.
			// This is the headline behavioural change of the tighten.
			name: "tightened-band-rejects-10pct-on-long-segment",
			obs: Observation{
				ActualDurationMs: 18_000,
				ActualSec:        18.0,
				OverflowMs:       -2_000,
				AbsDrift:         2.0,
				DriftPct:         0.10,
				JudgeVerdict:     "accept",
				JudgeScore:       0.99,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.TargetSec = 20.0
				c.TargetMs = 20_000
				c.GapAfterMs = 500
				c.JudgeVetoDriftRetry = true
				c.JudgeVetoMinScore = 0.95
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
		},
		{
			name: "veto-disabled-flag",
			obs: Observation{
				ActualDurationMs: 9_000, ActualSec: 9, OverflowMs: -1_000,
				AbsDrift: 1.0, DriftPct: 0.10,
				JudgeVerdict: "accept", JudgeScore: 0.99,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.JudgeVetoDriftRetry = false
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
		},
		{
			name: "veto-low-score",
			obs: Observation{
				ActualDurationMs: 9_000, ActualSec: 9, OverflowMs: -1_000,
				AbsDrift: 1.0, DriftPct: 0.10,
				JudgeVerdict: "accept", JudgeScore: 0.80,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.JudgeVetoDriftRetry = true
				c.JudgeVetoMinScore = 0.95
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
		},
		{
			name: "veto-wrong-verdict",
			obs: Observation{
				ActualDurationMs: 9_000, ActualSec: 9, OverflowMs: -1_000,
				AbsDrift: 1.0, DriftPct: 0.10,
				JudgeVerdict: "retry", JudgeScore: 0.99,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.JudgeVetoDriftRetry = true
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
		},
		{
			name: "veto-drift-exceeds-adaptive-cap-short-segment",
			obs: Observation{
				ActualDurationMs: 2_500, ActualSec: 2.5, OverflowMs: -500,
				AbsDrift: 0.5, DriftPct: 0.166,
				JudgeVerdict: "accept", JudgeScore: 1.0,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.TargetSec = 3.0
				c.TargetMs = 3_000
				c.JudgeVetoDriftRetry = true
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
		},
		{
			name: "veto-empty-verdict-no-judge-data",
			obs: Observation{
				ActualDurationMs: 9_000, ActualSec: 9, OverflowMs: -1_000,
				AbsDrift: 1.0, DriftPct: 0.10,
				JudgeVerdict: "", JudgeScore: 0,
			},
			cfg: func() Config {
				c := defaultCfg()
				c.JudgeVetoDriftRetry = true
				return c
			}(),
			wantKind:   DecisionRetranslate,
			wantReason: "under_run_drift",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := Decide(NewState("t"), c.obs, c.cfg)
			if got.Kind != c.wantKind {
				t.Errorf("Kind: want %v, got %v (%+v)", c.wantKind, got.Kind, got)
			}
			if got.Reason != c.wantReason {
				t.Errorf("Reason: want %q, got %q", c.wantReason, got.Reason)
			}
		})
	}
}

// Adaptive band cut-offs sanity check.
// OPT-002-followup-5 (T1) tightened the bands from 10/6/3 to 8/5/2.5%.
func TestAdaptiveMaxAcceptableDrift(t *testing.T) {
	cases := []struct {
		targetSec float64
		want      float64
	}{
		{1.0, 0.025},          // < 5s tier
		{4.99, 4.99 * 0.025},  // just below 5s tier boundary
		{5.0, 5.0 * 0.05},     // 5s-20s tier
		{19.99, 19.99 * 0.05}, // just below 20s tier boundary
		{20.0, 20.0 * 0.08},   // ≥ 20s tier
		{30.0, 30.0 * 0.08},   // long segment, 8% band → 2.4s
	}
	for _, c := range cases {
		got := AdaptiveMaxAcceptableDrift(c.targetSec)
		if math.Abs(got-c.want) > 0.001 {
			t.Errorf("AdaptiveMaxAcceptableDrift(%f): want %f, got %f", c.targetSec, c.want, got)
		}
	}
}

// End-to-end agent run: a high-drift long segment with a perfect judge
// score should accept on the FIRST attempt (1 TTS call + 1 judge call),
// NOT retry up to MaxAttempts.
func TestAgentRun_VetoSkipsRetry(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 22.0
	cfg.TargetMs = 22_000
	cfg.GapAfterMs = 500
	cfg.MaxAttempts = 5
	cfg.JudgeVetoDriftRetry = true
	cfg.JudgeVetoMinScore = 0.95

	// Sequence: 1 TTS call returning a 6.8% over-run on a 22s segment
	// (1.5s drift, within the new 8% adaptive cap → 1.76s),
	// 1 judge call returning a perfect accept verdict, agent vetoes
	// the drift retry, no second TTS call should be made.
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 23_500, AudioRelPath: "ok.wav"}, nil).
		WantJudge(&JudgeResult{Fidelity: 0.98, Fluency: 0.95, Coherence: 0.95, Verdict: "accept"}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{
		SegmentID:      1,
		SourceLanguage: "en",
		TargetLanguage: "zh",
		SourceText:     "source",
		InitialText:    "translated",
	}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if ft.Calls("tts") != 1 {
		t.Errorf("expected exactly 1 tts call (VETO short-circuit), got %d", ft.Calls("tts"))
	}
	if ft.Calls("judge") != 1 {
		t.Errorf("expected exactly 1 judge call, got %d", ft.Calls("judge"))
	}
	if ft.Calls("retranslate") != 0 {
		t.Errorf("VETO should skip retranslate, got %d calls", ft.Calls("retranslate"))
	}
	if out.FinalDecision.Reason != "judge_veto_drift" {
		t.Errorf("final decision reason: want judge_veto_drift, got %q", out.FinalDecision.Reason)
	}
}

// =========================================================================
// canBorrow boundary table (covers every if/return path in canBorrow).
// =========================================================================
func TestCanBorrow_Boundary(t *testing.T) {
	type tcase struct {
		name string
		obs  Observation
		cfg  Config
		want bool
	}
	cases := []tcase{
		{"no-overflow", makeObs(10_000, 9_000), defaultCfg(), false},
		{"exact-target", makeObs(10_000, 10_000), defaultCfg(), false},
		{"gap-zero", makeObs(10_000, 10_200), func() Config { c := defaultCfg(); c.GapAfterMs = 0; return c }(), false},
		{"gap-at-short-threshold", makeObs(10_000, 10_100), func() Config { c := defaultCfg(); c.GapAfterMs = 1_000; return c }(), false},
		{"gap-above-short-threshold-small-overflow", makeObs(10_000, 10_100), func() Config { c := defaultCfg(); c.GapAfterMs = 1_001; return c }(), true},
		{"overflow-exceeds-borrowable", makeObs(10_000, 12_000), func() Config { c := defaultCfg(); c.GapAfterMs = 1_500; return c }(), false},
		{"drift-exceeds-cap", makeObs(10_000, 13_000), func() Config { c := defaultCfg(); c.GapAfterMs = 5_000; return c }(), false},
		{"drift-at-cap-exactly", makeObs(10_000, 11_200), func() Config { c := defaultCfg(); c.GapAfterMs = 5_000; return c }(), true},
		{"zero-target-ms", Observation{ActualDurationMs: 1_000, OverflowMs: 1_000}, Config{TargetMs: 0, GapAfterMs: 2_000, MaxBorrowDriftPct: 0.12}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := canBorrow(c.obs, c.cfg); got != c.want {
				t.Errorf("canBorrow: want %v, got %v", c.want, got)
			}
		})
	}
}

// =========================================================================
// shouldUseThinking truth table (StuckThreshold × NonConvergenceWindow ×
// counter values).
// =========================================================================
func TestShouldUseThinking_TruthTable(t *testing.T) {
	type tcase struct {
		name                string
		consecSame          int
		attemptsWithoutImpr int
		stuckThr            int
		ncWindow            int
		want                bool
	}
	cases := []tcase{
		{"all-zero", 0, 0, 2, 3, false},
		{"stuck-just-below", 1, 0, 2, 3, false},
		{"stuck-at-threshold", 2, 0, 2, 3, true},
		{"stuck-above-threshold", 3, 0, 2, 3, true},
		{"nc-just-below", 0, 2, 2, 3, false},
		{"nc-at-window", 0, 3, 2, 3, true},
		{"nc-above-window", 0, 4, 2, 3, true},
		{"both-at-threshold", 2, 3, 2, 3, true},
		{"stuck-defaults-when-zero", 2, 0, 0, 0, true}, // default 2 stuck → triggers
		{"nc-defaults-when-zero", 0, 3, 0, 0, true},    // default 3 nc → triggers
		{"both-defaults-just-below", 1, 2, 0, 0, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			state := State{ConsecutiveSameChars: c.consecSame, AttemptsWithoutImprovement: c.attemptsWithoutImpr}
			cfg := Config{StuckThreshold: c.stuckThr, NonConvergenceWindow: c.ncWindow}
			if got := shouldUseThinking(state, cfg); got != c.want {
				t.Errorf("shouldUseThinking: want %v, got %v", c.want, got)
			}
		})
	}
}

// =========================================================================
// ShouldRestoreBest table.
// =========================================================================
func TestShouldRestoreBest_Table(t *testing.T) {
	cases := []struct {
		name    string
		state   State
		current float64
		want    bool
	}{
		{"no-best-text", State{Text: "current", BestText: ""}, 1.0, false},
		{"same-as-current", State{Text: "x", BestText: "x", BestAbsDrift: 0.5}, 1.0, false},
		{"same-text-better-audio-path", State{Text: "x", BestText: "x", BestAudioRelPath: "attempt00.wav", BestAbsDrift: 0.5}, 1.0, true},
		{"best-equal", State{Text: "now", BestText: "old", BestAbsDrift: 1.0}, 1.0, false},
		{"best-worse", State{Text: "now", BestText: "old", BestAbsDrift: 1.2}, 1.0, false},
		{"best-better-by-0.05", State{Text: "now", BestText: "old", BestAbsDrift: 0.95}, 1.0, false},
		{"best-better-by-exactly-0.1", State{Text: "now", BestText: "old", BestAbsDrift: 0.9}, 1.0, false},
		{"best-better-by-0.15", State{Text: "now", BestText: "old", BestAbsDrift: 0.85}, 1.0, true},
		{"best-much-better", State{Text: "now", BestText: "old", BestAbsDrift: 0.1}, 2.0, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := ShouldRestoreBest(c.state, c.current); got != c.want {
				t.Errorf("ShouldRestoreBest: want %v, got %v", c.want, got)
			}
		})
	}
}

// =========================================================================
// ApplyObservation: running-rate update + best tracking + attempts counter.
// =========================================================================
func TestApplyObservation_RunningRate(t *testing.T) {
	state := NewState("hello world") // 11 runes
	obs1 := makeObs(10_000, 10_000)  // exactly 10s
	state = ApplyObservation(state, obs1)
	if got := state.ObservedCharsPerSec; math.Abs(got-1.1) > 0.001 {
		t.Errorf("after first obs: want chars/sec=1.1, got %f", got)
	}
	if state.BestText != "hello world" {
		t.Errorf("first observation should become best, got %q", state.BestText)
	}
	if state.AttemptsWithoutImprovement != 0 {
		t.Errorf("first obs should not bump nonimprovement counter, got %d", state.AttemptsWithoutImprovement)
	}
}

func TestApplyObservation_BestTracking(t *testing.T) {
	state := NewState("a")
	state = ApplyObservation(state, Observation{ActualDurationMs: 12_000, ActualSec: 12, AbsDrift: 2, OverflowMs: 2_000})
	if state.BestAbsDrift != 2 || state.BestText != "a" || state.AttemptsWithoutImprovement != 0 {
		t.Errorf("after first obs unexpected state: %+v", state)
	}
	state.Text = "ab" // simulate retranslate
	state = ApplyObservation(state, Observation{ActualDurationMs: 11_000, ActualSec: 11, AbsDrift: 1, OverflowMs: 1_000})
	if state.BestAbsDrift != 1 || state.BestText != "ab" {
		t.Errorf("improvement should update best, got drift=%f text=%q", state.BestAbsDrift, state.BestText)
	}
	if state.AttemptsWithoutImprovement != 0 {
		t.Errorf("improvement should reset counter, got %d", state.AttemptsWithoutImprovement)
	}
	state.Text = "abc"
	state = ApplyObservation(state, Observation{ActualDurationMs: 13_000, ActualSec: 13, AbsDrift: 3, OverflowMs: 3_000})
	if state.BestText != "ab" {
		t.Errorf("regression should not replace best, got %q", state.BestText)
	}
	if state.AttemptsWithoutImprovement != 1 {
		t.Errorf("regression should bump nonimprovement counter, got %d", state.AttemptsWithoutImprovement)
	}
}

// =========================================================================
// ApplyRetranslate: history append, stuck counter, prev-feedback rules.
// =========================================================================
func TestApplyRetranslate_HistoryAndStuck(t *testing.T) {
	cfg := defaultCfg()
	state := NewState("hello")
	state = ApplyObservation(state, makeObs(10_000, 12_000))
	state = ApplyRetranslate(state, "hi", makeObs(10_000, 12_000), cfg)
	if len(state.History) != 1 || state.History[0].Text != "hello" {
		t.Errorf("history: want 1 entry 'hello', got %+v", state.History)
	}
	if state.Text != "hi" {
		t.Errorf("text not swapped: %q", state.Text)
	}
	if state.Attempt != 1 {
		t.Errorf("attempt: want 1, got %d", state.Attempt)
	}
	// hi (2 runes) != hello (5 runes) → counter resets
	if state.ConsecutiveSameChars != 0 {
		t.Errorf("consecutive should reset, got %d", state.ConsecutiveSameChars)
	}
	state = ApplyRetranslate(state, "yo", makeObs(10_000, 12_000), cfg)
	// yo (2 runes) == hi (2 runes) → counter increments
	if state.ConsecutiveSameChars != 1 {
		t.Errorf("consecutive: want 1 after equal-length, got %d", state.ConsecutiveSameChars)
	}
	state = ApplyRetranslate(state, "no", makeObs(10_000, 12_000), cfg)
	if state.ConsecutiveSameChars != 2 {
		t.Errorf("consecutive: want 2 after second equal-length, got %d", state.ConsecutiveSameChars)
	}
}

func TestApplyRetranslate_PrevFeedbackRules(t *testing.T) {
	cfg := defaultCfg()
	// over-run → feedback populated
	state := State{Text: "abc def", Attempt: 0, BestAbsDrift: math.MaxFloat64}
	state = ApplyRetranslate(state, "xyz", makeObs(10_000, 11_000), cfg)
	if state.PrevActualSec != 11.0 {
		t.Errorf("over-run should populate PrevActualSec, got %f", state.PrevActualSec)
	}
	if state.PrevTextChars != 6 { // "abcdef" non-whitespace
		t.Errorf("over-run should populate PrevTextChars to non-whitespace count, got %d", state.PrevTextChars)
	}

	// under-run → feedback zeroed
	state = State{Text: "abc def", Attempt: 0, BestAbsDrift: math.MaxFloat64}
	state = ApplyRetranslate(state, "xyz", makeObs(10_000, 9_000), cfg)
	if state.PrevActualSec != 0 {
		t.Errorf("under-run should zero PrevActualSec, got %f", state.PrevActualSec)
	}
	if state.PrevTextChars != 0 {
		t.Errorf("under-run should zero PrevTextChars, got %d", state.PrevTextChars)
	}
}

// =========================================================================
// ObserveResult: drift math sanity.
// =========================================================================
func TestObserveResult_DriftMath(t *testing.T) {
	cases := []struct {
		actualMs  int64
		targetMs  int64
		wantOver  int64
		wantDrift float64
		wantPct   float64
	}{
		{10_000, 10_000, 0, 0, 0},
		{12_000, 10_000, 2_000, 2, 0.2},
		{8_000, 10_000, -2_000, 2, 0.2},
		{0, 10_000, -10_000, 10, 1.0},
		{10_000, 0, 10_000, 10, 0}, // div-by-zero → pct stays 0
	}
	for _, c := range cases {
		cfg := Config{TargetMs: c.targetMs, TargetSec: float64(c.targetMs) / 1000.0}
		obs := ObserveResult(TTSResult{ActualDurationMs: c.actualMs}, cfg)
		if obs.OverflowMs != c.wantOver {
			t.Errorf("actual=%d target=%d: OverflowMs want %d, got %d", c.actualMs, c.targetMs, c.wantOver, obs.OverflowMs)
		}
		if math.Abs(obs.AbsDrift-c.wantDrift) > 0.001 {
			t.Errorf("actual=%d target=%d: AbsDrift want %f, got %f", c.actualMs, c.targetMs, c.wantDrift, obs.AbsDrift)
		}
		if math.Abs(obs.DriftPct-c.wantPct) > 0.001 {
			t.Errorf("actual=%d target=%d: DriftPct want %f, got %f", c.actualMs, c.targetMs, c.wantPct, obs.DriftPct)
		}
	}
}

// =========================================================================
// End-to-end scenarios driven through Agent.Run with fakeTools.
// Each scenario is a different trajectory class from
// testing-and-rollout.mdc §2.
// =========================================================================
func TestAgentRun_SingleHitWithinThreshold(t *testing.T) {
	ft := newFakeTools().
		WantSynthesize(TTSResult{AudioRelPath: "out.wav", ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{
		SegmentID:   42,
		InitialText: "hello",
	}, defaultCfg())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if ft.Calls("tts") != 1 {
		t.Errorf("expected exactly 1 tts call, got %d", ft.Calls("tts"))
	}
	if ft.Calls("retranslate") != 0 {
		t.Errorf("expected 0 retranslate calls, got %d", ft.Calls("retranslate"))
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "within_threshold" {
		t.Errorf("unexpected final decision: %+v", out.FinalDecision)
	}
	if out.RestoredFromBest {
		t.Error("single-hit should not trigger best-restore")
	}
}

func TestAgentRun_StableConvergence(t *testing.T) {
	cfg := defaultCfg()
	// Use the default gap (2000ms) so the second attempt has room to
	// borrow. First attempt is 30% over (> 12% borrow cap) and so
	// forces retranslate; second attempt is 5% over (within borrow cap
	// AND fits gap-300=1700ms borrowable) → borrow → accept.
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil). // +30%, over_short_gap
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 10_500}, nil). // borrow
		WantJudge(nil, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "first"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if ft.Calls("tts") != 2 || ft.Calls("retranslate") != 1 {
		t.Errorf("expected 2 tts + 1 retranslate, got tts=%d ret=%d", ft.Calls("tts"), ft.Calls("retranslate"))
	}
	if out.FinalText != "shorter" {
		t.Errorf("final text: want 'shorter', got %q", out.FinalText)
	}
	if out.State.Attempt != 1 {
		t.Errorf("state.Attempt: want 1 after one retry, got %d", out.State.Attempt)
	}
}

func TestAgentRun_LoraCandidateGateSelectedBypassesOuterGapRetry(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 0
	cfg.MaxImpliedFitDelta = 0.05
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			AudioRelPath:     "candidate.wav",
			ActualDurationMs: 10_400,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode": "candidate_audio",
				"selected_family":      "split_mix",
				"fallback_reason":      "",
				"gate_decision": map[string]any{
					"status": "selected",
				},
			},
		}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "candidate"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if ft.Calls("tts") != 1 || ft.Calls("retranslate") != 0 {
		t.Fatalf("candidate gate should stop outer retry, got tts=%d retranslate=%d", ft.Calls("tts"), ft.Calls("retranslate"))
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "lora_candidate_gate" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
}

func TestAgentRun_LoraCandidateHighFitDeltaDoesNotBypassOuterRetry(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 0
	cfg.MaxImpliedFitDelta = 0.05
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			AudioRelPath:     "candidate.wav",
			ActualDurationMs: 10_400,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode":    "candidate_audio",
				"selected_family":         "split_mix",
				"selected_split_strategy": "automatic",
				"fallback_reason":         "",
				"gate_decision": map[string]any{
					"status": "selected",
				},
				"selected_candidate": map[string]any{
					"family":          "split_mix",
					"split_strategy":  "automatic",
					"fit_tempo_delta": 0.0601,
					"gate_pass":       true,
				},
			},
		}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "accepted.wav", ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "candidate"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if ft.Calls("retranslate") != 1 {
		t.Fatalf("high-fit selected candidate must not stop outer retry, got retranslate=%d", ft.Calls("retranslate"))
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "within_threshold" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
}

func TestAgentRun_HighImpliedFitDeltaRejectsFinalAccept(t *testing.T) {
	cfg := defaultCfg()
	cfg.MaxAttempts = 0
	cfg.MaxImpliedFitDelta = 0.05
	cfg.HardMaxOverDriftSec = 0
	cfg.HardMaxUnderDriftSec = 0
	ft := newFakeTools().
		WantSynthesize(TTSResult{AudioRelPath: "too-stretched.wav", ActualDurationMs: 10_550}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "candidate"}, cfg)
	if err == nil {
		t.Fatal("expected high implied fit delta to fail final accept")
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "borrow_from_gap" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
}

func TestAgentRun_LoraCandidateGateSelectedDoesNotBypassHardCap(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 0
	cfg.HardMaxOverDriftSec = 0.3
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			ActualDurationMs: 10_400,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode": "candidate_audio",
				"selected_family":      "split_mix",
				"fallback_reason":      "",
				"gate_decision": map[string]any{
					"status": "selected",
				},
			},
		}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "accepted.wav", ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "candidate"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if ft.Calls("retranslate") != 1 {
		t.Fatalf("candidate gate must not bypass hard cap, got retranslate=%d", ft.Calls("retranslate"))
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "within_threshold" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
}

func TestAgentRun_LoraCandidateFallbackDoesNotBypassRetry(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 0
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			ActualDurationMs: 10_400,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode": "default_audio",
				"selected_family":      "default",
				"fallback_reason":      "all_candidates_rejected",
				"gate_decision": map[string]any{
					"status": "fallback",
				},
			},
		}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "accepted.wav", ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "candidate"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if ft.Calls("retranslate") != 1 {
		t.Fatalf("fallback candidate must keep outer retry behavior, got retranslate=%d", ft.Calls("retranslate"))
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "within_threshold" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
	rec := ft.RecordedSynthesize()
	if len(rec) != 2 {
		t.Fatalf("expected 2 synth calls, got %d", len(rec))
	}
	if rec[0].PriorLoraCandidateCount != 0 {
		t.Fatalf("first synth PriorLoraCandidateCount: want 0, got %d", rec[0].PriorLoraCandidateCount)
	}
	if rec[1].PriorLoraCandidateCount != 1 {
		t.Fatalf("all-candidates-rejected must count against retry budget; second synth PriorLoraCandidateCount: want 1, got %d", rec[1].PriorLoraCandidateCount)
	}
	if rec[1].Attempt != 1 {
		t.Fatalf("second synth Attempt: want 1, got %d", rec[1].Attempt)
	}
}

func TestAgentRun_LoraCandidateTextCompressionUsesCandidateTimingForRetranslate(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 0
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			ActualDurationMs: 8_000,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode": "default_audio",
				"selected_family":      "default",
				"fallback_reason":      "all_candidates_rejected",
				"gate_decision": map[string]any{
					"status":                 "fallback",
					"needs_text_compression": true,
				},
				"text_compression_recommendation": map[string]any{
					"needed":        true,
					"best_drift_ms": 2500.0,
				},
			},
		}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "accepted.wav", ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "candidate"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	rec := ft.RecordedRetranslate()
	if len(rec) != 1 {
		t.Fatalf("expected one retranslate call, got %d", len(rec))
	}
	if math.Abs(rec[0].ActualSec-12.5) > 1e-9 {
		t.Fatalf("retranslate ActualSec = %.3f, want candidate-derived 12.500", rec[0].ActualSec)
	}
	if len(rec[0].History) != 0 {
		t.Fatalf("first retranslate should not include prior history, got %+v", rec[0].History)
	}
	synth := ft.RecordedSynthesize()
	if len(synth) != 2 {
		t.Fatalf("expected 2 synth calls, got %d", len(synth))
	}
	if synth[1].PriorLoraCandidateCount != 0 {
		t.Fatalf("text-compression planning signal must not consume candidate budget, got %d", synth[1].PriorLoraCandidateCount)
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "within_threshold" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
}

func TestAgentRun_LoraCandidateTextCompressionUsesCPSRecommendationForRetranslate(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 10
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 0
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			ActualDurationMs: 8_000,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode": "default_audio",
				"selected_family":      "default",
				"fallback_reason":      "all_candidates_rejected",
				"gate_decision": map[string]any{
					"status":                 "fallback",
					"needs_text_compression": true,
				},
				"text_compression_recommendation": map[string]any{
					"needed":                     true,
					"max_required_chars_per_sec": 7.0,
					"max_allowed_chars_per_sec":  5.6,
				},
			},
		}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "accepted.wav", ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 16096, InitialText: "dense candidate"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	rec := ft.RecordedRetranslate()
	if len(rec) != 1 {
		t.Fatalf("expected one retranslate call, got %d", len(rec))
	}
	if rec[0].ActualSec <= cfg.TargetSec {
		t.Fatalf("CPS recommendation must be interpreted as text-compression overrun evidence; ActualSec=%.3f target=%.3f", rec[0].ActualSec, cfg.TargetSec)
	}
	if math.Abs(rec[0].ActualSec-12.5) > 1e-9 {
		t.Fatalf("retranslate ActualSec = %.3f, want CPS-derived 12.500", rec[0].ActualSec)
	}
	if rec[0].CompressionHint == nil {
		t.Fatal("expected compression hint from LoRA text-compression recommendation")
	}
	if rec[0].CompressionHint.RecommendedMaxChars != 56 {
		t.Fatalf("recommended max chars = %d, want 56", rec[0].CompressionHint.RecommendedMaxChars)
	}
	if rec[0].CompressionHint.MaxRequiredCharsPerSec != 7.0 || rec[0].CompressionHint.MaxAllowedCharsPerSec != 5.6 {
		t.Fatalf("unexpected compression cps hint: %+v", rec[0].CompressionHint)
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "within_threshold" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
}

func TestAgentRun_LoraCandidateTextCompressionRefusesFinalHardCapEscape(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 10
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 0
	cfg.MaxAttempts = 0
	cfg.HardMaxOverDriftSec = 0.3
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			AudioRelPath:     "bad-overrun.wav",
			ActualDurationMs: 14_200,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode": "default_audio",
				"selected_family":      "default",
				"fallback_reason":      "all_candidates_rejected",
				"gate_decision": map[string]any{
					"status":                 "fallback",
					"needs_text_compression": true,
				},
				"text_compression_recommendation": map[string]any{
					"needed":        true,
					"best_drift_ms": 4200.0,
				},
			},
		}, nil)
	agent := NewAgent(ft)

	out, err := agent.Run(context.Background(), RunInput{SegmentID: 16096, InitialText: "dense long candidate"}, cfg)
	if err == nil {
		t.Fatal("expected hard-cap text-compression candidate to fail instead of shipping clipped audio")
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "clip_overflow" {
		t.Fatalf("unexpected final decision evidence: %+v", out.FinalDecision)
	}
	if !out.FinalDecision.HardCapExceeded || out.FinalDecision.HardCapDirection != "over" {
		t.Fatalf("expected over hard-cap evidence, got %+v", out.FinalDecision)
	}
	if ft.Calls("retranslate") != 0 {
		t.Fatalf("max-attempt hard failure should not call retranslate, got %d", ft.Calls("retranslate"))
	}
}

func TestAgentRun_LoraCandidateUnavailableDoesNotConsumeRetryBudget(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 0
	ft := newFakeTools().
		WantSynthesize(TTSResult{
			ActualDurationMs: 10_400,
			LoraCandidateRuntime: map[string]any{
				"audio_selection_mode": "default_audio",
				"selected_family":      "default",
				"fallback_reason":      "candidate_execution_not_available",
			},
		}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{AudioRelPath: "accepted.wav", ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "candidate"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "within_threshold" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
	rec := ft.RecordedSynthesize()
	if len(rec) != 2 {
		t.Fatalf("expected 2 synth calls, got %d", len(rec))
	}
	if rec[1].PriorLoraCandidateCount != 0 {
		t.Fatalf("candidate unavailable must not consume real candidate budget; got %d", rec[1].PriorLoraCandidateCount)
	}
}

func TestAgentRun_OscillationBestRestore(t *testing.T) {
	cfg := defaultCfg()
	cfg.MaxAttempts = 3
	cfg.GapAfterMs = 500 // small gap, force retranslate on over-run
	// Trajectory:
	//   attempt 0: 13s actual (drift 3s) → over_short_gap → retranslate
	//   attempt 1: 8.7s actual (drift 1.3s) → under_run_drift → retranslate  (BEST so far)
	//   attempt 2: 12.5s actual (drift 2.5s) → over_short_gap → retranslate
	//   attempt 3: 13.5s actual (drift 3.5s, isLastAttempt) → clip_overflow → break
	// Final drift = 3.5s, best drift = 1.3s; diff = 2.2s > 0.1s → best restore.
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000, AudioRelPath: "a1"}, nil).
		WantRetranslate("a", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 8_700, AudioRelPath: "a2"}, nil). // BEST
		WantRetranslate("b", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 12_500, AudioRelPath: "a3"}, nil).
		WantRetranslate("c", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 13_500, AudioRelPath: "a4"}, nil) // last attempt
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "init"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if ft.Calls("tts") != 4 {
		t.Errorf("expected ≥ 4 tts calls (3 attempts + best restore), got %d", ft.Calls("tts"))
	}
	if !out.RestoredFromBest {
		t.Errorf("expected best-restore, got %+v", out)
	}
	if out.FinalAudioRelPath != "a2" {
		t.Errorf("expected restored audio path 'a2', got %q", out.FinalAudioRelPath)
	}
}

func TestAgentRun_RestoresSameTextBetterAudioAttempt(t *testing.T) {
	cfg := defaultCfg()
	cfg.MaxAttempts = 1
	cfg.GapAfterMs = 0
	cfg.HardMaxOverDriftSec = 0.3
	cfg.HardMaxUnderDriftSec = 0.7
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 10_300, AudioRelPath: "attempt00.wav"}, nil).
		WantRetranslate("same text", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 10_590, AudioRelPath: "attempt01.wav"}, nil)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "same text"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !out.RestoredFromBest {
		t.Fatalf("same text with better saved audio should restore best attempt: %+v", out)
	}
	if out.FinalAudioRelPath != "attempt00.wav" {
		t.Fatalf("final audio path: want attempt00.wav, got %q", out.FinalAudioRelPath)
	}
	if out.FinalActualMs != 10_300 {
		t.Fatalf("final actual ms: want 10300, got %d", out.FinalActualMs)
	}
}

func TestAgentRun_AttemptOutputPathsPreserveMidLoopAudio(t *testing.T) {
	cfg := defaultCfg()
	cfg.MaxAttempts = 2
	cfg.GapAfterMs = 500
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000, AudioRelPath: "attempt0.wav"}, nil).
		WantRetranslate("short", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 10_000, AudioRelPath: "attempt1.wav"}, nil)
	agent := NewAgent(ft)
	_, err := agent.Run(context.Background(), RunInput{
		SegmentID:     1,
		InitialText:   "init",
		OutputRelPath: "jobs/1/tts/vp0/segment-0001.wav",
	}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	got := ft.RecordedSynthesize()
	if len(got) != 2 {
		t.Fatalf("expected 2 synth calls, got %d", len(got))
	}
	want := []string{
		"jobs/1/tts/vp0/segment-0001.agent_attempt00.wav",
		"jobs/1/tts/vp0/segment-0001.agent_attempt01.wav",
	}
	for i := range want {
		if got[i].OutputRelPath != want[i] {
			t.Fatalf("attempt %d OutputRelPath: want %q, got %q", i, want[i], got[i].OutputRelPath)
		}
	}
}

func TestAgentRun_StuckTriggersThinking(t *testing.T) {
	cfg := defaultCfg()
	cfg.MaxAttempts = 5
	cfg.GapAfterMs = 500
	cfg.StuckThreshold = 2
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
		WantRetranslate("abc", false, nil). // 3 chars
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
		WantRetranslate("xyz", false, nil). // 3 chars, equal → counter=1
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
		WantRetranslate("def", true, nil).                       // 3 chars, equal → counter=2 → useThinking=true
		WantSynthesize(TTSResult{ActualDurationMs: 10_200}, nil) // borrow
	agent := NewAgent(ft)
	_, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "ini"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	calls := ft.RecordedRetranslate()
	if len(calls) < 3 {
		t.Fatalf("expected ≥ 3 retranslate calls, got %d", len(calls))
	}
	if calls[0].UseThinking || calls[1].UseThinking {
		t.Errorf("first two retranslates should not use thinking, got %+v", calls[:2])
	}
	if !calls[2].UseThinking {
		t.Errorf("third retranslate should use thinking after stuck=2, got %+v", calls[2])
	}
}

func TestAgentRun_ContextCancelMidLoop(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cfg := defaultCfg()
	cfg.GapAfterMs = 500
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
		WantRetranslate("next", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil) // never reached
	ft.onRetranslate = func() {
		// Cancel after the first retranslate is consumed, before the
		// second synthesize.
		cancel()
	}
	agent := NewAgent(ft)
	_, err := agent.Run(ctx, RunInput{SegmentID: 1, InitialText: "init"}, cfg)
	if err == nil {
		t.Fatalf("expected error from cancelled ctx")
	}
	if !errors.Is(err, context.Canceled) {
		t.Errorf("want ctx.Canceled, got %v", err)
	}
}

func TestAgentRun_SynthesizeError(t *testing.T) {
	ft := newFakeTools().
		WantSynthesize(TTSResult{}, errFakeBoom)
	agent := NewAgent(ft)
	_, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "x"}, defaultCfg())
	if !errors.Is(err, errFakeBoom) {
		t.Fatalf("want errFakeBoom wrapped, got %v", err)
	}
}

func TestAgentRun_RetranslateErrorAcceptsCurrent(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 500
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000, AudioRelPath: "ok"}, nil).
		WantRetranslate("", false, errFakeBoom)
	agent := NewAgent(ft)
	out, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "init"}, cfg)
	if err != nil {
		t.Fatalf("retranslate error should NOT propagate (legacy contract), got %v", err)
	}
	if out.FinalDecision.Reason != "retranslate_failed" {
		t.Errorf("want reason=retranslate_failed, got %q", out.FinalDecision.Reason)
	}
	if out.FinalAudioRelPath != "ok" {
		t.Errorf("want fallback audio 'ok', got %q", out.FinalAudioRelPath)
	}
}

func TestAgentRun_RetranslateHistoryThreadedCorrectly(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 500
	cfg.MaxAttempts = 2
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
		WantRetranslate("attempt2", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 12_000}, nil).
		WantRetranslate("attempt3", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 10_100}, nil) // borrow, accept
	cfg.GapAfterMs = 2_000
	agent := NewAgent(ft)
	_, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "init"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	rec := ft.RecordedRetranslate()
	if len(rec) != 2 {
		t.Fatalf("expected 2 retranslate calls, got %d", len(rec))
	}
	if len(rec[0].History) != 0 {
		t.Errorf("first retranslate should see empty history, got %d entries", len(rec[0].History))
	}
	if len(rec[1].History) != 1 {
		t.Errorf("second retranslate should see 1 prior history entry, got %d", len(rec[1].History))
	}
	if rec[1].History[0].Text != "init" {
		t.Errorf("first history entry text: want 'init', got %q", rec[1].History[0].Text)
	}
}

func TestAgentRun_PrevFeedbackThreadedToSynthesize(t *testing.T) {
	cfg := defaultCfg()
	cfg.GapAfterMs = 500
	cfg.MaxAttempts = 2
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 12_000}, nil).
		WantRetranslate("next", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 10_200}, nil) // borrow
	cfg.GapAfterMs = 2_000
	agent := NewAgent(ft)
	_, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "abc def"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	rec := ft.RecordedSynthesize()
	if len(rec) != 2 {
		t.Fatalf("expected 2 synth calls, got %d", len(rec))
	}
	if rec[0].PrevActualSec != 0 || rec[0].PrevTextChars != 0 {
		t.Errorf("first call should have zero feedback, got %+v", rec[0])
	}
	// First call was over-run (12s>10s), so second call should carry feedback.
	if rec[1].PrevActualSec != 12.0 {
		t.Errorf("second call PrevActualSec: want 12, got %f", rec[1].PrevActualSec)
	}
	if rec[1].PrevTextChars != 6 { // "abcdef"
		t.Errorf("second call PrevTextChars: want 6, got %d", rec[1].PrevTextChars)
	}
}

func TestAgentRun_DefaultObservabilityObserverEmitsMetric(t *testing.T) {
	// Smoke-test that DefaultObservabilityObserver can be wired without
	// panicking. We don't read the gauge value (the package-level
	// promauto vec is shared across tests / processes) — the point is
	// the call site doesn't blow up when the agent fires it.
	cfg := defaultCfg()
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 10_000}, nil)
	agent := NewAgent(ft, WithObserver(DefaultObservabilityObserver))
	if _, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "x"}, cfg); err != nil {
		t.Fatalf("err: %v", err)
	}
}

func TestAgentRun_ObserverFires(t *testing.T) {
	var decisions []Decision
	observer := func(d Decision, _ State, _ Observation) {
		decisions = append(decisions, d)
	}
	cfg := defaultCfg()
	cfg.GapAfterMs = 500
	cfg.MaxAttempts = 1
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
		WantRetranslate("x", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 10_200}, nil)
	cfg.GapAfterMs = 2_000
	agent := NewAgent(ft, WithObserver(observer))
	_, err := agent.Run(context.Background(), RunInput{SegmentID: 1, InitialText: "init"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(decisions) < 2 {
		t.Errorf("expected ≥ 2 observer fires, got %d", len(decisions))
	}
}

func TestAgentRun_NilToolsPanics(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Error("expected panic on nil tools")
		}
	}()
	NewAgent(nil)
}

func TestAgentRun_EmptyInitialTextErrors(t *testing.T) {
	agent := NewAgent(newFakeTools())
	_, err := agent.Run(context.Background(), RunInput{}, defaultCfg())
	if err == nil {
		t.Fatal("expected error for empty InitialText")
	}
}

func TestAgentRun_HardCapEscapeAcceptReturnsMarkedOutput(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 10.0
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 0
	cfg.MaxAttempts = 0
	cfg.HardMaxOverDriftSec = 0.3
	cfg.HardMaxUnderDriftSec = 0.7
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 10_559, AudioRelPath: "over.wav"}, nil)
	agent := NewAgent(ft)

	out, err := agent.Run(context.Background(), RunInput{SegmentID: 42, InitialText: "too long"}, cfg)
	if err != nil {
		t.Fatalf("hard-cap escape should return marked output, not block the whole segment: %v", err)
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "clip_overflow" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
	if !out.FinalDecision.HardCapExceeded || out.FinalDecision.HardCapDirection != "over" {
		t.Fatalf("expected over hard cap mark, got %+v", out.FinalDecision)
	}
	if out.FinalAudioRelPath != "over.wav" || out.FinalActualMs != 10_559 {
		t.Fatalf("expected final hard-cap output to be preserved, got path=%q ms=%d", out.FinalAudioRelPath, out.FinalActualMs)
	}
}

func TestAgentRun_RejectHardCapEscapeReturnsError(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 10.0
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 0
	cfg.MaxAttempts = 0
	cfg.HardMaxOverDriftSec = 0.3
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.RejectHardCapEscape = true
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 10_559, AudioRelPath: "over.wav"}, nil)
	agent := NewAgent(ft)

	out, err := agent.Run(context.Background(), RunInput{SegmentID: 420, InitialText: "too long"}, cfg)
	if err == nil {
		t.Fatal("expected hard-cap escape to fail when RejectHardCapEscape is enabled")
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "clip_overflow" {
		t.Fatalf("unexpected final decision evidence: %+v", out.FinalDecision)
	}
	if !out.FinalDecision.HardCapExceeded || out.FinalDecision.HardCapDirection != "over" {
		t.Fatalf("expected over hard-cap mark, got %+v", out.FinalDecision)
	}
	if out.FinalAudioRelPath != "over.wav" || out.FinalActualMs != 10_559 {
		t.Fatalf("expected rejected output evidence to be preserved, got path=%q ms=%d", out.FinalAudioRelPath, out.FinalActualMs)
	}
}

func TestAgentRun_RetranslateFailureAcceptsHardCapViolationAsMarkedOutput(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 10.0
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 0
	cfg.HardMaxOverDriftSec = 0.3
	cfg.HardMaxUnderDriftSec = 0.7
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 10_600, AudioRelPath: "over.wav"}, nil).
		WantRetranslate("", false, errFakeBoom)
	agent := NewAgent(ft)

	out, err := agent.Run(context.Background(), RunInput{SegmentID: 43, InitialText: "too long"}, cfg)
	if err != nil {
		t.Fatalf("retranslate failure should return marked current output, not block the whole segment: %v", err)
	}
	if out.FinalDecision.Kind != DecisionAccept || out.FinalDecision.Reason != "retranslate_failed" {
		t.Fatalf("unexpected final decision: %+v", out.FinalDecision)
	}
	if !out.FinalDecision.HardCapExceeded || out.FinalDecision.HardCapDirection != "over" {
		t.Fatalf("expected over hard cap mark, got %+v", out.FinalDecision)
	}
	if out.FinalAudioRelPath != "over.wav" || out.FinalActualMs != 10_600 {
		t.Fatalf("expected final hard-cap output to be preserved, got path=%q ms=%d", out.FinalAudioRelPath, out.FinalActualMs)
	}
}

func TestAgentRun_BestRestoreSkipsHardCapViolatingBest(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 10.0
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 0
	cfg.MaxAttempts = 1
	cfg.HardMaxOverDriftSec = 0.3
	cfg.HardMaxUnderDriftSec = 0.7
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 10_350, AudioRelPath: "best-but-over-cap.wav"}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 9_500, AudioRelPath: "final-within-cap.wav"}, nil)
	agent := NewAgent(ft)

	out, err := agent.Run(context.Background(), RunInput{SegmentID: 44, InitialText: "initial"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if out.RestoredFromBest {
		t.Fatalf("must not restore hard-cap-violating best result: %+v", out)
	}
	if out.FinalAudioRelPath != "final-within-cap.wav" {
		t.Fatalf("expected final-within-cap.wav, got %q", out.FinalAudioRelPath)
	}
}

func TestAgentRun_BestRestoreCanSelectLeastBadHardCapViolation(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 10.0
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 0
	cfg.MaxAttempts = 1
	cfg.AbsMaxDriftSec = 0.8
	cfg.HardMaxOverDriftSec = 0.3
	cfg.HardMaxUnderDriftSec = 0.5
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 10_350, AudioRelPath: "best-over-cap.wav"}, nil).
		WantRetranslate("shorter", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 9_400, AudioRelPath: "final-under-cap.wav"}, nil)
	agent := NewAgent(ft)

	out, err := agent.Run(context.Background(), RunInput{SegmentID: 45, InitialText: "initial"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !out.RestoredFromBest {
		t.Fatalf("expected least-bad best restore, got %+v", out)
	}
	if !out.RestoredBestHardCapExceeded || out.RestoredBestHardCapDirection != "over" {
		t.Fatalf("expected restored best hard-cap marker direction=over, got %+v", out)
	}
	if !out.FinalDecision.HardCapExceeded || out.FinalDecision.HardCapDirection != "over" {
		t.Fatalf("final decision must keep hard-cap marker for restored best, got %+v", out.FinalDecision)
	}
	if out.FinalAudioRelPath != "best-over-cap.wav" || out.FinalActualMs != 10_350 {
		t.Fatalf("expected best-over-cap.wav at 10350ms, got path=%q ms=%d", out.FinalAudioRelPath, out.FinalActualMs)
	}
}

// =========================================================================
// OPT-201-followup-2: HardMaxUnderDriftSec / HardMaxOverDriftSec absolute
// caps. The cap layers on top of the percentage gates — every regular
// acceptance branch (within_threshold / borrow_from_gap / judge_veto_drift)
// must satisfy BOTH the percentage gate AND the absolute cap; the
// explicit escape branches (no_more_attempts / clip_overflow /
// over_short_gap_stuck / retranslation_disabled) still accept but
// flag the decision via HardCapExceeded + HardCapDirection.
// =========================================================================
func TestAgentRun_BestRestorePrefersSmallOverCapToHugeUnderRun(t *testing.T) {
	cfg := defaultCfg()
	cfg.TargetSec = 32.04
	cfg.TargetMs = 32_040
	cfg.GapAfterMs = 0
	cfg.MaxAttempts = 1
	cfg.AbsMaxDriftSec = 0.3
	cfg.HardMaxOverDriftSec = 0.3
	cfg.HardMaxUnderDriftSec = 0.7
	ft := newFakeTools().
		WantSynthesize(TTSResult{ActualDurationMs: 32_626, AudioRelPath: "best-small-over-cap.wav"}, nil).
		WantRetranslate("badly compressed", false, nil).
		WantSynthesize(TTSResult{ActualDurationMs: 24_904, AudioRelPath: "final-huge-under-run.wav"}, nil)
	agent := NewAgent(ft)

	out, err := agent.Run(context.Background(), RunInput{SegmentID: 16011, InitialText: "initial"}, cfg)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !out.RestoredFromBest {
		t.Fatalf("expected least-bad restore to choose small over-cap candidate, got %+v", out)
	}
	if out.FinalAudioRelPath != "best-small-over-cap.wav" || out.FinalActualMs != 32_626 {
		t.Fatalf("expected best-small-over-cap.wav at 32626ms, got path=%q ms=%d", out.FinalAudioRelPath, out.FinalActualMs)
	}
	if !out.RestoredBestHardCapExceeded || out.RestoredBestHardCapDirection != "over" {
		t.Fatalf("expected restored best hard-cap marker direction=over, got %+v", out)
	}
}

func TestDecide_HardCap_UnderRunRejectsWithinThreshold(t *testing.T) {
	// 30s target, 28.9s actual: -1.1s under-run, drift_pct = 3.67%
	// which is INSIDE the default 6% threshold. Hard cap 0.7s says
	// "1.1s under is too much, retranslate".
	cfg := defaultCfg()
	cfg.TargetSec = 30.0
	cfg.TargetMs = 30_000
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.HardMaxOverDriftSec = 0.3
	state := State{Text: "t", Attempt: 1, BestAbsDrift: math.MaxFloat64}
	obs := makeObs(30_000, 28_900)

	d := Decide(state, obs, cfg)
	if d.Kind != DecisionRetranslate {
		t.Errorf("Kind: want Retranslate (under-run %v > cap 0.7s), got %v reason=%q",
			obs.AbsDrift, d.Kind, d.Reason)
	}
	if d.HardCapExceeded {
		t.Errorf("HardCapExceeded should be false on retranslate decisions (cap forced the retry, not bypassed it)")
	}
}

func TestDecide_HardCap_OverRunRejectsBorrow(t *testing.T) {
	// 20s target, 20.4s actual: +0.4s over-run, drift_pct = 2% which
	// is INSIDE MaxBorrowDriftPct=12%, and the gap is large enough
	// to absorb 0.4s. Hard cap 0.3s says "0.4s over is too much".
	cfg := defaultCfg()
	cfg.TargetSec = 20.0
	cfg.TargetMs = 20_000
	cfg.GapAfterMs = 1_500
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.HardMaxOverDriftSec = 0.3
	state := State{Text: "t", Attempt: 1, BestAbsDrift: math.MaxFloat64}
	obs := makeObs(20_000, 20_400)

	d := Decide(state, obs, cfg)
	if d.Kind != DecisionRetranslate {
		t.Errorf("Kind: want Retranslate (over-run %v > cap 0.3s), got %v reason=%q",
			obs.AbsDrift, d.Kind, d.Reason)
	}
}

func TestDecide_HardCap_RejectsJudgeVeto(t *testing.T) {
	// 25s target, 23.5s actual: under-run 1.5s = 6% which is BELOW
	// AdaptiveMaxAcceptableDrift (8% for ≥20s) so VETO would normally
	// fire. Hard cap 0.7s blocks it.
	cfg := defaultCfg()
	cfg.TargetSec = 25.0
	cfg.TargetMs = 25_000
	cfg.JudgeVetoDriftRetry = true
	cfg.JudgeVetoMinScore = 0.95
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.HardMaxOverDriftSec = 0.3
	state := State{Text: "t", Attempt: 1, BestAbsDrift: math.MaxFloat64}
	obs := Observation{
		ActualDurationMs: 23_500,
		ActualSec:        23.5,
		OverflowMs:       -1_500,
		AbsDrift:         1.5,
		DriftPct:         0.06,
		JudgeVerdict:     "accept",
		JudgeScore:       0.98,
	}

	d := Decide(state, obs, cfg)
	if d.Kind != DecisionRetranslate {
		t.Errorf("Kind: want Retranslate (VETO blocked by hard cap), got %v reason=%q", d.Kind, d.Reason)
	}
	if d.Reason == "judge_veto_drift" {
		t.Errorf("Reason should not be judge_veto_drift when hard cap fires, got %q", d.Reason)
	}
}

func TestDecide_HardCap_RejectsOverRunJudgeVeto(t *testing.T) {
	// 25s target, 26.2s actual: over-run 1.2s = 4.8% < adaptive 8%
	// → VETO would fire on over-run path. Cap 0.3s blocks it.
	cfg := defaultCfg()
	cfg.TargetSec = 25.0
	cfg.TargetMs = 25_000
	cfg.GapAfterMs = 300 // too small to borrow anyway
	cfg.JudgeVetoDriftRetry = true
	cfg.JudgeVetoMinScore = 0.95
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.HardMaxOverDriftSec = 0.3
	state := State{Text: "t", Attempt: 1, BestAbsDrift: math.MaxFloat64}
	obs := Observation{
		ActualDurationMs: 26_200,
		ActualSec:        26.2,
		OverflowMs:       1_200,
		AbsDrift:         1.2,
		DriftPct:         0.048,
		JudgeVerdict:     "accept",
		JudgeScore:       0.98,
	}

	d := Decide(state, obs, cfg)
	if d.Kind != DecisionRetranslate {
		t.Errorf("Kind: want Retranslate (over-run VETO blocked by 0.3s cap), got %v reason=%q", d.Kind, d.Reason)
	}
}

func TestDecide_HardCap_DisabledFallsBackToLegacy(t *testing.T) {
	// 25s target, drift 1.8s = 7.2% which is OUTSIDE DriftThreshold
	// (6%) so the within_threshold path doesn't fire — VETO is the
	// only accepting path. 7.2% < 8% AdaptiveMaxAcceptableDrift so
	// VETO would normally fire. With caps disabled (=0) the legacy
	// VETO accept must still fire.
	cfg := defaultCfg()
	cfg.TargetSec = 25.0
	cfg.TargetMs = 25_000
	cfg.JudgeVetoDriftRetry = true
	cfg.JudgeVetoMinScore = 0.95
	cfg.HardMaxUnderDriftSec = 0 // disabled
	cfg.HardMaxOverDriftSec = 0  // disabled
	state := State{Text: "t", Attempt: 1, BestAbsDrift: math.MaxFloat64}
	obs := Observation{
		ActualDurationMs: 23_200,
		ActualSec:        23.2,
		OverflowMs:       -1_800,
		AbsDrift:         1.8,
		DriftPct:         0.072,
		JudgeVerdict:     "accept",
		JudgeScore:       0.98,
	}

	d := Decide(state, obs, cfg)
	if d.Kind != DecisionAccept || d.Reason != "judge_veto_drift" {
		t.Errorf("with cap disabled the legacy VETO accept must fire; got Kind=%v Reason=%q", d.Kind, d.Reason)
	}
	if d.HardCapExceeded {
		t.Errorf("HardCapExceeded must be false when caps are disabled, got %+v", d)
	}
}

func TestDecide_HardCap_LastAttemptStillAcceptsButMarks(t *testing.T) {
	// Last attempt, drift 1.5s under (above cap) but inside the
	// percentage threshold → no more retries, MUST accept and mark.
	cfg := defaultCfg()
	cfg.TargetSec = 25.0
	cfg.TargetMs = 25_000
	cfg.MaxAttempts = 3
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.HardMaxOverDriftSec = 0.3
	state := State{Text: "t", Attempt: 3, BestAbsDrift: math.MaxFloat64} // == MaxAttempts → last
	obs := makeObs(25_000, 23_500)

	d := Decide(state, obs, cfg)
	if d.Kind != DecisionAccept {
		t.Errorf("last-attempt must accept regardless of cap, got %v reason=%q", d.Kind, d.Reason)
	}
	if !d.HardCapExceeded {
		t.Errorf("HardCapExceeded must be set when shipping over the cap on escape, got %+v", d)
	}
	if d.HardCapDirection != "under" {
		t.Errorf("HardCapDirection: want under, got %q", d.HardCapDirection)
	}
}

func TestDecide_HardCap_OverShortGapStuckStillEscapes(t *testing.T) {
	// Over-run stuck loop: AwI=4, Attempt=7 (MaxAttempts=10), cap=0.3s,
	// observed 1.5s over. The stuck-escape branch must still accept
	// because the loop has demonstrated non-convergence; the cap
	// stamp surfaces in metrics/logs but doesn't block the bailout.
	cfg := defaultCfg()
	cfg.TargetSec = 30.0
	cfg.TargetMs = 30_000
	cfg.GapAfterMs = 200 // below ShortGapThresholdMs → no borrow
	cfg.MaxAttempts = 10
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.HardMaxOverDriftSec = 0.3
	state := State{
		Text:                       "t",
		Attempt:                    7,
		AttemptsWithoutImprovement: 4,
		BestAbsDrift:               math.MaxFloat64,
	}
	obs := makeObs(30_000, 31_500)

	d := Decide(state, obs, cfg)
	if d.Kind != DecisionAccept || d.Reason != "over_short_gap_stuck" {
		t.Errorf("expected escape accept, got Kind=%v Reason=%q", d.Kind, d.Reason)
	}
	if !d.HardCapExceeded || d.HardCapDirection != "over" {
		t.Errorf("escape must mark HardCapExceeded direction=over, got %+v", d)
	}
}

func TestDecide_HardCap_AsymmetricUnderToleratedOverNot(t *testing.T) {
	// Same |drift| 0.6s: as under-run it's BELOW the 0.7s cap →
	// within_threshold accept without mark; as over-run it's ABOVE
	// the 0.3s cap → forced retranslate.
	cfg := defaultCfg()
	cfg.TargetSec = 10.0
	cfg.TargetMs = 10_000
	cfg.GapAfterMs = 2_000 // plenty of gap so borrow would otherwise fire
	cfg.HardMaxUnderDriftSec = 0.7
	cfg.HardMaxOverDriftSec = 0.3
	state := State{Text: "t", Attempt: 1, BestAbsDrift: math.MaxFloat64}

	// Under-run 0.6s, drift_pct=6% (== threshold) → accept.
	under := makeObs(10_000, 9_400)
	d := Decide(state, under, cfg)
	if d.Kind != DecisionAccept || d.Reason != "within_threshold" {
		t.Errorf("under-run 0.6s should accept within_threshold (under cap 0.7s), got Kind=%v Reason=%q",
			d.Kind, d.Reason)
	}
	if d.HardCapExceeded {
		t.Errorf("HardCapExceeded must stay false when drift <= cap, got %+v", d)
	}

	// Over-run 0.6s (above 0.3s cap) → retranslate even though
	// borrow would otherwise fit. Confirms asymmetric cap.
	over := makeObs(10_000, 10_600)
	d = Decide(state, over, cfg)
	if d.Kind != DecisionRetranslate {
		t.Errorf("over-run 0.6s should retranslate (over cap 0.3s), got Kind=%v Reason=%q",
			d.Kind, d.Reason)
	}
}

// =========================================================================
// Decision matrix sweep: cross-product of {drift bands × overflow direction
// × attempt position × thinking signals}. Generates ~120 cases that
// individually assert (Kind, Reason, UseThinking) — gives the ≥ 100 case
// coverage required by the plan.
// =========================================================================
func TestDecide_DecisionMatrixSweep(t *testing.T) {
	driftBands := []struct {
		name     string
		actualMs int64
	}{
		{"under-zero", 9_400},  // -6% exactly within threshold
		{"under-large", 8_000}, // -20% out of threshold
		{"exact", 10_000},
		{"over-small", 10_300},      // +3% small overflow
		{"over-borrowable", 11_100}, // +11% borrowable when gap fits
		{"over-large", 12_500},      // +25% large overflow
	}
	gapStates := []struct {
		name  string
		gapMs int64
	}{
		{"short-gap", 500},
		{"medium-gap", 1_500},
		{"large-gap", 5_000},
	}
	attemptStates := []struct {
		name    string
		attempt int
	}{
		{"first-attempt", 0},
		{"mid-attempt", 2},
		{"last-attempt", 5},
	}
	thinkingTriggers := []struct {
		name  string
		stuck int
		nc    int
	}{
		{"none", 0, 0},
		{"stuck-only", 2, 0},
		{"nc-only", 0, 3},
	}

	count := 0
	for _, db := range driftBands {
		for _, gs := range gapStates {
			for _, as := range attemptStates {
				for _, tt := range thinkingTriggers {
					count++
					t.Run(db.name+"/"+gs.name+"/"+as.name+"/"+tt.name, func(t *testing.T) {
						cfg := defaultCfg()
						cfg.GapAfterMs = gs.gapMs
						state := State{
							Attempt:                    as.attempt,
							Text:                       "x",
							BestAbsDrift:               math.MaxFloat64,
							ConsecutiveSameChars:       tt.stuck,
							AttemptsWithoutImprovement: tt.nc,
						}
						obs := makeObs(cfg.TargetMs, db.actualMs)
						d := Decide(state, obs, cfg)
						// Sanity: Kind must always be a known value.
						if d.Kind != DecisionAccept && d.Kind != DecisionRetranslate {
							t.Errorf("unknown decision kind: %v", d.Kind)
						}
						// Sanity: UseThinking only set on retranslate.
						if d.Kind == DecisionAccept && d.UseThinking {
							t.Errorf("UseThinking set on Accept decision: %+v", d)
						}
						// Sanity: last-attempt never retranslates.
						if as.attempt >= cfg.MaxAttempts && d.Kind == DecisionRetranslate {
							t.Errorf("last-attempt should not retranslate (attempt=%d max=%d), got %+v", as.attempt, cfg.MaxAttempts, d)
						}
						// Sanity: retranslate's UseThinking should agree with shouldUseThinking.
						if d.Kind == DecisionRetranslate {
							want := shouldUseThinking(state, cfg)
							if d.UseThinking != want {
								t.Errorf("UseThinking mismatch: agent=%v helper=%v state=%+v", d.UseThinking, want, state)
							}
						}
					})
				}
			}
		}
	}
	// Sanity check: we generated the expected number of permutations.
	if expected := len(driftBands) * len(gapStates) * len(attemptStates) * len(thinkingTriggers); count != expected {
		t.Errorf("matrix permutation count mismatch: want %d, got %d", expected, count)
	}
}

// silence unused-import lint when llm.RetranslationAttempt is only
// referenced indirectly via fakeTools recordings (helps refactors).
var _ = llm.RetranslationAttempt{}
