package agents

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
)

// fakeTools is the deterministic test fixture used by every agent unit
// test. Each method consumes the next entry from its programmed sequence;
// running off the end of a sequence is an explicit fail-fast (caller
// must declare the expected number of calls up front).
//
// The fixture supports six classes of trajectory required by
// testing-and-rollout.mdc §2:
//
//  1. single-hit       — first Synthesize is within tolerance → accept
//  2. stable convergence — drift improves monotonically over 2-3 attempts
//  3. oscillation     — drift bounces around without improving
//  4. stuck           — same text echoed back for ≥ stuck threshold
//  5. context cancel  — ctx is cancelled mid-flight
//  6. tool error      — one of the tools returns an error
//
// Construction style: create via newFakeTools and then push individual
// expectations with WantSynthesize / WantRetranslate / WantJudge so each
// test case reads like a script of the trajectory it wants to drive.
type fakeTools struct {
	mu sync.Mutex

	ttsSeq         []ttsExpect
	retranslateSeq []retranslateExpect
	ensembleSeq    []ensembleExpect
	judgeSeq       []judgeExpect

	ttsCalls         int
	retranslateCalls int
	ensembleCalls    int
	judgeCalls       int

	// Callbacks invoked before the next return value is consumed. Used
	// by the context-cancellation tests to cancel a parent ctx as soon
	// as the agent hits a specific tool. nil = no-op.
	onSynthesize  func()
	onRetranslate func()
	onEnsemble    func()
	onJudge       func()

	// recordedArgs is appended every time a method is called so tests
	// can assert what the agent actually asked for (e.g. that the
	// retranslate args include the History from prior attempts).
	recordedSynthesize  []TTSArgs
	recordedRetranslate []RetranslateArgs
	recordedEnsemble    []RetranslateArgs
	recordedJudge       []JudgeArgs
}

type ttsExpect struct {
	result TTSResult
	err    error
}

type retranslateExpect struct {
	result RetranslateResult
	err    error
}

type ensembleExpect struct {
	result EnsembleResult
	err    error
}

type judgeExpect struct {
	result *JudgeResult
	err    error
}

func newFakeTools() *fakeTools {
	return &fakeTools{}
}

// WantSynthesize programs the next Synthesize call to return result, err.
// The order of WantSynthesize calls is the order in which the agent will
// receive them — first programmed, first consumed.
func (f *fakeTools) WantSynthesize(result TTSResult, err error) *fakeTools {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ttsSeq = append(f.ttsSeq, ttsExpect{result: result, err: err})
	return f
}

// WantRetranslate programs the next RetranslateWithConstraint call.
func (f *fakeTools) WantRetranslate(text string, usedThinking bool, err error) *fakeTools {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.retranslateSeq = append(f.retranslateSeq, retranslateExpect{
		result: RetranslateResult{Text: text, UsedThinking: usedThinking},
		err:    err,
	})
	return f
}

// WantEnsemble programs the next RetranslateEnsemble call. Pass
// ErrEnsembleUnavailable to model "no ensemble models configured" so
// the agent falls back to a normal retranslate (the production
// fallback path).
func (f *fakeTools) WantEnsemble(result EnsembleResult, err error) *fakeTools {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ensembleSeq = append(f.ensembleSeq, ensembleExpect{result: result, err: err})
	return f
}

// WantJudge programs the next JudgeFidelity call. Pass nil for result
// to model "judge disabled / empty input" (current observe-only semantics).
func (f *fakeTools) WantJudge(result *JudgeResult, err error) *fakeTools {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.judgeSeq = append(f.judgeSeq, judgeExpect{result: result, err: err})
	return f
}

