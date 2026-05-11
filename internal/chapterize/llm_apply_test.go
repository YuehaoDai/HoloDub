package chapterize

import (
	"strings"
	"testing"
)

// segmentRun produces n synthetic segments each `dur` ms long with `gap`
// ms silence between them. Useful for predictable boundary-snap tests
// where you want to know exactly where the silences are.
func segmentRun(n int, durMs, gapMs int64) []Segment {
	out := make([]Segment, n)
	cursor := int64(0)
	for i := 0; i < n; i++ {
		out[i] = Segment{StartMs: cursor, EndMs: cursor + durMs}
		cursor += durMs + gapMs
	}
	return out
}

func TestValidateLLMPlan_HappyPath(t *testing.T) {
	plan := []LLMChapter{
		{StartSegmentIdx: 0, EndSegmentIdx: 4, TitleSource: "A"},
		{StartSegmentIdx: 5, EndSegmentIdx: 9, TitleSource: "B"},
	}
	if err := ValidateLLMPlan(plan, 10); err != nil {
		t.Fatalf("happy path should validate; got %v", err)
	}
}

func TestValidateLLMPlan_RejectsBreaches(t *testing.T) {
	cases := []struct {
		name    string
		plan    []LLMChapter
		nSegs   int
		wantSub string
	}{
		{"empty", nil, 10, "empty"},
		{"zero segments", []LLMChapter{{0, 0, "", "", ""}}, 0, "positive"},
		{"negative", []LLMChapter{{-1, 5, "", "", ""}}, 10, "negative"},
		{"overshoot", []LLMChapter{{0, 99, "", "", ""}}, 10, ">="},
		{"start>end", []LLMChapter{{5, 3, "", "", ""}}, 10, "start"},
		{"first chapter not at 0",
			[]LLMChapter{{1, 5, "", "", ""}, {6, 9, "", "", ""}},
			10, "must start at 0"},
		{"last chapter not at n-1",
			[]LLMChapter{{0, 4, "", "", ""}, {5, 7, "", "", ""}},
			10, "must end at 9"},
		{"gap between chapters",
			[]LLMChapter{{0, 4, "", "", ""}, {6, 9, "", "", ""}},
			10, "gap or overlap"},
		{"overlap between chapters",
			[]LLMChapter{{0, 5, "", "", ""}, {5, 9, "", "", ""}},
			10, "gap or overlap"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateLLMPlan(tc.plan, tc.nSegs)
			if err == nil {
				t.Fatalf("want error mentioning %q, got nil", tc.wantSub)
			}
			if !strings.Contains(err.Error(), tc.wantSub) {
				t.Fatalf("want error containing %q, got %q", tc.wantSub, err.Error())
			}
		})
	}
}

func TestSnapBoundariesToSilences_PrefersLongestNearbySilence(t *testing.T) {
	// Build 10 segments. Most have 200ms gaps (below threshold), but
	// segment 4→5 has a 1500ms gap (the natural pause). LLM said cut
	// after segment 5; snap should find the bigger gap at 4→5 and use
	// its midpoint instead.
	segs := []Segment{
		{StartMs: 0, EndMs: 4000},
		{StartMs: 4200, EndMs: 8200},
		{StartMs: 8400, EndMs: 12400},
		{StartMs: 12600, EndMs: 16600},
		{StartMs: 16800, EndMs: 20800},
		{StartMs: 22300, EndMs: 26300}, // 1500ms gap before this one
		{StartMs: 26500, EndMs: 30500},
		{StartMs: 30700, EndMs: 34700},
		{StartMs: 34900, EndMs: 38900},
		{StartMs: 39100, EndMs: 43100},
	}
	plan := []LLMChapter{
		{StartSegmentIdx: 0, EndSegmentIdx: 5},
		{StartSegmentIdx: 6, EndSegmentIdx: 9},
	}
	cuts := SnapBoundariesToSilences(segs, plan, 43100, 800, 3)
	// Last entry is always totalDurationMs.
	if cuts[1] != 43100 {
		t.Fatalf("last cut should be totalDurationMs, got %d", cuts[1])
	}
	// First cut should be the midpoint of the 4→5 silence (20800, 22300).
	wantCut := int64((20800 + 22300) / 2)
	if cuts[0] != wantCut {
		t.Fatalf("snap should prefer the 1500ms gap; want %d, got %d", wantCut, cuts[0])
	}
}

