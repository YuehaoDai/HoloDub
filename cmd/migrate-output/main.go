// Package main — cmd/migrate-output: OPT-403 back-fill tool.
//
// One-off CLI that lifts every existing Episode (typically the ~138
// historical 1-chapter Episodes that pre-date OPT-403's unified output
// layout) from output_layout_version=1 to v2:
//
//   - Hard-link (or copy when --use-hardlink=false / cross-fs) each
//     chapter's existing OutputRelPath into the canonical v2 location
//     episodes/{ep_id}/chapters/vp{vp}/ch{ord:02d}.mp4
//   - For 1-chapter Episodes, hard-link the same artefact to the
//     episode-level final at episodes/{ep_id}/output/vp{vp}/final.mp4
//   - Write a fresh chapters.json manifest at episodes/{ep_id}/
//     chapters.json
//   - Persist Episode.OutputRelPath / ChaptersManifestRelPath /
//     OutputLayoutVersion = 2
//
// The tool is fail-safe by default:
//
//   - --dry-run prints a planned-action report (per-episode disposition
//     + projected paths + total bytes touched) WITHOUT touching disk or
//     DB. Always run dry-run first.
//   - --use-hardlink=true (default) uses os.Link. Falls back to byte
//     copy on failure (NTFS across volumes, FUSE mounts, etc.) so
//     "hard-link unsupported here" never blocks back-fill.
//   - --keep-old=true (default) leaves the original chapter
//     OutputRelPath in place so the legacy UI download links keep
//     working until the operator confirms the v2 layout. Pass
//     --keep-old=false to delete the originals after back-fill.
//   - --episode-ids allows back-filling a curated subset, e.g. for
//     testing the migration on a single episode before running the
//     full sweep.
//
// Output: a JSON report on stdout (machine-readable) + a one-line
// human summary on stderr. The JSON is appended to the back-fill audit
// trail at $DATA_ROOT/episodes/.migration-history.jsonl when --record
// is passed.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"holodub/internal/config"
	episodepkg "holodub/internal/episode"
	"holodub/internal/models"
	"holodub/internal/observability"
	"holodub/internal/storage"
	"holodub/internal/store"
)

type flags struct {
	dryRun      bool
	useHardlink bool
	keepOld     bool
	episodeIDs  string
	record      bool
	limit       int
}

func parseFlags() flags {
	f := flags{}
	flag.BoolVar(&f.dryRun, "dry-run", true,
		"plan-only mode; prints disposition without touching disk or DB")
	flag.BoolVar(&f.useHardlink, "use-hardlink", true,
		"prefer os.Link (instant); falls back to byte copy on failure")
	flag.BoolVar(&f.keepOld, "keep-old", true,
		"leave the original chapter OutputRelPath in place after back-fill")
	flag.StringVar(&f.episodeIDs, "episode-ids", "",
		"comma-separated subset of episode ids to back-fill; empty = all")
	flag.BoolVar(&f.record, "record", false,
		"append the JSON report to $DATA_ROOT/episodes/.migration-history.jsonl")
	flag.IntVar(&f.limit, "limit", 0,
		"cap the number of episodes to process (0 = no cap); useful with --dry-run")
	flag.Usage = func() {
		fmt.Fprintf(flag.CommandLine.Output(), `cmd/migrate-output — OPT-403 back-fill tool

Usage: migrate-output [flags]

Examples:
  # Always start with a dry run on the entire database:
  migrate-output --dry-run

  # Migrate a single episode (e.g. id=42) live, hard-link mode, keep originals:
  migrate-output --dry-run=false --episode-ids=42

  # Bulk-migrate everything live, hard-link, KEEP originals, record audit row:
  migrate-output --dry-run=false --record

  # Same but DELETE the legacy chapter OutputRelPath after each migration:
  migrate-output --dry-run=false --record --keep-old=false

Flags:
`)
		flag.PrintDefaults()
	}
	flag.Parse()
	return f
}

