package pipeline

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	episodepkg "holodub/internal/episode"
	"holodub/internal/media"
	"holodub/internal/models"
)

func TestHardlinkOrCopy_EmptyArgsRejected(t *testing.T) {
	if err := hardlinkOrCopy("", "/tmp/dst"); err == nil ||
		!strings.Contains(err.Error(), "empty src/dst") {
		t.Fatalf("want empty src error, got %v", err)
	}
	if err := hardlinkOrCopy("/tmp/src", ""); err == nil ||
		!strings.Contains(err.Error(), "empty src/dst") {
		t.Fatalf("want empty dst error, got %v", err)
	}
}

func TestHardlinkOrCopy_MissingSrcReports(t *testing.T) {
	dir := t.TempDir()
	err := hardlinkOrCopy(filepath.Join(dir, "nope"), filepath.Join(dir, "dst"))
	if err == nil {
		t.Fatal("want error on missing src")
	}
	if !errors.Is(err, os.ErrNotExist) && !strings.Contains(err.Error(), "src") {
		t.Errorf("error should mention src missing; got %v", err)
	}
}

func TestHardlinkOrCopy_HappyPath(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "ch01.mp4")
	dst := filepath.Join(dir, "out", "vp0", "final.mp4")
	body := []byte("synthetic mp4 payload")
	if err := os.WriteFile(src, body, 0o644); err != nil {
		t.Fatalf("write src: %v", err)
	}
	if err := hardlinkOrCopy(src, dst); err != nil {
		t.Fatalf("hardlinkOrCopy: %v", err)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("read dst: %v", err)
	}
	if string(got) != string(body) {
		t.Fatalf("dst contents drift: %q != %q", got, body)
	}
}

func TestHardlinkOrCopy_OverwritesExistingDst(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.bin")
	dst := filepath.Join(dir, "dst.bin")
	if err := os.WriteFile(src, []byte("new"), 0o644); err != nil {
		t.Fatalf("write src: %v", err)
	}
	if err := os.WriteFile(dst, []byte("OLDOLDOLD"), 0o644); err != nil {
		t.Fatalf("write old dst: %v", err)
	}
	if err := hardlinkOrCopy(src, dst); err != nil {
		t.Fatalf("hardlinkOrCopy: %v", err)
	}
	got, _ := os.ReadFile(dst)
	if string(got) != "new" {
		t.Fatalf("dst was not overwritten: got %q", got)
	}
}

func TestBuildChaptersManifest_OneChapterEpisode(t *testing.T) {
	ep := &models.Episode{Name: "Ep 1", SourceLanguage: "en", TargetLanguage: "zh"}
	ep.ID = 138
	chapters := []models.Job{
		{
			ChapterOrdinal:         1,
			ChapterStartMs:         0,
			ChapterEndMs:           600 * 1000,
			ChapterTitle:           "Pilot",
			ChapterTitleTranslated: "试播",
			OutputRelPath:          "episodes/138/chapters/vp0/ch01.mp4",
		},
	}
	chapters[0].ID = 200

	m := buildChaptersManifest(ep, chapters,
		"episodes/138/output/vp0/final.mp4", nil)
	if m.SchemaVersion != episodepkg.ManifestSchemaVersion {
		t.Errorf("schema version drift: %d", m.SchemaVersion)
	}
	if m.TotalChapters != 1 {
		t.Errorf("want 1 chapter; got %d", m.TotalChapters)
	}
	if m.OutputLayoutVersion != 2 {
		t.Errorf("OutputLayoutVersion: want 2, got %d", m.OutputLayoutVersion)
	}
	if m.Chapters[0].TitleTranslated != "试播" {
		t.Errorf("non-ASCII title corruption: %q", m.Chapters[0].TitleTranslated)
	}
	if m.TotalDurationMs != 600*1000 {
		t.Errorf("total_duration_ms: want 600000, got %d", m.TotalDurationMs)
	}
	if err := m.Validate(); err != nil {
		t.Fatalf("manifest from buildChaptersManifest fails Validate: %v", err)
	}
}

func TestEpisodeMergeTargetDuration_PrefersEpisodeDuration(t *testing.T) {
	ep := &models.Episode{DurationMs: 12345}
	chapters := []models.Job{
		{ChapterStartMs: 0, ChapterEndMs: 1000},
		{ChapterStartMs: 1000, ChapterEndMs: 2000},
	}
	if got := episodeMergeTargetDurationMs(ep, chapters); got != 12345 {
		t.Fatalf("target duration should prefer episode duration; got %d", got)
	}
}

func TestEpisodeMergeTargetDuration_FallsBackToChapterEnd(t *testing.T) {
	chapters := []models.Job{
		{ChapterStartMs: 0, ChapterEndMs: 1000},
		{ChapterStartMs: 1000, ChapterEndMs: 2300},
	}
	if got := episodeMergeTargetDurationMs(&models.Episode{}, chapters); got != 2300 {
		t.Fatalf("target duration should fall back to max chapter end; got %d", got)
	}
}

func TestChapterMergeTargetDuration_UsesChapterRange(t *testing.T) {
	job := &models.Job{ChapterStartMs: 500, ChapterEndMs: 1750}
	if got := chapterMergeTargetDurationMs(job); got != 1250 {
		t.Fatalf("chapter target duration mismatch: got %d", got)
	}
}

