// Package pipeline — OPT-403 + OPT-405 chapterize episode-stage handler.
//
// The chapterize stage runs ONCE per episode immediately after
// ep_glossary_extract finishes. It decides whether the episode should
// stay as a single chapter (short-circuit path) or be fanned out into
// 2..N sibling chapter Jobs (long-form path).
//
// Cut decision (in priority order):
//
//  1. OPT-405 LLM-driven: if Episode.LLMChapters is populated AND the
//     plan validates against the segment list, snap each end_segment_idx
//     boundary to the nearest qualifying silence, enforce hard min/max
//     guardrails, and use the resulting ranges. Bilingual titles +
//     summaries come straight from the LLM (no extra OPT-403 Pass 3
//     call needed).
//
//  2. OPT-403 DP fallback: ExtractCandidates + DPOptimalCuts produce
//     deterministic cuts; the optional OPT-403 Pass 3 LLM review then
//     mints titles. Used when LLM-driven is disabled, when the LLM
//     call failed at glossary_extract, OR when the LLM-emitted plan
//     fails validation.
//
// Once cuts are decided the long-form path performs fan-out atomically:
//
//  3. ffmpeg-slice the source video / vocals / BGM into per-chapter files
//     at episodes/{ep_id}/chapters/source/ch{ord:02d}.{mp4,vocals.wav,bgm.wav}.
//  4. Reset chapter 1 to its new [0, cuts[0]) window + create chapter 2..N
//     siblings with the matching slice paths.
//  5. Reassign every segment to the chapter whose time window contains it,
//     subtracting the chapter's StartMs so per-chapter ordinals start at 0.
//  6. Bump Episode.TotalChapters + transition status to dispatched.
//  7. Enqueue StageSegmentReview on every chapter so the worker resumes the
//     pipeline in parallel — chapters skip media/separate/asr_smart because
//     those already ran at the episode level.
//
// Failure mode: best-effort. Any failure mid-fan-out aborts and leaves
// the episode at JobStatusAwaitingChapterize on chapter 1, which the
// operator can manually retry. The stage NEVER crashes the worker.
package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"

	"holodub/internal/chapterize"
	"holodub/internal/llm"
	"holodub/internal/media"
	"holodub/internal/models"
	"holodub/internal/storage"
	"holodub/internal/store"
)

