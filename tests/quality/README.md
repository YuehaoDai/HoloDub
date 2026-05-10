# Quality Regression

This folder contains artifacts for production-facing quality checks.

## Goals

- verify that a completed job really produced output media
- measure rough sync quality using duration deltas
- flag segment-level TTS drift
- capture manual review notes for translation, voice, and mixing quality

## Suggested workflow

1. Run a real job to `completed`.
2. Call the API to get the final `job_id`.
3. Run the regression script:

```powershell
python .\tests\quality\run_regression.py --api-base-url http://127.0.0.1:8080 --data-root .\data --job-id 34
```

4. Compare the report against the thresholds in `manifest.example.json`.
5. Fill out `scorecard.template.md` for manual listening checks.

## Minimum acceptance

- output file exists
- output duration delta within threshold
- every segment has a TTS artifact
- every segment duration delta within threshold
- translation length ratio remains inside the configured bound

## Baseline snapshots

These JSON files are append-only quality snapshots captured against the
production binaries at well-known points in the optimisation timeline.
Each one references the OPT-IDs from
[`docs/roadmap/optimization-roadmap.md`](../../docs/roadmap/optimization-roadmap.md)
that it validates so that any future regression can be triangulated to
the exact change set that introduced it.

| File | Job / Episode | Length | Validates | Key takeaway |
|---|---|---|---|---|
| [`baseline-pre-p0.json`](./baseline-pre-p0.json) | 60s smoke | 60s | pre-OPT-001 (no cache, no judge, no tools) | Reference floor; no LLM observability |
| [`baseline-post-p0.json`](./baseline-post-p0.json) | 60s smoke | 60s | OPT-001 + OPT-002 + OPT-003 | First time cache / judge / strict tools all wired up |
| [`baseline-post-p0-10min.json`](./baseline-post-p0-10min.json) | job 130 (cancelled) | 10min | OPT-001 + OPT-002 + OPT-003 (long-form) | Discovered the OPT-001 prompt-stability bug AND the long-segment retry-oscillation bug; both became P0 follow-ups |
| [`baseline-post-p0-10min-final.json`](./baseline-post-p0-10min-final.json) | job 131 | 10min | All P0 + episode-judge MVP, with TEMPORARY env tuning (`RETRANSLATION_INITIAL_MAX_ATTEMPTS=10` / `RETRANSLATION_MIN_DRIFT_THRESHOLD=0.06`) | First fully-green 10-min run; episode judge returns `production_ready` (7-axis 0.95-1.00) |
| [`baseline-post-p0-opt402-10min.json`](./baseline-post-p0-opt402-10min.json) | job 138 | 10min | OPT-001-followup-1 + OPT-FOLLOWUP-3(a) + OPT-401 + OPT-402 (default env, no manual tuning) | Translate cache hit `0% -> 23.9%`; wall time `2365s -> 1772s` (-25%); 24/24 segments synthesised under default env thanks to in-code adaptive drift floor |
| [`opt402-79min-episode-139.json`](./opt402-79min-episode-139.json) | episode 139 (cancelled before TTS) | 79min | OPT-402 episode-level stages on long content | Episode-level ASR finished in 4.5s and glossary extraction in 3.8s on a 79-min lecture, returning 6 stable terms + 301-char reference card. Cancelled by the user awaiting OPT-403 chapterize; preserved purely as evidence that the episode-level pipeline scales beyond the 1-chapter shortcut window |
| [`episode-judge-job-131.json`](./episode-judge-job-131.json) | job 131 episode-judge | - | OPT-002 episode-level extension via `scripts/episode_judge.ps1` | qwen-max post-merge scoring; 7-axis radar input for the upcoming OPT-406 productisation |

A new baseline should be added (not edited) any time a P0 / P1 OPT lands
and meaningfully changes any per-call token counts, wall-time numbers,
judge scores or retry-oscillation behaviour. Older baselines stay as-is
so that the `_compare_to_*` cross-references in newer files stay valid.