func TestChapterMergeTargetDuration_StandaloneJobHasNoOverride(t *testing.T) {
	job := &models.Job{}
	if got := chapterMergeTargetDurationMs(job); got != 0 {
		t.Fatalf("standalone job should not force target duration; got %d", got)
	}
}

func TestBuildChaptersManifest_AppendsMasterStatsAtFlatKey(t *testing.T) {
	ep := &models.Episode{Name: "Ep 2"}
	ep.ID = 200
	chapters := []models.Job{
		{ChapterOrdinal: 1, ChapterStartMs: 0, ChapterEndMs: 1000, OutputRelPath: "x"},
	}
	stats := &media.LoudnormStats{InputI: -19.5, OutputI: -23.0}

	m := buildChaptersManifest(ep, chapters, "out", stats)
	master, ok := m.LoudnormStats["vp0_master"]
	if !ok {
		t.Fatalf("vp0_master entry missing after master pass: %#v", m.LoudnormStats)
	}
	if _, ok := master.(*media.LoudnormStats); !ok {
		t.Fatalf("vp0_master should be *LoudnormStats; got %T", master)
	}
}

// Guards the schema contract: chapter-level loudnorm stats persisted by
// runMerge land at vp{N}_chXX, so when buildChaptersManifest deserialises
// Episode.LoudnormStats it must surface them at the same flat path. This
// test ensures the flat schema is honoured end-to-end.
func TestBuildChaptersManifest_PreservesFlatChapterStats(t *testing.T) {
	ep := &models.Episode{Name: "Ep 3"}
	ep.ID = 300
	ep.LoudnormStats = []byte(`{"vp0_ch01":{"input_i":-18.0,"output_i":-23.0}}`)
	chapters := []models.Job{
		{ChapterOrdinal: 1, ChapterStartMs: 0, ChapterEndMs: 1000, OutputRelPath: "x"},
	}

	m := buildChaptersManifest(ep, chapters, "out", nil)
	if _, ok := m.LoudnormStats["vp0_ch01"]; !ok {
		t.Fatalf("vp0_ch01 chapter stats stripped during manifest build: %#v", m.LoudnormStats)
	}
}

func TestEpisodeMergeEnqueueReason_TerminalEpisodeRebuild(t *testing.T) {
	tests := []struct {
		name   string
		status models.EpisodeStatus
		want   string
	}{
		{
			name:   "completed episode refresh",
			status: models.EpisodeStatusCompleted,
			want:   "chapter_merge_rebuilt_terminal_episode",
		},
		{
			name:   "failed episode refresh",
			status: models.EpisodeStatusFailed,
			want:   "chapter_merge_rebuilt_terminal_episode",
		},
		{
			name:   "active episode first merge",
			status: models.EpisodeStatusRunning,
			want:   "all_chapters_completed",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := episodeMergeEnqueueReason(tc.status); got != tc.want {
				t.Fatalf("episodeMergeEnqueueReason(%q): got %q, want %q", tc.status, got, tc.want)
			}
		})
	}
}

func TestEpisodeMergeShouldUpdateCompletedStatus_SkipsAlreadyCompleted(t *testing.T) {
	if episodeMergeShouldUpdateCompletedStatus(models.EpisodeStatusCompleted) {
		t.Fatal("completed episode refresh should not issue completed -> completed status update")
	}
	if !episodeMergeShouldUpdateCompletedStatus(models.EpisodeStatusRunning) {
		t.Fatal("active episode merge should still mark episode completed")
	}
}

// 2026-07-21 (P8): plain non-chapterized jobs carry ChapterStartMs/EndMs=0,
// which previously made ep_episode_merge fail WriteChaptersJSON with
// "chapters[0] invalid range [0, 0)". The builder must backfill the single
// chapter to the episode duration instead of failing the whole merge.
func TestBuildChaptersManifest_BackfillsUnsetChapterBounds(t *testing.T) {
	ep := &models.Episode{Name: "Smoke", DurationMs: 60000}
	ep.ID = 184
	chapters := []models.Job{{ChapterOrdinal: 1}}
	chapters[0].ID = 363

	m := buildChaptersManifest(ep, chapters, "episodes/184/output/vp0/final.mp4", nil)
	if got := m.Chapters[0].StartMs; got != 0 {
		t.Fatalf("StartMs: want 0, got %d", got)
	}
	if got := m.Chapters[0].EndMs; got != 60000 {
		t.Fatalf("EndMs: want episode duration 60000, got %d", got)
	}
	if err := m.Validate(); err != nil {
		t.Fatalf("manifest with backfilled bounds fails Validate: %v", err)
	}

	// Episode duration unknown (0): original zero range is preserved so
	// genuine data bugs still fail loudly at validation time.
	ep2 := &models.Episode{Name: "Broken"}
	ep2.ID = 999
	m2 := buildChaptersManifest(ep2, chapters, "out", nil)
	if err := m2.Validate(); err == nil {
		t.Fatal("zero-range chapter with unknown episode duration must still fail validation")
	}
}