func TestSnapBoundariesToSilences_FallsBackToLiteralEnd_NoQualifyingGap(t *testing.T) {
	// All gaps are 100ms (below the 800ms minGap threshold). Snap should
	// keep the LLM's literal segments[end].EndMs as the cut.
	segs := segmentRun(10, 4000, 100)
	plan := []LLMChapter{
		{StartSegmentIdx: 0, EndSegmentIdx: 4},
		{StartSegmentIdx: 5, EndSegmentIdx: 9},
	}
	totalMs := segs[9].EndMs
	cuts := SnapBoundariesToSilences(segs, plan, totalMs, 800, 3)
	if cuts[0] != segs[4].EndMs {
		t.Fatalf("with no qualifying silence, cut should fall back to seg[4].EndMs (%d), got %d",
			segs[4].EndMs, cuts[0])
	}
	if cuts[1] != totalMs {
		t.Fatalf("last cut should be totalDurationMs, got %d", cuts[1])
	}
}

func TestEnforceHardConstraints_MergesUndersize(t *testing.T) {
	segs := segmentRun(20, 30000, 1000) // 30s segments, 1s gaps
	// Three chapters: [0..4] = ~155s, [5..6] = ~62s (UNDERSIZE), [7..19] = ~390s
	ranges := []ChapterRange{
		{Ordinal: 1, StartMs: 0, EndMs: 155000, StartSegmentIdx: 0, EndSegmentIdx: 4},
		{Ordinal: 2, StartMs: 155000, EndMs: 217000, StartSegmentIdx: 5, EndSegmentIdx: 6},
		{Ordinal: 3, StartMs: 217000, EndMs: 619000, StartSegmentIdx: 7, EndSegmentIdx: 19},
	}
	meta := []LLMChapterMeta{
		{TitleSource: "Intro", TitleTranslated: "引言"},
		{TitleSource: "Tiny", TitleTranslated: "短"},
		{TitleSource: "Body", TitleTranslated: "正文"},
	}
	// hardMin = 90s — chapter 2 (62s) should merge into chapter 3.
	gotR, gotM := EnforceHardConstraints(ranges, meta, segs, 90000, 1_000_000, 800, 3)
	if len(gotR) != 2 {
		t.Fatalf("want 2 chapters after merge, got %d: %+v", len(gotR), gotR)
	}
	if gotR[0].EndMs != 155000 {
		t.Errorf("intro chapter unchanged; got end=%d", gotR[0].EndMs)
	}
	if gotR[1].StartMs != 155000 || gotR[1].EndMs != 619000 {
		t.Errorf("body chapter should absorb tiny; got [%d, %d]", gotR[1].StartMs, gotR[1].EndMs)
	}
	if !strings.Contains(gotM[1].TitleSource, "Tiny") || !strings.Contains(gotM[1].TitleSource, "Body") {
		t.Errorf("merged title should preserve both: %q", gotM[1].TitleSource)
	}
	if gotM[1].TitleTranslated != "短 • 正文" {
		t.Errorf("merged translated title shape: %q", gotM[1].TitleTranslated)
	}
	// Ordinals renumbered.
	if gotR[0].Ordinal != 1 || gotR[1].Ordinal != 2 {
		t.Errorf("ordinals should be 1..N: %v", []int{gotR[0].Ordinal, gotR[1].Ordinal})
	}
}

