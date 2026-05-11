# OPT-405.1 Chapterize Multi-Model Benchmark

`cmd/chapterize-bench` is an offline tool that compares N candidate
LLMs on the OPT-405 LLM-driven chapter-slicing task and ranks them
with an LLM-as-judge. It runs entirely OUTSIDE the production pipeline
— no queue writes, no chapter Job mutations — so the operator can
iterate on prompts and model choice without disturbing live jobs.

## When to use it

- Considering a new model for `GLOSSARY_MODEL` (which also drives
  OPT-405 chapter cuts)
- Validating a prompt change in `internal/llm/glossary.go` against the
  baseline by re-running the benchmark and diffing `report.md`
- Triaging "the chapter cuts feel worse since last week" — capture a
  new run vs the most recent committed `bench-baseline-*` to see what
  changed

## Quick start

```powershell
# Smoke test (1 model, 1 run, no judge — confirms wiring + DB access):
go run ./cmd/chapterize-bench --episode 142 --models kimi-k2.5 --runs 1 --skip-judge

# Full 6-model benchmark with default judge:
go run ./cmd/chapterize-bench `
  --episode 142 `
  --models "kimi-k2.5,kimi-k2-thinking,qwen3-235b-a22b-thinking-2507,qwen-max-latest,qwen-plus-latest,deepseek-v3" `
  --judge kimi-k2-thinking `
  --runs 3 `
  --out docs/opt-405/bench-baseline-2026-05-10
```

## Flags

| flag | default | meaning |
|---|---|---|
| `--episode` | `142` | episode id to benchmark; must already have ASR segments |
| `--models` | 6 default candidates | comma-separated model list (DashScope-compatible) |
| `--judge` | `kimi-k2-thinking` | model used to score every candidate's output |
| `--runs` | `3` | extract runs per candidate, used to assess stability |
| `--judge-runs` | `1` | judge runs per candidate (averaged); 1 is usually enough |
| `--out` | `docs/opt-405/bench-{ts}` | output directory |
| `--skip-extract` | `false` | skip extract; re-judge an existing `--out`'s `raw/` |
| `--skip-judge` | `false` | skip judge; only collect static metrics |
| `--dry-run` | `false` | load segments + print plan, no LLM calls |

## Output layout

```
{out}/
  raw/{model}-run{i}.json     # per-run extract output + static metrics
  judge/{model}-judgment.json # judge verdict (averaged across judge-runs)
  chapters-{model}.txt        # plain-text chapter list for human eyeball
  report.md                   # ranked summary + per-model details + spot checks
  report.json                 # same data, machine-readable
```

Note: chapter-text dumps are namespaced by model (`chapters-kimi-k2_5.txt`,
`chapters-qwen-max-latest.txt`, ...) so re-running with a different
candidate set never overwrites a previous snapshot. This follows the
lessons-learned #3 pattern (output paths must include all variation
dimensions to prevent cross-version collision).

## Cost / time budget

For the default 6 models × 3 runs + 1 judge on episode 142
(≈79min, 176 segments, ~14k chars transcript):

- Extract: 18 calls × 1–3 min ≈ 30–50 min wall time
- Judge: 6 calls × 1–2 min ≈ 10 min
- Total: ≈ 40–60 min wall, ≈ ¥30 in DashScope spend

Most calls run sequentially because the goal is per-model
reproducibility, not throughput. If you need a faster turnaround,
shrink `--models` first (e.g. compare 2–3 models).

## What the judge looks at

The judge gets the FULL transcript + the candidate chapter plan
(start/end segment indices + bilingual titles + summary), and emits a
single `score_chapter_cuts` tool call with two arrays:

- **boundary_scores** — per cut between consecutive chapters: 0..5
  on whether the cut lands at a real theme transition
- **chapter_scores** — per chapter: 0..5 on title quality + 0..5 on
  topic completeness (does the chapter say what it set out to say
  without spilling into the next one?)

Composite Total = mean of the three averaged axes (boundary, title,
topic). 1-chapter plans don't get a boundary score — Total then
averages title + topic only, so a degenerate "everything is one chapter"
candidate isn't auto-ranked at the bottom. The judge is the same model
for every candidate so rankings are on a single yardstick.

## Caveats

- Thinking-mode models (kimi-k2-thinking, qwen3-*-thinking) cannot
  be force-pinned to a specific tool. The bench falls back to
  `tool_choice="auto"` for those; the system + user prompts are
  explicit enough that they still call the tool, but if a thinking
  model returns prose instead, the candidate's verdict will say
  `model returned no tool call`. Switch to a non-thinking judge or
  retry — that path is rare in practice.
- The benchmark uses ONE episode by design (ep 142). Cross-episode
  generalisation is OPT-405.5 future work; treat top-1 here as a
  strong signal but not a final verdict before shipping.
- We deliberately do NOT take the average over multiple extract runs
  for the judge — the bench's interest is "what does this model
  produce typically", not "how stable across runs". Stability is
  reported separately as the chapter-count range across runs.

## Adding a new candidate model

1. Confirm the model id exists on DashScope (`curl https://dashscope.aliyuncs.com/compatible-mode/v1/models -H "Authorization: Bearer $env:OPENAI_API_KEY"`)
2. Add it to `--models`
3. If it's a thinking model whose name doesn't contain `thinking` (rare),
   extend `isThinkingModelName` in `internal/llm/glossary.go` and
   `isThinkingModel` in `cmd/chapterize-bench/judge.go`

## Reading the report

Open `report.md`. Top section is the leaderboard. Per-model detail
shows:

- per-run static metrics (chapters, mean dur, merge/split actions)
- judge axis breakdown
- the 3 worst-scored boundaries with the judge's rationale (jump
  straight to these in `chapters-{model}.txt` for spot checks)
- per-chapter title + topic verdict

When the leaderboard's top-1 has a >0.5 margin over runner-up the
recommendation says "CLEAR win — switch production GLOSSARY_MODEL".
Smaller margins are flagged for re-running on a different episode.
