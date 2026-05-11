// report.go — markdown + JSON report rendering for cmd/chapterize-bench.
//
// Two outputs:
//
//   - report.md: human-readable markdown. Top: leaderboard table sorted
//     by judge total (with N/A judge results listed last). Then per-model
//     details: extract-run stability, judge breakdown, three flagged
//     boundaries with the lowest scores for spot checks.
//
//   - report.json: machine-readable. Same data, structured shape that a
//     future dashboard / CI gate can consume without parsing markdown.
//
// Both files live at the root of --out so the operator can find them
// without remembering the timestamp folder. Per-model raw + judge JSON
// stays in raw/ + judge/ for deep dives.
//
// Lessons-learned #3 (path uniqueness) applies here too: chapter-text
// dumps are written as chapters-{model}.txt rather than chapters.txt,
// so re-running with a different candidate set doesn't overwrite.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"holodub/internal/llm"
	"holodub/internal/models"
)

// ReportMeta carries the run-level fields the operator wants to see at
// the top of the report (when, with which judge, how many runs).
type ReportMeta struct {
	GeneratedAt time.Time `json:"generated_at"`
	JudgeModel  string    `json:"judge_model"`
	Runs        int       `json:"runs_per_candidate"`
	JudgeRuns   int       `json:"judge_runs_per_candidate"`
}

// ReportJSON is the structured top-level document written to report.json.
type ReportJSON struct {
	Meta        ReportMeta              `json:"meta"`
	Episode     EpisodeMeta             `json:"episode"`
	Leaderboard []LeaderboardRow        `json:"leaderboard"`
	Models      map[string]ModelDetails `json:"models"`
}

type EpisodeMeta struct {
	ID              uint    `json:"id"`
	Name            string  `json:"name"`
	SourceLanguage  string  `json:"source_language"`
	TargetLanguage  string  `json:"target_language"`
	SegmentCount    int     `json:"segment_count"`
	TotalDurationMs int64   `json:"total_duration_ms"`
	TotalDurationMin float64 `json:"total_duration_min"`
}

type LeaderboardRow struct {
	Rank             int     `json:"rank"`
	Model            string  `json:"model"`
	JudgeTotal       float64 `json:"judge_total"`
	BoundaryAvg      float64 `json:"boundary_avg"`
	TitleAvg         float64 `json:"title_avg"`
	TopicAvg         float64 `json:"topic_avg"`
	AvgChapters      float64 `json:"avg_chapters"`
	MeanDurMin       float64 `json:"mean_dur_min"`
	ValidationPassRate float64 `json:"validation_pass_rate"`
	AvgWallTimeMs    int64   `json:"avg_wall_time_ms"`
	JudgeError       string  `json:"judge_error,omitempty"`
}

type ModelDetails struct {
	Runs           []ExtractRun  `json:"runs"`
	Judge          JudgeResult   `json:"judge"`
	ChapterCounts  []int         `json:"chapter_counts_per_run"`
	StabilityNote  string        `json:"stability_note,omitempty"`
	WorstBoundaries []BoundaryScore `json:"worst_boundaries,omitempty"`
}

func writeReport(
	outDir string,
	ep *models.Episode,
	segments []llm.EpisodeSegment,
	candidates []string,
	results map[string][]ExtractRun,
	judgments map[string]JudgeResult,
	meta ReportMeta,
) error {
	totalDurationMs := int64(0)
	if n := len(segments); n > 0 {
		totalDurationMs = segments[n-1].EndMs
	}
	report := ReportJSON{
		Meta: meta,
		Episode: EpisodeMeta{
			ID:               ep.ID,
			Name:             ep.Name,
			SourceLanguage:   ep.SourceLanguage,
			TargetLanguage:   ep.TargetLanguage,
			SegmentCount:     len(segments),
			TotalDurationMs:  totalDurationMs,
			TotalDurationMin: float64(totalDurationMs) / 60000.0,
		},
		Models: make(map[string]ModelDetails, len(candidates)),
	}

	for _, m := range candidates {
		runs := results[m]
		j, hasJudge := judgments[m]
		if !hasJudge {
			// No judge run for this candidate (--skip-judge or never
			// kicked off): keep the JudgeResult zero-valued but with
			// sentinel -1 averages so the leaderboard row + report
			// render "N/A" instead of a misleading 0.00 score.
			j = JudgeResult{
				CandidateModel: m,
				BoundaryAvg:    -1,
				TitleAvg:       -1,
				TopicAvg:       -1,
				Total:          -1,
			}
		}
		report.Leaderboard = append(report.Leaderboard, buildLeaderboardRow(m, runs, j))
		md := ModelDetails{Runs: runs, Judge: j}
		md.ChapterCounts = chapterCountsPerRun(runs)
		md.StabilityNote = stabilityNote(md.ChapterCounts)
		md.WorstBoundaries = worstN(j.BoundaryScores, 3)
		report.Models[m] = md
	}

	sortLeaderboard(report.Leaderboard)
	for i := range report.Leaderboard {
		report.Leaderboard[i].Rank = i + 1
	}

	if err := writeJSON(filepath.Join(outDir, "report.json"), report); err != nil {
		return fmt.Errorf("write report.json: %w", err)
	}
	if err := writeMarkdown(filepath.Join(outDir, "report.md"), report); err != nil {
		return fmt.Errorf("write report.md: %w", err)
	}
	return nil
}

