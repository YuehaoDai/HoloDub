// Package chapterize — OPT-405 LLM-driven chapter cut application.
//
// This file consumes the chapter plan produced by the LLM (see
// internal/llm/glossary.go ExtractEpisodeGlossary's chapters[] field)
// and turns it into the same ChapterRange shape that DPOptimalCuts +
// BuildChapterRanges produce, so the downstream stage_chapterize fan-
// out code path is unchanged.
//
// Three transformations are applied in order:
//
//  1. ValidateLLMPlan — every cut must be contiguous, in range, with
//     no overlap or gap. A breach makes the caller fall back to DP.
//
//  2. SnapBoundariesToSilences — each chapter's end-segment index maps
//     to a wall-clock ms cut position. We don't take the segment's
//     EndMs literally because that may sit milliseconds before the
//     next segment's StartMs (cutting in the speaker's breath). Instead
//     we walk a small window around the LLM boundary and pick the
//     midpoint of the LONGEST silence we find — the same heuristic the
//     deterministic DP path uses for "natural breath" cuts.
//
//  3. EnforceHardConstraints — the LLM is told "5–45min is a soft
//     guide" but a model occasionally emits an outlier (a 30s intro
//     chapter, or a 90min chapter that swallows two themes). This pass
//     enforces hard min/max in two steps:
//       • merge any chapter < HardMinMs into its neighbour (preserve
//         the title of the SHORTER chapter so the summary doesn't get
//         lost; metadata is concatenated with " • " separator)
//       • for any chapter > HardMaxMs, recursively split at the
//         nearest silence to the midpoint (one extra LLM verdict slot
//         per added split is filled with "Chapter N (cont.)")
//
// All three are pure functions taking only the algorithm's own
// Segment / ChapterRange types — easy to unit test and easy to reason
// about without GORM / models / config dependencies.

package chapterize

import (
	"errors"
	"fmt"
	"sort"
)

// LLMChapter is the algorithm-side mirror of llm.ChapterCut. We
// duplicate the type so the chapterize package stays free of llm
// imports (which keeps it testable in milliseconds and lets the snap /
// constraints logic be reused by future OPTs).
type LLMChapter struct {
	StartSegmentIdx int
	EndSegmentIdx   int
	TitleSource     string
	TitleTranslated string
	SummaryMD       string
}

// ValidateLLMPlan returns nil if the chapter plan is internally
// consistent given a segment count of n; otherwise it returns the first
// breach found. Callers must treat any error as "fall back to DP".
//
// Contract:
//   - At least one chapter
//   - StartSegmentIdx <= EndSegmentIdx
//   - First chapter starts at 0; last chapter ends at n-1
//   - Adjacent chapters are tightly contiguous (next.Start == prev.End+1)
func ValidateLLMPlan(plan []LLMChapter, segmentCount int) error {
	if len(plan) == 0 {
		return errors.New("empty chapter plan")
	}
	if segmentCount <= 0 {
		return errors.New("segment count must be positive")
	}
	for i, ch := range plan {
		if ch.StartSegmentIdx < 0 || ch.EndSegmentIdx < 0 {
			return fmt.Errorf("chapter %d: negative index", i+1)
		}
		if ch.EndSegmentIdx >= segmentCount {
			return fmt.Errorf("chapter %d: end_segment_idx %d >= segment_count %d", i+1, ch.EndSegmentIdx, segmentCount)
		}
		if ch.StartSegmentIdx > ch.EndSegmentIdx {
			return fmt.Errorf("chapter %d: start %d > end %d", i+1, ch.StartSegmentIdx, ch.EndSegmentIdx)
		}
	}
	if plan[0].StartSegmentIdx != 0 {
		return fmt.Errorf("first chapter must start at 0, got %d", plan[0].StartSegmentIdx)
	}
	if plan[len(plan)-1].EndSegmentIdx != segmentCount-1 {
		return fmt.Errorf("last chapter must end at %d, got %d", segmentCount-1, plan[len(plan)-1].EndSegmentIdx)
	}
	for i := 1; i < len(plan); i++ {
		expected := plan[i-1].EndSegmentIdx + 1
		if plan[i].StartSegmentIdx != expected {
			return fmt.Errorf("chapter %d must start at %d (gap or overlap with chapter %d)", i+1, expected, i)
		}
	}
	return nil
}

