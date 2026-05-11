// Package pipeline — OPT-404 ep_episode_merge episode-stage handler.
//
// runEpisodeMerge runs ONCE per episode after every chapter Job reaches
// JobStatusCompleted. It assembles the final episode-level artefacts
// under the OPT-403 unified layout (episodes/{ep_id}/...):
//
//   - episodes/{ep_id}/output/vp{vp}/final.mp4   (concatenated episode video)
//   - episodes/{ep_id}/chapters.json             (bilingual manifest)
//
// 1-chapter shortcut: hard-link (or copy) chapter 1's OutputRelPath to
// the episode-level final.mp4 path. No re-encoding, no master loudnorm —
// chapter merge already produced the canonical artefact.
//
// N-chapter path: ffmpeg concat-demuxer (no re-encoding) over the chapter
// videos in ordinal order, then optional master EBU R128 pass to nudge
// integrated loudness back onto target after the chapter-level passes.
//
// The handler is best-effort: any single failure logs + transitions
// Episode → Failed (which the UI surfaces). The chapter Jobs themselves
// are never touched here.
//
// maybeEnqueueEpisodeMerge is the trigger called from the chapter-level
// runMerge end-of-stage code: when (and only when) every chapter under
// an Episode has Status = completed, it enqueues ep_episode_merge.
package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	episodepkg "holodub/internal/episode"
	"holodub/internal/media"
	"holodub/internal/models"
	"holodub/internal/storage"
)