// buildLeaderboardRow folds runs[] + judge into one comparable row.
// AvgChapters is a float because runs may disagree (e.g. 5,5,6 → 5.33);
// the report displays it with one decimal.
func buildLeaderboardRow(model string, runs []ExtractRun, j JudgeResult) LeaderboardRow {
	row := LeaderboardRow{
		Model:        model,
		JudgeTotal:   j.Total,
		BoundaryAvg:  j.BoundaryAvg,
		TitleAvg:     j.TitleAvg,
		TopicAvg:     j.TopicAvg,
		JudgeError:   j.Error,
	}
	if len(runs) == 0 {
		return row
	}
	chSum, durSum, wallSum, validN := 0.0, int64(0), int64(0), 0
	for _, r := range runs {
		chSum += float64(r.StaticMetrics.FinalChapterCount)
		durSum += r.StaticMetrics.MeanDurMs
		wallSum += r.WallTimeMs
		if r.ValidationOK {
			validN++
		}
	}
	row.AvgChapters = chSum / float64(len(runs))
	row.MeanDurMin = float64(durSum) / float64(len(runs)) / 60000.0
	row.ValidationPassRate = float64(validN) / float64(len(runs))
	row.AvgWallTimeMs = wallSum / int64(len(runs))
	return row
}

func chapterCountsPerRun(runs []ExtractRun) []int {
	out := make([]int, len(runs))
	for i, r := range runs {
		out[i] = r.StaticMetrics.FinalChapterCount
	}
	return out
}

// stabilityNote returns a short human-friendly description of how
// consistent the chapter count was across runs. Three or more runs are
// the sweet spot — fewer makes "stable" non-meaningful.
func stabilityNote(counts []int) string {
	if len(counts) <= 1 {
		return ""
	}
	mn, mx := counts[0], counts[0]
	for _, c := range counts {
		if c < mn {
			mn = c
		}
		if c > mx {
			mx = c
		}
	}
	if mn == mx {
		return fmt.Sprintf("STABLE: every run produced %d chapters", mn)
	}
	return fmt.Sprintf("VARIABLE: chapter counts ranged %d..%d across %d runs", mn, mx, len(counts))
}

// worstN returns the N lowest-scoring boundaries from the verdict,
// ordered ascending by score (worst first). Used in the per-model
// report section so the operator can spot-check which cuts the judge
// disliked most.
func worstN(scores []BoundaryScore, n int) []BoundaryScore {
	if len(scores) == 0 || n <= 0 {
		return nil
	}
	cp := append([]BoundaryScore(nil), scores...)
	sort.Slice(cp, func(i, j int) bool {
		return cp[i].BoundaryCoherence < cp[j].BoundaryCoherence
	})
	if len(cp) > n {
		cp = cp[:n]
	}
	return cp
}