func TestEnforceHardConstraints_MergesUndersizeLastIntoPredecessor(t *testing.T) {
	segs := segmentRun(20, 30000, 1000)
	// Two chapters; second is undersize and IS the last → merge backward.
	ranges := []ChapterRange{
		{Ordinal: 1, StartMs: 0, EndMs: 400000, StartSegmentIdx: 0, EndSegmentIdx: 12},
		{Ordinal: 2, StartMs: 400000, EndMs: 450000, StartSegmentIdx: 13, EndSegmentIdx: 19},
	}
	meta := []LLMChapterMeta{
		{TitleSource: "Main", TitleTranslated: "主体"},
		{TitleSource: "Outro", TitleTranslated: "结尾"},
	}
	gotR, gotM := EnforceHardConstraints(ranges, meta, segs, 90000, 1_000_000, 800, 3)
	if len(gotR) != 1 {
		t.Fatalf("want 1 chapter (last absorbed), got %d", len(gotR))
	}
	if gotR[0].EndMs != 450000 {
		t.Errorf("merged chapter should extend to absorbed end; got %d", gotR[0].EndMs)
	}
	if gotM[0].TitleTranslated != "主体 • 结尾" {
		t.Errorf("merged title shape: %q", gotM[0].TitleTranslated)
	}
}

func TestEnforceHardConstraints_SplitsOversize(t *testing.T) {
	// 60 segments * 30s = 1800s = 30min. One chapter, hardMax 16min so a
	// single midpoint split (~15min each) brings both halves under cap.
	// Silence at the seam between segment 29 and 30 (3s) gives splitter
	// a target right at midpoint.
	segs := make([]Segment, 60)
	cursor := int64(0)
	for i := 0; i < 60; i++ {
		segs[i] = Segment{StartMs: cursor, EndMs: cursor + 30000}
		gap := int64(200)
		if i == 29 {
			gap = 3000 // big silence at seam
		}
		cursor += 30000 + gap
	}
	totalMs := segs[59].EndMs
	ranges := []ChapterRange{
		{Ordinal: 1, StartMs: 0, EndMs: totalMs, StartSegmentIdx: 0, EndSegmentIdx: 59},
	}
	meta := []LLMChapterMeta{
		{TitleSource: "Whole", TitleTranslated: "全章"},
	}
	hardMax := int64(16 * 60 * 1000)
	gotR, gotM := EnforceHardConstraints(ranges, meta, segs, 60000, hardMax, 800, 3)
	if len(gotR) < 2 {
		t.Fatalf("want >= 2 chapters after split; got %d", len(gotR))
	}
	for i, r := range gotR {
		dur := r.EndMs - r.StartMs
		if dur > hardMax {
			t.Errorf("chapter %d still oversize after split: %d > %d", i+1, dur, hardMax)
		}
	}
	// Right half should carry the (cont.) suffix.
	if !strings.Contains(gotM[1].TitleTranslated, "(cont.)") {
		t.Errorf("split right half should be marked as continuation; got %q", gotM[1].TitleTranslated)
	}
	// Coverage: first chapter starts at 0, last ends at totalMs, and
	// adjacent ranges meet cleanly with no gap or overlap.
	if gotR[0].StartMs != 0 || gotR[len(gotR)-1].EndMs != totalMs {
		t.Errorf("split must preserve coverage; got [%d, %d]", gotR[0].StartMs, gotR[len(gotR)-1].EndMs)
	}
	for i := 1; i < len(gotR); i++ {
		if gotR[i].StartMs != gotR[i-1].EndMs {
			t.Errorf("chapters must be contiguous; ch%d.start=%d, ch%d.end=%d",
				i+1, gotR[i].StartMs, i, gotR[i-1].EndMs)
		}
	}
}

