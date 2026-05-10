package chapterize

import (
	"reflect"
	"testing"
)

// helper: build n synthetic ASR segments each `lengthMs` long, separated by
// `gapMs` of silence. start at 0.
func makeSegments(n int, lengthMs, gapMs int64) []Segment {
	out := make([]Segment, 0, n)
	cursor := int64(0)
	for i := 0; i < n; i++ {
		out = append(out, Segment{StartMs: cursor, EndMs: cursor + lengthMs})
		cursor += lengthMs + gapMs
	}
	return out
}

// ── ExtractCandidates ───────────────────────────────────────────────────────

func TestExtractCandidates_TooFewSegments(t *testing.T) {
	if got := ExtractCandidates(nil, 800); got != nil {
		t.Fatalf("nil input → nil; got %v", got)
	}
	if got := ExtractCandidates([]Segment{{StartMs: 0, EndMs: 1000}}, 800); got != nil {
		t.Fatalf("single segment → nil; got %v", got)
	}
}

func TestExtractCandidates_GapsBelowThresholdSkipped(t *testing.T) {
	segs := []Segment{
		{StartMs: 0, EndMs: 1000},
		{StartMs: 1100, EndMs: 2000}, // gap 100ms — too small
		{StartMs: 3000, EndMs: 4000}, // gap 1000ms — kept
		{StartMs: 4500, EndMs: 5000}, // gap 500ms — too small
	}
	got := ExtractCandidates(segs, 800)
	if len(got) != 1 {
		t.Fatalf("want 1 candidate; got %d (%+v)", len(got), got)
	}
	if got[0].AfterSegmentIdx != 1 {
		t.Errorf("AfterSegmentIdx: want 1, got %d", got[0].AfterSegmentIdx)
	}
	if got[0].BoundaryMs != 2500 {
		t.Errorf("BoundaryMs: want midpoint 2500, got %d", got[0].BoundaryMs)
	}
	if got[0].SilenceGapMs != 1000 {
		t.Errorf("SilenceGapMs: want 1000, got %d", got[0].SilenceGapMs)
	}
}

func TestExtractCandidates_NegativeGapIgnored(t *testing.T) {
	// Overlapping segments (e.g. ASR jitter) → negative gap → never a candidate.
	segs := []Segment{
		{StartMs: 0, EndMs: 2000},
		{StartMs: 1500, EndMs: 3000}, // overlap by 500ms
		{StartMs: 5000, EndMs: 6000}, // gap 2000ms — kept
	}
	got := ExtractCandidates(segs, 800)
	if len(got) != 1 || got[0].AfterSegmentIdx != 1 {
		t.Fatalf("want only the seg1→seg2 boundary; got %+v", got)
	}
}

// ── DPOptimalCuts ───────────────────────────────────────────────────────────

func TestDPOptimalCuts_ShortVideoNoSplit(t *testing.T) {
	// 10min total, max 30min → no split needed.
	cands := []CandidateBoundary{
		{AfterSegmentIdx: 5, BoundaryMs: 5 * 60 * 1000, SilenceGapMs: 1500},
	}
	if got := DPOptimalCuts(cands, 10*60*1000, 22*60*1000, 18*60*1000, 30*60*1000); got != nil {
		t.Fatalf("short video should return nil; got %+v", got)
	}
}

func TestDPOptimalCuts_NoCandidatesNoSplit(t *testing.T) {
	if got := DPOptimalCuts(nil, 60*60*1000, 22*60*1000, 18*60*1000, 30*60*1000); got != nil {
		t.Fatalf("no candidates → nil; got %+v", got)
	}
}

func TestDPOptimalCuts_TwoChapterEvenSplit(t *testing.T) {
	// 40-minute video, candidates every 5 minutes; target 20min ⇒ ideally split
	// near 20min mark into two ~20min chapters.
	cands := make([]CandidateBoundary, 0, 7)
	for i := int64(1); i <= 7; i++ {
		cands = append(cands, CandidateBoundary{
			AfterSegmentIdx: int(i),
			BoundaryMs:      i * 5 * 60 * 1000,
			SilenceGapMs:    1500,
		})
	}
	chosen := DPOptimalCuts(cands, 40*60*1000, 20*60*1000, 15*60*1000, 25*60*1000)
	if len(chosen) != 1 {
		t.Fatalf("want 1 cut → 2 chapters; got %d cuts (%+v)", len(chosen), chosen)
	}
	// The cut should be near 20min; allow ±5 min slack.
	off := chosen[0].BoundaryMs - 20*60*1000
	if off < 0 {
		off = -off
	}
	if off > 5*60*1000 {
		t.Errorf("cut at %dms drifted >5min from target 1200000ms", chosen[0].BoundaryMs)
	}
}

