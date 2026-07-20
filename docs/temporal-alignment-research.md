# Temporal Alignment Research Notes

Date: 2026-06-16
Branch: `codex/temporal-planning-prototype`

## Goal

HoloDub's previous ASR-only validation allowed long-form outputs to pass even when
segment-level timing drift was unacceptable. The new direction is to stop treating
post-TTS retry as the primary control loop and move toward:

1. pre-TTS temporal planning,
2. multiple candidate generation strategies,
3. independent timing / semantic / perceptual scoring,
4. hard long-form validation gates.

## External References

### PrismAudio / ThinkSound

Primary sources:

- https://prismaudio-project.github.io/
- https://arxiv.org/abs/2511.18833
- https://github.com/FunAudioLLM/ThinkSound/tree/prismaudio
- https://huggingface.co/FunAudioLLM/PrismAudio

Useful findings:

- PrismAudio decomposes one monolithic plan into Semantic, Temporal, Aesthetic,
  and Spatial CoT modules, each paired with its own reward signal.
- The open `prismaudio` branch contains independent reward modules, including
  `grpo/fast_grpo/reward_synch.py` for temporal synchronization and
  `grpo/fast_grpo/rewards.py` for weighted multi-objective scoring.
- The direct model is V2A, not speech dubbing TTS, so we should not copy it
  mechanically. The transferable architecture is the separation of planning
  dimensions and reward dimensions.

HoloDub mapping:

- Semantic CoT -> translation fidelity / glossary / ASR-backtranscription score.
- Temporal CoT -> per-segment target window, char budget, breath groups, and
  post-synthesis drift.
- Aesthetic CoT -> TTS naturalness, clipping, ASR emptiness, and stretch ratio.
- Spatial CoT -> not primary for current mono lecture dubbing; can stay out of
  the MVP.

### Automatic Dubbing / Duration Control

Relevant methods found in literature and production-style systems:

- length-aware speech translation: generate short / normal / long candidates
  and choose the one closest to target duration;
- duration-controllable TTS: model-level duration predictors or length
  regulators;
- prosodic phrase alignment: align phrase groups and pauses, not only whole
  utterance length;
- limited time-stretch post-processing: useful only within a conservative ratio
  window and must be checked for intelligibility.
- speaking-rate naturalness: automatic dubbing literature treats phrase-level
  speaking rate as a first-class alignment variable, not just a side effect of
  duration. Emotional prosody studies also show arousal affects natural speaking
  rate, so pacing must be planned together with emotion.

Important implication: a single trick will not be enough. We need a candidate
pipeline and a scorer.

## Local Experiments

Source: episode 146, 180 synthesized segments, source duration about 1:17:36.

Measured baseline drift from latest validation:

- p50 absolute drift: about 0.81s
- p95 absolute drift: about 3.05s
- max absolute drift: about 7.79s
- 110 / 180 segments over 0.5s absolute drift

### Pre-TTS Timing Plan

Implemented prototype:

- `internal/pipeline/tts/timing_plan.go`
- `internal/pipeline/timing_plan.go`
- `TEMPORAL_PLAN_ENABLED`
- `TEMPORAL_PLAN_MIN_TARGET_SEC`
- `TEMPORAL_PLAN_CHARS_PER_SEC`

The planner emits:

- target window,
- predicted duration,
- min/max character budget,
- status: `fits`, `too_long`, `too_short`,
- breath groups with target milliseconds.
- required speaking rate (`required_chars_per_sec`),
- ratio to calibrated voice rate (`rate_ratio`),
- natural speaking-rate window after pacing / arousal adjustment,
- rate status: `natural`, `too_fast`, `too_slow`.

Dry-run finding:

- language default `zh=4.0 chars/sec` is too conservative for this episode;
- episode 146 observed median is about `5.24 chars/sec`;
- the planner must use voice / episode calibration before language fallback.

### 12-Segment TTS Experiment

Command:

```powershell
python tools\temporal_plan_experiment.py --episode-id 146 --per-bucket 3 --top-drift 6 --max-samples 12 --cps-mode episode --run-tts --repeats 1
```