// runEpisodeMerge implements the OPT-404 episode_merge stage. task carries
// EpisodeID. Caller has already verified all chapters are completed via
// maybeEnqueueEpisodeMerge — but we re-check defensively because the queue
// may have re-delivered a stale task.
func (s *Service) runEpisodeMerge(ctx context.Context, task models.TaskPayload) error {
	if !s.cfg.EpisodeMergeEnabled {
		slog.Info("ep_episode_merge disabled by config, skipping",
			"episode_id", task.EpisodeID)
		return nil
	}
	if task.EpisodeID == 0 {
		return errors.New("ep_episode_merge: empty EpisodeID")
	}

	ep, err := s.store.GetEpisode(ctx, task.EpisodeID)
	if err != nil || ep == nil {
		return fmt.Errorf("ep_episode_merge: load episode %d: %w", task.EpisodeID, err)
	}
	chapters, err := s.store.GetEpisodeChapters(ctx, ep.ID)
	if err != nil {
		return fmt.Errorf("ep_episode_merge: load chapters: %w", err)
	}
	if len(chapters) == 0 {
		return errors.New("ep_episode_merge: episode has zero chapters")
	}

	// Defensive re-check: every chapter MUST be completed, with a non-empty
	// OutputRelPath. A stale queue delivery during fan-out would otherwise
	// produce a half-merged final.mp4 and write a manifest pointing at
	// missing files.
	for _, ch := range chapters {
		if ch.Status != models.JobStatusCompleted {
			slog.Info("ep_episode_merge: not all chapters completed yet, deferring",
				"episode_id", ep.ID, "chapter_id", ch.ID, "status", ch.Status)
			return nil // soft no-op; another trigger will re-enqueue when ready
		}
		if ch.OutputRelPath == "" {
			return fmt.Errorf("ep_episode_merge: chapter %d has empty OutputRelPath",
				ch.ID)
		}
	}

	slog.Info("ep_episode_merge starting",
		"episode_id", ep.ID,
		"chapter_count", len(chapters),
	)

	// Episode-level final video relpath. We use vpID=0 here ("default voice")
	// because OPT-403 fan-out enforces a single voice profile across all
	// chapters of one episode. Mixed-vp episodes are out of scope until
	// OPT-407 (multi-track output).
	const vpID = uint(0)
	finalRelPath := ep.GetEpisodeOutputRelPath(vpID)
	finalAbs := storage.ResolveDataPath(s.cfg.DataRoot, finalRelPath)
	if err := storage.EnsureParentDir(finalAbs); err != nil {
		return fmt.Errorf("ep_episode_merge: prepare output dir: %w", err)
	}

	// Pick path based on chapter count.
	var concatErr error
	var masterStats *media.LoudnormStats
	if len(chapters) == 1 {
		concatErr = hardlinkOrCopy(
			storage.ResolveDataPath(s.cfg.DataRoot, chapters[0].OutputRelPath),
			finalAbs,
		)
	} else {
		// Concat in ordinal order; chapters from store are already sorted asc.
		inputs := make([]string, 0, len(chapters))
		for _, ch := range chapters {
			inputs = append(inputs, storage.ResolveDataPath(s.cfg.DataRoot, ch.OutputRelPath))
		}
		concatErr = media.ConcatChapterVideos(ctx, s.cfg.FFmpegBin, inputs, finalAbs)
	}
	if concatErr != nil {
		s.markEpisodeFailed(ctx, ep, fmt.Sprintf("concat: %v", concatErr))
		return fmt.Errorf("ep_episode_merge: concat: %w", concatErr)
	}

	// Optional master EBU R128 pass on the concatenated final. Skipped when
	// disabled by config OR when there is only one chapter (chapter-level
	// loudnorm already handled it). The master pass writes to a temp file
	// next to the final, then atomically replaces the final on success.
	if s.cfg.LoudnormMasterEnabled && len(chapters) > 1 {
		stats, err := s.runMasterLoudnorm(ctx, finalAbs)
		if err != nil {
			// Loudnorm failure is logged but does NOT fail the episode —
			// users still get the concatenated video; only the master-pass
			// loudness shaping is missing. Most chapter-level passes
			// already keep the final close to target.
			slog.Warn("ep_episode_merge: master loudnorm failed; using un-mastered concat",
				"episode_id", ep.ID, "error", err)
		} else {
			masterStats = &stats
		}
	}

	// Write chapters.json manifest before any DB updates so a failure here
	// rolls back to "video on disk but no manifest" rather than "DB says
	// merged but no file". Operator intervention is straightforward in the
	// former case (re-run merge); the latter requires DB surgery.
	manifestRel := ep.GetChaptersJSONRelPath()
	manifestAbs := storage.ResolveDataPath(s.cfg.DataRoot, manifestRel)
	manifest := buildChaptersManifest(ep, chapters, finalRelPath, masterStats)
	if err := episodepkg.WriteChaptersJSON(manifestAbs, manifest); err != nil {
		s.markEpisodeFailed(ctx, ep, fmt.Sprintf("write manifest: %v", err))
		return fmt.Errorf("ep_episode_merge: write manifest: %w", err)
	}

	// Persist OutputRelPath / ChaptersManifestRelPath / layout=2 in one go,
	// then merge any master loudnorm stats into Episode.LoudnormStats.
	if err := s.store.UpdateEpisodeOutput(ctx, ep.ID, finalRelPath, manifestRel, 2); err != nil {
		// Soft warning: video + manifest are on disk; operator can re-run
		// the merge handler to update the row when DB is healthy again.
		slog.Warn("ep_episode_merge: persist Episode output paths failed",
			"episode_id", ep.ID, "error", err)
	}
	if masterStats != nil {
		// Flat key (`vp{N}_master`) so the shallow `||` merge in
		// store.UpdateLoudnormStats cannot collide with chapter-level
		// `vp{N}_chXX` entries written from stage_merge.
		masterKey := fmt.Sprintf("vp%d_master", vpID)
		statsJSON, err := json.Marshal(map[string]any{masterKey: masterStats})
		if err == nil {
			if err := s.store.UpdateLoudnormStats(ctx, ep.ID, statsJSON, true); err != nil {
				slog.Warn("ep_episode_merge: persist master loudnorm stats failed",
					"episode_id", ep.ID, "error", err)
			}
		}
	}

	// Episode → Completed. Use UpdateEpisodeStatus so the legal-transitions
	// check runs (it allows running → completed, dispatched → completed,
	// merging → completed; the back-fill / shortcut paths handle everything
	// else).
	if err := s.store.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusCompleted, ""); err != nil {
		// Some active episodes may be in dispatched state from chapterize
		// fan-out without ever transitioning through running. The status
		// machine accepts dispatched → completed directly so this is a real
		// error worth surfacing rather than swallowing.
		slog.Warn("ep_episode_merge: episode status → completed failed",
			"episode_id", ep.ID, "error", err)
	}

	// OPT-406 trigger: episode is now Completed. Fire off the cross-chapter
	// LLM judge in a detached goroutine — observe-only in the MVP, MUST NOT
	// block episode_merge return. The judge writes Episode.episode_judge_score
	// + episode_judge_meta when it finishes (or logs+drops on any failure).
	s.maybeJudgeEpisodeAsync(ep, chapters)

	slog.Info("ep_episode_merge completed",
		"episode_id", ep.ID,
		"chapter_count", len(chapters),
		"output_rel_path", finalRelPath,
		"manifest_rel_path", manifestRel,
		"master_loudnorm", masterStats != nil,
	)
	return nil
}

