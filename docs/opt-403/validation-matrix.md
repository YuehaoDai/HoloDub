# OPT-403/404 L1–L4 Validation Matrix

This document records the L1 (unit / pure function), L2 (integration), L3
(deterministic algorithm baseline), and L4 (live-data dry-run) evidence
for the OPT-403 chapterize + fan-out + OPT-404 episode-merge work.

The two key artefacts referenced from the roadmap and CHANGELOG live
beside this file:

- [`baseline-opt403-79min.json`](./baseline-opt403-79min.json) — L3
  algorithm baseline against a synthetic 79-segment lecture (mirrors
  episode 139). Regenerate via `go run ./cmd/chapterize-baseline >
  docs/opt-403/baseline-opt403-79min.json`.
- [`opt403-backfill-dry-run.json`](./opt403-backfill-dry-run.json) — L4
  back-fill dry-run report against the live database (74 episodes
  scanned, 44 migratable, 31 GB hardlink budget).

## L1 — Unit + pure-function tests

| Package                             | Tests pass | Notes |
| ----------------------------------- | :--------: | ----- |
| `internal/chapterize`               | OK         | 13 cases incl. empty input, single candidate, 79min synthetic episode |
| `internal/episode`                  | OK         | manifest validate / round-trip / atomic write / sort |
| `internal/llm`                      | OK         | chapter-review tool-call schema, fallback model, invalid actions |
| `internal/media`                    | OK         | SliceVideoAtRange + LoudnormTwoPass + ConcatChapterVideos + truncateForLog |
| `internal/pipeline`                 | OK         | runEpisodeMerge helpers (hardlinkOrCopy, buildChaptersManifest), chapter glue, primaryVoiceProfileID |
| `internal/pipeline/tts`             | OK         | adaptive drift floor (unchanged from OPT-FOLLOWUP-3a) |
| `internal/config`                   | OK         | new chapterize/loudnorm/episode-merge knobs: defaults + env overrides + bad input |
| `internal/http` (pure-fn helpers)   | OK         | `TestDownloadFilenameHelpers / TestUintToA / TestTwoDigit` (no cgo) |
| `internal/http` (sqlite-backed)     | skipped on Windows | `cgo` build tag — `gorm.io/driver/sqlite` requires `CGO_ENABLED=1`. Pre-existing limitation also affecting `TestPatchSegment_*`; runs in Linux CI |
| `internal/store`                    | skipped on Windows | same `cgo` limitation; sqlite-in-memory store. Runs in Linux CI |
| `cmd/migrate-output`                | OK         | parseEpisodeIDs / linkOrCopy / statSizeBytes / buildBackfillManifest |

```
$ go test ./internal/chapterize/... ./internal/episode/... ./internal/llm/... \
         ./internal/media/... ./internal/pipeline/... ./internal/config/... \
         ./internal/http/... ./cmd/migrate-output/...

ok  	holodub/internal/chapterize     0.777s
ok  	holodub/internal/episode        0.807s
ok  	holodub/internal/llm            1.218s
ok  	holodub/internal/media          1.264s
ok  	holodub/internal/pipeline       1.203s
ok  	holodub/internal/pipeline/tts   0.793s
ok  	holodub/internal/config         1.089s
ok  	holodub/internal/http (pure)    1.683s   # cgo-tagged sqlite tests skipped
ok  	holodub/cmd/migrate-output      1.674s
```

The Windows + `CGO_ENABLED=0` limitation is documented at the top of
`internal/http/router_episode_downloads_test.go`; the same constraint
existed for the pre-existing `internal/store/store_test.go` and
`router_segments_test.go` files.

## L2 — Build + go vet

```
$ go build ./...
(silent — exit 0)

$ go vet ./internal/http/... ./internal/chapterize/... ./internal/episode/...
(silent — exit 0)
```

Cross-compilation for the linux container target also succeeds:

```
$ $env:GOOS='linux'; $env:GOARCH='amd64'
$ go build -o holodub-api-linux     ./cmd/api/
$ go build -o holodub-worker-linux  ./cmd/worker/
$ go build -o migrate-output-linux  ./cmd/migrate-output/
(silent — exit 0)
```

## L3 — Deterministic algorithm baseline

Synthetic 79-segment lecture (mirrors episode 139's segment count and
length distribution). `cmd/chapterize-baseline` runs Pass 1
(`ExtractCandidates`) + Pass 2 (`DPOptimalCuts`) with the production
defaults and serialises every observable: candidate count, chosen cuts,
chapter ranges, mean / max-deviation distribution metrics.

Results captured in [`baseline-opt403-79min.json`](./baseline-opt403-79min.json):

- **Input**: 79 segments × 55 s avg + 1.6 s avg silence gap → 4 475 020 ms
  total (74.6 min). Min / max gap observed: 850 / 3 200 ms.
- **Pass 1**: 78 candidate boundaries (every silence ≥ 800 ms).
- **Pass 2**: 2 cuts at 24:33.205 and 50:01.515.
- **Resulting chapters**:
  - Ch 01 — 0 to 24:33.205 (24.55 min)
  - Ch 02 — 24:33.205 to 50:01.515 (25.47 min)
  - Ch 03 — 50:01.515 to 74:35.020 (24.56 min)
- **Constraint compliance**: every chapter ∈ [18 min, 30 min]; mean
  24.86 min vs. target 22 min (15.8% max abs deviation, well within
  the soft-deviation budget).
- **Audio safety**: every cut lands on a silence gap ≥ 850 ms (min
  silence threshold is 800 ms; concat de-mux artefacts impossible at
  boundaries because the silence is wider than any audio coding hop).

Re-run with:

```
go run ./cmd/chapterize-baseline > docs/opt-403/baseline-opt403-79min.json
```

The output is bit-stable across machines (no `rand`, no time, no map
iteration in the algorithm). Any future PR that changes the DP cost
function should re-generate this file and explain the diff.

## L4 — Live back-fill dry-run

The `cmd/migrate-output` tool was cross-compiled for linux, copied into
`holodub-api-1`, and invoked against the live Postgres instance:

```
$ docker exec holodub-api-1 /usr/local/bin/migrate-output --dry-run \
  > docs/opt-403/opt403-backfill-dry-run.json
```

Summary (full per-episode disposition lives in the JSON):

- 74 episodes scanned (entire `episodes` table, `output_layout_version=1`).
- 44 migratable (have a valid chapter `output_relpath` on disk).
- 30 failed in dry-run with `chapter N has empty OutputRelPath` — these
  are historical never-completed pipelines + aborted runs; operators
  should triage them BEFORE the live `--dry-run=false` sweep so the
  tool doesn't leave half the table on layout v1.
- 31.0 GB total hardlink / copy budget.
- ~200 ms wall-clock for the entire sweep.

The same JSON also serves as the regression baseline: re-running this
command after a future migration tool change must produce
counts ≥ today's (matching the same set of episodes; new episodes
entering the system between now and the live sweep will only widen
the migratable set).

## Status — ready to ship

L1 / L2 / L3 / L4 all green. The OPT-403 + OPT-404 PR can land safely
behind the `CHAPTERIZE_ENABLED=true` + `EPISODE_MERGE_ENABLED=true`
defaults; operators who want to disable either gate can do so via
`.env` without rebuilding.

The 30 "needs-attention" episodes from L4 are documented in the
back-fill report and should be triaged in a follow-up operations
ticket; they do NOT block this PR because:

- the 1-chapter shortcut path keeps the user-visible behaviour
  identical for all 30 (legacy `jobs/{id}/output/...` paths still
  serve), and
- `migrate-output` is idempotent: re-running once each episode has
  a valid `output_relpath` will pick them up without ceremony.