func TestDPOptimalCuts_LongVideoMultipleChapters(t *testing.T) {
	// 79-minute video, 30 candidates evenly spaced ~2.5min apart.
	// target 22min, range [18,30] → expect 3 or 4 chapters.
	const total = int64(79 * 60 * 1000)
	cands := make([]CandidateBoundary, 0, 30)
	for i := int64(1); i <= 30; i++ {
		cands = append(cands, CandidateBoundary{
			AfterSegmentIdx: int(i),
			BoundaryMs:      i * total / 31,
			SilenceGapMs:    1200 + i*10,
		})
	}
	chosen := DPOptimalCuts(cands, total, 22*60*1000, 18*60*1000, 30*60*1000)
	if len(chosen) < 2 || len(chosen) > 4 {
		t.Fatalf("want 2–4 cuts (3–5 chapters); got %d cuts", len(chosen))
	}
	// Check chapter durations all in [18,30] min.
	prev := int64(0)
	for _, c := range chosen {
		dur := c.BoundaryMs - prev
		if dur < 18*60*1000 || dur > 30*60*1000 {
			t.Errorf("chapter duration %dms out of [18m,30m]", dur)
		}
		prev = c.BoundaryMs
	}
	tail := total - prev
	if tail < 18*60*1000 || tail > 30*60*1000 {
		t.Errorf("tail chapter duration %dms out of [18m,30m]", tail)
	}
}

func TestDPOptimalCuts_PrefersLongerSilence(t *testing.T) {
	// Two candidates near the optimal split point, one with much wider silence —
	// DP should prefer the wider-silence one.
	const total = int64(40 * 60 * 1000)
	cands := []CandidateBoundary{
		{AfterSegmentIdx: 10, BoundaryMs: 19 * 60 * 1000, SilenceGapMs: 800},  // narrow gap
		{AfterSegmentIdx: 11, BoundaryMs: 21 * 60 * 1000, SilenceGapMs: 5000}, // wide gap, slightly off-target
	}
	chosen := DPOptimalCuts(cands, total, 20*60*1000, 15*60*1000, 25*60*1000)
	if len(chosen) != 1 {
		t.Fatalf("want 1 cut; got %d", len(chosen))
	}
	if chosen[0].SilenceGapMs != 5000 {
		t.Errorf("DP should prefer wider silence; chose gap %dms", chosen[0].SilenceGapMs)
	}
}

func TestDPOptimalCuts_NoValidSubdivisionFallsBackToNil(t *testing.T) {
	// 60-minute video but min=25 max=30, candidate at 10 minutes only — there's
	// no way to fit two chapters whose first hits min=25min while only allowing
	// the 10-min cut. Expect nil → caller short-circuits to 1 chapter even
	// though totalDurationMs > maxMs (caller will log warn).
	cands := []CandidateBoundary{
		{AfterSegmentIdx: 1, BoundaryMs: 10 * 60 * 1000, SilenceGapMs: 1500},
	}
	got := DPOptimalCuts(cands, 60*60*1000, 22*60*1000, 25*60*1000, 30*60*1000)
	if got != nil {
		t.Fatalf("no valid subdivision → nil; got %+v", got)
	}
}

// ── BuildChapterRanges ──────────────────────────────────────────────────────

func TestBuildChapterRanges_SingleChapterCoversFullEpisode(t *testing.T) {
	segs := makeSegments(100, 5000, 200) // ~520s total
	got := BuildChapterRanges(segs, nil, 600*1000)
	want := []ChapterRange{{
		Ordinal:         1,
		StartMs:         0,
		EndMs:           600 * 1000,
		StartSegmentIdx: 0,
		EndSegmentIdx:   99,
	}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("BuildChapterRanges(no boundaries) mismatch:\n got %+v\nwant %+v", got, want)
	}
}

func TestBuildChapterRanges_EmptySegmentsHandled(t *testing.T) {
	got := BuildChapterRanges(nil, nil, 1000)
	if len(got) != 1 || got[0].StartSegmentIdx != -1 || got[0].EndSegmentIdx != -1 {
		t.Fatalf("empty segments: want 1 chapter with seg idx -1; got %+v", got)
	}
}