// runEpisodeChapterize executes the OPT-403 ep_chapterize episode-stage.
// task carries EpisodeID; the handler resolves chapter 1 internally.
func (s *Service) runEpisodeChapterize(ctx context.Context, task models.TaskPayload) error {
	if !s.cfg.ChapterizeEnabled {
		slog.Info("ep_chapterize disabled by config, skipping",
			"episode_id", task.EpisodeID)
		return nil
	}
	if task.EpisodeID == 0 {
		return errors.New("ep_chapterize: empty EpisodeID")
	}

	ep, err := s.store.GetEpisode(ctx, task.EpisodeID)
	if err != nil || ep == nil {
		return fmt.Errorf("ep_chapterize: load episode %d: %w", task.EpisodeID, err)
	}
	chapters, err := s.store.GetEpisodeChapters(ctx, ep.ID)
	if err != nil {
		return fmt.Errorf("ep_chapterize: load chapters: %w", err)
	}
	if len(chapters) == 0 {
		return errors.New("ep_chapterize: episode has zero chapters; nothing to chapterize")
	}
	if len(chapters) > 1 {
		// Episode already has 2+ chapters (likely re-run after a previous
		// chapterize). Idempotent no-op; chapter pipelines proceed independently.
		slog.Info("ep_chapterize: episode already fanned out, skipping",
			"episode_id", ep.ID, "chapter_count", len(chapters))
		return nil
	}
	chapter1 := chapters[0]

	segs, err := s.store.ListSegmentsByEpisode(ctx, ep.ID)
	if err != nil {
		return fmt.Errorf("ep_chapterize: list segments: %w", err)
	}
	if len(segs) == 0 {
		// ASR didn't produce segments yet — extremely rare race condition
		// where chapterize was enqueued before glossary completed. Punt.
		return errors.New("ep_chapterize: no segments yet; pipeline races detected")
	}

	episodeDurMs := segs[len(segs)-1].EndMs
	slog.Info("ep_chapterize starting",
		"episode_id", ep.ID,
		"chapter1_id", chapter1.ID,
		"segment_count", len(segs),
		"episode_duration_ms", episodeDurMs,
		"max_chapter_ms", s.cfg.ChapterizeMaxChapterMs,
	)

	chapterSegs := make([]chapterize.Segment, len(segs))
	for i, seg := range segs {
		chapterSegs[i] = chapterize.Segment{StartMs: seg.StartMs, EndMs: seg.EndMs}
	}

	// OPT-405 priority path: try the LLM-emitted plan first. Falls back
	// to the DP path on any breach (validation, missing column, parse
	// error). The fallback is intentionally indistinguishable from
	// "OPT-405 turned off" — operators get the safety net for free.
	if ranges, titles, source, ok := s.tryLLMChapterPlan(ep, chapterSegs); ok {
		// Short-circuit even on the LLM path when it returns a single
		// chapter (e.g. the model decided the episode is one theme).
		if len(ranges) == 1 {
			slog.Info("ep_chapterize: LLM returned single chapter; short-circuit",
				"episode_id", ep.ID, "source", source)
			return s.resumeChapter1IfWaiting(ctx, &chapter1, "llm_single_chapter")
		}
		slog.Info("ep_chapterize: cuts decided by LLM",
			"episode_id", ep.ID,
			"chapter_count", len(ranges),
			"mean_chapter_ms", chapterize.MeanChapterDuration(ranges),
			"source", source,
		)
		// LLM titles are already bilingual; no extra Pass 3 call.
		return s.runFanOutChapters(ctx, ep, &chapter1, ranges, titles, "", segs)
	}

	// Short-circuit: episode fits in a single chapter (DP fallback rule).
	if episodeDurMs <= s.cfg.ChapterizeMaxChapterMs {
		slog.Info("ep_chapterize short-circuit: episode fits one chapter (DP fallback)",
			"episode_id", ep.ID, "episode_duration_ms", episodeDurMs)
		return s.resumeChapter1IfWaiting(ctx, &chapter1, "short_circuit")
	}

	// OPT-403 DP fallback: deterministic cut decision.
	cands := chapterize.ExtractCandidates(chapterSegs, s.cfg.ChapterizeMinSilenceGapMs)
	cuts := chapterize.DPOptimalCuts(cands, episodeDurMs,
		s.cfg.ChapterizeTargetChapterMs,
		s.cfg.ChapterizeMinChapterMs,
		s.cfg.ChapterizeMaxChapterMs)
	if len(cuts) == 0 {
		// DP found no valid subdivision (e.g. silences too sparse). Fall
		// back to single chapter — the user gets one long video that's
		// no worse than current production behaviour.
		slog.Warn("ep_chapterize: DP found no valid subdivision; falling back to single chapter",
			"episode_id", ep.ID,
			"episode_duration_ms", episodeDurMs,
			"candidate_count", len(cands))
		return s.resumeChapter1IfWaiting(ctx, &chapter1, "no_valid_cuts")
	}

	ranges := chapterize.BuildChapterRanges(chapterSegs, cuts, episodeDurMs)
	slog.Info("ep_chapterize: cuts decided by DP",
		"episode_id", ep.ID,
		"chapter_count", len(ranges),
		"mean_chapter_ms", chapterize.MeanChapterDuration(ranges),
		"max_abs_dev_ms", chapterize.MaxAbsDeviation(ranges, s.cfg.ChapterizeTargetChapterMs),
	)

	// Pass 3 (optional): LLM nudge + bilingual titles for the DP path.
	titles, episodeTitle := s.maybeReviewChapterCuts(ctx, ep, &chapter1, ranges, segs)

	return s.runFanOutChapters(ctx, ep, &chapter1, ranges, titles, episodeTitle, segs)
}

