// Package episode provides episode-level helpers that span the OPT-403
// chapterize / merge / migration code paths and don't naturally belong
// to a single pipeline stage.
//
// Currently it exposes one type — ChaptersManifest — and one function —
// WriteChaptersJSON — used by stage_episode_merge to write the
// episodes/{ep_id}/chapters.json manifest, by cmd/migrate-output to
// seed the manifest for back-filled v1→v2 episodes, and by future API /
// CLI consumers that want the bilingual chapter list without going
// through the API.
//
// The manifest is the SOURCE OF TRUTH for "what chapters does this
// episode have, where do their files live, and how loud are they".
// The DB has subsets of this information (Job.ChapterTitle, Episode.
// LoudnormStats, …) but the manifest is an offline-readable, per-
// episode, schema-versioned snapshot suitable for archival exports and
// downstream tooling.
package episode

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"
)

// ManifestSchemaVersion is the semantic version of the chapters.json
// schema. Bump when adding a non-backwards-compatible field. Readers
// SHOULD honour the version to refuse parsing newer schemas they
// don't understand. Bump by one (1 → 2) on breaking change; minor
// expansions can stay at the same major version with extra optional
// fields that older readers can ignore.
const ManifestSchemaVersion = 1

// ChaptersManifest is the on-disk shape of episodes/{ep_id}/chapters.json.
// Field ordering matters for stable diff-friendly output; jsonMarshalIndent
// preserves struct order so we can review changes with a normal `diff`.
type ChaptersManifest struct {
	SchemaVersion       int               `json:"schema_version"`
	EpisodeID           uint              `json:"episode_id"`
	EpisodeName         string            `json:"episode_name,omitempty"`
	SourceLanguage      string            `json:"source_language,omitempty"`
	TargetLanguage      string            `json:"target_language,omitempty"`
	TotalChapters       int               `json:"total_chapters"`
	TotalDurationMs     int64             `json:"total_duration_ms"`
	OutputLayoutVersion int8              `json:"output_layout_version"`
	OutputRelPath       string            `json:"output_rel_path,omitempty"`
	GeneratedAt         time.Time         `json:"generated_at"`
	GeneratedBy         string            `json:"generated_by"` // e.g. "stage_episode_merge" / "cmd/migrate-output"
	Chapters            []ChapterEntry    `json:"chapters"`
	LoudnormStats       map[string]any    `json:"loudnorm_stats,omitempty"` // mirrors Episode.LoudnormStats
}

// ChapterEntry describes one chapter in the manifest. Bilingual title
// fields are intentionally NOT collapsed into a single map[string]string
// because consumers want strong typing and the tooling can present the
// pair side-by-side in the EpisodeDetail UI.
type ChapterEntry struct {
	Ordinal           int    `json:"ordinal"`
	JobID             uint   `json:"job_id"`
	StartMs           int64  `json:"start_ms"`
	EndMs             int64  `json:"end_ms"`
	DurationMs        int64  `json:"duration_ms"`
	TitleSource       string `json:"title_source,omitempty"`
	TitleTranslated   string `json:"title_translated,omitempty"`
	SummaryMD         string `json:"summary_md,omitempty"`
	OutputRelPath     string `json:"output_rel_path,omitempty"` // chapter video relpath
	StartCutSilenceMs int64  `json:"start_cut_silence_ms,omitempty"`
	EndCutSilenceMs   int64  `json:"end_cut_silence_ms,omitempty"`
}