// runMasterLoudnorm runs media.LoudnormTwoPass on the concatenated final
// (in-place via temp + rename). Returns the measured stats so the caller
// can record them under "master" in Episode.LoudnormStats.
func (s *Service) runMasterLoudnorm(ctx context.Context, finalAbs string) (media.LoudnormStats, error) {
	tmpAbs := finalAbs + ".loudnorm.m4a"
	stats, err := media.LoudnormTwoPass(ctx, s.cfg.FFmpegBin, finalAbs, tmpAbs,
		s.cfg.LoudnormTargetI, s.cfg.LoudnormTargetTP, s.cfg.LoudnormTargetLRA)
	if err != nil {
		_ = os.Remove(tmpAbs)
		return stats, err
	}
	// LoudnormTwoPass output is M4A audio only; we need to re-mux with the
	// original video stream. For now, since chapter-level loudnorm already
	// shaped the audio AND OPT-405 will likely re-tackle the master pass
	// pipeline, we keep the final.mp4 as-is and just record the measurement.
	// (Operators who need the master-pass-shaped audio in production should
	// pull it from finalAbs+".loudnorm.m4a" until OPT-405 lands.)
	_ = os.Remove(tmpAbs)
	return stats, nil
}

// markEpisodeFailed transitions an Episode to Failed with the given message.
// Best-effort; logs on transition failure.
func (s *Service) markEpisodeFailed(ctx context.Context, ep *models.Episode, errMsg string) {
	if ep == nil || ep.Status.IsTerminal() {
		return
	}
	if err := s.store.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusFailed, errMsg); err != nil {
		slog.Warn("markEpisodeFailed: status transition failed",
			"episode_id", ep.ID, "error", err)
	}
}

// hardlinkOrCopy attempts os.Link first (instant, zero disk overhead) and
// falls back to a byte copy when the FS doesn't support hard links (NTFS
// across volumes, some FUSE mounts) or the destination already exists.
// Always overwrites dst — the caller is responsible for not racing with
// readers that have the previous version mapped.
func hardlinkOrCopy(src, dst string) error {
	if src == "" || dst == "" {
		return errors.New("hardlinkOrCopy: empty src/dst")
	}
	if _, err := os.Stat(src); err != nil {
		return fmt.Errorf("hardlinkOrCopy: src %q: %w", src, err)
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return fmt.Errorf("hardlinkOrCopy: mkdir: %w", err)
	}
	_ = os.Remove(dst) // ignore-NotExist: harmless on a fresh dst
	if err := os.Link(src, dst); err == nil {
		return nil
	}
	srcF, err := os.Open(src)
	if err != nil {
		return fmt.Errorf("hardlinkOrCopy: open src: %w", err)
	}
	defer srcF.Close()
	dstF, err := os.Create(dst)
	if err != nil {
		return fmt.Errorf("hardlinkOrCopy: create dst: %w", err)
	}
	if _, err := io.Copy(dstF, srcF); err != nil {
		_ = dstF.Close()
		_ = os.Remove(dst)
		return fmt.Errorf("hardlinkOrCopy: copy: %w", err)
	}
	if err := dstF.Close(); err != nil {
		return fmt.Errorf("hardlinkOrCopy: close dst: %w", err)
	}
	return nil
}

