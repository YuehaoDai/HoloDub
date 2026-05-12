// Package pipeline — OPT-407-followup-6 TTS-stuck segment recovery.
//
// BackfillStuckTTSSegments scans for segments whose status remains
// "translated" long after the worker should have synthesised them. The
// canonical cause is a transient TTS failure (ml-service timeout, GPU
// OOM, network blip) that surfaced as a stage error: the surrounding
// stage was retried, but the per-segment write happened to land in a
// "stuck" state because runTTSDuration short-circuits on the first
// processOneTTSSegment error and never revisits the segment.
//
// The OPT-407 closed-loop rework engine cannot help here — its hook
// fires from maybeJudgeSegmentAsync after a successful synthesis, so
// segments stuck at "translated" never invoke the engine. This back-fill
// closes the gap by re-enqueueing the affected segments through
// (*Service).RetryJob (the same path OPT-407 segment_retry uses), so
// the recovery path is observable in the existing rework_attempts /
// task_queue metrics.
//
// Concurrency: a tiny in-memory grouping by job_id is performed before
// dispatch so a single chapter's stuck segments are batched into one
// RetryJob call (instead of N enqueues). This avoids re-running the
// full per-job tts_duration setup once per stuck segment.
package pipeline

import (
	"context"
	"log/slog"

	"holodub/internal/models"
)

// BackfillStuckTTSSegments is intended to be called from cmd/worker/main.go
// shortly after worker boot, AFTER BackfillSegmentJudges (which can take
// 10-30s on a populated DB). The 30s-after-boot delay should also let any
// in-flight tts_duration stage progress past the 2-minute updated_at cutoff
// in ListSegmentsStuckInTranslated, so the same query is not racing with
// active synthesis.
//
// Returns nil even when individual jobs fail to enqueue (the next worker
// startup will pick them up again). Returns a non-nil error only when the
// initial DB scan fails.
func (s *Service) BackfillStuckTTSSegments(ctx context.Context, limit int) error {
	if limit <= 0 {
		return nil
	}

	stuck, err := s.store.ListSegmentsStuckInTranslated(ctx, limit)
	if err != nil {
		return err
	}
	if len(stuck) == 0 {
		slog.Info("tts-stuck backfill: nothing to do", "limit", limit)
		return nil
	}

	// Group by parent job so we issue ONE RetryJob per chapter even when
	// many segments are stuck. RetryJob accepts a segment_ids slice and
	// processes them with the normal per-job tts_duration setup.
	//
	// Per-job tts_completed lookup is cached so consecutive stuck segments
	// from the same chapter share the answer (typical case: one chapter
	// has 3-5 stuck segments, scanning each individually would be wasteful).
	byJob := make(map[uint][]uint, 8)
	skipReasons := make(map[uint]string, 4)
	jobTTSCompleted := make(map[uint]bool, 8)
	for i := range stuck {
		seg := stuck[i]
		// We do NOT consult Job.CurrentStage here because OPT-407
		// segment_retry rewinds CurrentStage back to "translate" when
		// dispatching a retry round, even after tts_duration has
		// successfully completed once. Use the immutable stage_runs
		// history instead — once tts_duration completes, it stays
		// "completed" forever in that table.
		eligible, cached := jobTTSCompleted[seg.JobID]
		if !cached {
			done, herr := s.store.HasJobStageCompleted(ctx, seg.JobID, models.StageTTSDuration)
			if herr != nil {
				slog.Warn("tts-stuck backfill: stage history lookup failed; skip segment",
					"segment_id", seg.ID, "job_id", seg.JobID, "error", herr)
				continue
			}
			eligible = done
			jobTTSCompleted[seg.JobID] = eligible
		}
		if !eligible {
			skipReasons[seg.JobID] = "tts_duration_never_completed_for_this_job"
			continue
		}
		byJob[seg.JobID] = append(byJob[seg.JobID], seg.ID)
	}

	if len(byJob) == 0 {
		slog.Info("tts-stuck backfill: all stuck segments belong to active / pre-tts jobs; skipping",
			"scanned", len(stuck),
			"skipped_groups", len(skipReasons),
		)
		return nil
	}

	slog.Info("tts-stuck backfill: dispatching",
		"jobs", len(byJob),
		"total_segments", countTotalSegments(byJob),
		"scanned", len(stuck),
	)

	dispatched := 0
	for jobID, segIDs := range byJob {
		if rerr := s.RetryJob(ctx, jobID, models.StageTTSDuration, segIDs, "tts_stuck_recovery"); rerr != nil {
			slog.Warn("tts-stuck backfill: RetryJob failed",
				"job_id", jobID, "segments", segIDs, "error", rerr)
			continue
		}
		dispatched++
		slog.Info("tts-stuck backfill: re-enqueued chapter",
			"job_id", jobID, "segments_recovered", len(segIDs))
	}

	slog.Info("tts-stuck backfill: dispatch complete",
		"dispatched_jobs", dispatched, "total_jobs", len(byJob),
	)
	return nil
}

func countTotalSegments(byJob map[uint][]uint) int {
	n := 0
	for _, ids := range byJob {
		n += len(ids)
	}
	return n
}