// tryLLMChapterPlan reads Episode.LLMChapters, validates and snaps it,
// applies the hard min/max guardrails, and returns ChapterRange[] +
// llm.ChapterReviewVerdict[] (titles) ready for runFanOutChapters.
//
// Returns ok=false on ANY of: column empty/NULL, JSON parse error, plan
// validation failure (gaps/overlap/out-of-range indices), zero chapters
// after snap+enforce. The caller falls back to the DP path.
//
// `source` is a short label (llm_kept / llm_merged / llm_split) the
// caller logs so the dashboard can attribute outcomes; the values
// reflect whether EnforceHardConstraints had to intervene.
func (s *Service) tryLLMChapterPlan(
	ep *models.Episode,
	chapterSegs []chapterize.Segment,
) ([]chapterize.ChapterRange, []llm.ChapterReviewVerdict, string, bool) {
	if !s.cfg.ChapterizeEnabled || !s.cfg.ChapterizeLLMDriven {
		return nil, nil, "", false
	}
	raw := ep.LLMChapters
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil, "", false
	}
	var rawPlan []llm.ChapterCut
	if err := json.Unmarshal(raw, &rawPlan); err != nil {
		slog.Warn("ep_chapterize: failed to parse Episode.LLMChapters; falling back to DP",
			"episode_id", ep.ID, "error", err)
		return nil, nil, "", false
	}
	if len(rawPlan) == 0 {
		return nil, nil, "", false
	}

	// Convert llm.ChapterCut → chapterize.LLMChapter. Sort defensively
	// in case the provider reordered the array under load.
	plan := make([]chapterize.LLMChapter, len(rawPlan))
	for i, c := range rawPlan {
		plan[i] = chapterize.LLMChapter{
			StartSegmentIdx: c.StartSegmentIdx,
			EndSegmentIdx:   c.EndSegmentIdx,
			TitleSource:     c.TitleSource,
			TitleTranslated: c.TitleTranslated,
			SummaryMD:       c.SummaryMD,
		}
	}
	plan = chapterize.SortLLMPlan(plan)

	if err := chapterize.ValidateLLMPlan(plan, len(chapterSegs)); err != nil {
		slog.Warn("ep_chapterize: LLM plan failed validation; falling back to DP",
			"episode_id", ep.ID, "error", err, "plan_size", len(plan))
		return nil, nil, "", false
	}

	// Snap each end-segment boundary to the nearest qualifying silence.
	// lookahead=3 keeps the snap close to the LLM's intent (≤3 segments
	// either side); minSilenceGapMs uses the same threshold as the DP
	// candidate extraction so we never accept a cut the DP would reject.
	totalDurationMs := chapterSegs[len(chapterSegs)-1].EndMs
	trailingCuts := chapterize.SnapBoundariesToSilences(
		chapterSegs, plan, totalDurationMs, s.cfg.ChapterizeMinSilenceGapMs, 3)

	ranges := chapterize.BuildLLMChapterRanges(plan, trailingCuts)
	meta := make([]chapterize.LLMChapterMeta, len(plan))
	for i, p := range plan {
		meta[i] = chapterize.LLMChapterMeta{
			TitleSource:     p.TitleSource,
			TitleTranslated: p.TitleTranslated,
			SummaryMD:       p.SummaryMD,
		}
	}

	// Enforce hard min/max. maxSplitDepth=4 is enough to bring a 6×hardMax
	// chapter under cap (4 splits = 16 halves); deeper than that almost
	// certainly indicates a model that ignored its instructions and is
	// safer left as-is than over-fragmented.
	finalRanges, finalMeta := chapterize.EnforceHardConstraints(
		ranges, meta, chapterSegs,
		s.cfg.ChapterizeHardMinMs, s.cfg.ChapterizeHardMaxMs,
		s.cfg.ChapterizeMinSilenceGapMs, 4)
	if len(finalRanges) == 0 {
		return nil, nil, "", false
	}

	// Convert chapterize.LLMChapterMeta → llm.ChapterReviewVerdict so the
	// downstream runFanOutChapters code path is unchanged.
	titles := make([]llm.ChapterReviewVerdict, len(finalMeta))
	for i, m := range finalMeta {
		titles[i] = llm.ChapterReviewVerdict{
			Ordinal:         i + 1,
			Action:          "keep",
			TitleSource:     m.TitleSource,
			TitleTranslated: m.TitleTranslated,
			SummaryMD:       m.SummaryMD,
		}
	}

	source := "llm_kept"
	if len(finalRanges) != len(plan) {
		if len(finalRanges) < len(plan) {
			source = "llm_merged"
		} else {
			source = "llm_split"
		}
	}
	return finalRanges, titles, source, true
}

