# LoRA Adapter Training And Promotion SOP

This document freezes the current HoloDub LoRA adapter workflow as an
operational contract. It is intentionally conservative: an adapter may improve
long-form timing behavior, but it is not allowed to hide drift, truncation, bad
ASR, or failed joins.

## Asset Model

LoRA adapters and voice profiles are separate assets.

- `voice_profile_id` owns speaker identity, reference samples, and voice
  validation.
- `lora_adapter_id` owns model behavior: pacing, duration control, split
  candidate behavior, and domain-specific pronunciation pressure.
- Production provenance must record both IDs whenever candidate audio is used.
- A normal adapter is not a voice profile and must not replace voice profile
  management.

Adapter voice leakage is still possible. If a dataset is dominated by one
speaker, one prompt style, or one acoustic condition, the adapter can learn
style and prosody bias. A voice-scoped adapter is allowed only when it is
explicitly marked as compatible with a controlled voice profile set. Otherwise
it stays model-scoped and must remain independently selectable from voice.

Current pilot adapter scope:

- model-scoped
- silver/synthetic training data
- acceptable for smoke, canary, and opt-in experiments
- not a final production gold adapter

## Naming

Use stable IDs that encode the data and gate state:

```text
<domain>-<language>-<tier><hours>-r<rank>-s<step>-<gate>
```

Examples:

```text
raft-zh-silver0h7-r4-s80-pilot
raft-zh-gold2h-r8-s1200-canary84p
raft-zh-gold5h-r8-s1800-episode-shadow-pass
```

Registry labels may be friendlier, but the persisted adapter ID should stay
machine-readable. Do not name adapters only by date or by a vague term such as
`best`.

## Required Artifacts

Every adapter run directory should contain or reference:

- `adapter*.pt`
- `summary.json`
- feature manifest and train/val/test feature files
- dataset manifest with source tier
- `training_pack_audit.json` from
  `tools/mit6824_lora_training_pack_audit.py` before any non-smoke training
- `training_readiness.json` from
  `tools/mit6824_lora_training_readiness.py`, comparing the candidate pack to
  the current goldmatch/reference corpus before another adapter train starts
- canary reports and selected audio reports
- `quality_baseline.json` after episode shadow validation
- lecture/episode `fit_audit.json` for any promoted shadow or production run
- human review pack and human acceptance notes before opt-in promotion
- adapter SHA256 in the registry response

The registry scans `DATA_ROOT/training/lora_runs` and exposes artifacts through:

```text
GET /lora/adapters
GET /lora/training-contract
```

The operator UI entry is:

```text
/ui/lora-adapters
```

## Dataset Tiers

Silver data is useful for engineering feedback only.

- synthetic or TTS-generated samples
- historical auto-accepted samples
- quick smoke runs
- duration objective experiments

Gold data is required for promotion.

- 2 hours minimum human-approved audio
- 5 hours target before treating the adapter as stable
- fixed train/val/test split
- explicit coverage of short text, long text, Latin terms, numbers, section
  references, natural pauses, and tail completeness
- no sample may enter both training and canary/episode holdout

## Training Flow

Phase 0: silver smoke

- Build or refresh the silver manifest.
- Train a tiny adapter only to verify load/train/infer mechanics.
- No production quality claim is allowed.

Phase 1: gold seed

- Collect 2-5 hours of human-approved aligned audio.
- Lock a fixed holdout set before training.
- Record dataset source tier as `gold`.

Phase 2: local LoRA smoke

- Run on local RTX 5080 for small validation.
- Verify adapter loads once and stays disabled outside candidate context.
- Verify base/default TTS output is unchanged when LoRA is disabled.

Phase 3: canary regression

- Rebuild canary selected output from raw candidate reports and overrides.
- Required canary gates:
  - count >= 84
  - mean absolute drift < 50 ms
  - max absolute drift < 300 ms
  - mean ASR >= 0.99
  - min ASR >= 0.96
  - join quality failures = 0
  - `seg10569` must keep the approved selected lineage
  - `seg10686` must keep the successful split path

Checkpoint selection must use generated-audio holdout, not training loss alone.
Before a non-smoke duration/prosody training run starts, the dataset must pass
`tools/mit6824_lora_training_pack_audit.py`. The audit rejects training packs
that lack val/test splits, have insufficient rows/hours, contain selected fit
above 0.05, include failed-shadow sources, or leak fixed holdout samples into
training.

For duration/prosody work, every saved checkpoint that might be promoted must be
run on the fixed high-fit holdout. A checkpoint is rejected if generated audio
has any of these:

- max fit delta above 0.05 on selected holdout output
- CUDA/assert/generation crash on any required holdout sample
- tail truncation or missing final phrase
- ASR below the existing floor
- no clear improvement over the current production adapter

Phase 4: episode shadow

