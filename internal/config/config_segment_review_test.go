package config

import (
	"os"
	"testing"
)

var segmentReviewKnobsAndDefaults = []struct {
	envVar      string
	get         func(Config) any
	wantDefault any
}{
	{"SEGMENT_REVIEW_ENABLED", func(c Config) any { return c.SegmentReviewEnabled }, true},
	{"SEGMENT_REVIEW_USE_TOOLS", func(c Config) any { return c.SegmentReviewUseTools }, true},
	{"SEGMENT_REVIEW_REQUIRE_HUMAN", func(c Config) any { return c.SegmentReviewRequireHuman }, false},
	{"SEGMENT_REVIEW_AUTO_APPLY", func(c Config) any { return c.SegmentReviewAutoApply }, true},
	{"SEGMENT_REVIEW_AUTO_APPLY_MIN_CONFIDENCE", func(c Config) any { return c.SegmentReviewAutoApplyMinConfidence }, 0.82},
	{"SEGMENT_REVIEW_MAX_MERGE_GAP_MS", func(c Config) any { return c.SegmentReviewMaxMergeGapMs }, int64(2000)},
	{"SEGMENT_REVIEW_MAX_MERGED_DURATION_MS", func(c Config) any { return c.SegmentReviewMaxMergedDurationMs }, int64(20000)},
	{"SEGMENT_REVIEW_SHORT_SEGMENT_MS", func(c Config) any { return c.SegmentReviewShortSegmentMs }, int64(1000)},
	{"SEGMENT_REVIEW_MAX_MERGE_SEGMENTS", func(c Config) any { return c.SegmentReviewMaxMergeSegments }, 4},
}

func resetSegmentReviewKnobs(t *testing.T) {
	t.Helper()
	for _, k := range segmentReviewKnobsAndDefaults {
		_ = os.Unsetenv(k.envVar)
	}
}

func TestLoad_SegmentReviewKnobs_Defaults(t *testing.T) {
	resetSegmentReviewKnobs(t)
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load returned error with no env set: %v", err)
	}
	for _, k := range segmentReviewKnobsAndDefaults {
		got := k.get(cfg)
		if got != k.wantDefault {
			t.Errorf("%s default: got %v (%T), want %v (%T)",
				k.envVar, got, got, k.wantDefault, k.wantDefault)
		}
	}
}

func TestLoad_SegmentReviewKnobs_OverrideRoundTrip(t *testing.T) {
	cases := []struct {
		envVar string
		setTo  string
		check  func(Config) bool
	}{
		{"SEGMENT_REVIEW_USE_TOOLS", "false", func(c Config) bool { return !c.SegmentReviewUseTools }},
		{"SEGMENT_REVIEW_REQUIRE_HUMAN", "true", func(c Config) bool { return c.SegmentReviewRequireHuman }},
		{"SEGMENT_REVIEW_AUTO_APPLY", "false", func(c Config) bool { return !c.SegmentReviewAutoApply }},
		{"SEGMENT_REVIEW_AUTO_APPLY_MIN_CONFIDENCE", "0.91", func(c Config) bool { return c.SegmentReviewAutoApplyMinConfidence == 0.91 }},
		{"SEGMENT_REVIEW_MAX_MERGE_GAP_MS", "1500", func(c Config) bool { return c.SegmentReviewMaxMergeGapMs == 1500 }},
		{"SEGMENT_REVIEW_MAX_MERGED_DURATION_MS", "17000", func(c Config) bool { return c.SegmentReviewMaxMergedDurationMs == 17000 }},
		{"SEGMENT_REVIEW_SHORT_SEGMENT_MS", "750", func(c Config) bool { return c.SegmentReviewShortSegmentMs == 750 }},
		{"SEGMENT_REVIEW_MAX_MERGE_SEGMENTS", "3", func(c Config) bool { return c.SegmentReviewMaxMergeSegments == 3 }},
	}
	for _, tc := range cases {
		t.Run(tc.envVar, func(t *testing.T) {
			resetSegmentReviewKnobs(t)
			t.Setenv(tc.envVar, tc.setTo)
			cfg, err := Load()
			if err != nil {
				t.Fatalf("Load with %s=%s returned error: %v", tc.envVar, tc.setTo, err)
			}
			if !tc.check(cfg) {
				t.Fatalf("%s override did not flow through", tc.envVar)
			}
		})
	}
}

func TestLoad_SegmentReviewKnobs_BadInputs(t *testing.T) {
	resetSegmentReviewKnobs(t)
	t.Setenv("SEGMENT_REVIEW_AUTO_APPLY_MIN_CONFIDENCE", "82")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject confidence outside [0,1]")
	}

	resetSegmentReviewKnobs(t)
	t.Setenv("SEGMENT_REVIEW_MAX_MERGE_SEGMENTS", "1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject max merge segments < 2")
	}
}