// resumeChapter1IfWaiting wakes chapter 1 from JobStatusAwaitingChapterize
// (set by runASRSmart for long videos) and re-enqueues it at StageSegmentReview.
// On the short-circuit / no-cuts path, chapter 1 may already be past
// JobStatusAwaitingChapterize (e.g. ASR judged the video short enough to skip
// the gating); in that case this is a harmless no-op.
func (s *Service) resumeChapter1IfWaiting(ctx context.Context, chapter1 *models.Job, reason string) error {
	if chapter1.Status != models.JobStatusAwaitingChapterize {
		slog.Info("ep_chapterize: chapter 1 not waiting; nothing to resume",
			"chapter_id", chapter1.ID, "status", chapter1.Status, "reason", reason)
		return nil
	}
	return s.EnqueueStage(ctx, models.TaskPayload{
		JobID:       chapter1.ID,
		Stage:       models.StageSegmentReview,
		Attempt:     0,
		RequestedBy: "pipeline",
		Reason:      "chapterize_" + reason,
	})
}

// maybeReviewChapterCuts invokes the LLM Pass 3 review when enabled and folds
// the result back into the cut decisions. Returns one bilingual title per
// chapter (always len(ranges) entries — defaults to "Chapter N" / "第 N 章"
// if the LLM is disabled or fails) plus the optional episode-level title.
func (s *Service) maybeReviewChapterCuts(
	ctx context.Context,
	ep *models.Episode,
	chapter1 *models.Job,
	ranges []chapterize.ChapterRange,
	segs []models.Segment,
) ([]llm.ChapterReviewVerdict, string) {
	defaults := defaultChapterTitles(len(ranges), chapter1.SourceLanguage, chapter1.TargetLanguage)
	if !s.cfg.ChapterReviewLLMEnabled {
		return defaults, ""
	}
	inputs := buildChapterReviewInputs(ranges, segs)
	referenceCard := strings.TrimSpace(ep.ReferenceCard)

	result, err := s.llm.ReviewChapterCuts(ctx, inputs, referenceCard,
		chapter1.SourceLanguage, chapter1.TargetLanguage)
	if err != nil {
		slog.Warn("ep_chapterize: LLM Pass 3 failed; using default titles",
			"episode_id", ep.ID, "error", err)
		return defaults, ""
	}
	// At this point we trust the LLM-provided actions only enough to log
	// non-keep verdicts (we DO NOT shift cuts in this iteration — applying
	// boundary nudges would require re-slicing media and re-validating
	// duration constraints, which is OPT-403.1 territory). Titles ARE used.
	for _, v := range result.Verdicts {
		if v.Action != "keep" {
			slog.Info("ep_chapterize: LLM suggested boundary shift (deferred to OPT-403.1)",
				"episode_id", ep.ID, "ordinal", v.Ordinal, "action", v.Action,
				"rationale", v.Rationale)
		}
	}
	return result.Verdicts, strings.TrimSpace(result.EpisodeTitle)
}