// SnapBoundariesToSilences refines each chapter's end-segment boundary
// from a literal segments[end].EndMs to the midpoint of the LONGEST
// silence gap within ±lookahead segments of the boundary.
//
// Returned slice is parallel to plan: out[i] is the precise wall-clock
// cut position (in ms) for chapter i's TRAILING edge. The last chapter's
// trailing edge is always totalDurationMs (no snap — that's the episode
// end, which the caller obtains from the last segment's EndMs anyway).
//
// minGapMs filters out micro-pauses that are within-sentence. lookahead
// caps how far we'll walk to find a better cut — keeping it small (3–5)
// preserves the LLM's intent. A boundary that finds NO usable silence
// in its window keeps its original end position (segments[end].EndMs);
// no snap, no harm.
func SnapBoundariesToSilences(
	segments []Segment,
	plan []LLMChapter,
	totalDurationMs int64,
	minGapMs int64,
	lookahead int,
) []int64 {
	out := make([]int64, len(plan))
	for i, ch := range plan {
		if i == len(plan)-1 {
			// Last chapter's trailing edge is the episode end.
			out[i] = totalDurationMs
			continue
		}
		out[i] = snapOneBoundary(segments, ch.EndSegmentIdx, minGapMs, lookahead)
	}
	return out
}

// snapOneBoundary scans [endIdx-lookahead, endIdx+lookahead] for the
// widest qualifying silence and returns the silence midpoint, or the
// raw segments[endIdx].EndMs if no silence in window beats minGapMs.
//
// The window is clamped to [0, len(segments)-2] so we never look past
// the last segment (which has no "next" to measure a gap against).
func snapOneBoundary(segments []Segment, endIdx int, minGapMs int64, lookahead int) int64 {
	defaultCut := int64(0)
	if endIdx >= 0 && endIdx < len(segments) {
		defaultCut = segments[endIdx].EndMs
	}
	if len(segments) < 2 {
		return defaultCut
	}
	lo := endIdx - lookahead
	if lo < 0 {
		lo = 0
	}
	hi := endIdx + lookahead
	if hi > len(segments)-2 {
		hi = len(segments) - 2
	}
	if lo > hi {
		return defaultCut
	}
	bestGap := int64(0)
	bestCut := defaultCut
	for j := lo; j <= hi; j++ {
		gap := segments[j+1].StartMs - segments[j].EndMs
		if gap < minGapMs || gap <= bestGap {
			continue
		}
		bestGap = gap
		bestCut = (segments[j].EndMs + segments[j+1].StartMs) / 2
	}
	return bestCut
}

// BuildLLMChapterRanges turns the validated LLM plan + snapped trailing
// cuts into the same ChapterRange[] shape DPOptimalCuts produces, so the
// rest of the fan-out pipeline is layout-agnostic to the cut source.
//
// Conventions (matching BuildChapterRanges):
//   - Ordinal is 1-based
//   - StartMs / EndMs are inclusive-start, exclusive-end (the next
//     chapter's StartMs == this chapter's EndMs)
//   - StartCutSilenceMs / EndCutSilenceMs default to 0 — they're
//     informational only (the snap step has already absorbed the silence
//     bonus into the cut position itself)
func BuildLLMChapterRanges(
	plan []LLMChapter,
	trailingCutsMs []int64,
) []ChapterRange {
	if len(plan) == 0 || len(plan) != len(trailingCutsMs) {
		return nil
	}
	out := make([]ChapterRange, len(plan))
	prev := int64(0)
	for i, ch := range plan {
		out[i] = ChapterRange{
			Ordinal:         i + 1,
			StartMs:         prev,
			EndMs:           trailingCutsMs[i],
			StartSegmentIdx: ch.StartSegmentIdx,
			EndSegmentIdx:   ch.EndSegmentIdx,
		}
		prev = trailingCutsMs[i]
	}
	return out
}

// LLMChapterMeta is the per-chapter title bundle returned alongside the
// ranges so the caller (stage_chapterize) can persist them onto each
// chapter Job. Parallel index to the ChapterRange[] returned by the
// same call.
type LLMChapterMeta struct {
	TitleSource     string
	TitleTranslated string
	SummaryMD       string
}

