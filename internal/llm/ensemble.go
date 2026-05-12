// Package llm — OPT-202 Speculative Ensemble Retranslate.
//
// When the SegmentAgent's adaptive retry loop is about to give up on a
// segment (attempt_without_improvement >= threshold OR seg marked
// "important" OR judge_score < 0.7 after retry), running a single
// fresh retranslate is unlikely to break the deadlock — the same
// model that produced the stuck translation will keep producing
// translations from the same prior. The ensemble path fans the same
// retry input out across N pre-configured models (default
// deepseek-chat + qwen-plus), then has a thinking-class judge pick
// the best candidate pairwise.
//
// Why a separate file: the ensemble call is structurally different
// from a normal retranslate (parallel + judge), and keeping it
// isolated keeps client.go from growing yet another 200-line
// retranslate variant. The two functions share retranslateWithConstraintModel
// (the parameter-rich inner) so prompt construction stays in one
// place.
package llm

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"sync"
)

// EnsembleArgs is the input bundle for RetranslateEnsemble. Mirrors
// llm.RetranslateWithConstraint's parameter list 1:1; pulled into a
// struct because passing 13 positional arguments to a parallel-fanout
// function is unmaintainable.
type EnsembleArgs struct {
	SourceLanguage     string
	TargetLanguage     string
	SourceText         string
	CurrentTrans       string
	TargetSec          float64
	ActualSec          float64
	Attempt            int
	MaxAttempts        int
	DriftThresholdPct  float64
	History            []RetranslationAttempt
	ObservedCharsPerSec float64
	ContextBefore      []ContextSegment
	NextSourceText     string
	TranslationSummary string

	// EpisodeSummary is the smaller summary used by the inline judge
	// step. Distinct from TranslationSummary because the judge prompt
	// is fixed-budget; the bigger reference card + glossary lives in
	// TranslationSummary and is consumed by the retranslate prompt.
	EpisodeSummary string
}

// EnsembleResult is what RetranslateEnsemble returns. Best is the
// chosen text; Scores carries every candidate's text + judge verdict
// so the agent's observability layer can log the spread.
type EnsembleResult struct {
	Best        string
	BestModel   string
	BestVerdict JudgeResult
	Candidates  []EnsembleCandidate
}

// EnsembleCandidate captures one model's output + judgement.
type EnsembleCandidate struct {
	Model   string
	Text    string
	Verdict *JudgeResult // nil if the judge failed on this candidate
	Err     error
}

// RetranslateEnsemble runs args through every model in `models` in
// parallel, then judges each candidate via JudgeFidelity, and returns
// the candidate with the highest OverallScore. The judge model used
// for scoring is judgeModelOverride (when non-empty); otherwise the
// client's c.judgeModel is used.
//
// Cost: O(N) retranslate calls + O(N) judge calls. The judge is the
// thinking-class model so the per-candidate cost is comparable to the
// retranslate itself; running 2 models in ensemble roughly triples
// the cost of a single retranslate. The agent layer is responsible
// for gating ensemble use (only when stuck / important / judge<0.7).
//
// Cancellation: the parent ctx is propagated to every goroutine; the
// first ctx error short-circuits the wait and bubbles up.
//
// Empty/single-model behaviour: when len(models) == 0 the function
// returns an error (caller should fall back to RetranslateWithConstraint).
// When len(models) == 1 the function still runs (1 retranslate + 1
// judge); the result is identical to RetranslateWithConstraint plus
// one judge call, which is useful as a smoke-test path before the
// L4 default-on rollout.
func (c *Client) RetranslateEnsemble(ctx context.Context, args EnsembleArgs, models []string, judgeModelOverride string) (EnsembleResult, error) {
	if len(models) == 0 {
		return EnsembleResult{}, errors.New("RetranslateEnsemble: models list is empty")
	}
	if c.baseURL == "" || c.apiKey == "" {
		return EnsembleResult{}, errors.New("RetranslateEnsemble: OPENAI_BASE_URL and OPENAI_API_KEY are required")
	}

	// Phase 1: parallel retranslate fan-out.
	candidates := make([]EnsembleCandidate, len(models))
	var wg sync.WaitGroup
	for i, m := range models {
		i, m := i, m
		wg.Add(1)
		go func() {
			defer wg.Done()
			text, err := c.retranslateWithConstraintModel(
				ctx, m,
				args.SourceLanguage, args.TargetLanguage,
				args.SourceText, args.CurrentTrans,
				args.TargetSec, args.ActualSec,
				args.Attempt, args.MaxAttempts,
				args.DriftThresholdPct,
				args.History,
				false, // ensemble does not use thinking mode by default
				args.ObservedCharsPerSec,
				args.ContextBefore,
				args.NextSourceText,
				args.TranslationSummary,
			)
			candidates[i] = EnsembleCandidate{Model: m, Text: text, Err: err}
		}()
	}
	wg.Wait()

	if err := ctx.Err(); err != nil {
		return EnsembleResult{Candidates: candidates}, fmt.Errorf("RetranslateEnsemble cancelled before judging: %w", err)
	}

	// Phase 2: parallel judge each non-erroring candidate. The judge
	// model is threaded explicitly via judgeFidelityWithModel so
	// concurrent ensembles (or a regular JudgeFidelity call on the
	// same client) cannot race on a shared field. When override is
	// empty, fall back to c.judgeModel.
	judgeModel := judgeModelOverride
	if judgeModel == "" {
		judgeModel = c.judgeModel
	}

	wg = sync.WaitGroup{}
	for i := range candidates {
		i := i
		if candidates[i].Err != nil || candidates[i].Text == "" {
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			res, err := c.judgeFidelityWithModel(ctx, JudgeArgs{
				SrcText:        args.SourceText,
				TgtText:        candidates[i].Text,
				SrcLang:        args.SourceLanguage,
				TgtLang:        args.TargetLanguage,
				EpisodeSummary: args.EpisodeSummary,
				PrevContext:    args.ContextBefore,
			}, judgeModel)
			if err != nil {
				slog.Warn("ensemble judge failed for candidate",
					"model", candidates[i].Model, "error", err,
				)
				return
			}
			candidates[i].Verdict = res
		}()
	}
	wg.Wait()

	// Pick the candidate with the highest OverallScore. Ties broken
	// by model order (lower index wins) so the result is deterministic
	// across runs.
	type scored struct {
		idx   int
		score float64
	}
	var pool []scored
	for i, cand := range candidates {
		if cand.Err != nil || cand.Text == "" || cand.Verdict == nil {
			continue
		}
		pool = append(pool, scored{idx: i, score: cand.Verdict.OverallScore()})
	}
	if len(pool) == 0 {
		// Fallback: every candidate failed retranslate or judge.
		// Surface the first error so the caller can log + fall back
		// to single-model RetranslateWithConstraint.
		for _, cand := range candidates {
			if cand.Err != nil {
				return EnsembleResult{Candidates: candidates}, fmt.Errorf("RetranslateEnsemble: every candidate failed; first err: %w", cand.Err)
			}
		}
		return EnsembleResult{Candidates: candidates}, errors.New("RetranslateEnsemble: no candidate received a judge verdict")
	}
	sort.SliceStable(pool, func(i, j int) bool {
		if pool[i].score == pool[j].score {
			return pool[i].idx < pool[j].idx
		}
		return pool[i].score > pool[j].score
	})
	winner := candidates[pool[0].idx]
	return EnsembleResult{
		Best:        winner.Text,
		BestModel:   winner.Model,
		BestVerdict: *winner.Verdict,
		Candidates:  candidates,
	}, nil
}
