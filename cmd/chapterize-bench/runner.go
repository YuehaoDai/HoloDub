// runner.go — single-model single-run extract path for cmd/chapterize-bench.
//
// One ExtractRun captures EVERYTHING the report needs to compare a
// candidate model to its peers:
//
//   - The raw chapters[] array the LLM emitted, BEFORE any post-processing.
//     This is the diagnostic field — the operator can spot e.g. a model
//     that always overshoots end_segment_idx, or one whose titles are
//     just literal first-words.
//
//   - Validation result (pass/fail + first breach reason). When this
//     fails the production pipeline silently falls back to DP, so we
//     surface it explicitly here.
//
//   - The post-snap, post-hard-constraint chapter ranges + meta. This
//     is what stage_chapterize would actually persist on the chapter
//     Jobs in production. By saving these we can answer "if I switch
//     prod to model X, what would the chapters look like?" without
//     re-running the LLM.
//
//   - StaticMetrics: count, mean/min/max duration, snap-jitter, count
//     of merge / split actions. Cheap, deterministic, and what the
//     operator looks at first when diffing two models.
//
//   - WallTimeMs: end-to-end latency for the LLM call. Important for
//     "is this model fast enough to ship?" decisions independently of
//     output quality.
//
// One ExtractRun is written to bench-{ts}/raw/{model}-run{i}.json and
// the report aggregates across runs[] per model.
package main

import (
	"context"
	"time"

	"holodub/internal/chapterize"
	"holodub/internal/config"
	"holodub/internal/llm"
	"holodub/internal/models"
)

// ExtractRun is the persisted record of one (model, run) extract.
// All durations are in milliseconds; rendered as minutes only at
// report-time so JSON consumers stay precise.
type ExtractRun struct {
	Model       string `json:"model"`
	RunIndex    int    `json:"run_index"` // 1-based
	WallTimeMs  int64  `json:"wall_time_ms"`
	Error       string `json:"error,omitempty"`

	// RawChapters is what the model emitted — index pairs + titles. Empty
	// when Error != "" or the model declined to chapterize.
	RawChapters []llm.ChapterCut `json:"raw_chapters"`

	// ValidationOK / ValidationError reflect chapterize.ValidateLLMPlan
	// against the raw plan. When false the production pipeline falls
	// back to DP; bench reports this explicitly.
	ValidationOK    bool   `json:"validation_ok"`
	ValidationError string `json:"validation_error,omitempty"`

	// FinalRanges + FinalTitles are what stage_chapterize would persist
	// AFTER snap-to-silence + hard-constraint enforcement. Empty when
	// the raw plan failed validation.
	FinalRanges []chapterize.ChapterRange `json:"final_ranges,omitempty"`
	FinalTitles []chapterize.LLMChapterMeta `json:"final_titles,omitempty"`

	StaticMetrics StaticMetrics `json:"static_metrics"`

	// Glossary signal — secondary, but bundled because we got it for
	// free in the same call. A weak glossary can hint that the model
	// doesn't read the transcript well, which often correlates with
	// poor chapter cuts.
	GlossaryEntryCount   int `json:"glossary_entry_count"`
	SpeakerHintCount     int `json:"speaker_hint_count"`
	ReferenceCardChars   int `json:"reference_card_chars"`
}

// StaticMetrics is the cheap, deterministic shape derived from the raw +
// final chapter plan. No LLM involvement.
type StaticMetrics struct {
	RawChapterCount   int   `json:"raw_chapter_count"`
	FinalChapterCount int   `json:"final_chapter_count"`

	MeanDurMs int64 `json:"mean_dur_ms"`
	MinDurMs  int64 `json:"min_dur_ms"`
	MaxDurMs  int64 `json:"max_dur_ms"`

	// MergeActions / SplitActions count how many EnforceHardConstraints
	// rewrites happened. Both = 0 means the model nailed the soft 5–45min
	// range without help; non-zero is a smell that's worth surfacing.
	MergeActions int `json:"merge_actions"`
	SplitActions int `json:"split_actions"`

	// BoundaryJitterMs is the avg |snap_position - raw_segment_end| over
	// internal boundaries. Large jitter means the model picked cut
	// segments mid-sentence and the snap step had to walk far to a
	// natural breath — usually a sign of weak boundary intuition.
	BoundaryJitterMs int64 `json:"boundary_jitter_ms"`
}

