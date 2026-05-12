# Changelog

All notable changes to HoloDub are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once we cut a tagged release.

## [Unreleased]

> Forward-looking optimization items (planned / in_progress) are tracked
> separately in [docs/roadmap/optimization-roadmap.md](docs/roadmap/optimization-roadmap.md).
> Items only land in this changelog after they ship and pass L4 rollout;
> each entry below should reference its `(OPT-XXX)` ID when applicable.

### Added

- **Chapter 2 drift fix — PR-1 bug bundle (OPT-201-followup-1,
  OPT-202-followup-1, OPT-204-followup-1; code-complete, L2 pending)**:
  three independent fixes shipped together because they target the
  same chapter-2-of-episode-143 incident (job 154 had clusters of
  long segments accepting via `judge_veto_drift` with 8–10 % drift
  the operator considered audible).
  - **B1 — DUBBING_PLAN JSON robustness** (OPT-204-followup-1):
    `dubbingPlanSystemPrompt` now explicitly forbids ASCII `"` inside
    the `translation` field and demonstrates `「」/『』` typographic
    quotes instead. A single-pass recovery helper
    `tryRecoverDubbingPlanJSON` in `internal/llm/dubbing_plan.go`
    repairs the previously-observed "LLM wrote `他说"是的"` and broke
    the top-level parse" failure mode: it isolates the translation
    field with a non-greedy regex, alternates ASCII `"` to
    typographic open/close, and refuses to claim success unless the
    rebuilt JSON still has a non-empty `translation` string AND a
    valid `emotion` object AND a `pacing` string (so a regex that
    accidentally swallows downstream fields doesn't silently corrupt
    the parse). New Prometheus counter
    `holodub_llm_recovered_parse_total{operation}` tracks how often
    recovery fires — when it stops growing, the helper can be
    removed. Five new tests in `internal/llm/dubbing_plan_test.go`
    cover the happy path, the no-quotes early-out, the no-translation-
    field branch, the truncated-middle sanity check, and an end-to-
    end provider stub.
  - **B2 — ensemble abs-drift trigger** (OPT-202-followup-1):
    `shouldUseEnsemble` in `internal/agents/segment_agent.go` now
    fires when `state.Attempt >= 2 && obs.AbsDrift >
    AdaptiveMaxAcceptableDrift(targetSec)` — closing the gap the
    chapter-2 log showed (slow linear convergence kept
    `AttemptsWithoutImprovement` at 0 / 1 so the non-convergence
    trigger never fired). The new trigger reuses the same adaptive
    band as the VETO branch, so the two work in lockstep: a segment
    that ensemble fails to bring inside the band gets one more
    ensemble attempt (via the raised cap, see B3) before falling
    back to plain retranslate. Four new tests in
    `segment_agent_ensemble_test.go` cover the trigger above-band,
    the no-trigger inside-band case, the cap interaction, and the
    `attempt >= 2` minimum.
  - **B3 — per-segment ensemble cap raised + over-short-gap escape**
    (OPT-201-followup-1): `EnsembleMaxPerSegment` default lifted
    from 1 → 2 (`internal/agents/segment_agent.go` line ~270 and
    `internal/agents/types.go` doc); a new `over_short_gap_stuck`
    decision in `Decide` accepts the current audio with a clip when
    `AttemptsWithoutImprovement >= 4 && Attempt >= MaxAttempts-3` —
    reproducing the segment-10186-style deadlock saw retries burn
    14× the single-segment LLM cost without converging. A new
    `ENSEMBLE_MAX_PER_SEGMENT` env (default 2) lets operators
    revert to the conservative budget without a rebuild. Coverage:
    `TestDecide_OverShortGapStuckEscape` covers four state combos
    (escape fires, escape blocked by clip_overflow short-circuit at
    Attempt==MaxAttempts, AwI below threshold, attempt too early
    in the window).
- **Chapter 2 drift fix — PR-2 prompt + threshold tuning
  (OPT-204-followup-2, OPT-002-followup-5; code-complete, L2 pending)**:
  three coordinated tuning changes that complement PR-1 by shifting
  WHEN drift is acceptable (VETO bands tightened) and HOW the LLM
  is asked to fix it (retranslate prompt gives concrete expansion
  guidance).
  - **P1 — retranslate prompt teaches expansion** (OPT-204-followup-2):
    `retranslateWithConstraintModel` in `internal/llm/client.go` now
    adds an `[Adaptation strategy]` block to the system prompt when
    `direction == "under"`: it states the Chinese-vs-English density
    ratio (30–40 % shorter), lists four concrete expansion techniques
    (restore pronouns, four-character idioms, expanded acronyms,
    clarifying clauses) and explicitly grants permission to
    paraphrase aggressively at drift > 15 %. The user message
    switches from `"make minimal edits to THIS text"` to
    `"you may rewrite the translation more freely"` at the same
    > 15 % threshold; below that, the conservative phrasing stays
    so easy convergence cases don't lose stability. Four new tests
    in `internal/llm/client_test.go` use a request-capturing stub
    to byte-assert the prompt content: under-run includes the
    block, over-run omits it (compression is already handled by
    `charTargetInstruction`), severe under-run loosens the user
    instruction, mild under-run keeps the minimal-edits phrasing.
  - **T1 — AdaptiveMaxAcceptableDrift bands tightened**
    (OPT-002-followup-5): bands cut from 10 / 6 / 3 % to 8 / 5 /
    2.5 % at the ≥ 20 s, 5–20 s, < 5 s tiers respectively. Existing
    VETO test fixtures updated to the new boundaries plus a new
    regression case `tightened-band-rejects-10pct-on-long-segment`
    that fixes the tightening behaviour against future drift. The
    abs-drift ensemble trigger (B2) shares this same function, so
    tightening here also makes ensemble escalation more aggressive
    — by design.
  - **T2 — RETRANSLATION_MAX_ATTEMPTS default 10 → 30**: only the
    manual-retry path uses this value (pipeline initial run keeps
    `RETRANSLATION_INITIAL_MAX_ATTEMPTS=50`). Operators reported
    10 was too low when iterating from the UI's 重跑 button.
    Updated `.env`, `.env.example`, `internal/config/config.go`,
    and both READMEs.
  - **Operator playbook** in
    [docs/chapter2-drift-fix/pr3-rerun-playbook.md](docs/chapter2-drift-fix/pr3-rerun-playbook.md)
    documents the L2 rerun procedure, four KPI tracking signals,
    the keep / rollback decision matrix and an opt-in episode 143
    `rework_status` reset query for whole-episode replays.
- **Three-tier baseline regression harness (Quality Mainline Q PR-14)**:
  new `tests/quality/run_baseline_diff.py` complements the existing
  pass/fail `run_regression.py` with a *relative* regression gate:
  given a baseline JSON (60s / 10min / 79min) and a current job, it
  computes per-metric deltas (`drift_p50_sec`, `drift_p95_sec`,
  `max_segment_drift_sec`, `cost_usd_total`, `wall_time_sec`,
  `judge_score_mean`) and tags each row PASS/WARN/FAIL. Defaults
  follow `testing-and-rollout.mdc` §7: drift / cost / wall-time fail
  at +20%, judge score is tighter at -5%. Two modes: `collect` (pull
  a finished job over HTTP into a baseline-shaped JSON) and `diff`
  (compare two reports; exits non-zero on FAIL, suitable for CI
  gating). A `normalize_legacy_baseline` adapter reads the existing
  three on-disk baseline files (`baseline-pre-p0.json`,
  `baseline-post-p0-10min-final.json`, `opt402-79min-episode-139.json`)
  in their original shape (no rewrites required) and handles PowerShell
  UTF-8 BOMs transparently. L1 ships with 18 unit tests covering the
  percentile helper, regression-direction logic for both higher-is-worse
  and higher-is-better metrics, n/a-tolerance for missing baseline
  fields, FAIL aggregation, the legacy adapter, and a self-diff
  smoke test against the three on-disk baselines. Operator runbook in
  [docs/quality-mainline-q/pr14-regression-baselines.md](docs/quality-mainline-q/pr14-regression-baselines.md)
  walks through the L1→L2→L3 sequence, four-step ordered flag rollback
  if any tier fails, and the artifact-storage convention
  (`docs/quality-mainline-q/results/YYYY-MM-DD-{60s,10min,79min}.json`).