// EpisodeReport captures per-episode disposition for the JSON report.
// Fields are deliberately verbose (vs. cryptic single-letter codes) so
// the audit trail stays grep-friendly years from now.
type EpisodeReport struct {
	EpisodeID         uint     `json:"episode_id"`
	Name              string   `json:"name,omitempty"`
	ChapterCount      int      `json:"chapter_count"`
	PreLayoutVersion  int8     `json:"pre_layout_version"`
	Disposition       string   `json:"disposition"`
	NewEpisodeOutput  string   `json:"new_episode_output,omitempty"`
	NewManifestPath   string   `json:"new_manifest_path,omitempty"`
	BytesTouched      int64    `json:"bytes_touched"`
	HardLinkUsed      bool     `json:"hard_link_used"`
	OldFilesRemoved   []string `json:"old_files_removed,omitempty"`
	Errors            []string `json:"errors,omitempty"`
}

// ToolReport is the top-level JSON report written to stdout.
type ToolReport struct {
	StartedAt        time.Time       `json:"started_at"`
	FinishedAt       time.Time       `json:"finished_at"`
	DryRun           bool            `json:"dry_run"`
	UseHardlink      bool            `json:"use_hardlink"`
	KeepOld          bool            `json:"keep_old"`
	EpisodesScanned  int             `json:"episodes_scanned"`
	EpisodesMigrated int             `json:"episodes_migrated"`
	EpisodesSkipped  int             `json:"episodes_skipped"`
	EpisodesFailed   int             `json:"episodes_failed"`
	TotalBytes       int64           `json:"total_bytes"`
	Episodes         []EpisodeReport `json:"episodes"`
}

func main() {
	f := parseFlags()

	cfg, err := config.Load()
	if err != nil {
		fatalf("load config: %v", err)
	}
	logger := observability.NewLogger(cfg)
	slog.SetDefault(logger)

	st, err := store.New(cfg)
	if err != nil {
		fatalf("open store: %v", err)
	}

	idFilter, err := parseEpisodeIDs(f.episodeIDs)
	if err != nil {
		fatalf("--episode-ids: %v", err)
	}

	report, err := runMigration(context.Background(), cfg, st, f, idFilter)
	if err != nil {
		fatalf("migration: %v", err)
	}

	jsonOut, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		fatalf("marshal report: %v", err)
	}
	if _, err := os.Stdout.Write(append(jsonOut, '\n')); err != nil {
		fatalf("write report: %v", err)
	}

	fmt.Fprintf(os.Stderr,
		"migrate-output: scanned=%d migrated=%d skipped=%d failed=%d bytes=%d dry_run=%v\n",
		report.EpisodesScanned,
		report.EpisodesMigrated,
		report.EpisodesSkipped,
		report.EpisodesFailed,
		report.TotalBytes,
		report.DryRun,
	)

	if f.record {
		if err := appendAuditRow(cfg.DataRoot, report); err != nil {
			fmt.Fprintf(os.Stderr, "migrate-output: audit-row append failed: %v\n", err)
			os.Exit(1)
		}
	}
}

// runMigration is the testable entry point.  Pulled out of main so tests
// can wire a sqlite-in-memory store without forking a process.
func runMigration(
	ctx context.Context,
	cfg config.Config,
	st *store.Store,
	f flags,
	idFilter map[uint]bool,
) (*ToolReport, error) {
	report := &ToolReport{
		StartedAt:   time.Now().UTC(),
		DryRun:      f.dryRun,
		UseHardlink: f.useHardlink,
		KeepOld:     f.keepOld,
	}
	defer func() { report.FinishedAt = time.Now().UTC() }()

	episodes, err := st.ListEpisodes(ctx)
	if err != nil {
		return nil, fmt.Errorf("list episodes: %w", err)
	}

	processed := 0
	for _, ep := range episodes {
		if idFilter != nil && !idFilter[ep.ID] {
			continue
		}
		if f.limit > 0 && processed >= f.limit {
			break
		}
		processed++

		report.EpisodesScanned++
		entry := migrateOneEpisode(ctx, cfg, st, ep, f)
		report.Episodes = append(report.Episodes, entry)
		report.TotalBytes += entry.BytesTouched

		switch entry.Disposition {
		case "migrated":
			report.EpisodesMigrated++
		case "skipped":
			report.EpisodesSkipped++
		case "failed":
			report.EpisodesFailed++
		}
	}
	return report, nil
}

