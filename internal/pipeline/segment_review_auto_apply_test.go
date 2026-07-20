package pipeline

import (
	"testing"

	"holodub/internal/models"
)

func TestBuildSegmentReviewMergeCandidate_AllowsSafeMerge(t *testing.T) {
	segs := map[uint]models.Segment{
		10: {ID: 10, Ordinal: 0, StartMs: 0, EndMs: 900},
		11: {ID: 11, Ordinal: 1, StartMs: 1000, EndMs: 1800},
	}
	got, ok := buildSegmentReviewMergeCandidate(models.SegmentSuggestion{
		ID:         1,
		Action:     "merge",
		SegmentIDs: []uint{11, 10}, // caller order may be noisy; candidate normalises by ordinal.
		Confidence: 0.93,
	}, segs, 2000, 20000, 4)
	if !ok {
		t.Fatal("safe adjacent merge should be accepted")
	}
	if got.SuggestionID != 1 || got.MinOrdinal != 0 || got.MaxOrdinal != 1 {
		t.Fatalf("candidate metadata wrong: %+v", got)
	}
	if got.SegmentIDs[0] != 10 || got.SegmentIDs[1] != 11 {
		t.Fatalf("segment IDs should be sorted by ordinal, got %v", got.SegmentIDs)
	}
}

func TestBuildSegmentReviewMergeCandidate_RejectsUnsafeMerges(t *testing.T) {
	segs := map[uint]models.Segment{
		10: {ID: 10, Ordinal: 0, StartMs: 0, EndMs: 900},
		11: {ID: 11, Ordinal: 1, StartMs: 4000, EndMs: 5000},
		12: {ID: 12, Ordinal: 3, StartMs: 5100, EndMs: 6000},
		13: {ID: 13, Ordinal: 4, StartMs: 6100, EndMs: 26000},
	}
	cases := []struct {
		name string
		sug  models.SegmentSuggestion
	}{
		{
			name: "gap too large",
			sug:  models.SegmentSuggestion{Action: "merge", SegmentIDs: []uint{10, 11}},
		},
		{
			name: "ordinal gap",
			sug:  models.SegmentSuggestion{Action: "merge", SegmentIDs: []uint{11, 12}},
		},
		{
			name: "duration too long",
			sug:  models.SegmentSuggestion{Action: "merge", SegmentIDs: []uint{12, 13}},
		},
		{
			name: "not merge",
			sug:  models.SegmentSuggestion{Action: "split", SegmentIDs: []uint{10, 11}},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, ok := buildSegmentReviewMergeCandidate(tc.sug, segs, 2000, 20000, 4); ok {
				t.Fatal("unsafe merge should be rejected")
			}
		})
	}
}