- **Structured emotion / pacing / emphasis translate output (OPT-204,
  code-complete; L2/L3 staging pending)**: the translate stage now has
  an optional strict-tool path (`internal/llm/dubbing_plan.go::
  TranslateWithDubbingPlan`) that asks the LLM to emit not just the
  translation but also `{emotion: {valence, arousal, label}, pacing
  ∈ {slow, normal, fast}, emphasis_words: […], pause_after_ms: 0..1000}`
  in the same turn via an `emit_dubbing_plan` tool call. The strict
  schema is enforced at the provider level — content-mode hallucinations
  cannot smuggle ad-hoc fields through. Output is persisted on
  `segments.meta.dubbing` (no schema change; the column was already
  JSONB) and the ml-service TTS adapter
  (`ml_service/app/adapters/tts.py`) maps the operator-facing semantic
  representation (label strings + 0..1 floats) into the IndexTTS2
  conditioning surface: `valence`+`arousal` → an 8-element
  `emo_vector` (happy/sad/angry/surprised/fear/disgust/neutral/excited,
  L1-normalised), `emphasis_words` (anchored to words actually
  appearing in the translation), and `pause_after_ms` (applied as a
  trailing-silence ffmpeg `apad` pass). The conversion lives in the
  Python adapter so a future IndexTTS2 schema bump never requires a
  translator re-run. Backwards compatibility is total: segments
  without `meta.dubbing` and ml-service callers without
  `dubbing_meta` fall back to the legacy `INDEXTTS2_USE_EMO_TEXT`
  boolean; any strict-tool failure (provider ignoring `tool_choice`,
  malformed JSON, empty translation) downgrades to plain-text
  `TranslateTextWithDuration` with a single `WARN` log so the segment
  still ships. L1 ships with 18 new tests (6 Go covering parser /
  schema / defensive clipping / provider bypass / malformed JSON, 12
  Python covering emo_vector normalisation / quadrant assignments /
  threshold-step bound / emphasis-anchor filtering / missing-emotion
  fallback). Rollout playbook in [docs/opt-204/rollout.md](docs/opt-204/rollout.md)
  including the 50-segment human-eval design for the L3 quality gate
  (≥ 80% emotion-fit, ≥ 70% emphasis-correctness). Default off behind
  `DUBBING_PLAN_ENABLED=false`. Code: ~600 lines across
  `internal/llm/dubbing_plan.go`, `internal/pipeline/pipeline.go`,
  `internal/agents/segment_agent.go`, `internal/pipeline/stage_tts_agent.go`,
  `internal/pipeline/stage_tts.go`, `internal/ml/client.go`,
  `internal/config/config.go`, plus `ml_service/app/models.py` +
  `ml_service/app/adapters/tts.py`. Migration
  `migrations/012_segment_dubbing_meta.sql` is a comment-only marker
  (no DDL — the JSONB convention is enforced in code).