func TestBuildChapterRanges_TwoCutsThreeChapters(t *testing.T) {
	segs := makeSegments(30, 5000, 200) // ~155s total
	bounds := []CandidateBoundary{
		{AfterSegmentIdx: 9, BoundaryMs: 50000, SilenceGapMs: 1500},
		{AfterSegmentIdx: 19, BoundaryMs: 100000, SilenceGapMs: 1800},
	}
	got := BuildChapterRanges(segs, bounds, 155*1000)
	if len(got) != 3 {
		t.Fatalf("want 3 chapters from 2 cuts; got %d (%+v)", len(got), got)
	}
	// Chapter 1: [0, 50000), segments [0..9], left silence 0, right 1500.
	if got[0].Ordinal != 1 || got[0].StartMs != 0 || got[0].EndMs != 50000 ||
		got[0].StartSegmentIdx != 0 || got[0].EndSegmentIdx != 9 ||
		got[0].StartCutSilenceMs != 0 || got[0].EndCutSilenceMs != 1500 {
		t.Errorf("chapter 1 fields wrong: %+v", got[0])
	}
	// Chapter 2: [50000, 100000), segments [10..19], silences (1500, 1800).
	if got[1].Ordinal != 2 || got[1].StartMs != 50000 || got[1].EndMs != 100000 ||
		got[1].StartSegmentIdx != 10 || got[1].EndSegmentIdx != 19 ||
		got[1].StartCutSilenceMs != 1500 || got[1].EndCutSilenceMs != 1800 {
		t.Errorf("chapter 2 fields wrong: %+v", got[1])
	}
	// Chapter 3: [100000, 155000), segments [20..29], silences (1800, 0).
	if got[2].Ordinal != 3 || got[2].StartMs != 100000 || got[2].EndMs != 155*1000 ||
		got[2].StartSegmentIdx != 20 || got[2].EndSegmentIdx != 29 ||
		got[2].StartCutSilenceMs != 1800 || got[2].EndCutSilenceMs != 0 {
		t.Errorf("chapter 3 fields wrong: %+v", got[2])
	}
}

// ── Helpers ─────────────────────────────────────────────────────────────────

func TestMeanChapterDurationAndMaxAbsDeviation(t *testing.T) {
	ranges := []ChapterRange{
		{StartMs: 0, EndMs: 20 * 60 * 1000},
		{StartMs: 20 * 60 * 1000, EndMs: 45 * 60 * 1000},
		{StartMs: 45 * 60 * 1000, EndMs: 60 * 60 * 1000},
	}
	mean := MeanChapterDuration(ranges)
	if mean != 20*60*1000 {
		t.Errorf("mean: want 1.2e6 ms; got %f", mean)
	}
	maxDev := MaxAbsDeviation(ranges, 22*60*1000)
	// Chapters: 20, 25, 15 minutes. Deviations from 22min: 2,3,7 minutes ⇒ max 7min = 420000ms.
	if maxDev != 7*60*1000 {
		t.Errorf("max abs deviation: want 420000 ms; got %d", maxDev)
	}
}

// ── Integration: extract → DP → build ───────────────────────────────────────

func TestPipelineExtractDPBuild_79MinSyntheticEpisode(t *testing.T) {
	// ~80-minute synthetic episode (470 ASR segments × ~10.2s each = 4793.8s,
	// rounded up to 80 min for the wall-clock total). Inject wider silences
	// at three spots so ExtractCandidates has natural break opportunities.
	segs := makeSegments(470, 10000, 200)
	for _, breakIdx := range []int{120, 235, 360} {
		segs[breakIdx].EndMs -= 800 // widen the gap to next segment to ~1s
	}
	cands := ExtractCandidates(segs, 700)
	if len(cands) < 3 {
		t.Fatalf("expected at least 3 candidates from synthetic gaps; got %d", len(cands))
	}
	const total = int64(80 * 60 * 1000) // wall-clock end >= every segment.EndMs
	chosen := DPOptimalCuts(cands, total, 22*60*1000, 18*60*1000, 30*60*1000)
	if len(chosen) < 1 {
		t.Fatalf("expected DP to find valid subdivision for 80min")
	}
	ranges := BuildChapterRanges(segs, chosen, total)
	if len(ranges) != len(chosen)+1 {
		t.Fatalf("ranges: want %d; got %d", len(chosen)+1, len(ranges))
	}
	for i, r := range ranges {
		dur := r.EndMs - r.StartMs
		if dur < 18*60*1000 || dur > 30*60*1000 {
			t.Errorf("ranges[%d] duration %dms out of bounds", i, dur)
		}
	}
}