Result:

```text
old      mean abs drift 2.939s, p50 2.930s, p95 5.771s, max 6.287s
temporal mean abs drift 1.396s, p50 0.565s, p95 4.088s, max 4.527s
```

By duration bucket:

```text
short  old 0.504s -> temporal 0.785s  worse
medium old 2.320s -> temporal 0.662s  better
long   old 4.467s -> temporal 2.067s  better but not enough
```

Conclusion:

- temporal planning helps medium/long segments;
- temporal planning should not be globally enabled for short segments;
- default guard: `TEMPORAL_PLAN_MIN_TARGET_SEC=15.0`.
- duration feasibility and natural-rate feasibility are different. A segment can
  be `too_long` at the base rate but still fit naturally with a modest speed-up;
  another can be `too_short` and require unnaturally slow speech, which should
  trigger text expansion or natural pause planning instead of stretching.

### Strategy Matrix: Planning + Duration Fit

Added experiment variants:

- `old`
- `temporal`
- `old_fit`
- `temporal_fit`

`*_fit` uses FFmpeg `rubberband` or `atempo` after TTS, constrained by
`fit_min_tempo` / `fit_max_tempo` defaults of `0.85..1.15`.

Representative 4-segment result:

```text
old          mean abs drift 3.600s, max 7.631s
temporal     mean abs drift 2.558s, max 6.838s
old_fit      mean abs drift 0.253s, max 1.009s
temporal_fit mean abs drift 0.299s, max 1.195s
```

For long segments, fit ratios around `0.91..1.10` often make duration exact.
For short segments, required ratios can fall below `0.85`, and the fit is
skipped by design.

### ASR Risk Check

Subset ASR check:

- `temporal_fit` can preserve semantic quality on some long segments
  (`segment 10512` similarity about 0.92).
- On other segments, especially when the generated speech was already weak,
  fitted output can reduce ASR similarity or even produce empty ASR.

Conclusion:

- time-stretch cannot be an unconditional final step;
- it must be one candidate in a candidate set;
- candidate selection must include both timing and semantic/perceptual checks.

## Proposed Architecture

### Stage 1: Candidate Generation

For each segment, generate bounded candidates:

1. legacy TTS candidate,
2. temporal-plan chunked candidate for medium/long segments,
3. limited-fit version of each candidate when tempo ratio is within bounds,
4. optional length-aware retranslation candidates: short / normal / long.

Do not run all candidates for every segment. Use risk routing:

- short and low-risk: legacy only;
- medium/long or historically high drift: temporal + fit candidates;
- over hard cap after candidate pass: length-aware retranslation candidate.

### Stage 2: Multi-Dimensional Scoring

Score each candidate independently:

- timing score: absolute drift, direction, cap violation;
- rate score: required chars/sec, rate ratio, adjacent rate jump, and whether
  the candidate stays inside the pacing/emotion-adjusted natural window;
- semantic score: ASR back-transcription similarity and empty/weird ASR guard;
- perceptual score: clipping, tempo ratio, duration fit method, diagnostics;
- cost score: number of TTS / ASR / LLM calls.

Choose the highest candidate only if it passes hard guards:

- over-run cap: target + 0.3s,
- under-run cap: target - 0.7s,
- ASR non-empty,
- ASR similarity above threshold for non-short segments,
- required rate inside the natural window unless explicitly marked as an
  acceptable emphasis/fast passage,
- adjacent accepted segments must not create large rate jumps without a
  matching emotion/pacing transition,
- tempo ratio within configured quality window.

### Stage 3: Hard Validation Gate

The final validation report must include timing:

- segment count over 0.3s / 0.5s / 1.0s / 2.0s,
- p50 / p90 / p95 / p99 / max absolute drift,
- drift direction distribution,
- ASR empty / weird count,
- fit ratio distribution.

ASR-only PASS is invalid for release.

## Open Engineering Problems

1. Calibrated speaking-rate source:
   - voice profile `EstCharsPerSec` is often empty;
   - need episode-level median / rolling estimate from completed segments.