// Synthesize consumes the next ttsSeq entry. Failing here on overflow
// surfaces "the agent called Synthesize more times than the test
// expected" as an explicit, debuggable error, instead of a subtle
// hang or zero value.
func (f *fakeTools) Synthesize(ctx context.Context, args TTSArgs) (TTSResult, error) {
	if f.onSynthesize != nil {
		f.onSynthesize()
	}
	if err := ctx.Err(); err != nil {
		return TTSResult{}, fmt.Errorf("fakeTools.Synthesize: ctx cancelled before call: %w", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recordedSynthesize = append(f.recordedSynthesize, args)
	idx := f.ttsCalls
	f.ttsCalls++
	if idx >= len(f.ttsSeq) {
		return TTSResult{}, fmt.Errorf("fakeTools.Synthesize call %d: no more programmed results (programmed=%d)", idx+1, len(f.ttsSeq))
	}
	exp := f.ttsSeq[idx]
	return exp.result, exp.err
}

func (f *fakeTools) RetranslateWithConstraint(ctx context.Context, args RetranslateArgs) (RetranslateResult, error) {
	if f.onRetranslate != nil {
		f.onRetranslate()
	}
	if err := ctx.Err(); err != nil {
		return RetranslateResult{}, fmt.Errorf("fakeTools.Retranslate: ctx cancelled before call: %w", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recordedRetranslate = append(f.recordedRetranslate, args)
	idx := f.retranslateCalls
	f.retranslateCalls++
	if idx >= len(f.retranslateSeq) {
		return RetranslateResult{}, fmt.Errorf("fakeTools.Retranslate call %d: no more programmed results (programmed=%d)", idx+1, len(f.retranslateSeq))
	}
	exp := f.retranslateSeq[idx]
	return exp.result, exp.err
}

func (f *fakeTools) RetranslateEnsemble(ctx context.Context, args RetranslateArgs) (EnsembleResult, error) {
	if f.onEnsemble != nil {
		f.onEnsemble()
	}
	if err := ctx.Err(); err != nil {
		return EnsembleResult{}, fmt.Errorf("fakeTools.Ensemble: ctx cancelled before call: %w", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recordedEnsemble = append(f.recordedEnsemble, args)
	idx := f.ensembleCalls
	f.ensembleCalls++
	if idx >= len(f.ensembleSeq) {
		// Default to ErrEnsembleUnavailable so an agent test that
		// forgets to program an ensemble result falls back to the
		// normal-retranslate path (matching production semantics) and
		// the test author sees a clear "ensemble unavailable" line in
		// the agent's slog instead of a panic.
		return EnsembleResult{}, ErrEnsembleUnavailable
	}
	exp := f.ensembleSeq[idx]
	return exp.result, exp.err
}

func (f *fakeTools) JudgeFidelity(ctx context.Context, args JudgeArgs) (*JudgeResult, error) {
	if f.onJudge != nil {
		f.onJudge()
	}
	if err := ctx.Err(); err != nil {
		return nil, fmt.Errorf("fakeTools.Judge: ctx cancelled before call: %w", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recordedJudge = append(f.recordedJudge, args)
	idx := f.judgeCalls
	f.judgeCalls++
	if idx >= len(f.judgeSeq) {
		// Judge sequence overflow is allowed to be silent: production
		// callers wrap JudgeFidelity in a fire-and-forget goroutine and
		// treat nil as "no verdict observed". Tests that want to assert
		// judge call counts should inspect Calls("judge").
		return nil, nil
	}
	exp := f.judgeSeq[idx]
	return exp.result, exp.err
}

// Calls returns the recorded number of invocations per tool. Empty
// arg string ("") returns the total call count across all tools.
func (f *fakeTools) Calls(tool string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch tool {
	case "tts", "synthesize":
		return f.ttsCalls
	case "retranslate":
		return f.retranslateCalls
	case "ensemble":
		return f.ensembleCalls
	case "judge":
		return f.judgeCalls
	case "":
		return f.ttsCalls + f.retranslateCalls + f.ensembleCalls + f.judgeCalls
	default:
		return -1
	}
}

// RecordedEnsemble returns a copy of every RetranslateArgs the agent
// passed to RetranslateEnsemble. Symmetric with RecordedRetranslate.
func (f *fakeTools) RecordedEnsemble() []RetranslateArgs {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]RetranslateArgs, len(f.recordedEnsemble))
	copy(out, f.recordedEnsemble)
	return out
}

// RecordedSynthesize returns a copy of all TTSArgs the agent passed,
// in call order. Tests use it to assert the agent rebuilt PrevActualSec /
// PrevTextChars correctly between attempts.
func (f *fakeTools) RecordedSynthesize() []TTSArgs {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]TTSArgs, len(f.recordedSynthesize))
	copy(out, f.recordedSynthesize)
	return out
}

// RecordedRetranslate returns a copy of all RetranslateArgs the agent
// passed, in call order. Useful for asserting that retry History grew
// correctly across attempts.
func (f *fakeTools) RecordedRetranslate() []RetranslateArgs {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]RetranslateArgs, len(f.recordedRetranslate))
	copy(out, f.recordedRetranslate)
	return out
}

// RecordedJudge returns a copy of all JudgeArgs the agent passed.
func (f *fakeTools) RecordedJudge() []JudgeArgs {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]JudgeArgs, len(f.recordedJudge))
	copy(out, f.recordedJudge)
	return out
}