- Run a full episode with candidate execution opt-in only.
- Compare to the golden baseline for objective metrics and human listening.
- Required episode gates:
  - `fit_audit.status = pass`
  - no selected candidate needs tempo fit above 5%
  - no final accepted segment has `tts_agent.final_implied_fit_delta > 0.05`,
    including default/fallback audio that did not expose a selected LoRA
    candidate
  - `fit_audit` must parse both `tts_lora_candidate_runtime` and nested
    `tts_lora_candidate.runtime`; missing or nested runtime is not a reason to
    skip tempo-risk accounting
  - no segment over target by more than 300 ms
  - no segment under target by more than 700 ms
  - course validation profiles must set
    `SEGMENT_AGENT_REJECT_HARD_CAP_ESCAPE=true`; a segment-agent escape that
    still has `hard_cap_exceeded=true` is a failed synthesis, not acceptable
    output
  - course validation profiles must set `SEGMENT_AGENT_MAX_FIT_DELTA=0.05`;
    the SegmentAgent records `final_implied_fit_delta` for every accepted
    segment and rejects final accepts that would need more than 5% tempo fit to
    land on the target slot
  - ML candidate runtime must include `selected_candidate`, and the Go
    SegmentAgent must re-check the selected candidate's `fit_tempo_delta`,
    `fit_reject_reason`, and `reject_reasons` before accepting
    `lora_candidate_gate`
  - short dense segments that fail default retry must be escalated into the
    candidate/split path before any hard-cap output is accepted
  - `needs_text_compression` / `needs_text_compression_speech_rate` is a
    planning signal, not a normal candidate failure; it must not consume the
    candidate attempt budget, and it must drive retranslation or text
    compression before another synthesis attempt
  - no final or restored output may ship with hard drift cap exceeded while the
    active or previous candidate runtime still requires text compression
  - final mix ASR gate passes against original target text
  - no known tail truncation regressions
  - no hidden threshold relaxation
  - split candidate boundary repairs, if applied, are recorded in candidate
    diagnostics and risk review instead of silently bypassing join quality

Phase 5: human acceptance

- Build a compact review pack from the risk queue.
- Review selected, runner-up, teacher/reference, and known anchors.
- Human override may cover soft `flat_f0` only.
- Human override must not cover bad join quality, high drift, high fit delta,
  low ASR, low term recall, or tail truncation.

Phase 6: opt-in promotion

- Mark adapter `candidate_ready` only after objective and human gates pass.
- Production remains opt-in.
- Short/default TTS path remains unchanged.
- Rollback is immediate: disable adapter selection or remove the adapter ID from
  the opt-in config.

## Operator UI Flow

The LoRA Training Workbench is the canonical operator entry for adapter
validation work:

```text
/ui/lora-training
```

The Adapter Registry remains the canonical inventory view:

```text
/ui/lora-adapters
```

Use the workbench actions with these meanings:

- `Save Training Plan`: persist exact paths, hyperparameters, commands, and
  gates. This does not start GPU work.
- `Run 1-step smoke`: run an isolated mechanical check. It may prove that the
  ML runner can load/train/write a small adapter, but it is not a quality
  claim.
- `Verify existing canary`: run `lora_canary_candidate_loop.py
  --skip-inference` through ML service. It rebuilds split plans, summaries,
  gates, and listening packs from existing candidate reports without GPU
  inference.
- `Run canary loop`: regenerate candidate audio. Use this only when the
  adapter, candidate family policy, split planner, spoken-text policy, or
  duration policy changed.
- `Run regression gates`: run the golden gate checker against the selected
  canary run. This must pass before any episode shadow is considered.

The current validated skip-inference path is:

```text
pilot-episode146-code1500-r4-step80
canary84_full84_regen_v1
```

The workbench must show these linked artifacts after a successful canary or
regression run:

- `loop_summary.json`
- `candidate_gate_report.json`
- `listening_pack/index.html`
- `human_review_report.json`
- `risk_queue.json`
- canary and regression stdout/stderr logs

## When To Train

Do not train a new adapter for ordinary episode execution. A normal episode
run uses the already selected adapter and only regenerates candidate audio for
segments that need it.

Do not train one adapter per lecture by default. One failed lecture is evidence
for repair work and future batch review; it is not by itself a training
trigger.

One rejected lecture may start a controlled lecture-local duration/prosody
experiment when `fit_audit` proves systemic duration failure. That experiment is
allowed to diagnose and repair the lecture, but its adapter cannot be promoted
as the normal course adapter until the promotion gates below pass.

For MIT 6.824, the Lecture 7-aligned corpus filter is the current gold-standard
selection rule: judge score >= 0.8, ASR similarity >= 0.97, drift <= 500 ms,
drift ratio <= 5%, fit tempo delta <= 5%, and text length <= 220 chars. Data
that passes this rule but is generated by HoloDub is still silver unless the
audio has been explicitly human approved.

The current useful Lecture 7 seed is:

```text
data/training/lora_corpus/mit6824-lec07-v25-gold-candidate-v3
```

It contains 105 accepted samples and about 0.6567 hours of audio. Treat it as
the filtering and listening-standard reference, not as enough data for final
adapter promotion. The first two stricter attempts accepted zero samples, which
is a reminder that the selection rule must be explicit and measured rather than
reconstructed from memory.

