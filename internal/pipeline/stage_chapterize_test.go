package pipeline

import (
	"testing"

	"holodub/internal/chapterize"
	"holodub/internal/llm"
	"holodub/internal/models"
)

func TestChapterSourcePath(t *testing.T) {
	ep := &models.Episode{}
	ep.ID = 138

	cases := []struct {
		ordinal int
		kind    string
		want    string
	}{
		{1, "video", "episodes/138/chapters/source/ch01.mp4"},
		{14, "video", "episodes/138/chapters/source/ch14.mp4"},
		{1, "vocals", "episodes/138/chapters/source/ch01.vocals.wav"},
		{2, "bgm", "episodes/138/chapters/source/ch02.bgm.wav"},
		{1, "unknown", "episodes/138/chapters/source/ch01.bin"},
	}
	for _, tc := range cases {
		got := chapterSourcePath(ep, tc.ordinal, tc.kind)
		if got != tc.want {
			t.Errorf("chapterSourcePath(ord=%d, kind=%q) = %q, want %q",
				tc.ordinal, tc.kind, got, tc.want)
		}
	}
	// Critical correctness: vocals and bgm MUST NOT collide on disk.
	v := chapterSourcePath(ep, 1, "vocals")
	b := chapterSourcePath(ep, 1, "bgm")
	if v == b {
		t.Fatalf("vocals/bgm collide on disk: both paths %q", v)
	}
}

func TestDefaultChapterTitles(t *testing.T) {
	got := defaultChapterTitles(3, "en", "zh")
	if len(got) != 3 {
		t.Fatalf("want 3 titles; got %d", len(got))
	}
	for i, v := range got {
		ord := i + 1
		if v.Ordinal != ord {
			t.Errorf("titles[%d].Ordinal = %d, want %d", i, v.Ordinal, ord)
		}
		if v.Action != "keep" {
			t.Errorf("default action should be 'keep'; got %q", v.Action)
		}
	}
	// Locale check: zh fallback uses "第 N 章".
	if got[0].TitleTranslated != "第 1 章" {
		t.Errorf("zh translated title fallback: got %q", got[0].TitleTranslated)
	}
	if got[2].TitleSource != "Chapter 3" {
		t.Errorf("source title fallback: got %q", got[2].TitleSource)
	}

	if want := "Chapter 1"; localisedDefaultTitle("en", 1) != want {
		t.Errorf("en localised default: got %q, want %q", localisedDefaultTitle("en", 1), want)
	}
	if want := "1장"; localisedDefaultTitle("ko", 1) != want {
		t.Errorf("ko localised default: got %q, want %q", localisedDefaultTitle("ko", 1), want)
	}
}

func TestBuildChapterReviewInputs_PullsOpeningAndClosingSnippets(t *testing.T) {
	ranges := []chapterize.ChapterRange{
		{
			Ordinal:           1,
			StartMs:           0,
			EndMs:             20 * 60 * 1000,
			StartSegmentIdx:   0,
			EndSegmentIdx:     9,
			StartCutSilenceMs: 0,
			EndCutSilenceMs:   1500,
		},
		{
			Ordinal:           2,
			StartMs:           20 * 60 * 1000,
			EndMs:             40 * 60 * 1000,
			StartSegmentIdx:   10,
			EndSegmentIdx:     19,
			StartCutSilenceMs: 1500,
			EndCutSilenceMs:   0,
		},
	}
	segs := make([]models.Segment, 20)
	for i := range segs {
		segs[i].SourceText = "segment text " + string(rune('A'+i))
	}

	got := buildChapterReviewInputs(ranges, segs)
	if len(got) != 2 {
		t.Fatalf("want 2 chapter inputs; got %d", len(got))
	}
	if got[0].Ordinal != 1 || got[1].Ordinal != 2 {
		t.Errorf("ordinals not preserved: %+v", got)
	}
	if len(got[0].OpeningSegments) == 0 {
		t.Errorf("chapter 1 opening segments empty; want >=1 from %d segments", len(segs))
	}
	if got[0].SilenceLeftMs != 0 || got[0].SilenceRightMs != 1500 {
		t.Errorf("chapter 1 silence ms wrong: %+v", got[0])
	}
	if got[1].SilenceLeftMs != 1500 || got[1].SilenceRightMs != 0 {
		t.Errorf("chapter 2 silence ms wrong: %+v", got[1])
	}
}

func TestBuildChapterReviewInputs_HandlesEmptySourceText(t *testing.T) {
	// Empty segments should be skipped from openings/closings without
	// crashing. (Production occasionally has whitespace-only ASR segments.)
	ranges := []chapterize.ChapterRange{
		{Ordinal: 1, StartMs: 0, EndMs: 1000, StartSegmentIdx: 0, EndSegmentIdx: 2},
	}
	segs := []models.Segment{
		{SourceText: "  "},
		{SourceText: "real text"},
		{SourceText: ""},
	}
	got := buildChapterReviewInputs(ranges, segs)
	if len(got) != 1 {
		t.Fatalf("want 1 input; got %d", len(got))
	}
	if len(got[0].OpeningSegments) != 1 || got[0].OpeningSegments[0] != "real text" {
		t.Errorf("opening segments did not skip blanks: %+v", got[0].OpeningSegments)
	}
}

// Sanity: defaultChapterTitles MUST be callable by maybeReviewChapterCuts
// even when ChapterReviewLLMEnabled is false. Compile-time check via
// function-level assignment.
var _ = func() []llm.ChapterReviewVerdict { return defaultChapterTitles(1, "en", "zh") }