// errFakeBoom is the canonical "transient tool failure" used by error
// trajectory tests. Importing this constant ensures every error-path
// test reads the same way.
var errFakeBoom = errors.New("fake_tools: simulated transient error")

// TestFakeTools_Trajectories sanity-checks the six trajectory classes
// the fixture is required to support. Failure here means the fixture
// itself is broken and downstream agent tests cannot be trusted.
//
// This is intentionally a regression guard for the harness — it does
// NOT exercise the agent's Decide function (that's in
// segment_agent_test.go).
func TestFakeTools_Trajectories(t *testing.T) {
	t.Run("single-hit", func(t *testing.T) {
		ft := newFakeTools().
			WantSynthesize(TTSResult{AudioRelPath: "out.wav", ActualDurationMs: 10_000}, nil)
		res, err := ft.Synthesize(context.Background(), TTSArgs{TargetDurationSec: 10})
		if err != nil {
			t.Fatalf("unexpected err: %v", err)
		}
		if res.ActualDurationMs != 10_000 {
			t.Fatalf("want 10_000ms, got %d", res.ActualDurationMs)
		}
		if ft.Calls("tts") != 1 {
			t.Fatalf("expected 1 tts call, got %d", ft.Calls("tts"))
		}
	})

	t.Run("stable-convergence", func(t *testing.T) {
		ft := newFakeTools().
			WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
			WantSynthesize(TTSResult{ActualDurationMs: 10_500}, nil)
		for i := 0; i < 2; i++ {
			if _, err := ft.Synthesize(context.Background(), TTSArgs{TargetDurationSec: 10}); err != nil {
				t.Fatalf("call %d: %v", i, err)
			}
		}
	})

	t.Run("oscillation", func(t *testing.T) {
		ft := newFakeTools().
			WantSynthesize(TTSResult{ActualDurationMs: 13_000}, nil).
			WantSynthesize(TTSResult{ActualDurationMs: 8_000}, nil).
			WantSynthesize(TTSResult{ActualDurationMs: 12_000}, nil).
			WantSynthesize(TTSResult{ActualDurationMs: 8_500}, nil)
		for i := 0; i < 4; i++ {
			if _, err := ft.Synthesize(context.Background(), TTSArgs{TargetDurationSec: 10}); err != nil {
				t.Fatalf("call %d: %v", i, err)
			}
		}
	})

	t.Run("stuck-same-text", func(t *testing.T) {
		ft := newFakeTools().
			WantRetranslate("same text", false, nil).
			WantRetranslate("same text", false, nil).
			WantRetranslate("same text", false, nil)
		for i := 0; i < 3; i++ {
			res, err := ft.RetranslateWithConstraint(context.Background(), RetranslateArgs{})
			if err != nil {
				t.Fatalf("call %d: %v", i, err)
			}
			if res.Text != "same text" {
				t.Fatalf("call %d: want 'same text', got %q", i, res.Text)
			}
		}
	})

	t.Run("context-cancel-mid-flight", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		ft := newFakeTools().
			WantSynthesize(TTSResult{ActualDurationMs: 10_000}, nil).
			WantSynthesize(TTSResult{ActualDurationMs: 10_000}, nil)
		ft.onSynthesize = func() {
			// Cancel after the first programmed result is queued but
			// before the second one is consumed.
			if ft.Calls("tts") == 1 {
				cancel()
			}
		}
		if _, err := ft.Synthesize(ctx, TTSArgs{}); err != nil {
			t.Fatalf("first call should succeed: %v", err)
		}
		_, err := ft.Synthesize(ctx, TTSArgs{})
		if err == nil {
			t.Fatalf("expected ctx cancellation error on second call")
		}
	})

	t.Run("tool-error-bubbles-up", func(t *testing.T) {
		ft := newFakeTools().
			WantSynthesize(TTSResult{}, errFakeBoom)
		_, err := ft.Synthesize(context.Background(), TTSArgs{})
		if !errors.Is(err, errFakeBoom) {
			t.Fatalf("expected errFakeBoom, got %v", err)
		}
	})
}
