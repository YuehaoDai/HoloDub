# OPT-202 Speculative Ensemble — Rollout Playbook

> Status (as of `dev-win`): code + L1 (unit tests) shipped. L2/L3/L4
> staging require an operator with production credentials and `≥ 24h`
> soak windows; this doc is the script those operators follow.

## What's in this rollout

The `SegmentAgent` now has a new escalation path: when its single-model
retranslate loop is failing to converge, it can fan the same retry
input out across multiple LLM models in parallel and pick the highest-
scoring candidate via an LLM-judge pairwise comparison.

| Feature flag                  | Default | Effect                                                                                |
| ----------------------------- | ------- | ------------------------------------------------------------------------------------- |
| `ENSEMBLE_RETRANSLATE_ENABLED` | `false` | Master gate. Must also have `SEGMENT_AGENT_ENABLED=true` (OPT-201 must be live).      |
| `ENSEMBLE_MODELS`             | `deepseek-chat,qwen-plus` | Comma-separated. Lower index wins on ties (deterministic).               |
| `ENSEMBLE_JUDGE_MODEL`        | `kimi-k2.5` | Thinking-class judge. Empty falls back to `JUDGE_MODEL`.                            |

### Agent-side triggers (hard-coded in `internal/pipeline/stage_tts_agent.go`)

1. `state.AttemptsWithoutImprovement >= 2` — single-model retranslate
   has failed to improve drift twice. **Primary entry condition.**
2. `seg.meta.important == true` — operator-tagged "this segment must
   ship at maximum quality". Enables ensemble on every retranslate
   decision (still capped).
3. Judge score < 0.7 AND `state.Attempt >= 1` — the OPT-002 judge has
   weighed in and the score is in the "weak but salvageable" band.

### Cost guards (also hard-coded)

- `Config.EnsembleMaxPerSegment = 1` — a single segment can invoke
  ensemble at most ONCE; subsequent retranslate decisions go back to
  single-model (with `UseThinking` if applicable). Without this a
  pathologically non-converging segment could cost > $1 alone.
- Global episode-level ceiling stays at the rework engine layer
  (`accumulated_cost_usd`, env `EPISODE_REWORK_COST_CEILING_USD`).

## L1: unit tests (already green on `dev-win`)

```powershell
go test ./internal/agents/... ./internal/llm/... -count=1
```

What's covered:

- `RetranslateEnsemble` happy path / parallel fanout / one failure /
  all failures / context cancel / single model / judge override /
  tie-break determinism (9 tests in `internal/llm/ensemble_test.go`).
- `shouldUseEnsemble` decision table: disabled / non-convergence /
  judge-score / important / per-segment cap (5 tests in
  `internal/agents/segment_agent_ensemble_test.go`).
- End-to-end agent run with `RetranslateEnsemble`: happy path /
  unavailable falls back / failure falls back + logs / cap blocks
  second fanout / ensemble args include retry history (5 tests).

## L2: staging twin run (parity + cost comparison)

Pick **one** episode that recently failed L3 OPT-201 due to drift
(check `holodub_segment_agent_decisions_total{reason="under_run_drift"}`
or `over_short_gap`). Re-run it twice:

```powershell
# Run A: ensemble OFF (baseline).
$env:ENSEMBLE_RETRANSLATE_ENABLED = "false"
.\scripts\hot-reload.ps1
# Submit job, capture job_id_a.

# Run B: ensemble ON.
$env:ENSEMBLE_RETRANSLATE_ENABLED = "true"
$env:ENSEMBLE_MODELS = "deepseek-chat,qwen-plus"
$env:ENSEMBLE_JUDGE_MODEL = "kimi-k2.5"
.\scripts\hot-reload.ps1
# Re-submit the SAME source video, capture job_id_b.
```

For each segment, compare:

- `seg.TargetText` (which version "won")
- `seg.TTSDurationMs` (drift improvement)
- `seg.JudgeScore` (fidelity improvement)
- LLM cost from `holodub_external_calls_total` per-job filter

**Pass criteria:**

- Cost(B) / Cost(A) ≤ 1.20 across the whole episode
- Mean `JudgeScore` of segments that triggered ensemble ≥ `JudgeScore(A)` + 0.05
- No segment drift regression > 5% on segments where ensemble fired

If pass: proceed to L3. If fail: investigate (often `EnsembleModels` is
returning a model that's been silently deprecated by the provider; check
the per-candidate `Err` in the agent's `segment_agent: ensemble winner`
log line).

## L3: 79-minute golden-set regression

```powershell
$env:ENSEMBLE_RETRANSLATE_ENABLED = "true"
.\scripts\hot-reload.ps1
```

Then submit `tests/quality/opt402-79min-episode-139.json`'s source
video. Run the regression script if it's been hooked up:

```powershell
python tests\quality\run_regression.py --baseline tests\quality\opt402-79min-episode-139.json
```

**Pass criteria** (from the plan §4):

- Mean fidelity (`JudgeScore`) on the golden set ≥ baseline + 0.05
- Total cost ≤ baseline × 1.15
- Ensemble trigger rate < 10% of segments
- p95 drift ≤ baseline × 1.05 (ensemble should NOT make drift worse)

## L4: default ON (after ≥ 24h L3 soak)

```powershell
# Flip the default in .env.example so new env files pick it up.
# Existing operators must still flip their .env explicitly.
```

After ≥ 2 weeks of soak with no incident, the agent code can be
simplified (remove the fallback path warnings, hardcode default thresholds).

## Rollback

A single env flip:

```powershell
$env:ENSEMBLE_RETRANSLATE_ENABLED = "false"
.\scripts\hot-reload.ps1
```

Worker picks it up on the next stage lease (typically < 30s). No DB
migration is needed because OPT-202 stores no new persistent state on
segments — the winning model is recorded only in the agent log.

## Observability

New metric (OPT-202 piggybacks on the existing OPT-201 counter):

```text
holodub_segment_agent_decisions_total{
    decision="retranslate",
    reason="under_run_drift|over_short_gap",
    use_thinking="false",
    use_ensemble="true"
}
```

Grep the worker log for `segment_agent: ensemble winner` to see the
per-segment winning model + judge score + candidate count:

```text
INFO segment_agent: ensemble winner
  segment_id=12345 job_id=678 attempt=2
  winner_model=qwen-plus judge_score=0.91 candidate_count=2
```

Failures land on:

```text
WARN segment_agent: ensemble failed, falling back to single-model retranslate
  segment_id=12345 job_id=678 attempt=2 error="provider 500"
```

ErrEnsembleUnavailable is NOT logged at WARN (expected during rollout
when the flag is off).