// sortLeaderboard sorts by judge_total DESC; ties broken by validation
// pass rate (higher first) then mean wall time (lower first). Models
// with no judge result (Total == -1) are pushed to the bottom.
func sortLeaderboard(rows []LeaderboardRow) {
	sort.SliceStable(rows, func(i, j int) bool {
		ti, tj := rows[i].JudgeTotal, rows[j].JudgeTotal
		hasI := ti >= 0
		hasJ := tj >= 0
		if hasI != hasJ {
			return hasI
		}
		if hasI {
			if ti != tj {
				return ti > tj
			}
		}
		if rows[i].ValidationPassRate != rows[j].ValidationPassRate {
			return rows[i].ValidationPassRate > rows[j].ValidationPassRate
		}
		return rows[i].AvgWallTimeMs < rows[j].AvgWallTimeMs
	})
}

func writeMarkdown(path string, r ReportJSON) error {
	var sb strings.Builder
	fmt.Fprintf(&sb, "# Chapterize Multi-Model Benchmark — episode %d\n\n", r.Episode.ID)
	fmt.Fprintf(&sb, "- **Episode**: %q\n", r.Episode.Name)
	fmt.Fprintf(&sb, "- **Languages**: %s → %s\n", r.Episode.SourceLanguage, r.Episode.TargetLanguage)
	fmt.Fprintf(&sb, "- **Segments**: %d  (≈ %.1fmin)\n", r.Episode.SegmentCount, r.Episode.TotalDurationMin)
	fmt.Fprintf(&sb, "- **Generated**: %s\n", r.Meta.GeneratedAt.Format(time.RFC3339))
	fmt.Fprintf(&sb, "- **Judge**: `%s`  (runs/candidate: %d)\n", r.Meta.JudgeModel, r.Meta.JudgeRuns)
	fmt.Fprintf(&sb, "- **Extract runs/candidate**: %d\n\n", r.Meta.Runs)

	sb.WriteString("## Leaderboard (by judge total)\n\n")
	sb.WriteString("| Rank | Model | Total | Boundary | Title | Topic | Avg Chapters | Mean Dur (min) | Valid % | Wall Time (s) | Notes |\n")
	sb.WriteString("|---|---|---|---|---|---|---|---|---|---|---|\n")
	for _, row := range r.Leaderboard {
		notes := row.JudgeError
		if len(notes) > 60 {
			notes = notes[:57] + "..."
		}
		fmt.Fprintf(&sb,
			"| %d | `%s` | %s | %s | %s | %s | %.1f | %.1f | %.0f%% | %.1f | %s |\n",
			row.Rank, row.Model,
			fmt5(row.JudgeTotal), fmt5(row.BoundaryAvg), fmt5(row.TitleAvg), fmt5(row.TopicAvg),
			row.AvgChapters, row.MeanDurMin,
			row.ValidationPassRate*100,
			float64(row.AvgWallTimeMs)/1000.0,
			notes,
		)
	}
	sb.WriteString("\n_Total = mean(boundary, title, topic), 0..5 scale. N/A means the judge failed for that candidate (see error)._\n\n")

	sb.WriteString("## Per-model details\n\n")
	for _, row := range r.Leaderboard {
		md := r.Models[row.Model]
		fmt.Fprintf(&sb, "### %d. `%s`\n\n", row.Rank, row.Model)
		if md.StabilityNote != "" {
			fmt.Fprintf(&sb, "- %s (counts: %v)\n", md.StabilityNote, md.ChapterCounts)
		}
		fmt.Fprintf(&sb, "- Static: avg_chapters=%.1f mean_dur=%.1fmin valid_rate=%.0f%% avg_wall=%.1fs\n",
			row.AvgChapters, row.MeanDurMin,
			row.ValidationPassRate*100,
			float64(row.AvgWallTimeMs)/1000.0,
		)
		fmt.Fprintf(&sb, "- Judge: total=%s  boundary=%s  title=%s  topic=%s\n",
			fmt5(row.JudgeTotal), fmt5(row.BoundaryAvg), fmt5(row.TitleAvg), fmt5(row.TopicAvg),
		)
		if md.Judge.Error != "" {
			fmt.Fprintf(&sb, "- Judge error: `%s`\n", md.Judge.Error)
		}

		// Per-run static metrics (compact one-liner per run).
		for _, rr := range md.Runs {
			status := "ok"
			if !rr.ValidationOK {
				status = "INVALID: " + rr.ValidationError
			}
			if rr.Error != "" {
				status = "ERROR: " + rr.Error
			}
			fmt.Fprintf(&sb,
				"  - run %d: chapters=%d mean=%.1fmin merge=%d split=%d jitter=%dms wall=%.1fs (%s)\n",
				rr.RunIndex,
				rr.StaticMetrics.FinalChapterCount,
				float64(rr.StaticMetrics.MeanDurMs)/60000.0,
				rr.StaticMetrics.MergeActions,
				rr.StaticMetrics.SplitActions,
				rr.StaticMetrics.BoundaryJitterMs,
				float64(rr.WallTimeMs)/1000.0,
				status,
			)
		}

		// Worst-3 boundary spot checks for human review.
		if len(md.WorstBoundaries) > 0 {
			sb.WriteString("\n**Worst-scored boundaries (judge):**\n")
			for _, b := range md.WorstBoundaries {
				fmt.Fprintf(&sb, "- boundary %d  → score %d  — %s\n",
					b.BoundaryIdx, b.BoundaryCoherence, b.Rationale)
			}
		}

		// Chapter rationales — compact list, helps justify low title/topic scores.
		if len(md.Judge.ChapterScores) > 0 {
			sb.WriteString("\n**Per-chapter judge breakdown:**\n")
			for _, ch := range md.Judge.ChapterScores {
				fmt.Fprintf(&sb, "- ch%d  title=%d topic=%d  — %s\n",
					ch.ChapterIdx+1, ch.TitleQuality, ch.TopicCompleteness, ch.Rationale)
			}
		}

		// Pointer to the chapter-dump file for this model so the operator
		// can copy/paste segment text without spelunking through JSON.
		fmt.Fprintf(&sb, "\nFull chapter list (for spot checks): `chapters-%s.txt`\n\n", sanitiseModelName(row.Model))
	}

	sb.WriteString("\n## Recommendation\n\n")
	if top := topRecommendation(r.Leaderboard); top != "" {
		sb.WriteString(top)
	} else {
		sb.WriteString("Insufficient data — every candidate failed judge or extract.\n")
	}

	return os.WriteFile(path, []byte(sb.String()), 0o644)
}

