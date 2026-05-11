// Package main — cmd/chapterize-bench: OPT-405.1 multi-model
// chapterization benchmark.
//
// Drives the OPT-405 LLM-driven chapter slicing path across a list of
// candidate LLMs (DashScope-compatible) on ONE existing episode, and
// scores each candidate's output with an LLM-as-judge using a strict
// tool-call schema. The whole run is offline — it does NOT touch the
// pipeline / queue / production data — so the operator can iterate on
// prompt design + model choice without disturbing live jobs.
//
// Quick start (assuming episode 142 has already been ingested + ASR'd):
//
//	go run ./cmd/chapterize-bench \
//	  --episode 142 \
//	  --models "kimi-k2.5,kimi-k2-thinking,qwen3-235b-a22b-thinking-2507,qwen-max-latest,qwen-plus-latest,deepseek-v3" \
//	  --judge kimi-k2-thinking \
//	  --runs 3 \
//	  --out docs/opt-405/bench-baseline-2026-05-10
//
// Layout written under --out:
//
//	bench-{ts}/
//	  raw/{model}-run{i}.json        # per-run extract output + static metrics
//	  judge/{model}-judgment.json    # judge verdict (one per model, averaged across runs)
//	  chapters-{model}.txt           # plain-text chapter summary for human eyeballing
//	  report.md                      # ranked summary + per-model details + spot checks
//	  report.json                    # same data, machine-readable
//
// Exit codes: 0 on success (report written, regardless of any single
// model failing — failures are recorded as warnings inside the report).
// Non-zero only on un-recoverable infrastructure errors (config / store /
// out-dir creation).
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"holodub/internal/config"
	"holodub/internal/llm"
	"holodub/internal/models"
	"holodub/internal/observability"
	"holodub/internal/store"
)

type flags struct {
	episode    uint
	models     string
	judge      string
	runs       int
	judgeRuns  int
	out        string
	skipExtract bool
	skipJudge   bool
	dryRun     bool
}

func parseFlags() flags {
	f := flags{}
	flag.UintVar(&f.episode, "episode", 142,
		"episode id to benchmark (must already have ASR segments)")
	flag.StringVar(&f.models, "models",
		"kimi-k2.5,kimi-k2-thinking,qwen3-235b-a22b-thinking-2507,qwen-max-latest,qwen-plus-latest,deepseek-v3",
		"comma-separated list of candidate model names (DashScope-compatible)")
	flag.StringVar(&f.judge, "judge", "kimi-k2-thinking",
		"judge model used to score every candidate's output")
	flag.IntVar(&f.runs, "runs", 3,
		"number of extract runs per candidate model (used to assess stability)")
	flag.IntVar(&f.judgeRuns, "judge-runs", 1,
		"number of judge runs per candidate (averaged); 1 is usually enough")
	flag.StringVar(&f.out, "out", "",
		"output directory; defaults to docs/opt-405/bench-{timestamp}")
	flag.BoolVar(&f.skipExtract, "skip-extract", false,
		"skip the extract phase — re-judge an existing --out directory's raw/")
	flag.BoolVar(&f.skipJudge, "skip-judge", false,
		"skip the judge phase — only run extracts and write static metrics")
	flag.BoolVar(&f.dryRun, "dry-run", false,
		"load segments + print plan but do not call any LLM")
	flag.Usage = func() {
		fmt.Fprintf(flag.CommandLine.Output(), `cmd/chapterize-bench — OPT-405.1 multi-model chapterize benchmark

Usage: chapterize-bench [flags]

Examples:
  # 1 model, 1 run, no judge (smoke test the wiring on episode 142):
  chapterize-bench --episode 142 --models kimi-k2.5 --runs 1 --skip-judge

  # Full benchmark with default 6 models:
  chapterize-bench --episode 142 --runs 3 --out docs/opt-405/bench-baseline-2026-05-10

  # Re-judge an existing output dir with a different judge:
  chapterize-bench --skip-extract --out docs/opt-405/bench-baseline-2026-05-10 --judge qwen3-235b-a22b-thinking-2507

Flags:
`)
		flag.PrintDefaults()
	}
	flag.Parse()
	return f
}

