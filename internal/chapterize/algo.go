// Package chapterize implements the OPT-403 chapter-cut algorithm.
//
// The high-level pipeline (called from internal/pipeline/stage_chapterize.go) is:
//
//  1. Pass 1 — ExtractCandidates: scan the ASR segment list, collect every
//     inter-segment silence gap >= MinGapMs as a candidate boundary. Cheap,
//     deterministic, no LLM.
//
//  2. Pass 2 — DPOptimalCuts: dynamic programming over the candidate set to
//     pick the subset whose chapters all fall within [MinChapterMs, MaxChapterMs]
//     and minimise a quadratic distance from TargetChapterMs (with a small
//     bonus for cutting on long silences). O(n²) where n = candidate count;
//     n is < ~1k even for 4-hour episodes so this is microseconds.
//
//  3. Pass 3 — internal/llm/chapter_review.go.ReviewChapterCuts: optional LLM
//     pass that nudges boundaries by ±1 silence-gap if the cut splits a coherent
//     paragraph, AND mints a bilingual chapter title. Lives in the llm package
//     because it issues an OpenAI-compatible tool call; this package stays
//     pure-Go and trivially testable without mocks.
//
// The package has zero dependencies outside std-lib so its tests run in
// milliseconds and its determinism is bit-exact across machines — a hard
// requirement for the snapshot-style tests in algo_test.go that pin the
// chapter boundaries computed for the 79-minute reference episode.
package chapterize

// Segment is the algorithm's view of one ASR segment. Pipeline code converts
// from models.Segment at the call boundary to keep this package free of GORM /
// model imports (and therefore trivially mockable).
type Segment struct {
	StartMs int64
	EndMs   int64
}

// CandidateBoundary is one silence gap between two adjacent ASR segments that
// is wide enough to be a plausible chapter break.
type CandidateBoundary struct {
	// AfterSegmentIdx is the index in the input []Segment whose end is the
	// LEFT edge of the silence gap (i.e. the cut is between segments
	// [AfterSegmentIdx] and [AfterSegmentIdx+1]).
	AfterSegmentIdx int

	// BoundaryMs is the wall-clock cut position (ms from episode start),
	// chosen as the midpoint of the silence gap. ffmpeg slicing uses this.
	BoundaryMs int64

	// SilenceGapMs is the raw gap width — wider gaps are preferred during
	// DP scoring so we cut at "natural breaths" instead of mid-sentence.
	SilenceGapMs int64
}

// ExtractCandidates implements Pass 1 of the chapter-cut algorithm.
//
// Walks the ASR segments in order; for every adjacent pair (i, i+1) whose
// silence gap >= minGapMs, emits a CandidateBoundary positioned at the gap's
// midpoint. Returns nil for < 2 segments.
//
// The caller is expected to have already sorted segments by StartMs (which
// is the natural order ASR returns them in). Overlapping or out-of-order
// segments produce a negative gap which is silently ignored.
func ExtractCandidates(segments []Segment, minGapMs int64) []CandidateBoundary {
	if len(segments) < 2 {
		return nil
	}
	out := make([]CandidateBoundary, 0, len(segments)/4)
	for i := 0; i < len(segments)-1; i++ {
		gap := segments[i+1].StartMs - segments[i].EndMs
		if gap < minGapMs {
			continue
		}
		out = append(out, CandidateBoundary{
			AfterSegmentIdx: i,
			BoundaryMs:      (segments[i].EndMs + segments[i+1].StartMs) / 2,
			SilenceGapMs:    gap,
		})
	}
	return out
}

// DPOptimalCuts implements Pass 2 of the chapter-cut algorithm.
//
// Given the Pass 1 candidates, returns the subset whose chapters all fit in
// [minMs, maxMs] AND minimise a quadratic deviation from targetMs, with a
// small bonus for cutting on long silences. The result is the chosen
// candidates in original order — the caller turns them into chapter ranges
// by stitching with the virtual start (0) and end (totalDurationMs) cuts.
//
// Returns nil in three cases (caller must short-circuit to "1 chapter"):
//
//   - totalDurationMs <= maxMs: episode already fits in one chapter
//   - candidates is empty: no silence gap wide enough to cut on
//   - DP found no valid subdivision: e.g. minMs is so large that no two
//     consecutive boundaries are far enough apart, or the only available
//     cuts would leave a tail chapter shorter than minMs
//
// The score function is:
//
//	cost = Σ_chapter ((duration_chapter - targetMs)² / 1e6)   // duration penalty
//	     - Σ_cut (silenceGapMs_cut / 1000)                    // long-silence bonus
//
// The 1/1e6 keeps the duration penalty in roughly the same numeric range as
// the silence bonus (both are small positive numbers) so the optimiser
// doesn't degenerate into "always cut on the longest gap regardless of length".
func DPOptimalCuts(
	candidates []CandidateBoundary,
	totalDurationMs, targetMs, minMs, maxMs int64,
) []CandidateBoundary {
	if totalDurationMs <= maxMs {
		return nil
	}
	n := len(candidates)
	if n == 0 {
		return nil
	}

	// Build position array: V[0] = 0 (virtual start),
	// V[1..n] = candidate boundaries in input order,
	// V[n+1] = totalDurationMs (virtual end).
	// silenceBonus[i] is the score reward for cutting at V[i] (0 for virtuals).
	const sentinel = 1e30
	V := make([]int64, n+2)
	silenceBonus := make([]float64, n+2)
	V[n+1] = totalDurationMs
	for i, c := range candidates {
		V[i+1] = c.BoundaryMs
		silenceBonus[i+1] = float64(c.SilenceGapMs) / 1000.0
	}

	// dp[i] = minimum cost to choose V[i] as the END of a chapter (cumulative).
	// parent[i] = the V index of the cut just before V[i] in the optimal path.
	dp := make([]float64, n+2)
	parent := make([]int, n+2)
	for i := range dp {
		dp[i] = sentinel
		parent[i] = -1
	}
	dp[0] = 0

	for i := 1; i <= n+1; i++ {
		for j := 0; j < i; j++ {
			if dp[j] >= sentinel {
				continue
			}
			dur := V[i] - V[j]
			if dur < minMs || dur > maxMs {
				continue
			}
			deviation := float64(dur - targetMs)
			cost := dp[j] + (deviation*deviation)/1e6 - silenceBonus[i]
			if cost < dp[i] {
				dp[i] = cost
				parent[i] = j
			}
		}
	}

	if dp[n+1] >= sentinel {
		return nil
	}

	// Backtrack to recover chosen candidate indices. parent[n+1] is the last
	// real cut (or 0 if there is none → would be the "1 chapter" path which
	// the early-return at totalDurationMs<=maxMs already handles, so it
	// implies dp degenerated; we return nil to signal "1 chapter" anyway).
	chosenIdx := make([]int, 0, 8)
	cur := n + 1
	for cur > 0 {
		prev := parent[cur]
		if prev > 0 { // skip virtual start (V[0])
			chosenIdx = append(chosenIdx, prev-1) // -1 because candidates is 0-indexed
		}
		cur = prev
	}
	if len(chosenIdx) == 0 {
		return nil
	}
	// Reverse to original order.
	for i, j := 0, len(chosenIdx)-1; i < j; i, j = i+1, j-1 {
		chosenIdx[i], chosenIdx[j] = chosenIdx[j], chosenIdx[i]
	}
	out := make([]CandidateBoundary, len(chosenIdx))
	for i, ci := range chosenIdx {
		out[i] = candidates[ci]
	}
	return out
}