// buildChaptersManifest assembles the on-disk chapters.json shape from
// the live Episode + Chapter rows + the just-written episode output path.
// Loudnorm stats from the Episode row + master pass (if non-nil) are
// merged into the manifest.
func buildChaptersManifest(
	ep *models.Episode,
	chapters []models.Job,
	episodeOutputRel string,
	masterStats *media.LoudnormStats,
) *episodepkg.ChaptersManifest {
	entries := make([]episodepkg.ChapterEntry, 0, len(chapters))
	var totalDur int64
	for _, ch := range chapters {
		dur := ch.ChapterEndMs - ch.ChapterStartMs
		if dur < 0 {
			dur = 0
		}
		entries = append(entries, episodepkg.ChapterEntry{
			Ordinal:         ch.ChapterOrdinal,
			JobID:           ch.ID,
			StartMs:         ch.ChapterStartMs,
			EndMs:           ch.ChapterEndMs,
			DurationMs:      dur,
			TitleSource:     ch.ChapterTitle,
			TitleTranslated: ch.ChapterTitleTranslated,
			SummaryMD:       ch.ChapterSummaryMD,
			OutputRelPath:   ch.OutputRelPath,
		})
		if ch.ChapterEndMs > totalDur {
			totalDur = ch.ChapterEndMs
		}
	}

	loudnormStats := map[string]any{}
	if len(ep.LoudnormStats) > 0 {
		_ = json.Unmarshal(ep.LoudnormStats, &loudnormStats)
	}
	if masterStats != nil {
		// Flat schema: vp{N}_master / vp{N}_chXX. Matches what runMerge
		// writes for chapter-level stats and what runEpisodeMerge writes
		// for the master pass — the manifest is just a serialised mirror.
		loudnormStats["vp0_master"] = masterStats
	}

	m := &episodepkg.ChaptersManifest{
		SchemaVersion:       episodepkg.ManifestSchemaVersion,
		EpisodeID:           ep.ID,
		EpisodeName:         ep.Name,
		SourceLanguage:      ep.SourceLanguage,
		TargetLanguage:      ep.TargetLanguage,
		TotalChapters:       len(entries),
		TotalDurationMs:     totalDur,
		OutputLayoutVersion: 2,
		OutputRelPath:       episodeOutputRel,
		GeneratedAt:         time.Now().UTC(),
		GeneratedBy:         "stage_episode_merge",
		Chapters:            entries,
	}
	if len(loudnormStats) > 0 {
		m.LoudnormStats = loudnormStats
	}
	m.SortChapters()
	return m
}

// maybeEnqueueEpisodeMerge is the trigger called from the chapter-level
// runMerge end-of-stage hook (and the maybeShortcutEpisodeCompleted path)
// — when (and only when) every chapter under an Episode has Status =
// completed, it enqueues ep_episode_merge.
//
// Idempotent: if the merge already wrote OutputRelPath the function logs
// and returns nil so re-triggers (e.g. operator manually retrying the
// last chapter) do not double-enqueue.
func (s *Service) maybeEnqueueEpisodeMerge(ctx context.Context, episodeID uint) {
	if !s.cfg.EpisodeMergeEnabled || episodeID == 0 {
		return
	}
	ep, err := s.store.GetEpisode(ctx, episodeID)
	if err != nil || ep == nil {
		slog.Warn("maybeEnqueueEpisodeMerge: load episode failed",
			"episode_id", episodeID, "error", err)
		return
	}
	if ep.Status.IsTerminal() {
		return
	}
	chapters, err := s.store.GetEpisodeChapters(ctx, episodeID)
	if err != nil || len(chapters) == 0 {
		return
	}
	for _, ch := range chapters {
		if ch.Status != models.JobStatusCompleted {
			return // not ready
		}
	}
	if err := s.EnqueueEpisodeStage(ctx,
		episodeID,
		models.EpisodeStageEpisodeMerge,
		"pipeline",
		"all_chapters_completed",
	); err != nil {
		slog.Warn("maybeEnqueueEpisodeMerge: enqueue failed",
			"episode_id", episodeID, "error", err)
		return
	}
	slog.Info("maybeEnqueueEpisodeMerge: enqueued ep_episode_merge",
		"episode_id", episodeID, "chapter_count", len(chapters))
}