Lecture 8 must copy this standard before another full-episode run:

- fixed high-fit holdout first
- no failed shadow audio in training
- no sample leakage from holdout into training
- selected generated audio must stay at or below 5% fit delta
- ASR, critical terms, tail completeness, and join quality stay hard gates
- full episode shadow starts only after the fixed holdout beats the current
  goldmatch reference on generated audio

Train a new adapter only when at least one condition is true:

- new human-approved aligned audio materially changes the dataset
- the current adapter fails objective canary gates
- the current adapter fails full-episode shadow or human review after rule,
  split/candidate, and gate repairs are exhausted
- a new behavior target cannot be reached by candidate policy, split planning,
  spoken-text normalization, or gate tuning

If none of those is true, use canary reuse or full canary regeneration instead
of training.

When a lecture fails because final audio sounds dragged, do not start by
relaxing fit. Run `fit_audit` first. If selected output exceeds 5% fit delta,
the repair order is:

- reject the selected audio as production-ineligible
- require the main TTS flow to persist `tts_final_fit_audit` before marking a
  segment synthesized; training manifests must read this same field and reject
  `pass=false`
- inspect speech-rate feasibility and split-part chars/sec
- repair target text before TTS when the requested slot is too dense
- regenerate split candidates with the production 5% fit gate
- repair loud split boundaries only when join diagnostics prove a tail boundary
  artifact
- train or fine-tune only if these engineering repairs still leave repeated
  objective failures on the fixed holdout

When training is justified, the dataset must follow the same ordering. Start
from the current Lecture-7-style feasible corpus, then append only repaired
samples that passed the 5% fit gate, ASR, term recall, tail, and join checks.
Failed shadow audio is never promoted directly into training data. It first
enters a repair queue with action labels such as text compression, boundary
repair, or text completeness repair; only the regenerated and accepted repair
audio may become a repair adjunct.

Lecture 8 is the current reference example. Its initial run sounded dragged
because low final drift hid excessive fit. The useful result was not threshold
relaxation; it was the Lecture-7-style process: strict source filtering,
speech-rate feasibility checks, text compression, split planning, boundary
repair, generated-audio holdout, and 5% fit as a hard gate.

The current Lecture 8 adapter experiments prove why readiness must be explicit:

- `v7` remains the training-process reference because its generated-audio
  high-fit holdout improved materially without selected samples above 5% fit.
- `v9` was rejected after the promoted repair adjunct failed to improve the
  fixed generated-audio holdout.
- `v10` repeated the clean v7 goldmatch recipe without holdout leakage, but was
  also rejected because generated-audio holdout selection regressed.
- A strict non-holdout Lecture 8 candidate rebuild produced only 90 rows,
  0.6581 hours, and 0 new source ids relative to v7, so it is not a valid
  reason to retrain.

The current readiness artifact is:

```text
data/validation/mit6824_acceptance/lec-08/lecture08_training_readiness_v1.json
```

It is `not_ready_to_train` until new non-holdout, ASR-passing, fit<=5%,
human-approved or objectively accepted segments materially extend the reference
corpus.

For MIT 6.824, the expected cadence is to run the current course/domain profile
through lectures 8-20, review every lecture, and revisit training after every
2-3 accepted or blocked lectures. A course adapter update requires repeated
failure families plus enough human-approved aligned audio and a fixed held-out
set.

## Hard Gates

These are hard failures and cannot be bypassed by normal listening override:

- `bad_join_quality`
- `high_drift`
- `high_fit_delta`
- `low_asr`
- `low_term_recall`
- `tail_truncation`

`high_fit_delta` means tempo fitting would exceed 0.05 in production candidate
selection or lecture acceptance. It is a hard failure because larger tempo
correction can hide unnatural speaking rate and damage clarity. The older 0.08
threshold is kept only as a legacy/severity marker in historical reports or
explicitly labeled comparison runs.

The only current soft override reason is:

- `flat_f0`

## Registry Promotion States

`pilot_only`

- Missing gold data, missing quality baseline, or only smoke evidence exists.
- May be used for offline candidate experiments.

`needs_review`

- Required artifacts exist but the quality status is not yet accepted.
- Human and objective review must continue.

`candidate_ready`

- Gold data is present.
- Quality status is `shadow_pass` or `human_accepted`.
- Objective gates pass.
- The adapter can be used only by explicit opt-in configuration.

## UI Requirements

The UI must show:

- adapter file path, run ID, SHA, rank, step, and size
- dataset source tier and feature counts
- objective quality status, ASR, and drift metrics
- promotion state and blocking reasons
- training pack audit and training readiness status
- adapter-vs-voice explanation
- hard gates and naming convention

Later write operations should be added as separate guarded actions:

- start training
- attach gold dataset manifest
- run canary
- run episode shadow
- promote candidate
- roll back candidate

Until those actions are implemented, the registry is read-only by design.
