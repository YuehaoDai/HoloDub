# OPT-201 SegmentAgent ReAct — L2 → L3 → L4 Rollout

This doc is the operations playbook for flipping `SEGMENT_AGENT_ENABLED`
in production. It is **not** auto-runnable: every level requires manual
inspection of staging logs + a green light from the on-call before
promoting.

> Owner: pipeline platform. Last update: PR-5 of the OPT-201 plan.

---

## Pre-flight (once)

1. Make sure `docs/opt-201/baseline-legacy-79min.json` exists. If not,
   run the legacy path against the canonical 79min episode and capture
   the metrics described in [§ L3](#l3-79min-real-episode-flag-on)
   before flipping the flag.
2. Tag the rollback target image:

   ```powershell
   docker tag holodub-api:latest holodub-api:rollback-pre-opt201
   docker tag holodub-worker:latest holodub-worker:rollback-pre-opt201
   ```

## L1 — Local unit + integration

Already covered by CI:

```powershell
go vet ./...
go test ./internal/agents/... ./internal/pipeline/...
```

Expected: `holodub/internal/agents` passes **241 cases** (62 single
`Decide` cases + ~130 sweep cases + 11 end-to-end agent cases + 9 fake-
tools self-tests). Anything below means the agent's decision matrix
regressed.

## L2 — Staging binary parity (60s + 10min mock video)

Goal: prove that, **given the same TTS adapter responses and the same
LLM retranslate responses**, the agent produces a byte-identical
output to the legacy path. We compare:

| Field                                | Comparison           |
| ------------------------------------ | -------------------- |
| `segments[*].target_text`            | exact string         |
| `segments[*].tts_audio_rel_path`     | exact string         |
| `segments[*].tts_duration_ms`        | exact int64          |
| `segments[*].status`                 | exact string         |
| Per-segment retry count              | within ±0 attempts   |
| Final `JobStatus`                    | exact string         |

> **Why exact equality?** Both paths use the same `tts.EffectiveDriftThreshold`
> / `tts.AdaptiveMinDriftThreshold` / `tts.EffectiveBorrowDriftPct`
> helpers, the same retranslate prompt, and the same TTS adapter. The
> only difference is the loop driver. Any drift means a programming
> error in either the agent or the legacy code — both should be the
> committee for the bug.

Procedure:

```powershell
# 1. Start ml-service in deterministic mock mode.
$env:ML_TTS_BACKEND="silence"; $env:ML_ASR_BACKEND="mock"
docker compose up --build -d ml-service postgres redis

# 2. Run baseline with flag OFF.
$env:SEGMENT_AGENT_ENABLED="false"
.\scripts\hot-reload.ps1
# trigger a 60s smoke video, capture job_id_legacy

# 3. Run candidate with flag ON.
$env:SEGMENT_AGENT_ENABLED="true"
.\scripts\hot-reload.ps1
# trigger the SAME 60s video, capture job_id_agent

# 4. Diff the two jobs.
.\scripts\opt201-diff-jobs.ps1 -LegacyJobID $job_id_legacy -AgentJobID $job_id_agent
```

Expected output: the diff script prints `OK: 9 segments, 0 mismatches`.

**Gate to L3**: 0 diffs on the 60s smoke AND the 10min episode
(`baseline-post-p0-10min-final.json`).

If the 10min job shows ≥ 1 mismatch:

1. Capture the offending segment_id + agent state from the structured
   `agent_decision` log lines.
2. Reproduce in `internal/agents/segment_agent_test.go` as a new fake-
   tools trajectory (use `RecordedSynthesize` / `RecordedRetranslate`
   from the staging logs to seed the fixture).
3. Fix the parity divergence and rerun L1.
4. Only then re-attempt L2.

## L3 — 79min real episode (flag ON)

Goal: prove that on a real GPU + real LLM call sequence, the agent
matches or beats the legacy path on the production-relevant metrics.

```powershell
$env:SEGMENT_AGENT_ENABLED="true"
.\scripts\hot-reload.ps1
# enqueue the 79min canonical episode (job ~ 139 / episode 139)
```

Capture into `docs/opt-201/baseline-agent-79min-<git_sha>.json`:

```json
{
  "git_sha": "<short>",
  "config": {"SEGMENT_AGENT_ENABLED": true},
  "wall_time_sec": ...,
  "drift": {"p50": ..., "p95": ..., "p99": ...},
  "retry_counts": {"p50": ..., "p95": ...},
  "thinking_escalations_total": ...,
  "best_restore_invocations": ...,
  "judge_score_mean": ...,
  "cost_usd_total": ...
}
```

Compare against `baseline-legacy-79min.json`:

| Metric                | Acceptance |
| --------------------- | ---------- |
| `drift.p95`           | ≤ legacy × 1.05 |
| `wall_time_sec`       | ≤ legacy × 1.20 (≤ 20% latency regression OK during rollout) |
| `cost_usd_total`      | ≤ legacy × 1.10 |
| `judge_score_mean`    | ≥ legacy − 0.01 (no regression) |
| `thinking_escalations_total` | within ±10% of legacy |

**Gate to L4**: every metric inside the acceptance band AND no
unexpected segment failures in the agent log.

## L4 — Default ON

After ≥ 1 week at flag=true on staging with no incidents:

1. Bump `.env.example`:

   ```
   SEGMENT_AGENT_ENABLED=true   # OPT-201 default ON since vX.Y.Z
   ```

2. Roll forward production env files.
3. Tag release: `git tag v1.6.0-opt-201-on`.
4. Watch the dashboard for 48h, looking for:

   - `holodub_segment_agent_decisions_total{decision="retranslate", reason="retranslate_failed"}` spikes (would indicate transient LLM provider issues feeding the legacy-contract fallback).
   - `agent_decision` log lines with `reason="no_more_attempts"` or
     `reason="clip_overflow"` — those used to be rare; if the rate
     doubles, the agent is exhausting retries more aggressively.
   - `holodub_segment_agent_decisions_total{use_thinking="true"}`
     vs the matching legacy `holodub_external_calls_total{...}` ratio.

## L5 — Delete legacy path (PR-6)

After ≥ 2 weeks of L4 with no rollback:

- Delete `processOneTTSSegment`'s legacy `for attempt := ...` body (lines
  ~257–501 of `internal/pipeline/stage_tts.go`).
- Inline `runSegmentAgentV2` into `processOneTTSSegment`.
- Drop the `SEGMENT_AGENT_ENABLED` flag from `internal/config/config.go`
  and `.env.example` (keep a one-line comment for git archaeology).
- Update the optimization roadmap: OPT-201 `status=done`.
- Tag `v1.6.1-opt-201-cleanup`.

## Rollback

At any L2/L3/L4 stage:

```powershell
$env:SEGMENT_AGENT_ENABLED="false"
.\scripts\hot-reload.ps1
# new jobs use legacy path. In-flight jobs are not affected
# (each segment decides at top-of-stage which path to use).
```

If the binaries themselves regressed (e.g. legacy path is calling a
function that was renamed by mistake), rollback to the pre-OPT-201
tag:

```powershell
docker tag holodub-api:rollback-pre-opt201 holodub-api:latest
docker tag holodub-worker:rollback-pre-opt201 holodub-worker:latest
docker compose -f docker-compose.yml restart api worker
```

Target rollback time: ≤ 1 minute (testing-and-rollout.mdc §8).
