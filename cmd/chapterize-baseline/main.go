// Package main — cmd/chapterize-baseline.
//
// Generates the OPT-403 algorithm baseline at
// docs/opt-403/baseline-opt403-79min.json.  Synthetic input mirrors the
// 79-minute MIT 6.824 lecture (episode 139): 79 ASR segments roughly 60s
// long with silence gaps in [800ms, 3000ms]. Runs Pass 1 + Pass 2 of the
// chapterize algorithm with the production knob defaults and serialises:
//
//   - input parameters (target/min/max/min-gap)
//   - candidate count + first / last candidate timestamps
//   - chosen DP cuts (ordinal, boundary_ms, silence_gap_ms)
//   - chapter ranges (start/end + duration_ms)
//   - distribution metrics (mean, max abs deviation, min/max duration)
//
// The JSON is the long-term contract for "the chapterize algorithm
// produces these chapters on this synthetic input"; any future PR that
// changes the DP cost function should re-run this tool, diff the JSON
// against HEAD, and explain the deltas in the PR description.
//
// Run with:
//
//	go run ./cmd/chapterize-baseline > docs/opt-403/baseline-opt403-79min.json
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"holodub/internal/chapterize"
)

type baselineReport struct {
	GeneratedBy string `json:"generated_by"`
	Input       struct {
		EpisodeName       string `json:"episode_name"`
		TotalDurationMs   int64  `json:"total_duration_ms"`
		SegmentCount      int    `json:"segment_count"`
		AvgSegmentMs      int64  `json:"avg_segment_ms"`
		AvgGapMs          int64  `json:"avg_gap_ms"`
		MinGapMs          int64  `json:"min_gap_ms_seen"`
		MaxGapMs          int64  `json:"max_gap_ms_seen"`
	} `json:"input"`
	AlgorithmKnobs struct {
		TargetChapterMs    int64 `json:"target_chapter_ms"`
		MinChapterMs       int64 `json:"min_chapter_ms"`
		MaxChapterMs       int64 `json:"max_chapter_ms"`
		MinSilenceGapMs    int64 `json:"min_silence_gap_ms"`
	} `json:"algorithm_knobs"`
	Pass1Candidates struct {
		Count   int   `json:"count"`
		FirstMs int64 `json:"first_boundary_ms,omitempty"`
		LastMs  int64 `json:"last_boundary_ms,omitempty"`
	} `json:"pass1_candidates"`
	Pass2Cuts []struct {
		Ordinal      int   `json:"ordinal"`
		BoundaryMs   int64 `json:"boundary_ms"`
		SilenceGapMs int64 `json:"silence_gap_ms"`
	} `json:"pass2_cuts"`
	Chapters []struct {
		Ordinal      int   `json:"ordinal"`
		StartMs      int64 `json:"start_ms"`
		EndMs        int64 `json:"end_ms"`
		DurationMs   int64 `json:"duration_ms"`
		DurationMin  float64 `json:"duration_min"`
	} `json:"chapters"`
	Metrics struct {
		ChapterCount         int     `json:"chapter_count"`
		MeanDurationMs       float64 `json:"mean_duration_ms"`
		MeanDurationMin      float64 `json:"mean_duration_min"`
		MaxAbsDeviationMs    int64   `json:"max_abs_deviation_ms"`
		MaxAbsDeviationRatio float64 `json:"max_abs_deviation_ratio"`
		MinChapterMs         int64   `json:"min_chapter_ms_observed"`
		MaxChapterMs         int64   `json:"max_chapter_ms_observed"`
		MinSilenceCutMs      int64   `json:"min_silence_at_cuts_ms"`
	} `json:"metrics"`
}