// migrateOneEpisode does the per-episode work and returns the report row.
// Errors do NOT abort the run — they're surfaced in entry.Errors so the
// operator can fix one episode without re-running the whole sweep.
func migrateOneEpisode(
	ctx context.Context,
	cfg config.Config,
	st *store.Store,
	ep models.Episode,
	f flags,
) EpisodeReport {
	entry := EpisodeReport{
		EpisodeID:        ep.ID,
		Name:             ep.Name,
		PreLayoutVersion: ep.OutputLayoutVersion,
		HardLinkUsed:     f.useHardlink,
	}

	if ep.OutputLayoutVersion >= 2 {
		entry.Disposition = "skipped"
		entry.Errors = append(entry.Errors, "already on output_layout_version >= 2")
		return entry
	}

	chapters, err := st.GetEpisodeChapters(ctx, ep.ID)
	if err != nil {
		entry.Disposition = "failed"
		entry.Errors = append(entry.Errors, fmt.Sprintf("load chapters: %v", err))
		return entry
	}
	entry.ChapterCount = len(chapters)
	if len(chapters) == 0 {
		entry.Disposition = "skipped"
		entry.Errors = append(entry.Errors, "no chapters")
		return entry
	}

	const vpID = uint(0)
	episodeOutputRel := ep.GetEpisodeOutputRelPath(vpID)
	manifestRel := ep.GetChaptersJSONRelPath()
	episodeOutputAbs := storage.ResolveDataPath(cfg.DataRoot, episodeOutputRel)
	manifestAbs := storage.ResolveDataPath(cfg.DataRoot, manifestRel)

	planned := make([]plannedAction, 0, len(chapters)+2)
	for _, ch := range chapters {
		if ch.OutputRelPath == "" {
			entry.Errors = append(entry.Errors,
				fmt.Sprintf("chapter %d has empty OutputRelPath", ch.ID))
			continue
		}
		newChRel := ep.GetChapterOutputRelPath(ch.ChapterOrdinal, vpID)
		newChAbs := storage.ResolveDataPath(cfg.DataRoot, newChRel)
		oldAbs := storage.ResolveDataPath(cfg.DataRoot, ch.OutputRelPath)
		planned = append(planned, plannedAction{
			kind:    "chapter_link",
			srcAbs:  oldAbs,
			dstAbs:  newChAbs,
			oldRel:  ch.OutputRelPath,
			newRel:  newChRel,
			ordinal: ch.ChapterOrdinal,
		})
	}

	// 1-chapter shortcut: the episode-level final.mp4 is the same artefact
	// as ch01.mp4. Plan it explicitly so the report reflects the second
	// link / copy.
	if len(chapters) == 1 && chapters[0].OutputRelPath != "" {
		planned = append(planned, plannedAction{
			kind:   "episode_final_link",
			srcAbs: storage.ResolveDataPath(cfg.DataRoot, chapters[0].OutputRelPath),
			dstAbs: episodeOutputAbs,
			oldRel: chapters[0].OutputRelPath,
			newRel: episodeOutputRel,
		})
	}

	if f.dryRun {
		var plannedBytes int64
		for _, a := range planned {
			plannedBytes += statSizeBytes(a.srcAbs)
		}
		entry.BytesTouched = plannedBytes
		entry.NewEpisodeOutput = episodeOutputRel
		entry.NewManifestPath = manifestRel
		// Dry-run disposition mirrors the live path: any error queued during
		// planning (e.g. an orphaned chapter row with empty OutputRelPath)
		// surfaces as "failed" so operators don't see "migrated" + an
		// errors[] list and assume the back-fill will quietly succeed.
		if len(entry.Errors) > 0 {
			entry.Disposition = "failed"
		} else {
			entry.Disposition = "migrated"
		}
		return entry
	}

	for _, a := range planned {
		if err := storage.EnsureParentDir(a.dstAbs); err != nil {
			entry.Errors = append(entry.Errors,
				fmt.Sprintf("%s: ensure parent dir: %v", a.dstAbs, err))
			continue
		}
		if err := linkOrCopy(a.srcAbs, a.dstAbs, f.useHardlink); err != nil {
			entry.Errors = append(entry.Errors,
				fmt.Sprintf("%s: %v", a.kind, err))
			continue
		}
		entry.BytesTouched += statSizeBytes(a.dstAbs)
		if !f.keepOld && a.kind == "chapter_link" {
			if err := os.Remove(a.srcAbs); err == nil {
				entry.OldFilesRemoved = append(entry.OldFilesRemoved, a.oldRel)
			} else if !errors.Is(err, os.ErrNotExist) {
				entry.Errors = append(entry.Errors,
					fmt.Sprintf("remove old %s: %v", a.oldRel, err))
			}
		}
	}

	if len(entry.Errors) > 0 {
		entry.Disposition = "failed"
		return entry
	}

	manifest := buildBackfillManifest(&ep, chapters, episodeOutputRel)
	if err := episodepkg.WriteChaptersJSON(manifestAbs, manifest); err != nil {
		entry.Errors = append(entry.Errors, fmt.Sprintf("write manifest: %v", err))
		entry.Disposition = "failed"
		return entry
	}

	if err := st.UpdateEpisodeOutput(ctx, ep.ID, episodeOutputRel, manifestRel, 2); err != nil {
		entry.Errors = append(entry.Errors, fmt.Sprintf("update episode row: %v", err))
		entry.Disposition = "failed"
		return entry
	}

	entry.NewEpisodeOutput = episodeOutputRel
	entry.NewManifestPath = manifestRel
	entry.Disposition = "migrated"
	return entry
}