// buildChapterReviewInputs converts ChapterRange + segments into the LLM-
// friendly ChapterCutInput shape (opening / closing snippets per chapter).
func buildChapterReviewInputs(
	ranges []chapterize.ChapterRange,
	segs []models.Segment,
) []llm.ChapterCutInput {
	const openCount, closeCount = 5, 3
	out := make([]llm.ChapterCutInput, 0, len(ranges))
	for _, r := range ranges {
		in := llm.ChapterCutInput{
			Ordinal:         r.Ordinal,
			StartMs:         r.StartMs,
			EndMs:           r.EndMs,
			StartSegmentIdx: r.StartSegmentIdx,
			EndSegmentIdx:   r.EndSegmentIdx,
			SilenceLeftMs:   r.StartCutSilenceMs,
			SilenceRightMs:  r.EndCutSilenceMs,
		}
		if r.StartSegmentIdx >= 0 && r.EndSegmentIdx >= 0 {
			start := r.StartSegmentIdx
			end := start + openCount - 1
			if end > r.EndSegmentIdx {
				end = r.EndSegmentIdx
			}
			for i := start; i <= end && i < len(segs); i++ {
				if t := strings.TrimSpace(segs[i].SourceText); t != "" {
					in.OpeningSegments = append(in.OpeningSegments, t)
				}
			}
			tail := r.EndSegmentIdx
			head := tail - closeCount + 1
			if head < r.StartSegmentIdx {
				head = r.StartSegmentIdx
			}
			if head <= start+openCount-1 {
				head = start + openCount // avoid duplicating openings as closings
			}
			for i := head; i <= tail && i < len(segs); i++ {
				if i < 0 {
					continue
				}
				if t := strings.TrimSpace(segs[i].SourceText); t != "" {
					in.ClosingSegments = append(in.ClosingSegments, t)
				}
			}
		}
		out = append(out, in)
	}
	return out
}

// defaultChapterTitles produces "Chapter N" / "第 N 章" placeholders used
// when the LLM Pass 3 is disabled or fails. Source-language string follows
// English convention; target-language uses the localised template.
func defaultChapterTitles(n int, srcLang, tgtLang string) []llm.ChapterReviewVerdict {
	out := make([]llm.ChapterReviewVerdict, n)
	for i := 0; i < n; i++ {
		ord := i + 1
		out[i] = llm.ChapterReviewVerdict{
			Ordinal:         ord,
			Action:          "keep",
			TitleSource:     fmt.Sprintf("Chapter %d", ord),
			TitleTranslated: localisedDefaultTitle(tgtLang, ord),
		}
	}
	return out
}

func localisedDefaultTitle(lang string, ordinal int) string {
	switch strings.ToLower(strings.TrimSpace(lang)) {
	case "zh", "zh-cn", "zh-tw", "zh-hant":
		return fmt.Sprintf("第 %d 章", ordinal)
	case "ja":
		return fmt.Sprintf("第 %d 章", ordinal)
	case "ko":
		return fmt.Sprintf("%d장", ordinal)
	default:
		return fmt.Sprintf("Chapter %d", ordinal)
	}
}