// ChapterRange describes one chapter as produced by the cut decision.
// Callers convert this into a Job row + an ffmpeg slicing command.
type ChapterRange struct {
	Ordinal           int
	StartMs           int64
	EndMs             int64
	StartSegmentIdx   int // inclusive; -1 if no segment falls in this range
	EndSegmentIdx     int // inclusive; -1 if no segment falls in this range
	StartCutSilenceMs int64 // silence on the LEFT edge (0 for first chapter)
	EndCutSilenceMs   int64 // silence on the RIGHT edge (0 for last chapter)
}

// BuildChapterRanges turns the chosen DP boundaries into K = len(boundaries)+1
// ChapterRanges, each tagged with its first/last segment indices for downstream
// segment reassignment in store.ReassignSegmentsToChapters.
//
// boundaries must be the original-order output of DPOptimalCuts. segments must
// be the same slice fed into ExtractCandidates (so AfterSegmentIdx remains
// valid). totalDurationMs must equal the original input.
//
// When boundaries is empty the function returns a single ChapterRange covering
// [0, totalDurationMs) — the "1-chapter" short-circuit.
func BuildChapterRanges(
	segments []Segment,
	boundaries []CandidateBoundary,
	totalDurationMs int64,
) []ChapterRange {
	if len(boundaries) == 0 {
		startIdx := -1
		endIdx := -1
		if len(segments) > 0 {
			startIdx = 0
			endIdx = len(segments) - 1
		}
		return []ChapterRange{{
			Ordinal:         1,
			StartMs:         0,
			EndMs:           totalDurationMs,
			StartSegmentIdx: startIdx,
			EndSegmentIdx:   endIdx,
		}}
	}

	out := make([]ChapterRange, 0, len(boundaries)+1)
	prevCutMs := int64(0)
	prevSegIdx := -1 // first segment of the next chapter (0 for first chapter)
	if len(segments) > 0 {
		prevSegIdx = 0
	}
	prevSilence := int64(0)
	for i, b := range boundaries {
		ch := ChapterRange{
			Ordinal:           i + 1,
			StartMs:           prevCutMs,
			EndMs:             b.BoundaryMs,
			StartSegmentIdx:   prevSegIdx,
			EndSegmentIdx:     b.AfterSegmentIdx,
			StartCutSilenceMs: prevSilence,
			EndCutSilenceMs:   b.SilenceGapMs,
		}
		out = append(out, ch)
		prevCutMs = b.BoundaryMs
		prevSegIdx = b.AfterSegmentIdx + 1
		prevSilence = b.SilenceGapMs
	}
	// Last chapter [last cut, totalDurationMs).
	endIdx := -1
	if len(segments) > 0 {
		endIdx = len(segments) - 1
	}
	out = append(out, ChapterRange{
		Ordinal:           len(boundaries) + 1,
		StartMs:           prevCutMs,
		EndMs:             totalDurationMs,
		StartSegmentIdx:   prevSegIdx,
		EndSegmentIdx:     endIdx,
		StartCutSilenceMs: prevSilence,
		EndCutSilenceMs:   0,
	})
	return out
}

// MeanChapterDuration returns the arithmetic mean chapter duration in ms.
// Useful for tests that want to assert "DP picked roughly target-sized chapters".
func MeanChapterDuration(ranges []ChapterRange) float64 {
	if len(ranges) == 0 {
		return 0
	}
	var sum int64
	for _, r := range ranges {
		sum += r.EndMs - r.StartMs
	}
	return float64(sum) / float64(len(ranges))
}

// MaxAbsDeviation returns the largest |duration - targetMs| across the chapters.
// Useful for tests that want to assert "no chapter is wildly off target".
func MaxAbsDeviation(ranges []ChapterRange, targetMs int64) int64 {
	var worst int64
	for _, r := range ranges {
		d := (r.EndMs - r.StartMs) - targetMs
		if d < 0 {
			d = -d
		}
		if d > worst {
			worst = d
		}
	}
	return worst
}

