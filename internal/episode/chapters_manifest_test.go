package episode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func validManifest() *ChaptersManifest {
	return &ChaptersManifest{
		SchemaVersion:       ManifestSchemaVersion,
		EpisodeID:           138,
		EpisodeName:         "Test Episode",
		SourceLanguage:      "en",
		TargetLanguage:      "zh",
		TotalChapters:       2,
		TotalDurationMs:     45 * 60 * 1000,
		OutputLayoutVersion: 2,
		OutputRelPath:       "episodes/138/output/vp0/final.mp4",
		GeneratedBy:         "stage_episode_merge",
		Chapters: []ChapterEntry{
			{
				Ordinal:         1,
				JobID:           139,
				StartMs:         0,
				EndMs:           20 * 60 * 1000,
				DurationMs:      20 * 60 * 1000,
				TitleSource:     "Intro to Raft",
				TitleTranslated: "Raft 入门",
				OutputRelPath:   "episodes/138/chapters/vp0/ch01.mp4",
			},
			{
				Ordinal:         2,
				JobID:           140,
				StartMs:         20 * 60 * 1000,
				EndMs:           45 * 60 * 1000,
				DurationMs:      25 * 60 * 1000,
				TitleSource:     "Log Replication",
				TitleTranslated: "日志复制",
				OutputRelPath:   "episodes/138/chapters/vp0/ch02.mp4",
			},
		},
	}
}

func TestValidate_HappyPath(t *testing.T) {
	if err := validManifest().Validate(); err != nil {
		t.Fatalf("happy-path validation failed: %v", err)
	}
}

func TestValidate_RejectsZeroEpisodeID(t *testing.T) {
	m := validManifest()
	m.EpisodeID = 0
	if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "episode_id") {
		t.Fatalf("want episode_id error, got %v", err)
	}
}

func TestValidate_RejectsTotalChaptersMismatch(t *testing.T) {
	m := validManifest()
	m.TotalChapters = 5
	if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "total_chapters=5") {
		t.Fatalf("want total_chapters mismatch error, got %v", err)
	}
}

func TestValidate_RejectsOrdinalDrift(t *testing.T) {
	m := validManifest()
	m.Chapters[1].Ordinal = 99
	if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "ordinal=99") {
		t.Fatalf("want ordinal-mismatch error, got %v", err)
	}
}

func TestValidate_RejectsBadDuration(t *testing.T) {
	m := validManifest()
	m.Chapters[0].DurationMs = 1234 // not equal to end-start
	if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "duration_ms=1234") {
		t.Fatalf("want duration_ms mismatch, got %v", err)
	}
}

func TestValidate_RejectsBadLayoutVersion(t *testing.T) {
	m := validManifest()
	m.OutputLayoutVersion = 0
	if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "output_layout_version") {
		t.Fatalf("want layout_version error, got %v", err)
	}
}

func TestSortChapters_StableByOrdinal(t *testing.T) {
	m := validManifest()
	m.Chapters[0], m.Chapters[1] = m.Chapters[1], m.Chapters[0] // swap
	m.SortChapters()
	if m.Chapters[0].Ordinal != 1 || m.Chapters[1].Ordinal != 2 {
		t.Fatalf("SortChapters did not restore order: %+v", m.Chapters)
	}
}

func TestWriteAndReadChaptersJSON_RoundTrip(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "138", "chapters.json")
	m := validManifest()
	m.GeneratedAt = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

	if err := WriteChaptersJSON(p, m); err != nil {
		t.Fatalf("WriteChaptersJSON: %v", err)
	}
	body, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read written file: %v", err)
	}
	if !strings.HasSuffix(string(body), "\n") {
		t.Errorf("written JSON should end with newline; got %q",
			body[len(body)-10:])
	}
	if !json.Valid(body) {
		t.Fatalf("written body is not valid JSON: %s", body)
	}
	got, err := ReadChaptersJSON(p)
	if err != nil {
		t.Fatalf("ReadChaptersJSON: %v", err)
	}
	if got == nil {
		t.Fatal("ReadChaptersJSON: nil after write")
	}
	if got.EpisodeID != m.EpisodeID || got.TotalChapters != m.TotalChapters {
		t.Fatalf("round-trip drift: %+v vs %+v", got, m)
	}
	if got.Chapters[1].TitleTranslated != "日志复制" {
		t.Fatalf("non-ASCII round-trip drift: %q", got.Chapters[1].TitleTranslated)
	}
}

func TestWriteChaptersJSON_AtomicRenameNoTempLeftover(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "manifest.json")
	if err := WriteChaptersJSON(p, validManifest()); err != nil {
		t.Fatalf("WriteChaptersJSON: %v", err)
	}
	if _, err := os.Stat(p + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("temp file leaked after successful write: stat err=%v", err)
	}
}

func TestWriteChaptersJSON_RejectsInvalid(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "bad.json")
	bad := validManifest()
	bad.EpisodeID = 0
	if err := WriteChaptersJSON(p, bad); err == nil {
		t.Fatal("want validation error, got nil")
	}
	if _, err := os.Stat(p); !os.IsNotExist(err) {
		t.Fatalf("file should not be written when validation fails; stat err=%v", err)
	}
}

func TestReadChaptersJSON_MissingFileReturnsNilNoError(t *testing.T) {
	got, err := ReadChaptersJSON(filepath.Join(t.TempDir(), "nope.json"))
	if err != nil {
		t.Fatalf("missing file should not error; got %v", err)
	}
	if got != nil {
		t.Fatalf("missing file should yield nil manifest; got %+v", got)
	}
}

func TestWriteChaptersJSON_AutoStampsGeneratedAt(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "stamp.json")
	m := validManifest()
	if !m.GeneratedAt.IsZero() {
		t.Fatal("test setup error: GeneratedAt should be zero before write")
	}
	if err := WriteChaptersJSON(p, m); err != nil {
		t.Fatalf("WriteChaptersJSON: %v", err)
	}
	if m.GeneratedAt.IsZero() {
		t.Fatal("WriteChaptersJSON should auto-stamp GeneratedAt")
	}
	if time.Since(m.GeneratedAt) > time.Minute {
		t.Errorf("auto-stamp drifted into the past: %v", m.GeneratedAt)
	}
}

func TestSchemaVersionConstantPinned(t *testing.T) {
	// Existing v1 readers in cmd/migrate-output and the UI rely on this;
	// bumping requires migrating both.
	if ManifestSchemaVersion != 1 {
		t.Fatalf("ManifestSchemaVersion drifted: got %d, want 1",
			ManifestSchemaVersion)
	}
}