// runOneExtract clones cfg, swaps GlossaryModel to the candidate, builds
// a fresh llm.Client, calls ExtractEpisodeGlossary with chapterizeEnabled=true,
// and applies the same Validate → Snap → Hard pipeline that
// stage_chapterize.go does in production. Errors are recorded on the
// returned ExtractRun.Error and do NOT panic — the bench keeps marching.
func runOneExtract(
	ctx context.Context,
	cfg config.Config,
	ep *models.Episode,
	segments []llm.EpisodeSegment,
	model string,
	runIndex int,
) ExtractRun {
	out := ExtractRun{Model: model, RunIndex: runIndex}

	// Clone cfg into a local copy and swap the model. Note: llm.New reads
	// glossaryModel from cfg.GlossaryModel only at construction time so a
	// per-call client is the simplest way to avoid leaking state across
	// candidates.
	candidateCfg := cfg
	candidateCfg.GlossaryModel = model

	client := llm.New(candidateCfg)

	started := time.Now()
	result, err := client.ExtractEpisodeGlossary(
		ctx, segments, ep.SourceLanguage, ep.TargetLanguage, true,
	)
	out.WallTimeMs = time.Since(started).Milliseconds()
	if err != nil {
		out.Error = err.Error()
		return out
	}

	out.RawChapters = result.Chapters
	out.GlossaryEntryCount = len(result.Glossary)
	out.SpeakerHintCount = len(result.Speakers)
	out.ReferenceCardChars = len(result.ReferenceCard)
	out.StaticMetrics.RawChapterCount = len(result.Chapters)

	if len(result.Chapters) == 0 {
		out.ValidationError = "model returned chapters=[]"
		return out
	}

	// Convert llm.ChapterCut → chapterize.LLMChapter (same one-to-one
	// mapping stage_chapterize.go uses). Sort defensively in case the
	// provider reordered under load.
	plan := make([]chapterize.LLMChapter, len(result.Chapters))
	for i, c := range result.Chapters {
		plan[i] = chapterize.LLMChapter{
			StartSegmentIdx: c.StartSegmentIdx,
			EndSegmentIdx:   c.EndSegmentIdx,
			TitleSource:     c.TitleSource,
			TitleTranslated: c.TitleTranslated,
			SummaryMD:       c.SummaryMD,
		}
	}
	plan = chapterize.SortLLMPlan(plan)

	if err := chapterize.ValidateLLMPlan(plan, len(segments)); err != nil {
		out.ValidationError = err.Error()
		return out
	}
	out.ValidationOK = true

	// Build chapterize.Segment slice from llm.EpisodeSegment for snap +
	// enforce. Drops the text/speaker fields the algorithm doesn't need.
	algoSegs := make([]chapterize.Segment, len(segments))
	for i, s := range segments {
		algoSegs[i] = chapterize.Segment{StartMs: s.StartMs, EndMs: s.EndMs}
	}
	totalDurationMs := segments[len(segments)-1].EndMs

	// Use the same minSilenceGapMs + lookahead as stage_chapterize.go.
	// We DO NOT read those from cfg here on purpose — the bench should
	// reflect what production does, and production hard-codes lookahead=3
	// inside stage_chapterize. Min silence comes from cfg though.
	trailingCuts := chapterize.SnapBoundariesToSilences(
		algoSegs, plan, totalDurationMs, cfg.ChapterizeMinSilenceGapMs, 3,
	)

	// BoundaryJitter: how far did snap have to move each non-final
	// boundary from segments[end].EndMs? Average over interior cuts only.
	jitterSum, jitterN := int64(0), int64(0)
	for i, ch := range plan {
		if i == len(plan)-1 {
			continue
		}
		raw := algoSegs[ch.EndSegmentIdx].EndMs
		snap := trailingCuts[i]
		d := snap - raw
		if d < 0 {
			d = -d
		}
		jitterSum += d
		jitterN++
	}
	if jitterN > 0 {
		out.StaticMetrics.BoundaryJitterMs = jitterSum / jitterN
	}

	rawRanges := chapterize.BuildLLMChapterRanges(plan, trailingCuts)
	rawMeta := make([]chapterize.LLMChapterMeta, len(plan))
	for i, p := range plan {
		rawMeta[i] = chapterize.LLMChapterMeta{
			TitleSource:     p.TitleSource,
			TitleTranslated: p.TitleTranslated,
			SummaryMD:       p.SummaryMD,
		}
	}
	preCount := len(rawRanges)

	finalRanges, finalMeta := chapterize.EnforceHardConstraints(
		rawRanges, rawMeta, algoSegs,
		cfg.ChapterizeHardMinMs, cfg.ChapterizeHardMaxMs,
		cfg.ChapterizeMinSilenceGapMs, 4,
	)
	postCount := len(finalRanges)

	// Action counts: post < pre means merges happened; post > pre means
	// splits happened. They can't both be non-zero in one run (merges
	// happen before splits, never both), but tracking them separately
	// makes the report clearer about which guardrail tripped.
	if postCount < preCount {
		out.StaticMetrics.MergeActions = preCount - postCount
	}
	if postCount > preCount {
		out.StaticMetrics.SplitActions = postCount - preCount
	}

	out.FinalRanges = finalRanges
	out.FinalTitles = finalMeta
	out.StaticMetrics.FinalChapterCount = postCount

	if postCount > 0 {
		var sum, mn, mx int64
		mn = 1 << 62
		for _, r := range finalRanges {
			d := r.EndMs - r.StartMs
			sum += d
			if d < mn {
				mn = d
			}
			if d > mx {
				mx = d
			}
		}
		out.StaticMetrics.MeanDurMs = sum / int64(postCount)
		out.StaticMetrics.MinDurMs = mn
		out.StaticMetrics.MaxDurMs = mx
	}

	return out
}