// Validate enforces the basic invariants we want manifests to honour
// before they hit disk. Returns the FIRST violation as a typed error —
// callers usually surface this at write time and bail.
func (m *ChaptersManifest) Validate() error {
	if m == nil {
		return errors.New("ChaptersManifest: nil receiver")
	}
	if m.SchemaVersion <= 0 {
		return fmt.Errorf("ChaptersManifest: invalid schema_version %d", m.SchemaVersion)
	}
	if m.EpisodeID == 0 {
		return errors.New("ChaptersManifest: episode_id is zero")
	}
	if m.TotalChapters != len(m.Chapters) {
		return fmt.Errorf("ChaptersManifest: total_chapters=%d but chapters=%d",
			m.TotalChapters, len(m.Chapters))
	}
	if m.OutputLayoutVersion <= 0 {
		return fmt.Errorf("ChaptersManifest: invalid output_layout_version %d",
			m.OutputLayoutVersion)
	}
	for i, ch := range m.Chapters {
		if ch.Ordinal != i+1 {
			return fmt.Errorf("ChaptersManifest: chapters[%d].ordinal=%d expected %d",
				i, ch.Ordinal, i+1)
		}
		if ch.EndMs <= ch.StartMs {
			return fmt.Errorf("ChaptersManifest: chapters[%d] invalid range [%d, %d)",
				i, ch.StartMs, ch.EndMs)
		}
		if ch.DurationMs > 0 && ch.DurationMs != ch.EndMs-ch.StartMs {
			return fmt.Errorf("ChaptersManifest: chapters[%d] duration_ms=%d != end-start=%d",
				i, ch.DurationMs, ch.EndMs-ch.StartMs)
		}
	}
	return nil
}

// SortChapters sorts the manifest's Chapters slice by Ordinal in place.
// Callers that build the manifest from a map (or unsorted DB rows)
// should call this BEFORE Validate / WriteChaptersJSON.
func (m *ChaptersManifest) SortChapters() {
	if m == nil {
		return
	}
	sort.SliceStable(m.Chapters, func(i, j int) bool {
		return m.Chapters[i].Ordinal < m.Chapters[j].Ordinal
	})
}

// WriteChaptersJSON marshals the manifest with 2-space indentation and
// writes it atomically to absPath (write to .tmp + rename). The atomic
// write protects readers (UI download, external CLI) from racing the
// writer mid-update.
//
// absPath should already be the absolute filesystem path under DataRoot;
// callers typically derive it via:
//
//	abs := storage.ResolveDataPath(cfg.DataRoot, ep.GetChaptersJSONRelPath())
//
// The function ensures the parent directory exists (MkdirAll 0o755) and
// validates the manifest before serialising.
func WriteChaptersJSON(absPath string, m *ChaptersManifest) error {
	if absPath == "" {
		return errors.New("WriteChaptersJSON: empty path")
	}
	if err := m.Validate(); err != nil {
		return fmt.Errorf("WriteChaptersJSON: validate: %w", err)
	}
	if m.GeneratedAt.IsZero() {
		m.GeneratedAt = time.Now().UTC()
	}
	body, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return fmt.Errorf("WriteChaptersJSON: marshal: %w", err)
	}
	body = append(body, '\n') // POSIX-friendly trailing newline

	if err := os.MkdirAll(filepath.Dir(absPath), 0o755); err != nil {
		return fmt.Errorf("WriteChaptersJSON: mkdir: %w", err)
	}
	tmp := absPath + ".tmp"
	if err := os.WriteFile(tmp, body, 0o644); err != nil {
		return fmt.Errorf("WriteChaptersJSON: write tmp: %w", err)
	}
	if err := os.Rename(tmp, absPath); err != nil {
		_ = os.Remove(tmp) // best-effort cleanup
		return fmt.Errorf("WriteChaptersJSON: rename: %w", err)
	}
	return nil
}

// ReadChaptersJSON reads + parses an existing chapters.json. Returns
// (nil, nil) when the file does not exist (caller decides whether
// missing == fresh episode or error). Validate is not called here —
// the caller should run Validate after reading.
func ReadChaptersJSON(absPath string) (*ChaptersManifest, error) {
	body, err := os.ReadFile(absPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("ReadChaptersJSON: %w", err)
	}
	var m ChaptersManifest
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, fmt.Errorf("ReadChaptersJSON: unmarshal: %w", err)
	}
	return &m, nil
}
