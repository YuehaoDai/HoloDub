// Package pipeline — OPT-002-followup-2 segment-judge back-fill.
//
// BackfillSegmentJudges scans for segments that were synthesised but never
// received an asynchronous OPT-002 judge verdict (typically because the
// worker process restarted after the segment finished synthesising but
// before its detached judge goroutine completed). The function dispatches
// each missing segment through the same maybeJudgeSegmentAsync goroutine
// pattern used at synthesis time, so the back-fill path stays observably
// identical (same metrics, same failure handling) to the steady-state path.
//
// Bounded concurrency: a tiny semaphore limits the number of inflight
// judge calls so a worker boot does not stampede the LLM provider; a
// concurrency=3 default matches the single-segment cost of qwen-turbo
// without spiking the judge_in_flight gauge.
//
// The function returns nil even when individual segments fail to dispatch
// (judge calls are observe-only, dropping a back-fill is no worse than the
// original gap); it returns a non-nil error only when the initial DB scan
// itself fails.
package pipeline

import (
	"context"
	"log/slog"
	"sync"
)

// BackfillSegmentJudges is intended to be called from cmd/worker/main.go
// shortly after worker boot, and only when JudgeModel + JudgeBackfillOnStart
// are both set. It is safe to call multiple times: the underlying query
// always re-filters on judge_score IS NULL so a previously back-filled
// segment is naturally excluded next time.
func (s *Service) BackfillSegmentJudges(ctx context.Context, limit, concurrency int) error {
	if s.cfg.JudgeModel == "" {
		return nil
	}
	if limit <= 0 {
		return nil
	}
	if concurrency <= 0 {
		concurrency = 3
	}

	segments, err := s.store.ListSegmentsAwaitingJudge(ctx, limit)
	if err != nil {
		return err
	}
	if len(segments) == 0 {
		slog.Info("judge backfill: nothing to do",
			"limit", limit,
			"concurrency", concurrency,
		)
		return nil
	}

	slog.Info("judge backfill: dispatching",
		"count", len(segments),
		"limit", limit,
		"concurrency", concurrency,
	)

	// Bounded concurrency via buffered channel acting as a semaphore.
	// We use this instead of a worker-pool goroutine because each
	// maybeJudgeSegmentAsync call already spawns its own goroutine —
	// the semaphore here just throttles dispatch so we don't enqueue
	// 500 inflight LLM calls at once on big restarts.
	sem := make(chan struct{}, concurrency)
	var dispatched int
	var dispatchedMu sync.Mutex

	for i := range segments {
		seg := segments[i] // capture by value so the closure does not reference the loop variable
		// Reload the parent Job to pick up SourceLanguage / TargetLanguage /
		// TranslationSummary; ListSegmentsAwaitingJudge does NOT preload the
		// Job because gorm + sqlite + nested association is brittle and the
		// extra round-trip here is tiny (< 500 calls per worker boot).
		job, err := s.store.GetJob(ctx, seg.JobID)
		if err != nil {
			slog.Warn("judge backfill: get parent job failed; skip segment",
				"segment_id", seg.ID,
				"job_id", seg.JobID,
				"error", err,
			)
			continue
		}
		// PrevContext nil: back-fill simplification (see plan §3, debt-3b).
		// Loses the "prev sentence coherence" signal vs steady-state, but
		// keeps the back-fill cheap (no extra DB reads per segment) and
		// observability-comparable. OPT-002-followup-3 may add it back.
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			slog.Warn("judge backfill: context cancelled mid-dispatch",
				"dispatched_so_far", dispatched,
				"remaining", len(segments)-i,
			)
			return ctx.Err()
		}
		go func() {
			defer func() { <-sem }()
			s.maybeJudgeSegmentAsync(job, seg, nil)
		}()
		dispatchedMu.Lock()
		dispatched++
		dispatchedMu.Unlock()
	}

	// Wait for all in-flight dispatch slots to drain so callers (eg. tests)
	// can observe completion. The judge goroutines themselves keep running
	// against their own detached contexts inside maybeJudgeSegmentAsync.
	for i := 0; i < concurrency; i++ {
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			slog.Warn("judge backfill: context cancelled awaiting dispatch drain",
				"drained", i,
				"target", concurrency,
			)
			return ctx.Err()
		}
	}

	slog.Info("judge backfill: dispatch complete",
		"dispatched", dispatched,
	)
	return nil
}
