// OPT-403/404 config knob coverage. Lives in its own _test.go to keep the
// table tests grouped under a stable filename when other config tests
// land later. Each test case sets / unsets one env var, calls Load(),
// and asserts the parsed value. Defaults are also exercised via a
// "no env" baseline so a future refactor that renames an env var (or
// drops a getEnv* call) breaks loudly.
package config

import (
	"os"
	"testing"
)

// chapterizeKnobsAndDefaults is the source of truth for "what value does
// each OPT-403/404 knob land on when nothing is set". Keep in sync with
// config.go and .env.example. If a default changes, update both this
// table AND the .env.example doc string.
var chapterizeKnobsAndDefaults = []struct {
	envVar      string
	get         func(Config) any
	wantDefault any
}{
	{"CHAPTERIZE_ENABLED", func(c Config) any { return c.ChapterizeEnabled }, true},
	{"CHAPTERIZE_MIN_CHAPTER_MS", func(c Config) any { return c.ChapterizeMinChapterMs }, int64(18 * 60 * 1000)},
	{"CHAPTERIZE_TARGET_CHAPTER_MS", func(c Config) any { return c.ChapterizeTargetChapterMs }, int64(22 * 60 * 1000)},
	{"CHAPTERIZE_MAX_CHAPTER_MS", func(c Config) any { return c.ChapterizeMaxChapterMs }, int64(30 * 60 * 1000)},
	{"CHAPTERIZE_MIN_SILENCE_GAP_MS", func(c Config) any { return c.ChapterizeMinSilenceGapMs }, int64(800)},
	{"CHAPTER_REVIEW_LLM_ENABLED", func(c Config) any { return c.ChapterReviewLLMEnabled }, true},
	{"CHAPTER_REVIEW_MODEL", func(c Config) any { return c.ChapterReviewModel }, ""},
	{"LOUDNORM_TARGET_I", func(c Config) any { return c.LoudnormTargetI }, -23.0},
	{"LOUDNORM_TARGET_TP", func(c Config) any { return c.LoudnormTargetTP }, -1.0},
	{"LOUDNORM_TARGET_LRA", func(c Config) any { return c.LoudnormTargetLRA }, 7.0},
	{"LOUDNORM_CHAPTER_ENABLED", func(c Config) any { return c.LoudnormChapterEnabled }, true},
	{"LOUDNORM_MASTER_ENABLED", func(c Config) any { return c.LoudnormMasterEnabled }, true},
	{"EPISODE_MERGE_ENABLED", func(c Config) any { return c.EpisodeMergeEnabled }, true},
}

// resetChapterizeKnobs unsets every env var in the table so each test gets
// a clean "no env" baseline regardless of what the developer's shell or
// CI happens to have set.
func resetChapterizeKnobs(t *testing.T) {
	t.Helper()
	for _, k := range chapterizeKnobsAndDefaults {
		_ = os.Unsetenv(k.envVar)
	}
}

func TestLoad_ChapterizeKnobs_Defaults(t *testing.T) {
	resetChapterizeKnobs(t)
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load returned error with no env set: %v", err)
	}
	for _, k := range chapterizeKnobsAndDefaults {
		got := k.get(cfg)
		if got != k.wantDefault {
			t.Errorf("%s default: got %v (%T), want %v (%T)",
				k.envVar, got, got, k.wantDefault, k.wantDefault)
		}
	}
}

// TestLoad_ChapterizeKnobs_OverrideRoundTrip exercises every knob with a
// non-default value to ensure the env var actually flows through into
// Config (vs. silently being ignored after a typo / refactor).
func TestLoad_ChapterizeKnobs_OverrideRoundTrip(t *testing.T) {
	cases := []struct {
		envVar string
		setTo  string
		check  func(c Config) bool
		desc   string
	}{
		{"CHAPTERIZE_ENABLED", "false", func(c Config) bool { return !c.ChapterizeEnabled },
			"ChapterizeEnabled should be false"},
		{"CHAPTERIZE_MIN_CHAPTER_MS", "600000", func(c Config) bool { return c.ChapterizeMinChapterMs == 600000 },
			"ChapterizeMinChapterMs should be 600000"},
		{"CHAPTERIZE_TARGET_CHAPTER_MS", "900000", func(c Config) bool { return c.ChapterizeTargetChapterMs == 900000 },
			"ChapterizeTargetChapterMs should be 900000"},
		{"CHAPTERIZE_MAX_CHAPTER_MS", "1500000", func(c Config) bool { return c.ChapterizeMaxChapterMs == 1500000 },
			"ChapterizeMaxChapterMs should be 1500000"},
		{"CHAPTERIZE_MIN_SILENCE_GAP_MS", "1500", func(c Config) bool { return c.ChapterizeMinSilenceGapMs == 1500 },
			"ChapterizeMinSilenceGapMs should be 1500"},
		{"CHAPTER_REVIEW_LLM_ENABLED", "false", func(c Config) bool { return !c.ChapterReviewLLMEnabled },
			"ChapterReviewLLMEnabled should be false"},
		{"CHAPTER_REVIEW_MODEL", "qwen-turbo", func(c Config) bool { return c.ChapterReviewModel == "qwen-turbo" },
			"ChapterReviewModel should be qwen-turbo"},
		{"LOUDNORM_TARGET_I", "-16.0", func(c Config) bool { return c.LoudnormTargetI == -16.0 },
			"LoudnormTargetI should be -16.0"},
		{"LOUDNORM_TARGET_TP", "-2.0", func(c Config) bool { return c.LoudnormTargetTP == -2.0 },
			"LoudnormTargetTP should be -2.0"},
		{"LOUDNORM_TARGET_LRA", "11.0", func(c Config) bool { return c.LoudnormTargetLRA == 11.0 },
			"LoudnormTargetLRA should be 11.0"},
		{"LOUDNORM_CHAPTER_ENABLED", "false", func(c Config) bool { return !c.LoudnormChapterEnabled },
			"LoudnormChapterEnabled should be false"},
		{"LOUDNORM_MASTER_ENABLED", "false", func(c Config) bool { return !c.LoudnormMasterEnabled },
			"LoudnormMasterEnabled should be false"},
		{"EPISODE_MERGE_ENABLED", "false", func(c Config) bool { return !c.EpisodeMergeEnabled },
			"EpisodeMergeEnabled should be false"},
	}
	for _, tc := range cases {
		t.Run(tc.envVar, func(t *testing.T) {
			resetChapterizeKnobs(t)
			t.Setenv(tc.envVar, tc.setTo)
			cfg, err := Load()
			if err != nil {
				t.Fatalf("Load with %s=%s returned error: %v", tc.envVar, tc.setTo, err)
			}
			if !tc.check(cfg) {
				t.Errorf("%s override did not flow through; %s", tc.envVar, tc.desc)
			}
		})
	}
}

// TestLoad_ChapterizeKnobs_BadInputs guards against silently-defaulted bad
// env values. CHAPTERIZE_MAX_CHAPTER_MS=not_an_int should return error,
// not silently fall back to the default — that masks operator typos.
func TestLoad_ChapterizeKnobs_BadInputs(t *testing.T) {
	resetChapterizeKnobs(t)
	t.Setenv("CHAPTERIZE_MAX_CHAPTER_MS", "not-an-int")
	if _, err := Load(); err == nil {
		t.Fatal("Load should return error when CHAPTERIZE_MAX_CHAPTER_MS is non-numeric")
	}
}