func main() {
	const (
		segmentCount = 79
		segmentLenMs = int64(55_000) // 55s base length
		// Gap sequence is deterministic (no rand) so the baseline is
		// bit-identical across machines / OSes / Go versions. Hand-tuned
		// to interleave a handful of long silences (= obvious chapter
		// boundaries) with normal sub-second pauses.
		baseGapMs = int64(2_000)
	)

	gaps := []int64{
		2_500, 1_200, 1_800, 950, 1_400, 2_200, 850, 1_650,
		2_900, 1_100, 1_750, 1_300, 1_500, 2_700, 880, 1_950,
		2_400, 1_650, 1_200, 1_400, 850, 2_800, 1_100, 1_500,
		3_200, 950, 1_750, 1_400, 2_200, 1_650, 880, 1_200,
		2_950, 1_100, 1_500, 1_750, 850, 2_400, 1_200, 1_650,
		2_700, 1_100, 1_400, 1_500, 950, 2_200, 880, 1_750,
		3_100, 1_200, 1_500, 1_650, 850, 2_900, 1_100, 1_400,
		2_400, 950, 1_750, 1_500, 880, 2_700, 1_200, 1_650,
		2_950, 1_100, 1_400, 1_500, 850, 2_400, 1_200, 1_750,
		2_900, 1_100, 1_650, 1_400, 950, 1_500,
	}

	segs := make([]chapterize.Segment, 0, segmentCount)
	cursor := int64(0)
	gapMin, gapMax, gapSum := int64(1<<62), int64(0), int64(0)
	for i := 0; i < segmentCount; i++ {
		startMs := cursor
		endMs := startMs + segmentLenMs
		segs = append(segs, chapterize.Segment{StartMs: startMs, EndMs: endMs})

		var nextGap int64
		if i < len(gaps) {
			nextGap = gaps[i]
		} else {
			nextGap = baseGapMs
		}
		if i < segmentCount-1 {
			if nextGap < gapMin {
				gapMin = nextGap
			}
			if nextGap > gapMax {
				gapMax = nextGap
			}
			gapSum += nextGap
		}
		cursor = endMs + nextGap
	}
	totalDuration := cursor
	avgGap := gapSum / int64(segmentCount-1)

	const (
		targetMs    = 22 * 60 * 1000 // 22 min
		minMs       = 18 * 60 * 1000 // 18 min
		maxMs       = 30 * 60 * 1000 // 30 min
		minGapMs    = int64(800)
	)

	candidates := chapterize.ExtractCandidates(segs, minGapMs)
	cuts := chapterize.DPOptimalCuts(candidates, totalDuration, targetMs, minMs, maxMs)
	ranges := chapterize.BuildChapterRanges(segs, cuts, totalDuration)

	report := baselineReport{GeneratedBy: "cmd/chapterize-baseline"}
	report.Input.EpisodeName = "synthetic 79-segment lecture (mirrors episode 139)"
	report.Input.TotalDurationMs = totalDuration
	report.Input.SegmentCount = segmentCount
	report.Input.AvgSegmentMs = segmentLenMs
	report.Input.AvgGapMs = avgGap
	report.Input.MinGapMs = gapMin
	report.Input.MaxGapMs = gapMax

	report.AlgorithmKnobs.TargetChapterMs = targetMs
	report.AlgorithmKnobs.MinChapterMs = minMs
	report.AlgorithmKnobs.MaxChapterMs = maxMs
	report.AlgorithmKnobs.MinSilenceGapMs = minGapMs

	report.Pass1Candidates.Count = len(candidates)
	if len(candidates) > 0 {
		report.Pass1Candidates.FirstMs = candidates[0].BoundaryMs
		report.Pass1Candidates.LastMs = candidates[len(candidates)-1].BoundaryMs
	}

	for i, c := range cuts {
		report.Pass2Cuts = append(report.Pass2Cuts, struct {
			Ordinal      int   `json:"ordinal"`
			BoundaryMs   int64 `json:"boundary_ms"`
			SilenceGapMs int64 `json:"silence_gap_ms"`
		}{i + 1, c.BoundaryMs, c.SilenceGapMs})
	}

	minCh, maxCh := int64(1<<62), int64(0)
	minSilence := int64(1<<62)
	for _, r := range ranges {
		dur := r.EndMs - r.StartMs
		if dur < minCh {
			minCh = dur
		}
		if dur > maxCh {
			maxCh = dur
		}
		if r.EndCutSilenceMs > 0 && r.EndCutSilenceMs < minSilence {
			minSilence = r.EndCutSilenceMs
		}
		report.Chapters = append(report.Chapters, struct {
			Ordinal     int     `json:"ordinal"`
			StartMs     int64   `json:"start_ms"`
			EndMs       int64   `json:"end_ms"`
			DurationMs  int64   `json:"duration_ms"`
			DurationMin float64 `json:"duration_min"`
		}{r.Ordinal, r.StartMs, r.EndMs, dur, float64(dur) / 60_000.0})
	}
	if minSilence == 1<<62 {
		minSilence = 0
	}

	report.Metrics.ChapterCount = len(ranges)
	report.Metrics.MeanDurationMs = chapterize.MeanChapterDuration(ranges)
	report.Metrics.MeanDurationMin = report.Metrics.MeanDurationMs / 60_000.0
	report.Metrics.MaxAbsDeviationMs = chapterize.MaxAbsDeviation(ranges, targetMs)
	report.Metrics.MaxAbsDeviationRatio = float64(report.Metrics.MaxAbsDeviationMs) / float64(targetMs)
	report.Metrics.MinChapterMs = minCh
	report.Metrics.MaxChapterMs = maxCh
	report.Metrics.MinSilenceCutMs = minSilence

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(&report); err != nil {
		fmt.Fprintln(os.Stderr, "encode baseline:", err)
		os.Exit(1)
	}
}