// runFanOutChapters performs the irreversible fan-out work in seven discrete
// steps so a failure on any one step has a clear blast radius.
func (s *Service) runFanOutChapters(
	ctx context.Context,
	ep *models.Episode,
	chapter1 *models.Job,
	ranges []chapterize.ChapterRange,
	titles []llm.ChapterReviewVerdict,
	episodeTitle string,
	segs []models.Segment,
) error {
	// Step 1: slice source video + master vocals/BGM into per-chapter files
	// under episodes/{ep_id}/chapters/source/. Failures abort fan-out before
	// any DB mutation so the operator can retry from awaiting_chapterize.
	sourceVideoRel := chapter1.InputRelPath
	sourceVocalsRel := chapter1.VocalsRelPath
	sourceBgmRel := chapter1.BgmRelPath
	type slicedPaths struct {
		Video  string
		Vocals string
		Bgm    string
	}
	sliced := make([]slicedPaths, len(ranges))
	for i, r := range ranges {
		sliced[i].Video = chapterSourcePath(ep, r.Ordinal, "video")
		if sourceVocalsRel != "" {
			sliced[i].Vocals = chapterSourcePath(ep, r.Ordinal, "vocals")
		}
		if sourceBgmRel != "" {
			sliced[i].Bgm = chapterSourcePath(ep, r.Ordinal, "bgm")
		}
		if err := media.SliceVideoAtRange(s.cfg.DataRoot, s.cfg.FFmpegBin,
			sourceVideoRel, sliced[i].Video, r.StartMs, r.EndMs); err != nil {
			return fmt.Errorf("ep_chapterize: slice video ch%02d: %w", r.Ordinal, err)
		}
		if sliced[i].Vocals != "" {
			if err := media.TrimAudioSegment(s.cfg.FFmpegBin,
				storage.ResolveDataPath(s.cfg.DataRoot, sourceVocalsRel),
				storage.ResolveDataPath(s.cfg.DataRoot, sliced[i].Vocals),
				r.StartMs, r.EndMs); err != nil {
				return fmt.Errorf("ep_chapterize: slice vocals ch%02d: %w", r.Ordinal, err)
			}
		}
		if sliced[i].Bgm != "" {
			if err := media.TrimAudioSegment(s.cfg.FFmpegBin,
				storage.ResolveDataPath(s.cfg.DataRoot, sourceBgmRel),
				storage.ResolveDataPath(s.cfg.DataRoot, sliced[i].Bgm),
				r.StartMs, r.EndMs); err != nil {
				return fmt.Errorf("ep_chapterize: slice bgm ch%02d: %w", r.Ordinal, err)
			}
		}
	}
	slog.Info("ep_chapterize: media sliced",
		"episode_id", ep.ID, "chapter_count", len(ranges))

	// Step 2: update chapter 1 with its new [0, cuts[0]) window + sliced media.
	first := ranges[0]
	if err := s.store.UpdateChapterRange(ctx, chapter1.ID,
		first.StartMs, first.EndMs,
		sliced[0].Video, sliced[0].Vocals, sliced[0].Bgm); err != nil {
		return fmt.Errorf("ep_chapterize: rewrite chapter 1 range: %w", err)
	}
	if t := titles[0]; t.TitleSource != "" || t.TitleTranslated != "" {
		if err := s.store.UpdateChapterMetadata(ctx, chapter1.ID,
			t.TitleSource, t.TitleTranslated, t.SummaryMD); err != nil {
			slog.Warn("ep_chapterize: write chapter 1 title failed; continuing",
				"chapter_id", chapter1.ID, "error", err)
		}
	}

	// Step 3: create chapter 2..N siblings.
	createdJobs := make([]*models.Job, 0, len(ranges)-1)
	for i := 1; i < len(ranges); i++ {
		r := ranges[i]
		in := store.ChapterFanOutInput{
			Ordinal:         r.Ordinal,
			StartMs:         r.StartMs,
			EndMs:           r.EndMs,
			Title:           titles[i].TitleSource,
			TitleTranslated: titles[i].TitleTranslated,
			SummaryMD:       titles[i].SummaryMD,
			InputRelPath:    sliced[i].Video,
			VocalsRelPath:   sliced[i].Vocals,
			BgmRelPath:      sliced[i].Bgm,
		}
		newJob, err := s.store.CreateChapterJob(ctx, ep.ID, chapter1, in)
		if err != nil {
			return fmt.Errorf("ep_chapterize: create chapter %d: %w", r.Ordinal, err)
		}
		createdJobs = append(createdJobs, newJob)
	}
	slog.Info("ep_chapterize: chapter siblings created",
		"episode_id", ep.ID, "new_chapters", len(createdJobs))

	// Step 4: build segment reassignments. Each segment goes to the chapter
	// whose [StartMs, EndMs) contains its midpoint, with start_ms/end_ms
	// shifted by -chapter.StartMs so per-chapter ordinals start at 0.
	chapterIDByOrdinal := map[int]uint{1: chapter1.ID}
	for _, j := range createdJobs {
		chapterIDByOrdinal[j.ChapterOrdinal] = j.ID
	}
	reassignments := make([]store.SegmentReassignment, 0, len(segs))
	for _, seg := range segs {
		mid := (seg.StartMs + seg.EndMs) / 2
		var assigned bool
		for _, r := range ranges {
			if mid >= r.StartMs && mid < r.EndMs {
				targetID := chapterIDByOrdinal[r.Ordinal]
				reassignments = append(reassignments, store.SegmentReassignment{
					SegmentID:   seg.ID,
					TargetJobID: targetID,
					NewStartMs:  seg.StartMs - r.StartMs,
					NewEndMs:    seg.EndMs - r.StartMs,
				})
				assigned = true
				break
			}
		}
		if !assigned {
			// Should be unreachable because BuildChapterRanges covers
			// [0, totalDurationMs); log so a future regression surfaces.
			slog.Warn("ep_chapterize: segment fell outside any chapter; assigning to last",
				"segment_id", seg.ID, "start_ms", seg.StartMs, "end_ms", seg.EndMs)
			last := ranges[len(ranges)-1]
			reassignments = append(reassignments, store.SegmentReassignment{
				SegmentID:   seg.ID,
				TargetJobID: chapterIDByOrdinal[last.Ordinal],
				NewStartMs:  seg.StartMs - last.StartMs,
				NewEndMs:    seg.EndMs - last.StartMs,
			})
		}
	}
	if err := s.store.ReassignSegmentsToChaptersAndShift(ctx, reassignments); err != nil {
		return fmt.Errorf("ep_chapterize: reassign segments: %w", err)
	}

	// Step 5: bump Episode.TotalChapters + record optional episode title.
	if err := s.store.UpdateEpisodeChapters(ctx, ep.ID, len(ranges)); err != nil {
		return fmt.Errorf("ep_chapterize: bump TotalChapters: %w", err)
	}
	if episodeTitle != "" {
		if err := s.store.UpdateEpisodeName(ctx, ep.ID, episodeTitle); err != nil {
			slog.Warn("ep_chapterize: update episode name failed; continuing",
				"episode_id", ep.ID, "error", err)
		}
	}

	// Step 6: transition Episode to Dispatched.
	if err := s.store.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusDispatched, ""); err != nil {
		// Episode may already be in Running; treat as soft warning.
		slog.Warn("ep_chapterize: episode status transition failed; continuing",
			"episode_id", ep.ID, "error", err)
	}

	// Step 7: enqueue StageSegmentReview on every chapter (including the
	// reset chapter 1). EnqueueStage flips status from awaiting_chapterize /
	// pending to queued atomically before publishing the task.
	for ord := 1; ord <= len(ranges); ord++ {
		jobID := chapterIDByOrdinal[ord]
		if err := s.EnqueueStage(ctx, models.TaskPayload{
			JobID:       jobID,
			Stage:       models.StageSegmentReview,
			Attempt:     0,
			RequestedBy: "pipeline",
			Reason:      "chapterize_fan_out",
		}); err != nil {
			return fmt.Errorf("ep_chapterize: enqueue chapter %d: %w", ord, err)
		}
	}

	slog.Info("ep_chapterize: fan-out complete",
		"episode_id", ep.ID,
		"total_chapters", len(ranges))
	return nil
}

// chapterSourcePath returns the relpath of one sliced source artefact
// (kind ∈ {"video", "vocals", "bgm"}) under the OPT-403 unified layout.
// Kept private to this file because the source/ subtree is an internal
// pipeline artefact (not surfaced via Episode.GetXxxRelPath, which only
// exposes user-facing artefacts).
//
// Layout examples for episode 138:
//
//	episodes/138/chapters/source/ch01.mp4         (video slice)
//	episodes/138/chapters/source/ch01.vocals.wav  (master vocals slice)
//	episodes/138/chapters/source/ch01.bgm.wav     (master BGM slice)
//
// vocals + bgm need distinct suffixes so they do not collide on disk.
func chapterSourcePath(ep *models.Episode, ordinal int, kind string) string {
	suffix := "bin"
	switch kind {
	case "video":
		suffix = "mp4"
	case "vocals":
		suffix = "vocals.wav"
	case "bgm":
		suffix = "bgm.wav"
	}
	// filepath.ToSlash because storage.ResolveDataPath joins with the host
	// separator; relpaths in DB always use forward slashes by convention.
	return filepath.ToSlash(fmt.Sprintf("episodes/%d/chapters/source/ch%02d.%s", ep.ID, ordinal, suffix))
}