2. Candidate reuse:
   - current experiment script synthesizes `old_fit` separately;
   - production should synthesize once, then derive fit candidates from the
     same audio.

3. ASR cost:
   - ASR releases IndexTTS2 in the current service path;
   - candidate scoring needs either batched post-pass ASR or a separate worker
     profile so scoring does not thrash TTS model loading.

4. Semantic scoring brittleness:
   - ASR confuses technical tokens such as `S2`;
   - use normalized glossary-aware scoring, not raw string similarity only.

5. Long-form acceptance target:
   - desired target should be explicit before full migration, e.g.
     p95 <= 0.7s, max <= 1.5s, zero over 2.0s, ASR empty count = 0.

6. Prosody continuity:
   - final merge should report speaking-rate variance and adjacent rate jumps;
   - abrupt speed changes should require an explicit pacing/emotion reason;
   - otherwise the selector should prefer a slightly worse timing candidate
     over a jarring rate jump.

## Current Recommendation

Do not flip temporal planning globally yet.

## Closed-Loop Chunk Selection Update

Implemented prototype:

- `tools/temporal_chunk_select_experiment.py`
- `tools/temporal_closed_loop_canary.py`
- chunk-local candidate generation before final concat;
- ASR + duration + critical-term scoring per chunk;
- global combination search instead of greedy per-chunk choice;
- final fit only when drift exceeds tolerance and remains inside the tempo
  quality window;
- PCM re-encode concat to avoid WAV stream-copy boundary issues;
- technical-term guard for protocol terms, nodes, log terms, indexes, clients,
  and linearizability.

Key lessons:

- Whole-segment ASR is unreliable for long concatenated lecture audio; boundary
  or source-chunk ASR is the primary quality signal.
- Final rubberband fit can damage terms such as `linearizability`; raw audio is
  preferred when drift is already within about `700ms`.
- Candidate repeats are not blind retry when they are local, bounded, scored,
  and selected by independent reward dimensions. Four candidates per chunk was
  enough for the current hard cases.
- Text planning must repair known TTS/ASR traps before synthesis:
  - `server 1/2` is clearer as `one/two node` in Chinese lecture speech;
  - `previous log index` is clearer as `previous entry number` when the exact
    word `index` is repeatedly misread;
  - isolated `log` is unstable, while `log entry` is stable;
  - compact text must end as a complete sentence, never a hard-cut tail.

Representative strict canary results after these changes:

```text
run 20260616-163312 strict auto-sample, episode 146:
segments: 6/6 quality-pass
mean_abs_drift_ms: 237.333
p95_abs_drift_ms: 597.000
max_abs_drift_ms: 681.000
mean_boundary_asr: 0.982562
min_boundary_asr: 0.894737
mean_term_recall: 1.000000
min_term_recall: 1.000000
min_text_retention_ratio: 0.884892
max_effective_tempo_delta: 0.070208
max_adjacent_rate_jump: 0.220661

selected routes:
10543 old/4       drift -149ms ASR 1.000 terms 1.000
10565 old/4       drift +345ms ASR 1.000 terms 1.000
10537 compact/12  drift -681ms ASR 0.951 terms 1.000
10663 compact/4   drift -249ms ASR 0.987 terms 1.000
10514 compact/4   drift    0ms ASR 0.962 terms 1.000
10527 old/12      drift    0ms ASR 0.995 terms 1.000
```

Current reliable defaults for the chunk experiment:

```text
candidate_repeats=4
escalate_repeats=12
max_chunks=7
min_chunk_chars=20
max_chunk_chars=70
final_fit_min_drift_ms=700
final_fit_max_delta=0.10
quality_fit_max_delta=0.10
quality_max_adjacent_rate_jump=0.24
quality_min_boundary_asr=0.92
quality_min_chunk_boundary_asr=0.85
quality_min_term_recall=0.98
quality_min_chunk_term_recall=1.00
```

Additional changes from the strict loop:

- `Raft` stays as a natural English technical token in TTS text. ASR spellings
  such as `Raph`, `Raphter`, `Ruft`, `Rough`, and `Rafting` are treated as a
  soft match for evaluation only.
- Chinese protocol terms remain hard gates: `server/node`, `log`, `leader`,
  `term`, `RPC request`, `linearizability`, `candidate/election` must be
  recovered by boundary ASR.
- A compact variant is required to end as a complete sentence only when it
  actually rewrites the original text. If compact routing returns the original
  cross-boundary segment unchanged, the original segment boundary is respected.
- If natural speech is far shorter than target and would require excessive
  slowing, the experiment can fill shortfall with silence at chunk boundaries
  instead of rubberbanding speech. This records `effective_tempo_delta` so the
  quality gate measures audible speed change rather than segment duration.
- Known clarity repair: `因任期更高` -> `因为任期更高`, which lifted 10537's
  weakest chunk from roughly 0.83 to roughly 0.89 boundary ASR.

Residual manual-listening watch points:

- 10514: ASR rendered `具体来说` as `据起来说`; terms are preserved, but this
  phrase should be spot-checked by ear.
- 10537: ASR rendered `当前任期是八` as `当天任期是八`; term recall remains
  correct, but this should be spot-checked by ear.
- Silence filling must be validated on full episode merge; padding is safer
  than slowing speech, but excessive pauses should be capped by source-pause
  evidence in production.

Community IndexTTS2 duration-control probe, 2026-06-16:

- Implemented an experimental `INDEXTTS2_DURATION_CONTROL_MODE=embedding`
  path based on the JarodMica community patch idea: consume
  `target_duration_tokens` before `UnifiedVoice.inference_speech` calls
  HuggingFace `generate`, and replace the second conditioning token with a
  mel-position duration embedding.
- The official release still has no stable duration API; this remains
  off-by-default and must not be treated as upstream-supported behavior.
- Probe run:
  `data/validation/indextts2_duration_probe/20260616-200343/results.asr.jsonl`.
- `duration_token_rate=50.0` failed badly: 10514 compact target 27.86s became
  58.04s. This matches community reports that the exposed scale is not a plain
  50 semantic tokens/sec knob.
- `duration_token_rate=23.4375` was promising for longer segments:
  - 10514 compact/off: 30.32s, drift +2.46s, ASR 0.183, terms 0.333.
  - 10514 compact/embedding: 27.18s, drift -0.68s, ASR 0.912, terms 0.667.
  - 10537 compact/off: 17.96s, drift -3.84s, ASR 0.939, terms 1.000.
  - 10537 compact/embedding: 20.96s, drift -0.84s, ASR 0.937, terms 0.875.
- The same setting hurt short text:
  - 10543 compact/off: 4.86s for a 5.78s slot, ASR 1.000.
  - 10543 compact/embedding: 3.53s, drift -2.25s, ASR 0.857.
- Code response: add `INDEXTTS2_DURATION_CONTROL_MIN_SEC=15.0`, so the
  experimental embedding path is only considered for longer requests. Short
  segments continue to use the regular IndexTTS2 path.
- Interpretation: community duration embedding can be useful as a candidate
  generator for medium/long under-filled or over-compressed segments, but it is
  not yet a universal replacement for planning, chunk selection, or human
  listening gates.

Smoke command for the closed-loop canary runner:

```powershell
python tools\temporal_closed_loop_canary.py --episode-id 146 --segment-plan 10543:old --llm-compact
```

Representative learned plan:

```powershell
python tools\temporal_closed_loop_canary.py --episode-id 146 --llm-compact
```

Routing observed so far:

- short/natural segments should try original spoken text first;
- medium segments should try original chunk-select before compacting;
- high-drift/too-fast long technical segments should try compact chunk-select;
- final stretch is a scored candidate, not a mandatory cleanup step.

Next implementation milestone:

1. build candidate scorer and selector behind a new flag;
2. route only medium/long risky segments into candidate generation;
3. add timing drift to validation gate;
4. run episode 146 full canary;
5. only then decide whether to replace SegmentAgent retry as the main loop.