// plannedAction is one disk-level operation queued by migrateOneEpisode.
type plannedAction struct {
	kind    string
	srcAbs  string
	dstAbs  string
	oldRel  string
	newRel  string
	ordinal int
}

// buildBackfillManifest mirrors stage_episode_merge.buildChaptersManifest
// but is intentionally NOT shared (yet) — the back-fill path may want to
// stamp a different GeneratedBy + skip loudnorm stats it doesn't have.
func buildBackfillManifest(
	ep *models.Episode,
	chapters []models.Job,
	episodeOutputRel string,
) *episodepkg.ChaptersManifest {
	entries := make([]episodepkg.ChapterEntry, 0, len(chapters))
	var totalDur int64
	for _, ch := range chapters {
		dur := ch.ChapterEndMs - ch.ChapterStartMs
		if dur < 0 {
			dur = 0
		}
		entries = append(entries, episodepkg.ChapterEntry{
			Ordinal:         ch.ChapterOrdinal,
			JobID:           ch.ID,
			StartMs:         ch.ChapterStartMs,
			EndMs:           ch.ChapterEndMs,
			DurationMs:      dur,
			TitleSource:     ch.ChapterTitle,
			TitleTranslated: ch.ChapterTitleTranslated,
			SummaryMD:       ch.ChapterSummaryMD,
			OutputRelPath:   ep.GetChapterOutputRelPath(ch.ChapterOrdinal, 0),
		})
		if ch.ChapterEndMs > totalDur {
			totalDur = ch.ChapterEndMs
		}
	}
	m := &episodepkg.ChaptersManifest{
		SchemaVersion:       episodepkg.ManifestSchemaVersion,
		EpisodeID:           ep.ID,
		EpisodeName:         ep.Name,
		SourceLanguage:      ep.SourceLanguage,
		TargetLanguage:      ep.TargetLanguage,
		TotalChapters:       len(entries),
		TotalDurationMs:     totalDur,
		OutputLayoutVersion: 2,
		OutputRelPath:       episodeOutputRel,
		GeneratedAt:         time.Now().UTC(),
		GeneratedBy:         "cmd/migrate-output",
		Chapters:            entries,
	}
	m.SortChapters()
	return m
}