func TestEnforceHardConstraints_SplitsOversize_RecursiveDeep(t *testing.T) {
	// 90min chapter that needs THREE splits to get under a 16min hardMax
	// (90/2=45→needs split; 45/2=22.5→needs split; 22.5/2≈11.25 ok).
	// Distribute silences every 15min so the splitter has good targets.
	segs := make([]Segment, 180) // 30s each, ~90min total
	cursor := int64(0)
	for i := 0; i < 180; i++ {
		segs[i] = Segment{StartMs: cursor, EndMs: cursor + 30000}
		gap := int64(200)
		// Big silence every 30 segments (~15min).
		if i > 0 && i%30 == 29 {
			gap = 3000
		}
		cursor += 30000 + gap
	}
	totalMs := segs[179].EndMs
	ranges := []ChapterRange{
		{Ordinal: 1, StartMs: 0, EndMs: totalMs, StartSegmentIdx: 0, EndSegmentIdx: 179},
	}
	meta := []LLMChapterMeta{
		{TitleSource: "Mega", TitleTranslated: "超长"},
	}
	hardMax := int64(16 * 60 * 1000)
	gotR, _ := EnforceHardConstraints(ranges, meta, segs, 60000, hardMax, 800, 5)
	for i, r := range gotR {
		dur := r.EndMs - r.StartMs
		if dur > hardMax {
			t.Errorf("chapter %d oversize after recursive split: %d > %d", i+1, dur, hardMax)
		}
	}
	if len(gotR) < 4 {
		t.Errorf("90min episode should split into >= 4 chapters with hardMax=16min; got %d", len(gotR))
	}
}

func TestEnforceHardConstraints_KeepsValidChaptersUnchanged(t *testing.T) {
	segs := segmentRun(20, 30000, 1000)
	ranges := []ChapterRange{
		{Ordinal: 1, StartMs: 0, EndMs: 200000, StartSegmentIdx: 0, EndSegmentIdx: 6},
		{Ordinal: 2, StartMs: 200000, EndMs: 400000, StartSegmentIdx: 7, EndSegmentIdx: 12},
		{Ordinal: 3, StartMs: 400000, EndMs: 619000, StartSegmentIdx: 13, EndSegmentIdx: 19},
	}
	meta := []LLMChapterMeta{
		{TitleSource: "A", TitleTranslated: "A"},
		{TitleSource: "B", TitleTranslated: "B"},
		{TitleSource: "C", TitleTranslated: "C"},
	}
	gotR, gotM := EnforceHardConstraints(ranges, meta, segs, 90000, 1_000_000, 800, 3)
	if len(gotR) != 3 {
		t.Fatalf("want unchanged 3 chapters; got %d", len(gotR))
	}
	for i, r := range gotR {
		if r.Ordinal != i+1 {
			t.Errorf("ordinal %d", r.Ordinal)
		}
		if gotM[i].TitleSource != meta[i].TitleSource {
			t.Errorf("title %d should be unchanged", i)
		}
	}
}

func TestSortLLMPlan_OrdersByStart(t *testing.T) {
	plan := []LLMChapter{
		{StartSegmentIdx: 5, EndSegmentIdx: 9},
		{StartSegmentIdx: 0, EndSegmentIdx: 4},
		{StartSegmentIdx: 10, EndSegmentIdx: 14},
	}
	got := SortLLMPlan(plan)
	for i := 0; i < len(got)-1; i++ {
		if got[i].StartSegmentIdx >= got[i+1].StartSegmentIdx {
			t.Fatalf("sort failed at %d: %+v", i, got)
		}
	}
	// Original slice unchanged.
	if plan[0].StartSegmentIdx != 5 {
		t.Errorf("SortLLMPlan must not mutate input")
	}
}

func TestBuildLLMChapterRanges_Shape(t *testing.T) {
	plan := []LLMChapter{
		{StartSegmentIdx: 0, EndSegmentIdx: 4},
		{StartSegmentIdx: 5, EndSegmentIdx: 9},
		{StartSegmentIdx: 10, EndSegmentIdx: 14},
	}
	cuts := []int64{300_000, 600_000, 900_000}
	got := BuildLLMChapterRanges(plan, cuts)
	if len(got) != 3 {
		t.Fatalf("want 3 ranges, got %d", len(got))
	}
	if got[0].StartMs != 0 || got[0].EndMs != 300_000 {
		t.Errorf("first range %+v", got[0])
	}
	if got[1].StartMs != 300_000 || got[1].EndMs != 600_000 {
		t.Errorf("second range %+v", got[1])
	}
	if got[2].StartMs != 600_000 || got[2].EndMs != 900_000 {
		t.Errorf("third range %+v", got[2])
	}
	if got[2].EndSegmentIdx != 14 {
		t.Errorf("end segment idx %d", got[2].EndSegmentIdx)
	}
}