func main() {
	f := parseFlags()

	cfg, err := config.Load()
	if err != nil {
		fatalf("load config: %v", err)
	}
	logger := observability.NewLogger(cfg)
	slog.SetDefault(logger)

	if cfg.OpenAIBaseURL == "" || cfg.OpenAIAPIKey == "" {
		fatalf("OPENAI_BASE_URL and OPENAI_API_KEY must be set in environment")
	}

	st, err := store.New(cfg)
	if err != nil {
		fatalf("open store: %v", err)
	}

	candidates := parseModelList(f.models)
	if len(candidates) == 0 {
		fatalf("--models is empty")
	}

	outDir := f.out
	if outDir == "" {
		outDir = filepath.Join("docs", "opt-405",
			"bench-"+time.Now().UTC().Format("20060102-150405"))
	}
	if err := os.MkdirAll(filepath.Join(outDir, "raw"), 0o755); err != nil {
		fatalf("mkdir raw: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(outDir, "judge"), 0o755); err != nil {
		fatalf("mkdir judge: %v", err)
	}

	ctx := context.Background()

	ep, segments, err := loadEpisodeSegments(ctx, st, f.episode)
	if err != nil {
		fatalf("load episode %d: %v", f.episode, err)
	}
	totalDurMin := 0.0
	if len(segments) > 0 {
		totalDurMin = float64(segments[len(segments)-1].EndMs) / 60000.0
	}
	fmt.Fprintf(os.Stderr,
		"chapterize-bench: episode=%d name=%q lang=%s->%s segments=%d total≈%.1fmin\n",
		ep.ID, ep.Name, ep.SourceLanguage, ep.TargetLanguage, len(segments), totalDurMin)
	fmt.Fprintf(os.Stderr,
		"chapterize-bench: candidates=%v runs=%d judge=%s out=%s dry_run=%v\n",
		candidates, f.runs, f.judge, outDir, f.dryRun)

	if f.dryRun {
		fmt.Fprintln(os.Stderr, "chapterize-bench: --dry-run set; exiting before any LLM call")
		return
	}

	results := make(map[string][]ExtractRun, len(candidates))
	if !f.skipExtract {
		for _, m := range candidates {
			runs := make([]ExtractRun, 0, f.runs)
			for i := 0; i < f.runs; i++ {
				slog.Info("bench extract", "model", m, "run", i+1, "of", f.runs)
				rr := runOneExtract(ctx, cfg, ep, segments, m, i+1)
				runs = append(runs, rr)
				writeJSON(filepath.Join(outDir, "raw",
					fmt.Sprintf("%s-run%d.json", sanitiseModelName(m), i+1)), rr)
			}
			results[m] = runs
		}
	} else {
		results = readExistingExtracts(filepath.Join(outDir, "raw"), candidates)
	}

	judgments := make(map[string]JudgeResult, len(candidates))
	if !f.skipJudge {
		for _, m := range candidates {
			runs := results[m]
			if len(runs) == 0 {
				slog.Warn("bench judge skipped (no extract runs)", "model", m)
				continue
			}
			judgePath := filepath.Join(outDir, "judge",
				fmt.Sprintf("%s-judgment.json", sanitiseModelName(m)))
			// Skip judge re-run when a previously-written verdict for this
			// candidate is already present AND has a valid non-sentinel
			// score AND no error string. Saves ~5–10 min per candidate
			// on a re-judge after a transient EOF or rate-limit hit.
			// Pass --judge with the same model name to reuse cache, OR
			// remove the file under judge/ to force a fresh judge.
			if cached, ok := readCachedJudgment(judgePath); ok {
				slog.Info("bench judge cached (skipping)",
					"model", m, "judge", cached.JudgeModel,
					"total", cached.Total, "from", judgePath)
				judgments[m] = cached
				continue
			}
			best := pickBestRun(runs)
			slog.Info("bench judge", "model", m, "judge", f.judge,
				"using_run", best.RunIndex, "chapters", len(best.FinalRanges))
			j := runJudge(ctx, cfg, ep, segments, m, best, f.judge, f.judgeRuns)
			judgments[m] = j
			writeJSON(judgePath, j)
		}
	}

	for _, m := range candidates {
		dumpChapterTxt(filepath.Join(outDir,
			fmt.Sprintf("chapters-%s.txt", sanitiseModelName(m))),
			m, results[m], segments)
	}

	if err := writeReport(outDir, ep, segments, candidates, results, judgments,
		ReportMeta{
			GeneratedAt: time.Now().UTC(),
			JudgeModel:  f.judge,
			Runs:        f.runs,
			JudgeRuns:   f.judgeRuns,
		}); err != nil {
		fatalf("write report: %v", err)
	}
	fmt.Fprintf(os.Stderr, "chapterize-bench: done. report at %s/report.md\n", outDir)
}

// loadEpisodeSegments fetches ep + every chapter's ASR segments and
// flattens them into a single chronologically-ordered llm.EpisodeSegment
// slice. Empty-text segments are skipped (matches the production
// pipeline's behaviour in stage_glossary_extract.go), but indices in
// the returned slice are dense (0..N-1) — those indices are what the
// LLM and the judge see as [N] tags in the user message.
func loadEpisodeSegments(ctx context.Context, st *store.Store, episodeID uint) (*models.Episode, []llm.EpisodeSegment, error) {
	ep, err := st.GetEpisode(ctx, episodeID)
	if err != nil || ep == nil {
		return nil, nil, fmt.Errorf("get episode: %w", err)
	}
	chapters, err := st.GetEpisodeChapters(ctx, episodeID)
	if err != nil {
		return nil, nil, fmt.Errorf("get chapters: %w", err)
	}
	if len(chapters) == 0 {
		return nil, nil, fmt.Errorf("episode %d has no chapters", episodeID)
	}
	out := make([]llm.EpisodeSegment, 0, 256)
	for _, ch := range chapters {
		segs, err := st.ListSegments(ctx, ch.ID, nil)
		if err != nil {
			return nil, nil, fmt.Errorf("list segments for chapter %d: %w", ch.ID, err)
		}
		for _, seg := range segs {
			if strings.TrimSpace(seg.SourceText) == "" {
				continue
			}
			out = append(out, llm.EpisodeSegment{
				StartMs:      ch.ChapterStartMs + seg.StartMs,
				EndMs:        ch.ChapterStartMs + seg.EndMs,
				Text:         strings.TrimSpace(seg.SourceText),
				SpeakerLabel: seg.SpeakerLabel,
			})
		}
	}
	if len(out) == 0 {
		return nil, nil, fmt.Errorf("episode %d has no usable segments", episodeID)
	}
	return ep, out, nil
}

func parseModelList(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	seen := map[string]bool{}
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" || seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	return out
}

// sanitiseModelName turns a model id like "qwen3-235b-a22b-thinking-2507"
// into a filesystem-safe tag. Periods (kimi-k2.5) are replaced with
// underscores so glob patterns like *.json don't get tripped up.
func sanitiseModelName(m string) string {
	r := strings.NewReplacer(".", "_", "/", "_", " ", "_")
	return r.Replace(m)
}

// pickBestRun picks the extract run we hand to the judge. Strategy: pick
// the first run that PASSED ValidateLLMPlan; fall back to the first run.
// We deliberately do NOT score across runs — the judge is expensive and
// the bench's real interest is "what does this model produce typically",
// not "how stable is it". The runs[] field in the report still surfaces
// stability via min/max chapter count.
func pickBestRun(runs []ExtractRun) ExtractRun {
	for _, r := range runs {
		if r.ValidationOK && len(r.FinalRanges) > 0 {
			return r
		}
	}
	return runs[0]
}

// dumpChapterTxt writes a plain-text chapter list for human spot checks.
// Filename is namespaced by model so re-running with a different
// candidate doesn't overwrite (per lessons-learned #3).
func dumpChapterTxt(path, model string, runs []ExtractRun, segments []llm.EpisodeSegment) {
	if len(runs) == 0 {
		_ = os.WriteFile(path, []byte(model+": no runs\n"), 0o644)
		return
	}
	r := pickBestRun(runs)
	var sb strings.Builder
	fmt.Fprintf(&sb, "model: %s\n", model)
	fmt.Fprintf(&sb, "best_run: %d (validation_ok=%v) chapters=%d\n",
		r.RunIndex, r.ValidationOK, len(r.FinalRanges))
	fmt.Fprintf(&sb, "static metrics: mean=%.1fmin min=%.1fmin max=%.1fmin merge=%d split=%d\n\n",
		float64(r.StaticMetrics.MeanDurMs)/60000.0,
		float64(r.StaticMetrics.MinDurMs)/60000.0,
		float64(r.StaticMetrics.MaxDurMs)/60000.0,
		r.StaticMetrics.MergeActions,
		r.StaticMetrics.SplitActions)
	for i, ch := range r.FinalRanges {
		titleSrc := ""
		titleTgt := ""
		if i < len(r.FinalTitles) {
			titleSrc = r.FinalTitles[i].TitleSource
			titleTgt = r.FinalTitles[i].TitleTranslated
		}
		fmt.Fprintf(&sb, "ch%02d [%s — %s] %.1fmin segs[%d..%d]\n  src: %s\n  tgt: %s\n\n",
			i+1,
			formatMMSS(ch.StartMs),
			formatMMSS(ch.EndMs),
			float64(ch.EndMs-ch.StartMs)/60000.0,
			ch.StartSegmentIdx,
			ch.EndSegmentIdx,
			titleSrc,
			titleTgt,
		)
		// First + last segment text on each side of the boundary for spot checks.
		if ch.StartSegmentIdx >= 0 && ch.StartSegmentIdx < len(segments) {
			fmt.Fprintf(&sb, "  first_seg: %s\n",
				truncate(segments[ch.StartSegmentIdx].Text, 120))
		}
		if ch.EndSegmentIdx >= 0 && ch.EndSegmentIdx < len(segments) {
			fmt.Fprintf(&sb, "  last_seg:  %s\n\n",
				truncate(segments[ch.EndSegmentIdx].Text, 120))
		}
	}
	_ = os.WriteFile(path, []byte(sb.String()), 0o644)
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

func formatMMSS(ms int64) string {
	if ms < 0 {
		ms = 0
	}
	totalSec := ms / 1000
	mm := totalSec / 60
	ss := totalSec % 60
	return strconv.FormatInt(mm, 10) + ":" + leftPadZero(ss, 2)
}

func leftPadZero(n int64, w int) string {
	s := strconv.FormatInt(n, 10)
	for len(s) < w {
		s = "0" + s
	}
	return s
}

// readCachedJudgment returns a previously-written valid judgement, or
// (zero, false) if the file is missing / unparseable / from a different
// judge / errored. Used to skip judge re-runs in --skip-extract retries
// after a transient network glitch — only the failed entries get re-run.
//
// "Valid" requires:
//   - file exists and parses
//   - error == "" (we shouldn't reuse a cached failure)
//   - Total >= 0 (sentinel -1 means "no scores collected")
//
// We deliberately do NOT cross-check the judge model id — operators may
// rename their --judge flag between runs and still want the cached
// verdict; the JudgeModel field IS still surfaced in the report so the
// reader sees who scored what. If you need to FORCE a fresh judge,
// delete the JSON file or pass a different --out directory.
func readCachedJudgment(path string) (JudgeResult, bool) {
	var j JudgeResult
	if err := readJSON(path, &j); err != nil {
		return JudgeResult{}, false
	}
	if j.Error != "" {
		return JudgeResult{}, false
	}
	if j.Total < 0 {
		return JudgeResult{}, false
	}
	return j, true
}

// readExistingExtracts re-loads the raw/{model}-run*.json files written
// by a previous run so --skip-extract can re-judge without paying for
// the extract calls again. Files that fail to parse are skipped with a
// warning — partial data is fine for re-judging.
func readExistingExtracts(dir string, models []string) map[string][]ExtractRun {
	out := make(map[string][]ExtractRun, len(models))
	entries, err := os.ReadDir(dir)
	if err != nil {
		slog.Warn("bench --skip-extract: read raw dir failed", "dir", dir, "error", err)
		return out
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		var rr ExtractRun
		if err := readJSON(filepath.Join(dir, e.Name()), &rr); err != nil {
			slog.Warn("bench --skip-extract: parse failed", "file", e.Name(), "error", err)
			continue
		}
		out[rr.Model] = append(out[rr.Model], rr)
	}
	for k := range out {
		sort.Slice(out[k], func(i, j int) bool {
			return out[k][i].RunIndex < out[k][j].RunIndex
		})
	}
	return out
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "chapterize-bench: "+format+"\n", args...)
	os.Exit(1)
}