// EnforceHardConstraints applies the OPT-405 hard min/max guardrails:
//
//   - Any chapter shorter than hardMinMs is merged INTO ITS SUCCESSOR
//     (last chapter merges into its predecessor instead). Titles +
//     summaries are concatenated with " • " so no info is lost.
//   - Any chapter longer than hardMaxMs is split at the midpoint, snapped
//     to the longest in-window silence (same logic as snapOneBoundary).
//     Splits are RECURSIVE up to maxSplitDepth — if a 90min chapter still
//     exceeds the cap after one split, both halves are checked again.
//     Generated splits get the title "<original> (cont.)" so the
//     manifest stays human-readable.
//
// Returns the post-enforcement ranges + parallel metadata. Both slices
// have the same length and order; ranges[i].Ordinal is renumbered 1..N.
//
// The caller must ensure ranges and meta are parallel in input. We
// don't take the segments slice for splits — we just use the midpoint
// of the chapter's [StartMs, EndMs) and call back into the snap helper
// with a synthesised "virtual segment" pair. This keeps the function
// dependency-free.
func EnforceHardConstraints(
	ranges []ChapterRange,
	meta []LLMChapterMeta,
	segments []Segment,
	hardMinMs, hardMaxMs int64,
	minSilenceGapMs int64,
	maxSplitDepth int,
) ([]ChapterRange, []LLMChapterMeta) {
	if len(ranges) == 0 || len(ranges) != len(meta) {
		return ranges, meta
	}

	// Step 1: merge undersized chapters.
	mergedR, mergedM := mergeShortChapters(ranges, meta, hardMinMs)

	// Step 2: split oversized chapters (recursive, capped depth).
	splitR, splitM := splitLongChapters(mergedR, mergedM, segments, hardMaxMs, minSilenceGapMs, maxSplitDepth)

	// Step 3: renumber ordinals 1..N.
	for i := range splitR {
		splitR[i].Ordinal = i + 1
	}
	return splitR, splitM
}

func mergeShortChapters(
	ranges []ChapterRange,
	meta []LLMChapterMeta,
	hardMinMs int64,
) ([]ChapterRange, []LLMChapterMeta) {
	if len(ranges) <= 1 {
		// 1-chapter episodes always get a free pass — there's no
		// neighbour to merge into and a single very-short episode
		// shouldn't be artificially extended.
		return ranges, meta
	}
	outR := make([]ChapterRange, 0, len(ranges))
	outM := make([]LLMChapterMeta, 0, len(meta))
	for i := 0; i < len(ranges); i++ {
		dur := ranges[i].EndMs - ranges[i].StartMs
		if dur >= hardMinMs {
			outR = append(outR, ranges[i])
			outM = append(outM, meta[i])
			continue
		}
		// Undersize. Prefer merging into the SUCCESSOR; only merge into
		// the predecessor when this is the last chapter (no successor).
		if i < len(ranges)-1 {
			next := ranges[i+1]
			next.StartMs = ranges[i].StartMs
			if ranges[i].StartSegmentIdx >= 0 {
				next.StartSegmentIdx = ranges[i].StartSegmentIdx
			}
			ranges[i+1] = next
			meta[i+1] = mergeMeta(meta[i], meta[i+1])
			// Don't append outR/outM for i — it's been folded forward.
			continue
		}
		// Last chapter: merge backward into the most recent appended one.
		if len(outR) == 0 {
			// All earlier chapters were undersize and got folded forward;
			// fall through and just emit this one (degenerate but safe).
			outR = append(outR, ranges[i])
			outM = append(outM, meta[i])
			continue
		}
		last := outR[len(outR)-1]
		last.EndMs = ranges[i].EndMs
		if ranges[i].EndSegmentIdx >= 0 {
			last.EndSegmentIdx = ranges[i].EndSegmentIdx
		}
		outR[len(outR)-1] = last
		outM[len(outM)-1] = mergeMeta(outM[len(outM)-1], meta[i])
	}
	return outR, outM
}

func mergeMeta(a, b LLMChapterMeta) LLMChapterMeta {
	join := func(x, y string) string {
		switch {
		case x == "":
			return y
		case y == "":
			return x
		default:
			return x + " • " + y
		}
	}
	return LLMChapterMeta{
		TitleSource:     join(a.TitleSource, b.TitleSource),
		TitleTranslated: join(a.TitleTranslated, b.TitleTranslated),
		SummaryMD:       join(a.SummaryMD, b.SummaryMD),
	}
}

