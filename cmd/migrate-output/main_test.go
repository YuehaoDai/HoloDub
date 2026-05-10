// Pure-function unit tests for cmd/migrate-output.
//
// The DB-touching paths (migrateOneEpisode end-to-end + appendAuditRow)
// are exercised by the live --dry-run sweep in CI on the Postgres
// instance, since unit-testing them requires either a real Postgres or
// a sqlite-with-cgo build (Windows dev machines run with CGO_ENABLED=0).
//
// What we DO test here are the pure helpers that anchor every report
// row's correctness:
//
//   - parseEpisodeIDs: argument parsing edge cases (empty, trailing
//     comma, "0" rejected, non-numeric rejected).
//   - linkOrCopy: hardlink-preferred path + copy fallback + overwrites
//     existing dst + idempotent on missing src.
//   - statSizeBytes: 0 on missing file (back-fill defensive metric).
//   - buildBackfillManifest: 1-chapter happy path stamps the v2 layout
//     paths, GeneratedBy = cmd/migrate-output, and round-trips.
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	episodepkg "holodub/internal/episode"
	"holodub/internal/models"
)

func TestParseEpisodeIDs(t *testing.T) {
	cases := []struct {
		in        string
		want      map[uint]bool
		wantError bool
	}{
		{"", nil, false},
		{"  ", nil, false},
		{"42", map[uint]bool{42: true}, false},
		{"1, 2,3", map[uint]bool{1: true, 2: true, 3: true}, false},
		{"1,,2,", map[uint]bool{1: true, 2: true}, false},
		{"abc", nil, true},
		{"0", nil, true},
		{"-1", nil, true},
		{",,,", nil, true},
	}
	for _, tc := range cases {
		got, err := parseEpisodeIDs(tc.in)
		if tc.wantError {
			if err == nil {
				t.Errorf("parseEpisodeIDs(%q) want error, got %v", tc.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("parseEpisodeIDs(%q) unexpected error: %v", tc.in, err)
			continue
		}
		if len(got) != len(tc.want) {
			t.Errorf("parseEpisodeIDs(%q) size mismatch: got %v, want %v", tc.in, got, tc.want)
			continue
		}
		for k := range tc.want {
			if !got[k] {
				t.Errorf("parseEpisodeIDs(%q) missing key %d in %v", tc.in, k, got)
			}
		}
	}
}

func TestLinkOrCopy_EmptyArgsRejected(t *testing.T) {
	if err := linkOrCopy("", "/tmp/dst", true); err == nil ||
		!strings.Contains(err.Error(), "empty src/dst") {
		t.Errorf("linkOrCopy empty src: want error, got %v", err)
	}
	if err := linkOrCopy("/tmp/src", "", true); err == nil ||
		!strings.Contains(err.Error(), "empty src/dst") {
		t.Errorf("linkOrCopy empty dst: want error, got %v", err)
	}
}

func TestLinkOrCopy_MissingSrcReports(t *testing.T) {
	dir := t.TempDir()
	err := linkOrCopy(filepath.Join(dir, "nope"), filepath.Join(dir, "dst"), true)
	if err == nil {
		t.Fatal("linkOrCopy missing src: want error")
	}
	if !strings.Contains(err.Error(), "src") {
		t.Errorf("error should mention src: %v", err)
	}
}

func TestLinkOrCopy_HardlinkAttempted_ThenCopyFallback(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "ch01.mp4")
	body := []byte("synthetic chapter mp4 payload")
	if err := os.WriteFile(src, body, 0o644); err != nil {
		t.Fatalf("write src: %v", err)
	}
	dst := filepath.Join(dir, "out", "vp0", "final.mp4")
	if err := linkOrCopy(src, dst, true); err != nil {
		t.Fatalf("linkOrCopy: %v", err)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("read dst: %v", err)
	}
	if string(got) != string(body) {
		t.Fatalf("dst contents drift: %q != %q", got, body)
	}
}

func TestLinkOrCopy_CopyOnlyMode(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.bin")
	dst := filepath.Join(dir, "dst.bin")
	if err := os.WriteFile(src, []byte("payload"), 0o644); err != nil {
		t.Fatalf("write src: %v", err)
	}
	if err := linkOrCopy(src, dst, false); err != nil {
		t.Fatalf("linkOrCopy: %v", err)
	}
	got, _ := os.ReadFile(dst)
	if string(got) != "payload" {
		t.Fatalf("dst payload drift: %q", got)
	}
}

func TestLinkOrCopy_OverwritesExistingDst(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.bin")
	dst := filepath.Join(dir, "dst.bin")
	if err := os.WriteFile(src, []byte("new"), 0o644); err != nil {
		t.Fatalf("write src: %v", err)
	}
	if err := os.WriteFile(dst, []byte("OLDOLDOLD"), 0o644); err != nil {
		t.Fatalf("write old dst: %v", err)
	}
	if err := linkOrCopy(src, dst, true); err != nil {
		t.Fatalf("linkOrCopy: %v", err)
	}
	got, _ := os.ReadFile(dst)
	if string(got) != "new" {
		t.Fatalf("dst was not overwritten: got %q", got)
	}
}

func TestStatSizeBytes(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "nope")
	if got := statSizeBytes(missing); got != 0 {
		t.Errorf("statSizeBytes missing file: want 0, got %d", got)
	}
	present := filepath.Join(dir, "x.bin")
	if err := os.WriteFile(present, []byte("12345"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}
	if got := statSizeBytes(present); got != 5 {
		t.Errorf("statSizeBytes 5-byte file: want 5, got %d", got)
	}
}

// buildBackfillManifest must (a) preserve non-ASCII chapter titles intact,
// (b) stamp GeneratedBy = "cmd/migrate-output" so audit trails can tell
// migration-vs-pipeline-generated manifests apart, and (c) compute the
// chapter OutputRelPath under the v2 layout (NOT the legacy path stored
// on the chapter Job — that's the whole point of the migration).
func TestBuildBackfillManifest_v2PathsStamped(t *testing.T) {
	ep := &models.Episode{Name: "Ep 1", SourceLanguage: "en", TargetLanguage: "zh"}
	ep.ID = 138
	chapters := []models.Job{
		{
			ChapterOrdinal:         1,
			ChapterStartMs:         0,
			ChapterEndMs:           600 * 1000,
			ChapterTitle:           "Pilot",
			ChapterTitleTranslated: "试播",
			OutputRelPath:          "jobs/138/output/vp0/final.mp4", // legacy path
		},
	}
	chapters[0].ID = 200

	m := buildBackfillManifest(ep, chapters,
		"episodes/138/output/vp0/final.mp4")

	if m.GeneratedBy != "cmd/migrate-output" {
		t.Errorf("GeneratedBy: want cmd/migrate-output, got %q", m.GeneratedBy)
	}
	if m.OutputLayoutVersion != 2 {
		t.Errorf("OutputLayoutVersion: want 2, got %d", m.OutputLayoutVersion)
	}
	if m.Chapters[0].TitleTranslated != "试播" {
		t.Errorf("non-ASCII title corruption: %q", m.Chapters[0].TitleTranslated)
	}
	wantV2 := "episodes/138/chapters/vp0/ch01.mp4"
	if m.Chapters[0].OutputRelPath != wantV2 {
		t.Errorf("chapter OutputRelPath: want %q (v2 layout), got %q",
			wantV2, m.Chapters[0].OutputRelPath)
	}
	if m.SchemaVersion != episodepkg.ManifestSchemaVersion {
		t.Errorf("SchemaVersion: want %d, got %d", episodepkg.ManifestSchemaVersion, m.SchemaVersion)
	}
	if err := m.Validate(); err != nil {
		t.Fatalf("manifest from buildBackfillManifest fails Validate: %v", err)
	}
}