// topRecommendation produces a 2–3 sentence recommendation for the
// production model, based on the leaderboard. Conservative: only
// recommends the top candidate if it has a valid judge total.
func topRecommendation(rows []LeaderboardRow) string {
	if len(rows) == 0 || rows[0].JudgeTotal < 0 {
		return ""
	}
	top := rows[0]
	var sb strings.Builder
	fmt.Fprintf(&sb,
		"Top candidate: **`%s`** with judge total %.2f (boundary %.2f / title %.2f / topic %.2f).\n",
		top.Model, top.JudgeTotal, top.BoundaryAvg, top.TitleAvg, top.TopicAvg,
	)
	fmt.Fprintf(&sb,
		"It produced an average of %.1f chapters per run (mean dur %.1fmin) with %.0f%% validation pass rate and %.1fs avg wall time.\n",
		top.AvgChapters, top.MeanDurMin, top.ValidationPassRate*100,
		float64(top.AvgWallTimeMs)/1000.0,
	)
	if len(rows) > 1 && rows[1].JudgeTotal >= 0 {
		gap := top.JudgeTotal - rows[1].JudgeTotal
		fmt.Fprintf(&sb,
			"Margin over runner-up `%s`: %.2f points. ",
			rows[1].Model, gap,
		)
		switch {
		case gap >= 0.5:
			sb.WriteString("This is a CLEAR win — recommend switching production GLOSSARY_MODEL to the top candidate.\n")
		case gap >= 0.2:
			sb.WriteString("Moderate margin — switch is justified but worth a second benchmark on a different episode to confirm.\n")
		default:
			sb.WriteString("Margin is within noise — DEFER decision until a multi-episode benchmark (OPT-405.5).\n")
		}
	} else {
		sb.WriteString("(only candidate with a valid judge verdict)\n")
	}
	return sb.String()
}

// fmt5 renders a 0..5 score with one decimal, or "N/A" for sentinel -1.
func fmt5(v float64) string {
	if v < 0 {
		return "N/A"
	}
	return fmt.Sprintf("%.2f", v)
}

// writeJSON marshals v with indent and writes to path. Atomicity isn't
// critical here — bench reports are written once at the end of a run.
func writeJSON(path string, v any) error {
	body, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	return os.WriteFile(path, append(body, '\n'), 0o644)
}

func readJSON(path string, into any) error {
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(body, into)
}