func splitLongChapters(
	ranges []ChapterRange,
	meta []LLMChapterMeta,
	segments []Segment,
	hardMaxMs, minSilenceGapMs int64,
	maxDepth int,
) ([]ChapterRange, []LLMChapterMeta) {
	if maxDepth <= 0 {
		return ranges, meta
	}
	for i := 0; i < len(ranges); i++ {
		dur := ranges[i].EndMs - ranges[i].StartMs
		if dur <= hardMaxMs {
			continue
		}
		// Find midpoint, snap to nearest in-range silence.
		mid := (ranges[i].StartMs + ranges[i].EndMs) / 2
		cutMs := snapToNearestSilence(segments, mid, ranges[i].StartMs, ranges[i].EndMs, minSilenceGapMs)
		if cutMs <= ranges[i].StartMs || cutMs >= ranges[i].EndMs {
			// No usable silence inside the chapter — accept it as-is.
			// Better an oversize chapter than a cut at an arbitrary
			// non-silence position. The dashboard alert (operator-side)
			// is the right place to surface this rare case.
			continue
		}
		// Find the segment-index split corresponding to cutMs.
		splitSegIdx := segmentIdxAtMs(segments, cutMs, ranges[i].StartSegmentIdx, ranges[i].EndSegmentIdx)
		// Build the two halves.
		left := ChapterRange{
			Ordinal:         ranges[i].Ordinal, // renumbered later
			StartMs:         ranges[i].StartMs,
			EndMs:           cutMs,
			StartSegmentIdx: ranges[i].StartSegmentIdx,
			EndSegmentIdx:   splitSegIdx,
		}
		right := ChapterRange{
			Ordinal:         ranges[i].Ordinal + 1,
			StartMs:         cutMs,
			EndMs:           ranges[i].EndMs,
			StartSegmentIdx: splitSegIdx + 1,
			EndSegmentIdx:   ranges[i].EndSegmentIdx,
		}
		leftMeta := meta[i]
		rightMeta := LLMChapterMeta{
			TitleSource:     contTitle(meta[i].TitleSource),
			TitleTranslated: contTitle(meta[i].TitleTranslated),
			SummaryMD:       meta[i].SummaryMD, // duplicated; UI / chapters.json can dedupe
		}
		// Splice [left, right] in place of ranges[i].
		newR := make([]ChapterRange, 0, len(ranges)+1)
		newR = append(newR, ranges[:i]...)
		newR = append(newR, left, right)
		newR = append(newR, ranges[i+1:]...)
		newM := make([]LLMChapterMeta, 0, len(meta)+1)
		newM = append(newM, meta[:i]...)
		newM = append(newM, leftMeta, rightMeta)
		newM = append(newM, meta[i+1:]...)
		// Recurse to handle the case where one half is still too long.
		return splitLongChapters(newR, newM, segments, hardMaxMs, minSilenceGapMs, maxDepth-1)
	}
	return ranges, meta
}

func contTitle(t string) string {
	if t == "" {
		return ""
	}
	return t + " (cont.)"
}

// snapToNearestSilence finds the silence gap whose MIDPOINT is closest
// to targetMs while staying strictly inside (windowStartMs, windowEndMs)
// AND whose width >= minGapMs. Returns 0 if no gap qualifies — caller
// treats that as "do not split".
func snapToNearestSilence(
	segments []Segment,
	targetMs, windowStartMs, windowEndMs int64,
	minGapMs int64,
) int64 {
	bestCut := int64(0)
	bestDist := int64(-1)
	for j := 0; j < len(segments)-1; j++ {
		gap := segments[j+1].StartMs - segments[j].EndMs
		if gap < minGapMs {
			continue
		}
		mid := (segments[j].EndMs + segments[j+1].StartMs) / 2
		if mid <= windowStartMs || mid >= windowEndMs {
			continue
		}
		d := mid - targetMs
		if d < 0 {
			d = -d
		}
		if bestDist < 0 || d < bestDist {
			bestDist = d
			bestCut = mid
		}
	}
	return bestCut
}

// segmentIdxAtMs returns the index of the LAST segment whose midpoint is
// <= cutMs, restricted to [lo, hi]. Used by splitLongChapters to derive
// the new chapter's EndSegmentIdx after a midpoint split. Falls back to
// (lo + hi) / 2 if nothing matches (degenerate; only happens when every
// segment in range starts after cutMs, which our snap caller prevents).
func segmentIdxAtMs(segments []Segment, cutMs int64, lo, hi int) int {
	if hi >= len(segments) {
		hi = len(segments) - 1
	}
	if lo < 0 {
		lo = 0
	}
	if lo > hi {
		return lo
	}
	// Binary search would be neater but lo..hi is typically <100; linear
	// scan keeps the code obviously correct.
	last := lo
	for j := lo; j <= hi; j++ {
		mid := (segments[j].StartMs + segments[j].EndMs) / 2
		if mid <= cutMs {
			last = j
		} else {
			break
		}
	}
	return last
}

// SortLLMPlan returns a copy of the LLM plan sorted by StartSegmentIdx.
// Some providers return chapters in a non-monotonic order under load;
// sorting before validation makes the rest of the pipeline simpler.
func SortLLMPlan(plan []LLMChapter) []LLMChapter {
	out := make([]LLMChapter, len(plan))
	copy(out, plan)
	sort.Slice(out, func(i, j int) bool {
		return out[i].StartSegmentIdx < out[j].StartSegmentIdx
	})
	return out
}