- **Speculative ensemble retranslate (OPT-202, code-complete; L2/L3
  staging pending)**: the `SegmentAgent` (OPT-201) now has an escalation
  path that fans the same retry input out across multiple LLM models in
  parallel (`internal/llm/ensemble.go::RetranslateEnsemble`), then has a
  thinking-class judge score the candidates pairwise and pick the
  highest-`OverallScore` winner. The escalation triggers from three
  independent conditions (any-hit): `state.AttemptsWithoutImprovement
  >= 2` (single-model retranslate has demonstrably stopped converging),
  `seg.meta.important == true` (operator-tagged "this segment must ship
  at maximum quality"), or `judge_score < 0.7 && state.Attempt >= 1`
  (OPT-002 judge has rated the current translation weak-but-salvageable).
  Cost is bounded by a per-segment cap (`EnsembleMaxPerSegment = 1`, so
  a chronically non-converging segment cannot fan out N×; subsequent
  retranslate decisions fall back to single-model with `UseThinking` if
  appropriate) and by the existing episode-level cost ledger
  (`accumulated_cost_usd`, OPT-407 rework). All paths are observability-
  instrumented: the OPT-201 `holodub_segment_agent_decisions_total`
  counter gained a `use_ensemble` label so dashboards can show ensemble
  vs thinking-vs-plain shares without re-labelling existing queries,
  and the per-segment winner is logged as `segment_agent: ensemble
  winner segment_id=… winner_model=qwen-plus judge_score=… candidate_count=…`.
  Real failures (provider 500, every candidate erroring) fall back to a
  single-model retranslate with a `WARN` log; the typed sentinel
  `agents.ErrEnsembleUnavailable` lets the executor distinguish
  "operator hasn't opted in" (silent fallback) from "ensemble broken"
  (logged at WARN). Default off behind `ENSEMBLE_RETRANSLATE_ENABLED`;
  `ENSEMBLE_MODELS=deepseek-chat,qwen-plus` and
  `ENSEMBLE_JUDGE_MODEL=kimi-k2.5` are the recommended starting
  configuration. L1 ships with 14 new tests covering parallel fanout,
  one-failure resilience, all-failures error, context cancellation,
  judge model override, tie-break determinism, per-segment cap, history
  threading, and four end-to-end agent runs. Rollout playbook in
  [docs/opt-202/rollout.md](docs/opt-202/rollout.md). Code: ~700 lines
  across `internal/llm/ensemble.go`, `internal/agents/segment_agent.go`,
  `internal/pipeline/stage_tts_agent.go`, `internal/config/config.go`,
  `internal/observability/metrics.go`.
- **SegmentAgent ReAct refactor (OPT-201, code-complete; L2/L3/L4
  staging pending)**: the 180+ line hand-written retry loop in
  `internal/pipeline/stage_tts.go::processOneTTSSegment` (lines 143-501,
  five interleaved decisions covering drift retry / borrow-from-gap /
  best-result restore / stuck detection / thinking-model escalation) is
  now optionally replaced by an explicit pure-function agent
  (`internal/agents/segment_agent.go::Decide(state, obs, cfg) Decision`)
  driving a narrow `DubbingTools` interface
  (`internal/agents/dubbing_tools.go`: `Synthesize`,
  `RetranslateWithConstraint`, `JudgeFidelity`,
  `RetranslateEnsemble` — the last gates OPT-202). The state machine
  splits the legacy loop into three clean layers: (1) `State` + `Config`
  pure data, (2) `Decide` pure function that returns
  `Decision{Kind, UseThinking, UseEnsemble, Reason}` for every transition
  (decision kinds: `accept`, `retranslate`, `restore_best`,
  `judge_veto_drift`, `split_segment_marker`, `stuck`, `cancelled`),
  (3) `Agent.Run` orchestrator that calls `tools.*` for side effects.
  Tools are accessed exclusively through the interface, so the entire
  agent is unit-testable via a hand-written `fakeTools`
  (`internal/agents/fake_tools_test.go`) that programs deterministic
  trajectories — the test suite covers ≥ 100 case scenarios (241 cases
  across `segment_agent_test.go` + `segment_agent_ensemble_test.go` +
  `split_test.go`): single-attempt convergence, oscillation requiring
  best-restore, stuck (consecutive identical drift), non-convergence,
  context cancellation mid-loop, tool errors, drift threshold borders,
  borrow-from-gap geometry, adaptive thresholds for long segments,
  judge VETO drift retry (OPT-002-followup-4 below), ensemble escalation
  (OPT-202 above). A new `realDubbingTools` adapter
  (`internal/pipeline/stage_tts_agent.go`) wires `ml.Client` /
  `llm.Client` / `Store` into the interface; `runSegmentAgentV2WithHint`
  is the pipeline-side entry point that constructs `agents.Config` from
  `Config`, builds `agents.RunInput` including
  `DubbingMeta: extractDubbingMeta(seg)` (OPT-204), and calls
  `agent.Run`. The legacy path remains the default behaviour: a single
  `if s.cfg.SegmentAgentEnabled { ... }` branch in
  `processOneTTSSegmentWithHint` decides which code path runs, so a
  feature-flag flip (`SEGMENT_AGENT_ENABLED=false`) cleanly restores
  the legacy loop without a redeploy. Observability ships day-one: a
  new `holodub_segment_agent_decisions_total{decision, reason,
  use_ensemble}` Prometheus counter (registered in
  `internal/observability/metrics.go::IncSegmentAgentDecision`),
  OTEL-style structured `slog` lines for every decision
  (`segment_agent: decided segment_id=… decision=… reason=…
  attempts_without_improvement=… best_drift_sec=…
  use_ensemble=… ensemble_calls=…`), and audit-grade attribution that
  makes "why did this segment take 5 attempts?" answerable from logs
  alone. Default off behind `SEGMENT_AGENT_ENABLED=false`; the L4
  cleanup PR (Phase 1 / PR-6 in the plan) will flip the default after
  ≥ 2 weeks of soak and only then delete the legacy code. Rollout
  playbook in [docs/opt-201/rollout.md](docs/opt-201/rollout.md) with
  an L2 binary-parity script
  ([scripts/opt201-diff-jobs.ps1](scripts/opt201-diff-jobs.ps1)) that
  runs the same input through both code paths and reports the diff
  rate on `tts_audio_path` / `target_text` / `tts_duration_ms` for each
  segment (target: 0% diff). Code: ~2200 lines across
  `internal/agents/{segment_agent.go, dubbing_tools.go, types.go,
  fake_tools_test.go, segment_agent_test.go, split.go, split_test.go}`
  + `internal/pipeline/stage_tts_agent.go` +
  `internal/pipeline/stage_tts.go` +
  `internal/observability/metrics.go` + `internal/config/config.go`.
- **Judge VETO drift retry (OPT-002-followup-4, OPT-FOLLOWUP-3 part b)**:
  high-confidence LLM judge verdicts now short-circuit the
  duration-only retry loop inside the SegmentAgent's pure `Decide`
  function. When `Decide` would otherwise pick
  `retranslate` solely because `|drift| > drift_threshold` AND the
  segment's most-recent judge call returned `verdict='accept'` with
  `OverallScore >= JudgeVetoMinScore` (default `0.95`) AND the absolute
  drift is bounded by `AdaptiveMaxAcceptableDrift(targetSec)` (10% for
  ≥ 20 s segments, 6% for 5-20 s, 3% for < 5 s — symmetric counterpart
  to `internal/pipeline/tts/budget.go::AdaptiveMinDriftThreshold`),
  the new `shouldVetoDriftRetry` branch routes the segment to a
  `Decision{Kind: accept, Reason: "judge_veto_drift"}` instead. The
  judge is called synchronously inside the agent (via
  `agents.Agent.maybeAttachJudge`) ONLY on the iteration where a
  drift-only retranslate is about to fire — this is the cost-minimal
  hook point (one extra judge call per about-to-retry segment, not per
  attempt). Sibling improvements: the synchronous judge call inherits
  the segment's `PrevContext` so the verdict sees the same surrounding
  text that the async judge would have. New env knobs
  `JUDGE_VETO_DRIFT_RETRY=true` (default ON, since OPT-002 has been in
  observe-only mode long enough to trust the scores) /
  `JUDGE_VETO_MIN_SCORE=0.95`. The whole change is gated by both
  `SegmentAgentEnabled` AND `JudgeVetoDriftRetry` so legacy-path
  operators see no behaviour change. Unit-test fixture
  `TestDecide_JudgeVetoDriftRetry` covers happy path (long segment +
  high score → veto), too-low score (no veto), missing judge result
  (no veto), and the AdaptiveMaxAcceptableDrift upper bound (drift
  beyond the adaptive cap → veto declined, fall through to normal
  retranslate). Documented end-to-end in the OPT-201 rollout playbook;
  cures the long-standing issue where segment 4 of the 79-min
  benchmark (judge `OverallScore=1.0`, drift 11.5%) would loop 5×
  retranslate before falling back to thinking model.
- **Rework dispatch now drives SegmentAgent (OPT-407-followup-2)**: the
  three-tier closed-loop rework engine (OPT-407) previously
  re-translated a chapter from the top whenever a segment-level
  verdict said `retry` (cheap to implement, expensive to run — every
  unrelated segment in the chapter got retranslated). The dispatch
  path now narrows to "rerun ONLY this segment through SegmentAgent,
  with a rework-aware hint": `internal/rework/engine.go::execute`
  builds a `models.ReworkHint{PrevVerdict, PrevReason,
  DriftThresholdHint}` and calls the newly-exported
  `RetryJobAPI.DispatchSegmentRework(jobID, segmentID, *ReworkHint)`
  (instead of the broad `RetryJob`). `Service` implements
  `DispatchSegmentRework` via a new `retryJobWithHint` helper that
  queues a single TaskPayload with the hint attached; the worker
  picks it up and routes it through
  `processOneTTSSegmentWithHint → runSegmentAgentV2WithHint`, which
  applies `hint.DriftThresholdHint` to `driftThreshold` and stamps
  every log line with `rework=true prev_verdict=… prev_reason=…` so
  attribution is clear. The existing broad-chapter `RetryJob` path is
  preserved (chapter- and episode-level actions still use it),
  ensuring backward compatibility while delivering the cost savings
  for segment-level rework — typical per-attempt cost drops from
  "chapter retranslate × N segments + chapter merge + audio mux" to
  "single segment retranslate + single TTS + status update", roughly
  40-60% cheaper depending on chapter size. `internal/rework/
  engine_dispatch_test.go` covers the wiring with a fake
  `RetryJobAPI` and verifies the hint is correctly populated for both
  `ActionSegmentRetry` and `ActionEscalateToThinking` (the latter
  sets `DriftThresholdHint` slightly tighter to encourage thinking
  model escalation).
- **Segment split algorithm marker (OPT-407-followup-1)**: the
  closed-loop rework's `ActionSegmentSplit` is no longer a pure
  observability stub. `internal/agents/split.go::SplitSourceText`
  finds a natural break point in `seg.SourceText` (preferring
  punctuation, falling back to silence gaps inferred from
  `seg.Meta["word_timings"]` when available, else word-count
  halfway), validates that both children clear the minimum target
  duration, and emits a `SplitProposal{ChildOneText, ChildTwoText,
  BoundaryCharIndex, BoundaryMs}`. Companion `AllocateChildTimings`
  splits `[start_ms, end_ms]` proportional to the source-character
  ratio so the children's slot durations sum exactly to the parent's.
  `internal/agents/split_test.go` ships 10 cases: punctuation break,
  silence-gap break, fallback halving, too-short rejection, balance
  invariant, character-index correctness, timing allocation arithmetic,
  parent-meta plumbing, source language unicode, and empty input
  defence. The Go state machine plumbing is end-to-end ready: a new
  `internal/models/models.go::Segment.ParentSegmentID *uint` field
  +`migrations/011_segments_split.sql` migration
  (`ALTER TABLE segments ADD COLUMN parent_segment_id BIGINT NULL` +
  partial index) is reversible via the paired `_down.sql`. The
  execute path remains marker-only behind
  `SEGMENT_AGENT_ALLOW_SPLIT=false` (default OFF) — a future PR-9.1
  will wire `agents.Decision{Kind: split_segment_marker}` from the
  rework engine through to actual `store.CreateChildSegments` writes
  after we have ≥ 1 week of marker observability to confirm the
  algorithm proposes split locations that operators agree with.
- **Drift-aware verdict guard + TTS-stuck recovery (OPT-407-followup-6)**:
  the OPT-407 closed-loop now overrides a high-confidence LLM judge
  verdict when the segment's actual TTS audio length deviates from its
  target slot beyond an operator-tunable hard limit. The LLM judge prompt
  scores translation quality (fidelity / fluency / coherence) but never
  sees the audio length, so before this followup it routinely rated
  high-drift segments at `1.0` while the dub was unusable (over-runs
  leak into the next slot, under-runs leave dead air); operators had to
  catch and rework these manually. The new guard lives inside the same
  pure `Decide()` function as the rest of the decision table —
  `internal/rework/decision.go::shouldDriftOverrideToRetry` checks
  `DriftSec` against asymmetric thresholds
  (`SEGMENT_DRIFT_HARD_LIMIT_OVER_SEC` default `0.3` for over-run,
  `SEGMENT_DRIFT_HARD_LIMIT_UNDER_SEC` default `0.7` for under-run; set
  either to `0` to disable that side) and rewrites `verdict` from
  `accept` to `retry`, then defers to the existing per-verdict rules so
  the override flows through the normal segment_retry capping +
  oscillation detection + cost ceiling. Verdict `retry` and `split`
  pass through unchanged (the guard only escalates, never overrides a
  remediation path the LLM already chose). Drift is computed inside
  `internal/pipeline/stage_tts.go::maybeJudgeSegmentAsync` from
  `tts_duration_ms - (end_ms - start_ms)` and threaded through the new
  `MaybeReworkSegment(..., driftSec float64)` parameter; callers that
  pass `0` (e.g. backfill paths that don't have audio metadata) silently
  disable the guard for that segment so the migration is safe-by-
  default. The same followup ships **TTS-stuck recovery**
  (`internal/pipeline/tts_stuck_backfill.go`): a 30s-after-boot scanner
  that finds segments whose status remains `translated` long after their
  parent job's `tts_duration` stage completed (typical signature: a
  transient ml-service timeout that errored out one segment of a batch
  while the surrounding stage was retried), groups them by job, and
  re-enqueues each chapter through the normal `RetryJob` path —
  observable in the existing `rework_attempts` / `task_queue` metrics.
  Eligibility is decided by `Store.HasJobStageCompleted(jobID,
  StageTTSDuration)` instead of `Job.CurrentStage` because OPT-407
  segment_retry rewinds `CurrentStage` back to `translate` after a
  retry round, which would otherwise mis-classify legitimate stuck
  segments as "TTS hasn't run yet". A two-minute `updated_at` cooldown
  filter prevents the scanner from racing with an actively-running
  `tts_duration` stage. The judge backfill path picks up the same
  improvement: it now caches per-episode whether `episodes.rework_status`
  is `halted_*` / `escalated_*` (via the newly exported
  `rework.IsHaltedReworkStatus` helper) and skips segments in those
  episodes — staging worker boots no longer burn LLM tokens judging
  segments whose verdicts the rework engine will then refuse to act on
  anyway. Documented end-to-end in `docs/roadmap/optimization-roadmap.md`
  under **OPT-407-followup-6**.
- **Closed-loop rework engine (OPT-407)**: the three-tier judge stack
  (segment OPT-002 / chapter OPT-409 / episode OPT-406) is now the input
  side of an automatic rework loop instead of an observe-only signal.
  After each judge writes its verdict, a new package
  [`internal/rework`](internal/rework/) runs a pure decision function
  (`Decide(DecideInput) Action`, exhaustively unit-tested via
  `internal/rework/decision_test.go`) that maps `(level, verdict,
  history, accumulated cost)` to a concrete `Action`: `segment_retry`,
  `escalate_to_thinking`, `accept_with_borrow`, `revise_weakest_segments`,
  `escalate_chapter`, `broadcast_glossary`, `escalate_human_review`,
  `escalate_oscillation`, or `halt_cost`. The engine
  ([`internal/rework/engine.go`](internal/rework/engine.go)) loads the
  episode's history, calls `Decide`, persists the resulting attempt onto
  three new `episodes.*` columns
  (`migrations/010_rework_attempts.sql`: `rework_attempts JSONB`,
  `rework_status TEXT`, `accumulated_cost_usd NUMERIC`), then dispatches
  the side-effect through a narrow `RetryJobAPI` interface implemented by
  `pipeline.Service` (`RetryJob` + `EnqueueEpisodeStage`) — no circular
  import. Segment- and chapter-level actions reuse the existing
  `(*Service).RetryJob`; episode-level glossary broadcast goes through a
  new on-demand `EpisodeStageGlossaryBroadcast` stage handler
  (`internal/pipeline/stage_episode_glossary_broadcast.go`) that
  re-extracts the OPT-402 glossary, diffs old vs new term targets, and
  re-translates segments containing changed source terms (capped at
  20/chapter via `maxGlossaryBroadcastSegmentsPerChapter` to bound the
  blast radius). The decision logic is gated by three independent safety
  rails in priority order: a feature flag (`REWORK_ENGINE_LEVEL`,
  default `none` for backward-compatible observe-only behaviour, can be
  raised one notch at a time `none`→`segment`→`chapter`→`episode`), a
  per-episode USD cost ceiling (`EPISODE_REWORK_COST_CEILING_USD`,
  default 2.0; computed live from `internal/llm/pricing.go`'s
  per-model price table feeding the new `holodub_llm_cost_usd_total`
  Prometheus counter), and oscillation detection
  (`REWORK_OSCILLATION_THRESHOLD`, default 2 — same target+verdict
  consecutive escalates immediately). The three judge hook points
  (`stage_tts.go::maybeJudgeSegmentAsync` post
  `UpdateSegmentJudgeResult`, `stage_tts.go::maybeJudgeChapterAsync`
  post `UpdateChapterJudgeResult`, and
  `stage_episode_judge.go::maybeJudgeEpisodeAsync` post
  `UpdateEpisodeJudgeResult`) call the engine fire-and-forget; the
  engine swallows its own errors so a rework failure can never fail the
  surrounding judge goroutine or the original chapter / episode
  pipeline. Async safety is preserved by gating dispatch on the parent
  job already being `Completed` (the lease is released before the judge
  goroutine fires) and by an additional `isHaltedStatus` short-circuit
  that refuses any further dispatch once an episode is escalated. New
  store helpers `AppendEpisodeReworkAttempt` (transactional, partial
  UPDATE on three columns only) and `SetEpisodeReworkStatus` (also
  partial UPDATE) prevent the rework goroutine from clobbering
  unrelated episode columns written by the parallel state machine, and
  every metric / log carries the level + action so dashboards can alert
  on `holodub_rework_actions_total{level,action,dispatched}` without
  inspecting the JSONB column. Rolling back is one env flip
  (`REWORK_ENGINE_LEVEL=none`); promoting to a higher level can be
  staged independently per environment.
- **Episode-level LLM-as-Judge (OPT-406)**: every `Episode` is now
  scored asynchronously after `pipeline.runEpisodeMerge` transitions
  it to `Completed`. The new `internal/llm/episode_judge.go`
  `JudgeEpisode` drives a strict `emit_episode_judge_verdict` tool
  call against `EPISODE_JUDGE_MODEL` (default `kimi-k2.5` — same
  model used by OPT-409 chapter judge for cost/quality parity) and
  returns seven 0–1 axis scores covering exactly the cross-chapter
  dimensions that segment-level OPT-002 and chapter-level OPT-409
  judges cannot see: terminology consistency (cross-chapter glossary
  drift), register consistency (academic / casual stays stable
  across chapters), narrative coherence (end-to-end discourse flow),
  character voice stability (one speaker keeps one voice across
  chapters), cultural localization, overall fidelity, overall
  fluency. Plus TWO weakest lists — `top_3_weakest_chapters` (whole-
  chapter rework candidates) AND `top_3_weakest_segments` (each
  pinpointed by `chapter_ordinal:ordinal` so OPT-407 closed-loop
  rework can dispatch chapter-level OR segment-level retranslate),
  a `terminology_glossary_observed` array (cross-chapter terms with
  inconsistent translations flagged), and a verdict enum
  (`production_ready` / `needs_minor_revision` /
  `needs_major_revision`, stricter than chapter-level because
  episode-judge is the final gate). Results land on the pre-reserved
  `episodes.episode_judge_score` (`NUMERIC`) +
  `episodes.episode_judge_meta` (`JSONB`) columns (migration
  `migrations/005_episodes.sql`). The dispatcher
  `internal/pipeline/stage_episode_judge.go` `maybeJudgeEpisodeAsync`
  mirrors the OPT-409 contract: detached background context with
  configurable timeout (`EPISODE_JUDGE_TIMEOUT_SEC`, default 90 s
  to absorb the larger episode prompt = reference card + glossary +
  chapter overview + every segment), best-effort log-and-drop on any
  failure, never fails episode merge or anything downstream. The
  frontend (`ui/src/components/EpisodeDetail.vue`) now renders the
  score on the episode header card as a green / amber / red badge
  (≥ 0.9 / 0.8 / < 0.8 — stricter than the chapter-level 0.85 / 0.7
  thresholds because the episode judge is the final whole-output
  gate) with a hover tooltip showing every axis sub-score plus the
  weak-chapters list, the weak-segments list, the observed cross-
  chapter glossary, and a one-paragraph summary. New env knobs
  `EPISODE_JUDGE_MODEL=kimi-k2.5` / `EPISODE_JUDGE_OBSERVE_ONLY=true`
  / `EPISODE_JUDGE_TIMEOUT_SEC=90` /
  `EPISODE_JUDGE_ESCALATE_MODEL=` (the MVP is observe-only and
  single-model; the escalate hook is reserved for OPT-406-followup-2).
  Reuses the OPT-405 `isThinkingModelName` helper to auto-degrade
  `tool_choice` to `"auto"` for DashScope reasoning models, and the
  existing `Store.ListSegmentsByEpisode` to bulk-load every segment
  in one DB round-trip (no N+1). Validated end-to-end on staging
  episode 131: 9 s LLM round-trip, persisted
  verdict=`production_ready` overall_fidelity=0.95 (every axis ≥ 0.95,
  zero weak chapters / segments, eight cross-chapter glossary terms
  observed including `MapReduce` → `MapReduce`, `fault tolerance` →
  `容错`).
- **Chapter-level LLM-as-Judge (OPT-409)**: every chapter (one `Job`
  under a multi-chapter `Episode`, see OPT-401) is now scored
  asynchronously after `pipeline.runMerge` persists the chapter
  outputs. The new `internal/llm/chapter_judge.go` `JudgeChapter`
  drives a strict `emit_chapter_judge_verdict` tool call against
  `CHAPTER_JUDGE_MODEL` (default `kimi-k2.5` — same model that
  already runs OPT-405 chapterization, validated by the OPT-405.1
  benchmark) and returns six 0–1 axis scores covering exactly the
  cross-segment dimensions that segment-level OPT-002 judge cannot
  see and that OPT-406 episode-judge would see too late: narrative
  coherence, speaker voice stability, terminology consistency,
  register consistency, overall fidelity, overall fluency. Plus a
  top-3-weakest-segments list (with concrete `issue` + `recommended_fix`
  for each weak segment, ready to seed OPT-407 closed-loop rework)
  and a verdict enum (`chapter_ready` / `needs_revision` /
  `needs_major_rework`). Results land on the new
  `jobs.chapter_judge_score` (overall `NUMERIC`) +
  `jobs.chapter_judge_meta` (`JSONB`) columns (migration
  `migrations/009_chapter_judge_score.sql`, partial index on
  non-NULL scores). The dispatcher
  `internal/pipeline/stage_tts.go` `maybeJudgeChapterAsync` mirrors
  the established `maybeJudgeSegmentAsync` contract: detached
  background context (60 s timeout — chapter prompts are larger than
  segment prompts and thinking models can take 10–15 s), best-effort
  log-and-drop on any failure, never fails the chapter or the
  downstream episode merge. The frontend
  (`ui/src/components/EpisodeDetail.vue`) now renders the score on
  every chapter card with a green / amber / red badge (≥ 0.85 / 0.7 /
  < 0.7) and a hover tooltip showing every axis sub-score plus the
  weakest-segment list with their proposed fixes. New env knobs
  `CHAPTER_JUDGE_MODEL=kimi-k2.5` / `CHAPTER_JUDGE_OBSERVE_ONLY=true`
  (the MVP is observe-only — no decisions wired in yet, deferred to
  OPT-407 once verdict thresholds are calibrated against operator
  labels). Reuses the OPT-405 `isThinkingModelName` helper to
  auto-degrade `tool_choice` to `"auto"` for DashScope reasoning
  models. Validated end-to-end on staging job 131 (1-chapter): the
  judge fired ≈ 30 s after the merge re-trigger and persisted
  verdict=`chapter_ready` overall_fidelity=0.95 (every axis
  ≥ 0.92, zero weak segments) on the rendered chapter.
- **LLM-driven semantic chapterization (OPT-405)**: long-form chapterize
  is no longer purely DP-driven. When `CHAPTERIZE_LLM_DRIVEN=true`
  (default), `ExtractEpisodeGlossary`
  (`internal/llm/glossary.go`) is invoked once per episode with the
  full ASR transcript indexed as `EpisodeSegment[]` and now also emits
  a top-level semantic chapter plan
  (`chapters[{title, title_translated, summary_md, start_segment_index,
  end_segment_index, theme}]`) via the same strict
  `emit_episode_glossary` tool call. The plan is persisted to the new
  `episodes.llm_chapters` JSONB column (migration
  `migrations/008_llm_chapters.sql`) and consumed by
  `internal/pipeline/stage_chapterize.go` `runEpisodeChapterize` →
  `tryLLMChapterPlan` before the legacy DP path runs (DP becomes the
  fall-back when the LLM plan is absent or rejected). The new
  `internal/chapterize/llm_apply.go` package owns the post-processing:
  `ValidateLLMPlan` rejects malformed / overlapping / out-of-range
  segment indices, `SnapBoundariesToSilences` shifts every cut to the
  nearest ASR silence ≥ `CHAPTERIZE_MIN_SILENCE_GAP_MS`,
  `EnforceHardConstraints` merges chapters shorter than
  `CHAPTERIZE_HARD_MIN_MS` (default 5 min) into their neighbour and
  splits chapters longer than `CHAPTERIZE_HARD_MAX_MS` (default 45 min)
  at the widest internal silence. Two new env knobs
  (`CHAPTERIZE_LLM_DRIVEN`, `CHAPTERIZE_HARD_MAX_MS`,
  `CHAPTERIZE_HARD_MIN_MS`) make the behaviour fully tuneable, and
  `GLOSSARY_MODEL=kimi-k2.5` (now the production default per the
  OPT-405.1 benchmark below) drives both the glossary AND the chapter
  plan from a single LLM call. The same code path also taught
  `internal/llm/client.go` `doChatToolOnce` to swap in
  `c.thinkingHTTPClient` (10-min timeout) whenever the model name
  contains `thinking` so DashScope reasoning models no longer time out
  mid-tool-call (regression caught while running OPT-405.1 against
  `kimi-k2-thinking`), and `glossary.go` to dynamically downgrade
  `tool_choice` from `forceToolChoice("emit_episode_glossary")` to
  `"auto"` for thinking models (DashScope rejects strict tool_choice on
  reasoning endpoints). Validated end-to-end on episode 142 (79-min
  lecture, 176 segments): kimi-k2.5 produced 8 chapters that scored
  4.76 / 5 across boundary coherence + title quality + topic
  completeness with `kimi-k2-thinking` as judge — see OPT-405.1 below.
- **Multi-model chapterize benchmark CLI (OPT-405.1)**: the new
  `cmd/chapterize-bench` tool runs the OPT-405 chapter plan against
  N candidate models × M runs each, normalises every plan through the
  full validate / snap-to-silence / hard-constraint pipeline, then
  asks an LLM-as-judge to score every plan on three axes (boundary
  coherence, title quality, topic completeness, 0–5) and emits a
  ranked markdown leaderboard + machine-readable JSON. The runner
  (`runner.go`) records per-model wall time, chapter count, target
  duration deviation, snap displacement and merge / split events;
  the judge (`judge.go`) drives a strict `score_chapter_cuts` tool
  call, supports multiple judge runs averaged into a single verdict,
  and skips re-runs when an existing valid
  `judge/{model}-judgment.json` is present (cheap reruns after
  transient errors). New helpers
  in `internal/llm/bench.go` (`Client.RunBenchToolCall`) expose a
  generic tool-call entry point so offline evaluation tools share the
  same retry / observability / timeout transport as the production
  pipeline. Baseline run pinned to
  `docs/opt-405/bench-baseline-2026-05-11/`: 6 candidates ×
  3 runs × 1 judge → **kimi-k2.5 wins 4.76 / 5** (clear gap of
  +0.70 over runner-up `qwen-max-latest` at 4.06); supporting
  artefacts include per-run raw plans (`raw/{model}-run{i}.json`),
  per-model judgments (`judge/{model}-judgment.json`),
  chapter-list snapshots (`chapters-{model}.txt`) and the rendered
  `report.md` / `report.json`. Usage docs live in
  `docs/opt-405/bench-README.md`. This locks in `kimi-k2.5` as the
  recommended `GLOSSARY_MODEL` and provides a repeatable harness for
  evaluating future chapterization model changes.
- **Chapterize + fan-out 多 chapter job (OPT-403/404)**: long-form videos
  (≥ ~22 min by default) are now automatically split into 18–30 min
  chapters with bilingual LLM titles, then re-stitched into a single
  episode-level final video. The pipeline runs three deterministic
  passes — `internal/chapterize/algo.go` `ExtractCandidates` (silence-
  aware boundary harvesting) → `DPOptimalCuts` (O(n²) DP that minimises
  quadratic deviation from `CHAPTERIZE_TARGET_CHAPTER_MS` while honouring
  min/max bounds and rewarding wider cut silences) → an optional Pass 3
  LLM review (`internal/llm/chapter_review.go`, strict
  `emit_chapter_review` tool call) that nudges boundaries and emits the
  bilingual `chapter_title` + `chapter_title_translated` + `chapter_
  summary_md`. Fan-out (`internal/pipeline/stage_chapterize.go`
  `runFanOutChapters`) atomically slices the source media into N
  per-chapter sub-videos via `media.SliceVideoAtRange`, creates ch2..N
  sibling Job rows (`store.CreateChapterJob`), reassigns + time-shifts
  every Segment into its new chapter (`store.ReassignSegmentsToChapters
  AndShift`), and re-enqueues `StageSegmentReview` for every chapter so
  downstream translation / TTS proceeds in parallel. Once the last
  chapter merges, `stage_episode_merge.go` runs `media.ConcatChapter
  Videos` (ffmpeg concat demuxer, no re-encode) over the per-chapter
  finals, runs an optional master EBU R128 pass
  (`media.LoudnormTwoPass`), writes `chapters.json` via the new
  `internal/episode` package, and stamps the Episode row with
  `output_layout_version=2` + `output_relpath` + `chapters_manifest_rel_
  path`. New API surface: `GET /episodes/:id/download/final`,
  `GET /episodes/:id/chapters.json`, `GET /jobs/:id/download/final`
  (all read paths from DB, never reconstruct from naming conventions —
  honours lessons-learned rule 1). Frontend: `EpisodeDetail.vue` gains
  a layout v1/v2 badge, an `loudnorm ✓` indicator when
  `Episode.LoudnormStats` is populated, two new pipeline pills
  (`chapterize` + `episode_merge`), bilingual chapter titles on the
  chapter grid, and a per-chapter download button. New
  `JobStatusAwaitingChapterize` parks chapter 1 between ASR and
  fan-out so segment_review never operates on pre-chapterize segment
  ranges. Back-fill is a one-off operator tool: `cmd/migrate-output`
  hard-links (or copies on cross-fs) every layout v1 episode into the
  unified `episodes/{ep_id}/...` layout with `--dry-run`,
  `--use-hardlink`, `--keep-old`, `--episode-ids`, `--limit`,
  `--record` flags. Live dry-run against the staging DB scanned 74
  episodes (44 migratable, 31 GB hardlink budget) in ~200 ms — see
  `docs/opt-403/opt403-backfill-dry-run.json`. Algorithm baseline
  pinned by `cmd/chapterize-baseline` to
  `docs/opt-403/baseline-opt403-79min.json` (3 chapters at 24.55 /
  25.47 / 24.56 min on the synthetic 79-min lecture; mean 24.86 min
  vs. target 22 min). Twelve new env knobs cover every constraint:
  `CHAPTERIZE_ENABLED / MIN_CHAPTER_MS / TARGET_CHAPTER_MS /
  MAX_CHAPTER_MS / MIN_SILENCE_GAP_MS`, `CHAPTER_REVIEW_LLM_ENABLED /
  MODEL`, `LOUDNORM_TARGET_I / TP / LRA / CHAPTER_ENABLED /
  MASTER_ENABLED`, `EPISODE_MERGE_ENABLED`. Migration:
  `migrations/007_chapter_metadata.sql`. Validation matrix:
  `docs/opt-403/validation-matrix.md`.

- **Episode / Chapter data model with 1-chapter shortcut (OPT-401)**: a new
  top-level `episodes` table represents the user's uploaded video, while the
  existing `jobs` table is repositioned as a "chapter-level execution unit"
  via four new columns (`episode_id`, `chapter_ordinal`, `chapter_start_ms`,
  `chapter_end_ms`). A back-fill migration in
  `migrations/005_episodes.sql` retro-fits every historical job to its own
  1-chapter episode so `GET /jobs/:id` and the existing UI keep working
  unchanged. Three new endpoints (`GET /episodes`, `GET /episodes/:id`,
  `GET /episodes/:id/chapters`) plus `EpisodeDetail.vue` expose the new
  hierarchy. The 9-state `EpisodeStatus` machine
  (`pending → chaptering → dispatched → running → merging → judging →
  reworking → completed → failed`) is the foundation for the upcoming
  multi-chapter pipeline (OPT-402..408). 1-chapter jobs auto-create and
  link to a 1-chapter episode and synchronously propagate status updates,
  so single-video users never need to reason about episodes.
- **Episode-level pipeline stages and glossary extraction (OPT-402)**:
  introduces a new `EpisodeStage` enum running parallel to the per-chapter
  `JobStage` (`ep_media → ep_separate → ep_asr_smart → ep_glossary_extract
  → ep_chapterize`), so for long videos, separation, ASR and glossary
  extraction run exactly once at the episode level instead of being
  duplicated per chapter. A new `internal/llm/glossary.go` calls
  `ExtractEpisodeGlossary` via the strict OpenAI-compatible
  `emit_episode_glossary` tool, returning `{glossary[], speakers[],
  reference_card_md}` from the full ASR transcript; results are persisted
  to `episodes.glossary_jsonb / reference_card / glossary_done_at` (added
  by `migrations/006_episode_pipeline.sql`) and injected into every
  `RetranslateWithConstraint` prompt so terminology stays consistent
  across chapters. For 1-chapter jobs the chapter's `vocals.wav` /
  `bgm.wav` and `asr_done_at` are double-written back to the episode row
  so the episode-stage progress UI lights up immediately. Validated
  end-to-end on episode 139 (79-minute MIT 6.824 lecture): ASR completed
  in 4.5 s and glossary extraction in 3.8 s, returning 6 terms + a
  301-char reference card (snapshot in
  `tests/quality/opt402-79min-episode-139.json`). The frontend
  `EpisodeDetail.vue` now shows an episode-stage progress block and a
  glossary table.
- **Per-operation LLM token observability (OPT-001)**: every LLM call now
  emits `holodub_llm_input_tokens_total`, `holodub_llm_output_tokens_total`
  and `holodub_llm_cached_tokens_total` with `{model, operation}` labels
  (operations: `translate / retranslate / retranslate_thinking / summary /
  review / judge`). The `chatCompletionResponse.Usage` parser accepts all
  three known cache field shapes (`cached_tokens` / `prompt_cache_hit_tokens`
  / nested `prompt_tokens_details.cached_tokens`) so DashScope, DeepSeek and
  OpenAI-native providers all surface cache hits identically. The translation
  system prompt is now byte-stable across segments within a single job
  (assembled by the new pure `buildTranslateSystemPrompt`), satisfying the
  prefix-match requirement of every provider's auto-cache. A new worker-side
  `:8081/metrics` endpoint exposes the worker process' own counters separately
  from the api process. See `tests/quality/baseline-post-p0.json` for the
  validation snapshot.
- **Function calling for segment_review (OPT-003)**: LLM-merged
  segment-review suggestions now flow through a strict OpenAI-compatible
  `tools` + `tool_choice` path (`emit_segment_suggestions(suggestions[...])`)
  instead of the legacy "describe JSON in prompt + json.Unmarshal" route. A
  failed tool call gracefully falls back to the legacy parser and bumps
  `holodub_llm_strict_parse_failed_total{operation="review"}` so a sustained
  regression is visible on a dashboard. Gated by
  `SEGMENT_REVIEW_USE_TOOLS=false` (default off during gradual rollout).
  The supporting `chatMessage / toolDef / functionDef / toolCall` named types
  and `doChatTool` helper are reused by OPT-002.
- **LLM-as-Judge in observe-only mode (OPT-002)**: every TTS segment now
  receives an asynchronous fidelity / fluency / coherence score via
  `JudgeFidelity` (strict tool-call schema). The verdict is recorded in the
  new `segments.judge_score / judge_meta` columns and surfaced as an "AI 评分"
  column in the segment table, but does NOT yet influence retry decisions —
  that integration is reserved for OPT-201 (SegmentAgent ReAct refactor).
  Gated by `JUDGE_MODEL=""` (default disabled). When enabled (e.g.
  `JUDGE_MODEL=qwen-turbo`), the judge call uses a detached background
  context so a worker SIGTERM mid-flight does not silently lose the verdict.
  Validated end-to-end on the 60s test video: 5/5 segments judged, 1.8s
  average judge latency, judge correctly identified a real semantic-loss
  segment that the duration-only retry loop kept thrashing on (job 129
  segment 4, "missing 'monitoring' translation" issue).

- **Segment-review per-segment ASR transcript correction**: each row in
  the awaiting_review UI now carries an ``✏ 编辑原文`` control (manual
  textarea edit, ≤ 8 KiB, awaiting_review-only) and a ``↻ 重新识别``
  button (re-runs faster-whisper on just that segment's
  ``[start_ms, end_ms]`` window of ``vocals.wav``).  Both paths share the
  new ``store.UpdateSegmentSourceText`` writer that touches only
  ``source_text + updated_at`` — start_ms / end_ms / status /
  target_text / tts_* are guaranteed untouched, so the existing job-
  level ``↻ 重试 ASR 分段`` "nuclear" button and any prior manual
  merge / split / time edits remain intact.  Word-level Whisper
  timestamps are still not persisted (``segment.Meta`` keeps no
  ``word_timings`` key), so split's character-proportion algorithm is
  unchanged: editing or re-recognising a transcript only shifts the
  baseline characters that future splits will linearly interpolate
  against.
- **ml-service ``POST /asr/transcribe_segment``**: re-transcribes a
  single time window without running the smart_split / VAD pipeline
  (which would otherwise reject clips shorter than the
  ``min_segment_sec`` floor).  ``ASRAdapter.transcribe_window`` clips
  the requested window with ``ffmpeg -ss/-t``, runs faster-whisper with
  ``word_timestamps=False`` and ``vad_filter=False``, joins
  ``segments[].text`` into a single punctuated string, then deletes the
  temp file.  Empty transcriptions return 200 with
  ``{warning: "empty_transcription"}`` so the UI can prompt the user to
  edit manually instead of treating it as a hard failure.
- **CI quality gates**: `golangci-lint`, `ruff`, `mypy`, `eslint`,
  `prettier`, `vue-tsc` typecheck, `govulncheck`, `pip-audit`,
  `npm audit`, Trivy filesystem scan, gitleaks secret scan, Redocly
  OpenAPI lint.
- `Dependabot` configuration for `gomod`, `pip`, `npm`, `github-actions`
  and `docker` ecosystems.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODEOWNERS`, PR template, structured
  GitHub issue templates.
- Standalone `/readyz` probe (DB + Redis + ML readiness) in addition to
  the lightweight `/healthz` liveness probe. ml-service now ships its
  own `/readyz` returning 503 while `tts_warmup_status` is `loading`
  or `error` and 200 only when ready/idle, so orchestrators stop
  routing traffic until IndexTTS2 is actually serviceable.
- IndexTTS2 warm-up watchdog: the lifespan starts a companion daemon
  thread that prints a heartbeat every 30s, marks the warm-up as
  `error` if the loader thread vanishes (segfault, OS kill, ...) or
  exceeds a 30-minute hard timeout, and proactively flushes logging
  handlers so failure paths never silently disappear.
- `internal/storage.SecureJoinUnderRoot` helper used by every file
  serving handler to prevent path traversal (with table-driven tests).
- `internal/pipeline/tts` package: pure decision functions for TTS
  duration budgeting / overflow policy / drift threshold computation,
  extracted from the 350-line `processOneTTSSegment` for unit testing.
- `internal/httpx` package: typed `APIError`, retry helper with
  exponential backoff + jitter, used by both ml-service and LLM
  outbound calls.
- New Prometheus metrics: `holodub_external_calls_total{service,operation,result}`,
  `holodub_external_call_duration_seconds`, plus an `ml-service`
  `/metrics` endpoint exposing `holodub_ml_http_requests_total`,
  `holodub_ml_inference_duration_seconds{stage}`,
  `holodub_ml_gpu_wait_seconds{stage}`, `holodub_ml_tts_warmup_status`.
- Request-id propagation between Go and ml-service via
  `X-Request-Id` (FastAPI `RequestIDMiddleware`).
- Strong-typed `models.SegmentStatus` with a `Transition()` validator
  and corresponding state-machine unit tests.
- Versioned schema baseline under `migrations/000_initial.sql` plus
  `migrations/README.md` describing the upcoming move off
  `AutoMigrate`.
- Frontend `lib/api-client.ts` (`ApiError` + timeout +
  `AbortSignal.any`), `lib/toast.ts` notification store and
  `ToastContainer.vue`, `composables/usePolling.ts`.
- `internal/service` package introducing `JobsAPI` interface +
  `JobService` implementation as a starting point for the use-case
  layer; `internal/http/router_segments.go` extracted from
  `router.go`.
- Public OpenAPI spec at `docs/openapi.yaml` (Redocly-linted in CI).
- Operator-grade docs: `docs/observability/` (Grafana dashboard,
  Prometheus rules, scrape config) and `deploy/helm/holodub`
  (Chart skeleton).
- `docker-compose.prod.yml` with secrets, structured logging, restart
  policies, healthcheck for `api`, and resource caps.
- `.goreleaser.yaml` + `.github/workflows/release.yml` for tag-driven
  multi-arch image + binary releases pushed to GHCR.
- `ModelRegistry` upgrade: optional `max_models` LRU eviction,
  `unload(key)`, `clear()`, plus `/admin/models[/unload|/clear]`
  endpoints. New `MODEL_REGISTRY_MAX_MODELS` env knob.
- Graceful worker shutdown via `signal.NotifyContext` plus a
  `runCmdCtx` ffmpeg variant.

### Changed

- **Worker-startup judge back-fill goroutine (OPT-002-followup-2)**:
  on every worker boot, when both `JUDGE_MODEL` and the new
  `JUDGE_BACKFILL_ON_START=true` (default) are set, the worker now
  scans for at most `JUDGE_BACKFILL_LIMIT` (default 500) synthesised
  segments that are missing a judge verdict (typically because the
  worker process was restarted after synthesis but before the
  segment's detached judge goroutine completed) and dispatches them
  through the same observe-only `maybeJudgeSegmentAsync` pipeline used
  at synthesis time. Bounded concurrency (3) prevents a stampede of
  the LLM provider on big restarts; the dispatch starts 15 s after
  worker boot so Redis / DB / ML health checks settle first. New
  `internal/store/store.go` `ListSegmentsAwaitingJudge(ctx, limit)`
  returns the scan with full unit-test coverage (recent-first
  ordering, limit/zero short-circuit, filters out empty source/target
  text and already-judged rows including `judge_score=0`). New
  `internal/pipeline/judge_backfill.go`
  `(*Service).BackfillSegmentJudges(ctx, limit, concurrency)` does
  the dispatch with semaphore-bounded concurrency and per-segment
  `GetJob` enrichment so the back-fill judge sees the same
  `SourceLanguage` / `TargetLanguage` / `TranslationSummary` the
  synthesis-time judge would have used (PrevContext is `nil` in the
  back-fill path — a deliberate simplification, see plan §3 / debt-3b;
  loses prev-sentence coherence signal but keeps the back-fill cheap
  and observability-comparable). Validated on staging:
  worker boot → `judge backfill: dispatching count=500 limit=500
  concurrency=3` → 500 verdicts persisted within ~12 s, including
  segments from jobs 119 / 120 / 121 that had been unjudged for days
  due to prior restart windows.
- **Roadmap status sync for OPT-402 / OPT-403 / OPT-404**: the three
  detail cards in `docs/roadmap/optimization-roadmap.md` §4 still
  carried `Status: planned` even though §3 + §6 archive both already
  showed them as done. Fixed all three cards to mirror the OPT-401
  template: top `Status: done (date; ...)` line + new bottom-of-card
  `实际改动 / 实际工时 / 验证` block summarising the §6 archive
  evidence. No code change.
- **OPT-001-followup-2 verified**: `internal/llm/client.go`
  `doChatStream` was already parsing the SSE final-chunk `usage`
  field (lines 969–979) and emitting it through
  `observability.ObserveLLMTokens(model, operation, ...)` (line
  992-993) since the OPT-001 wrap-up — the roadmap line just never
  got marked done. Confirmed live by checking
  `worker:8081/metrics` after running a `kimi-k2.5` streaming call:
  `holodub_llm_input_tokens_total{model="kimi-k2.5"}` is now > 0
  instead of 0. Roadmap line 208 marked DONE 2026-05-11 with a note
  explaining the late catch.
- **Translate system prompt is now fully byte-stable across segments
  (OPT-001-followup-1)**: `buildTranslateSystemPrompt` no longer takes
  per-segment `targetSec` / `limit` arguments — those values are now
  appended to the user message as a single `Hard duration constraint:
  target ~Xs (≤Y chars).` line. The system prompt now varies only with
  `targetLanguage` and the optional `translationSummary`, satisfying the
  prefix-cache requirement of every OpenAI-compatible provider. The
  `TestSystemPromptStable` unit test was inverted to actively assert that
  the system text is identical regardless of `targetSec` / `limit`, and
  a companion `TestTranslateUserMsgContainsPerSegmentConstraints` proves
  the constraints still flow through to the user role. `RetranslateText`
  applies the same split. This unblocks the original OPT-001 cache
  observability work, whose 0% translate-path hit ratio was provably
  caused by the prompt-stability bug rather than the metric pipeline.
- **Adaptive drift threshold for long TTS segments (OPT-FOLLOWUP-3a)**:
  `internal/pipeline/tts/budget.go` adds a pure
  `AdaptiveMinDriftThreshold(targetSec, userFloor)` that lifts the
  effective `RETRANSLATION_MIN_DRIFT_THRESHOLD` floor based on segment
  length (≥ 20 s → 0.06, ≥ 10 s → 0.05, ≤ 5 s → keep 0.03) without
  ever relaxing a stricter user-configured floor. `processOneTTSSegment`
  applies the adaptive floor when computing whether a retranslate is
  worth its cost, eliminating the long-segment retry oscillation that
  caused the 10-min validation cancel observed in
  `tests/quality/baseline-post-p0-10min.json`. The temporary `.env`
  warnings recommending `RETRANSLATION_INITIAL_MAX_ATTEMPTS=10 /
  RETRANSLATION_MIN_DRIFT_THRESHOLD=0.06` are now obsolete and
  documented as `adaptive floor handled by code`. Six new test cases in
  `budget_test.go` cover short / medium / long segments, the boundary
  conditions and the "do not relax stricter user floors" invariant.
  Sub-task (b) — letting `judge.verdict='accept'` short-circuit a drift
  retry — remains planned and is gated on OPT-201 SegmentAgent decision
  routing.
- `apiKeyAuthMiddleware` now refuses to start in production when no
  `API_AUTH_TOKEN` is configured, instead of silently allowing all
  traffic.
- `TTSAdapter.synthesize` no longer falls back to a silent WAV when the
  configured backend is unavailable; it raises
  `UnsupportedTTSBackendError` and the FastAPI route maps it to
  `503 tts_backend_unsupported` so the worker can surface the
  misconfiguration.
- IndexTTS2 inline warm-up is now serialised by an
  *event-and-state-machine* protocol instead of a single long-held
  mutex: the heavy `IndexTTS2(...)` construction runs *outside* any
  lock so a crashing loader thread can no longer strand subsequent
  TTS requests with an unreleased mutex; concurrent waiters block on
  a `threading.Event` with a 30-minute timeout and fail fast with a
  503-mappable error if the loader never resolves. A new
  `force_indextts2_load_error` API lets the lifespan watchdog publish
  a terminal failure from outside the loader.
- `ml.Client` and `llm.Client` now classify upstream errors via the
  shared `httpx.APIError` and retry transient failures (429/5xx,
  network) with exponential backoff + jitter.
- Worker enters its main loop with a context derived from
  `signal.NotifyContext`; `processOneTTSSegment` now polls the
  context between attempts so a `SIGTERM` or job cancellation
  propagates promptly.

### Fixed

- `serveSegmentAudio` / `serveOriginalAudio` / `servePreviewAudio` /
  `listFiles` now reject paths that resolve outside `DATA_ROOT`.
- Several `alert()` calls in the SPA replaced with structured toast
  notifications.
- IndexTTS2 inline warm-up no longer hangs indefinitely during the
  ``_load_gpt`` BigVGAN fused-anti-alias-activation custom CUDA kernel
  preload step. On RTX-50-class (sm_120) GPUs with PyTorch 2.x +
  CUDA 12.8, ``torch.utils.cpp_extension.load`` invoked from inside
  the FastAPI lifespan worker thread sporadically hangs at the
  ``[1/2] nvcc ...`` JIT stage even though the same nvcc command runs
  to completion when invoked from a plain shell. The fix has two
  layers:

  1. ``docker/precompile_bigvgan.py`` was rewritten to monkey-patch
     ``torch.cuda`` so it simulates the deployment GPU (default
     ``BIGVGAN_TARGET_SM=120``) and then defers to IndexTTS' own
     ``load.load()``; this guarantees the compiled artifacts land in
     the EXACT directory that runtime IndexTTS reads from
     (``<site-packages>/indextts/.../cuda/build/``) with the EXACT
     cc_flags that runtime will recompute, so PyTorch's cache check
     reports "ninja: no work to do" and ``dlopen``s in <5 s. The old
     precompile silently wrote to the wrong directory
     (``~/.cache/torch_extensions``) which IndexTTS never consults.
  2. A new ``INDEXTTS2_USE_CUDA_KERNEL`` setting (default ``False`` to
     stay safe for operators running an old image) lets you pin
     whether the kernel is preloaded. With a freshly-built image the
     ``.so`` is already in place and you can flip the flag to ``true``
     to enjoy the full fused-kernel inference speed; without a fresh
     image the runtime falls back to the PyTorch native path, which
     produces identical audio at a small inference-time cost.

  The ``ml.Dockerfile`` now passes ``BIGVGAN_TARGET_SM`` through as a
  build arg and verifies the compiled ``.so`` exists immediately after
  the precompile runs, so image-build failures are loud rather than
  silent.

## Pre-changelog history

For changes prior to the introduction of this changelog, see the git log
and the project status section in `README.md`.
