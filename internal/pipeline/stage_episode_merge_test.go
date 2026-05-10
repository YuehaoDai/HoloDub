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

func TestBuildChaptersManifest_AppendsMasterStatsToVP0(t *testing.T) {
	ep := &models.Episode{Name: "Ep 2"}
	ep.ID = 200
	chapters := []models.Job{
		{ChapterOrdinal: 1, ChapterStartMs: 0, ChapterEndMs: 1000, OutputRelPath: "x"},
	}
	stats := &media.LoudnormStats{InputI: -19.5, OutputI: -23.0}

	m := buildChaptersManifest(ep, chapters, "out", stats)
	bucket, ok := m.LoudnormStats["vp0"].(map[string]any)
	if !ok {
		t.Fatalf("vp0 bucket missing or wrong type: %#v", m.LoudnormStats)
	}
	master, ok := bucket["master"]
	if !ok {
		t.Fatal("vp0.master entry missing after master pass")
	}
	if _, ok := master.(*media.LoudnormStats); !ok {
		t.Fatalf("vp0.master should be *LoudnormStats; got %T", master)
	}
}