// linkOrCopy mirrors stage_episode_merge.hardlinkOrCopy semantics but is
// duplicated here to keep the cmd/ binary independent of the pipeline
// package (avoids dragging FFmpeg / queue dependencies into the migration
// tool's binary). When useHardlink is false we go straight to copy.
func linkOrCopy(src, dst string, useHardlink bool) error {
	if src == "" || dst == "" {
		return errors.New("linkOrCopy: empty src/dst")
	}
	if _, err := os.Stat(src); err != nil {
		return fmt.Errorf("linkOrCopy: src %q: %w", src, err)
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return fmt.Errorf("linkOrCopy: mkdir: %w", err)
	}
	_ = os.Remove(dst)
	if useHardlink {
		if err := os.Link(src, dst); err == nil {
			return nil
		}
	}
	srcF, err := os.Open(src)
	if err != nil {
		return fmt.Errorf("linkOrCopy: open src: %w", err)
	}
	defer srcF.Close()
	dstF, err := os.Create(dst)
	if err != nil {
		return fmt.Errorf("linkOrCopy: create dst: %w", err)
	}
	if _, err := io.Copy(dstF, srcF); err != nil {
		_ = dstF.Close()
		_ = os.Remove(dst)
		return fmt.Errorf("linkOrCopy: copy: %w", err)
	}
	if err := dstF.Close(); err != nil {
		return fmt.Errorf("linkOrCopy: close dst: %w", err)
	}
	return nil
}

// statSizeBytes returns the byte count of a file or 0 if it doesn't exist.
// Best-effort — used only for the bytes_touched report metric.
func statSizeBytes(path string) int64 {
	st, err := os.Stat(path)
	if err != nil {
		return 0
	}
	return st.Size()
}

// parseEpisodeIDs parses "1,2,3" into {1,2,3}. Empty string returns nil
// (= no filter). Whitespace and zero entries are tolerated.
func parseEpisodeIDs(s string) (map[uint]bool, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil, nil
	}
	parts := strings.Split(s, ",")
	out := map[uint]bool{}
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		v, err := strconv.ParseUint(p, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("invalid episode id %q: %w", p, err)
		}
		if v == 0 {
			return nil, fmt.Errorf("episode id 0 is invalid")
		}
		out[uint(v)] = true
	}
	if len(out) == 0 {
		return nil, errors.New("no valid episode ids parsed")
	}
	// sort for stable error messages — Go map iteration is unordered.
	keys := make([]uint, 0, len(out))
	for k := range out {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	return out, nil
}

// appendAuditRow records this run's report as one JSONL row in
// $DATA_ROOT/episodes/.migration-history.jsonl. The directory is
// created on demand.
func appendAuditRow(dataRoot string, report *ToolReport) error {
	dir := filepath.Join(dataRoot, "episodes")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir audit dir: %w", err)
	}
	path := filepath.Join(dir, ".migration-history.jsonl")
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open audit file: %w", err)
	}
	defer f.Close()
	row, err := json.Marshal(report)
	if err != nil {
		return fmt.Errorf("marshal audit row: %w", err)
	}
	if _, err := f.Write(append(row, '\n')); err != nil {
		return fmt.Errorf("write audit row: %w", err)
	}
	return nil
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "migrate-output: "+format+"\n", args...)
	os.Exit(1)
}
