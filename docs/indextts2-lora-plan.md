# IndexTTS2 LoRA Pilot

This is the HoloDub LoRA route for reducing swallowed speech, unstable pacing,
and duration drift in technical Chinese dubbing.

## Current Closed-Loop Report

Read this first for the current baseline, user-listening regressions, risk
queue, and next engineering steps:

```text
docs/lora-closed-loop-report-2026-06-18.md
```

This pilot document is now treated as the historical experiment log. New results
should be summarized in structured closed-loop reports first, then linked here
only when they change the current decision.

## Current Decision

Do not train a full TTS model from scratch. Start with a parameter-efficient
adapter on the IndexTTS2 GPT/T2S stage:

- train target: text/emotion/speaker conditioning to semantic speech tokens;
- frozen: semantic codec, S2Mel, BigVGAN/vocoder, speaker encoders;
- adapter: LoRA on GPT linear layers only;
- duration conditioning: include target semantic-token length for medium/long
  segments, with dropout so the model does not collapse into hard speed control.

The official `indextts2-inference` package currently ships inference modules,
not a trainer. Community references are useful but incomplete for our exact
target:

- `JarodMica/index-tts` branch `training_v2`: GPT v2 training scaffold and
  duration-control idea, not a drop-in LoRA trainer.
- `asr-pub/index-tts-lora`: LoRA trainer for IndexTTS 1.5, useful for PEFT
  patterns, not directly compatible with IndexTTS2.

## Data Tiers

`gold`

Human-recorded or explicitly human-approved Chinese dubbing audio. This is the
only tier allowed for quality claims.

`silver`

Existing HoloDub synthetic TTS output after automatic filtering. This can be
used for smoke tests, pipeline validation, and duration-token-rate calibration.
It must not be used as proof that the model improved, because it can teach the
model our current failures.

## Started Tooling

Build a LoRA corpus manifest:

```powershell
python tools\lora_dataset_manifest.py --episode-ids 146 --max-samples 200
```

Run environment/data preflight:

```powershell
python tools\lora_preflight.py --manifest-dir data\training\lora_corpus\<run>
```

Pilot config:

```text
config/indextts2-lora-pilot.example.json
```

First smoke manifest, 2026-06-16:

```text
data/training/lora_corpus/smoke-episode146
accepted samples: 118
accepted audio: 0.6948 hours
splits: train 103, val 8, test 7
tier: silver
```

Preflight result:

```text
GPU: NVIDIA GeForce RTX 5080, 16303 MiB
indextts2 trainer modules: missing
peft: missing
accelerate: present
omegaconf: present
```

## First Local Training Smoke

Run: `data/training/lora_runs/smoke-episode146-r4-step20`

Feature cache:

```text
data/training/lora_features/smoke-episode146
train: 48 samples, 220-1193 semantic tokens
val: 4 samples, 404-954 semantic tokens
test: 3 samples, 456-965 semantic tokens
duration token rate: 23.4375
```

Adapter:

```text
rank: 4
alpha: 16
target modules: GPT transformer Conv1D attention + MLP projections
trainable parameters: 1,966,080 / 867,946,719
steps: 20
```

Teacher-forced validation improved, which means the adapter is learning the
cached semantic-code distribution:

```text
val text_loss: 2.2090 -> 1.9816
val mel_loss: 4.6915 -> 4.1069
val mel_top1: 0.1044 -> 0.1383
```

Generated test comparison:

```text
seg10601 baseline drift -186ms, ASR sim 0.9649
seg10601 LoRA     drift +441ms, ASR sim 0.9825
seg10647 baseline drift -199ms, ASR sim 0.9744
seg10647 LoRA     drift +160ms, ASR sim 1.0000
seg10670 baseline drift -2139ms, ASR sim 0.9796
seg10670 LoRA     drift  +66ms, ASR sim 0.9796
baseline mean ASR sim: 0.9730
LoRA mean ASR sim:     0.9873
```

Interpretation:

- The training/inference/ASR loop is now operational on the local RTX 5080.
- This smoke is not a production-quality claim because the corpus is silver
  synthetic TTS, not human-approved gold audio.
- The strongest signal is the long test sample: duration drift improved from
  -2.139s to +0.066s without ASR similarity loss.
- Short samples moved slightly long but stayed within 0.5s; they need a
  duration-aware gate before this adapter is used broadly.

## Expanded Local Pilot

Run: `data/training/lora_runs/pilot-episode146-code1500-r4-step80`

Feature cache:

```text
data/training/lora_features/pilot-episode146-code1500
train: 76 samples, 220-1496 semantic tokens, target 4.6-31.7s
val: 4 samples, 404-954 semantic tokens
test: 4 samples, 456-1369 semantic tokens, target 8.28-26.35s
duration token rate: 23.4375
```

Training:

```text
rank: 4
alpha: 16
steps: 80
gradient checkpointing: enabled
best checkpoint: adapter_step60.pt
```

Teacher-forced validation:

```text
step 0  mel_loss=4.6915, text_loss=2.2090, mel_top1=0.1044
step 20 mel_loss=4.1326, text_loss=1.9619, mel_top1=0.1372
step 40 mel_loss=4.0794, text_loss=1.9247, mel_top1=0.1451
step 60 mel_loss=4.0316, text_loss=1.8740, mel_top1=0.1562
step 80 mel_loss=4.0518, text_loss=1.8781, mel_top1=0.1485
```

Generated test comparison using `adapter_step60.pt`:

```text
seg10601 baseline drift  +220ms, ASR sim 0.9825
seg10601 LoRA     drift  +998ms, ASR sim 0.9649
seg10647 baseline drift  -420ms, ASR sim 1.0000
seg10647 LoRA     drift  +497ms, ASR sim 1.0000
seg10670 baseline drift +1018ms, ASR sim 1.0000
seg10670 LoRA     drift  -456ms, ASR sim 0.9898
seg10678 baseline drift -3711ms, ASR sim 0.9478
seg10678 LoRA     drift -3107ms, ASR sim 0.9565
```

The raw LoRA adapter improved long-sample drift but regressed one shorter
sample. A duration/ASR gate now chooses LoRA only when the drift gain is
meaningful and ASR does not fall beyond tolerance:

```text
selection report: data/training/lora_runs/pilot-episode146-code1500-r4-step80/inference_step60/selection_report.json
baseline mean abs drift: 1342ms, mean ASR sim 0.9826, min ASR sim 0.9478
LoRA mean abs drift:     1264ms, mean ASR sim 0.9778, min ASR sim 0.9565
gated mean abs drift:    1051ms, mean ASR sim 0.9822, min ASR sim 0.9565
```

Gate policy:

```text
min_lora_target_sec: 15.0
min_drift_gain_ms: 250
max_similarity_drop: 0.015
min_lora_similarity: 0.95
short_max_lora_drift_ms: 500
```

Interpretation:

- The adapter is useful for long segments but should not be globally enabled.
- Gated selection is currently better than either pure baseline or pure LoRA
  on this tiny holdout.
- This still does not prove final quality because training data is silver
  synthetic teacher audio and the holdout is too small.

## Duration-Control Fix and Gatefit Result

Finding, 2026-06-16:

- IndexTTS2's T2S design exposes duration control through an optional speech
  token count. See the IndexTTS2 paper and project page:
  <https://arxiv.org/html/2506.21619v2> and
  <https://index-tts.github.io/index-tts2.github.io/>.
- Our LoRA training path already used duration embeddings under teacher
  forcing, but the LoRA inference script was not passing
  `target_duration_tokens` into GPT generation. This explained why raw LoRA
  helped some long samples but did not strongly suppress drift.
- `tools/lora_infer_indextts2.py` now patches IndexTTS2 inference locally,
  passes `target_duration_tokens`, supports a piecewise token-rate policy, and
  records the actual rate/tokens used per sample.
- `tools/lora_duration_fit.py` adds a bounded final tempo fit. It is gated by
  source drift and maximum tempo delta, because a full fit can hurt ASR even
  when the tempo change is small.
- `ml_service` now supports the same piecewise duration-token-rate policy behind
  `INDEXTTS2_DURATION_TOKEN_RATE_POLICY=piecewise`; default remains
  `constant` and duration control remains off unless explicitly enabled.

Validated candidate:

```text
source inference:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/inference_step60_duration_calibrated

gatefit output:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/inference_step60_duration_calibrated_gatefit

policy:
fit only if abs drift > 300ms
max tempo delta: 0.03
filter: atempo
```

Per-sample gatefit result:

```text
seg10601 drift  +13ms, ASR sim 0.9825, fit tempo 1.0279
seg10647 drift +242ms, ASR sim 1.0000, fit skipped
seg10670 drift +264ms, ASR sim 1.0000, fit skipped
seg10678 drift  +86ms, ASR sim 0.9652, fit skipped

mean abs drift: 151ms
max abs drift:  264ms
mean ASR sim:   0.9844
min ASR sim:    0.9652
```

Comparison to the previous non-duration baseline on the same four-sample
holdout:

```text
baseline mean abs drift: 1342ms, mean ASR sim 0.9826, min ASR sim 0.9478
gatefit  mean abs drift:  151ms, mean ASR sim 0.9844, min ASR sim 0.9652
```

Rejected variant:

```text
full tempo fit mean abs drift: ~7.5ms
full tempo fit mean ASR sim:  0.9789
reason rejected: seg10670 ASR dropped from 1.0000 to 0.9592 after a small
1.3% tempo change.
```

Conclusion:

- The best current route is not "make every segment exactly match duration."
  It is: duration-conditioned LoRA candidate, ASR/term gate, then tempo fit
  only for drift outliers.
- On this holdout, the current target of mean drift under 300ms is met without
  ASR loss versus the old baseline.
- This is still a pilot result. The next proof point must use a larger canary
  and gold or human-approved audio.

Training hook added for the next adapter:

```text
tools/lora_train_indextts2_smoke.py
--mel-stop-loss-weight
--mel-end-loss-weight
--mel-end-window
```

These options allow the next LoRA run to emphasize the final semantic tokens
and stop token, targeting swallowed endings without increasing text/ASR loss.

## Prosody / Robot-Like Pitch Follow-Up

User listening note, 2026-06-16:

```text
Tone color and speaking rate are acceptable, but pitch is too flat and sounds
robotic.
```

Measurement confirmed this. The accepted duration gatefit candidate had much
narrower F0 dynamics than the reference prompt:

```text
accepted gatefit mean F0 10-90 range: 4.68 semitones
teacher/silver mean F0 10-90 range:   6.00 semitones
voice reference F0 10-90 range:       7.74 semitones
```

New tooling:

```text
tools/lora_prosody_eval.py
```

It computes per-sample and per-variant F0 range, F0 standard deviation, energy
range, energy standard deviation, and silence ratio. This becomes the third
gate next to duration and ASR.

Prosody candidates tested:

```text
1. use_emo_text=True, emo_alpha=1.0
   raw: strong F0 improvement, but duration drift too high
   gatefit: mean F0 range 7.34, mean ASR 0.9865
   concern: required up to 8.5% tempo fit on this holdout

2. explicit light vector [0.12,0,0,0,0,0,0.08,0.15]
   gatefit: mean F0 range 5.76, mean ASR 0.9878
   benefit: conservative and stable; max tempo fit 4.4%

3. use_emo_text=True, emo_alpha=0.5
   gatefit: mean F0 range 5.89, mean ASR 0.9880
   duration: all samples within 42ms after gatefit
   fit: max tempo delta 6.3%
```

Current recommended candidate:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/inference_step60_prosody_emotext_a05_gatefit
```

Comparison:

```text
previous accepted gatefit:
  mean abs drift: 151ms
  mean ASR sim:   0.9844
  min ASR sim:    0.9652
  mean F0 range:  4.68 semitones

prosody alpha=0.5 gatefit:
  mean abs drift: ~23ms
  max abs drift:  ~42ms
  mean ASR sim:   0.9880
  min ASR sim:    0.9694
  mean F0 range:  5.89 semitones
```

Interpretation:

- The robotic sound is not caused by the final tempo fit; it is caused by the
  generation path being optimized for duration and ASR while emotion/prosody is
  mostly disabled.
- Full `use_emo_text` restores expressive F0 movement but can disturb duration
  too much.
- `use_emo_text` with `emo_alpha=0.5` is the best current compromise: it gets
  very close to teacher F0 dynamics while improving ASR and duration metrics on
  this holdout.
- This still needs human listening. F0 range is a useful guardrail, not a MOS
  replacement.

## Canary8 Closed-Loop Result

Run date: 2026-06-17.

New tooling:

```text
tools/lora_canary_select.py
tools/lora_candidate_gate.py
tools/lora_build_listening_pack.py
```

Canary sets:

```text
8-sample smoke:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary_8.features.jsonl

24-sample expanded canary:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary_24.features.jsonl
```

The 8-sample set intentionally mixes short/medium/long segments and
code-switching risk. It is still silver data, so it is an engineering canary,
not a final quality claim.

Candidates tested:

```text
flat:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary8_flat_gatefit

light_vector:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary8_light_vector_gatefit

emotext_a05:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary8_emotext_a05_gatefit
```

Per-candidate metrics:

```text
flat:
  mean abs drift: 42.5ms
  max abs drift:  262.3ms
  mean ASR sim:   0.9840
  min ASR sim:    0.9559
  mean F0 range:  4.12 semitones

light_vector:
  mean abs drift: 37.0ms
  max abs drift:  115.6ms
  mean ASR sim:   0.9923
  min ASR sim:    0.9739
  mean F0 range:  5.53 semitones

emotext_a05:
  mean abs drift: 553.7ms
  max abs drift:  3533.6ms
  mean ASR sim:   0.9845
  min ASR sim:    0.9545
  mean F0 range:  5.74 semitones
```

Important correction:

- The first F0 gate used `min_f0_range=5.5`, which was too strict because this
  canary's teacher mean F0 range is 5.39 semitones and teacher minimum is 4.11.
- That caused fallback selection to trade away ASR for a small F0 gain.
- `tools/lora_candidate_gate.py` is now ASR-first:
  `min_similarity=0.97`, `target_similarity=0.985`,
  `min_f0_range=4.5`, `target_f0_range=5.4`, with ASR weighted more heavily
  than F0.

Selected canary8 result:

```text
selected report:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary8_candidate_gate_report.json

selected audio:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary8_candidate_gate_selected

mean abs drift: 15.5ms
max abs drift:  44.4ms
mean ASR sim:   0.9936
min ASR sim:    0.9739
mean F0 range:  5.73 semitones
min F0 range:   4.53 semitones
max tempo delta: 5.7%
```

Selected decisions:

```text
seg10546 emotext_a05
seg10562 light_vector
seg10601 flat
seg10627 light_vector
seg10647 light_vector
seg10663 light_vector
seg10670 emotext_a05
seg10678 light_vector
```

Listening pack:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary8_listening_pack/index.html
```

The page contains `selected`, all three candidates, teacher audio, and the
voice reference. The manifest was validated for missing audio links.

## Canary24 Term-Aware Result

Run date: 2026-06-17.

This expanded canary keeps the same ASR-first candidate gate, but now adds
critical-term recall. The term recall has two layers:

- Latin/code terms with ASR-tolerant aliases, for example `AppendEntries`,
  `XTerm`, `RPC`, `Fsync`, `Raft`, `x`, and numbers.
- Strict Chinese domain terms, for example `日志`, `日志条目`, `持久化`,
  `易失性`, `执行历史`, `全序`, `线性一致性`, `快照`, and `介质`.

Selected report:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_candidate_gate_report.json
```

Selected result:

```text
mean abs drift:              24.4ms
max abs drift:               223.1ms
mean ASR sim:                0.9855
min ASR sim:                 0.9615
mean critical-term recall:   0.9237
min critical-term recall:    0.5000
mean Latin surface recall:   0.9722
mean F0 range:               5.66 semitones
min F0 range:                4.16 semitones
max tempo delta:             7.86%
```

Important rescues:

```text
seg10686 split_mix:
  problem: long text tail loss / several-second drift in monolithic candidates
  result:  drift +30.9ms, ASR 0.9796, critical-term recall 1.0,
           F0 range 6.46 semitones
  path:    data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_rescue_split_seg10686_mix_gatefit

seg10569 emotext_a05_08:
  problem: r23 rescue fixed duration but ASR fell to 0.9314
  result:  drift -4.9ms, ASR 0.9744, critical-term recall 1.0
  note:    Whisper still writes AppendEntries as "Appendantries"; this is kept
           as an accepted ASR alias because the audio is code-switch-like.
```

Negative result:

```text
seg10584:
  problem: ASR writes 易失性 as 意识性
  tried:   text rewrite variants and two-clause split synthesis
  result:  no improvement; rewrite variants reduced ASR similarity
  status:  keep the original selected emotext_a05 candidate and require human
           listening on this sample before treating strict Chinese term recall
           as fully resolved
```

Remaining strict Chinese term misses in selected canary24:

```text
seg10584: 易失性 -> ASR writes 意识性
seg10611: 介质 -> ASR writes 戒指
seg10662: 全序 -> ASR writes 权序
seg10678: 全序 -> ASR writes 权序
```

Interpretation:

- The duration target is now comfortably inside the requested 300ms average
  drift gate and below the earlier 500ms per-sample reject gate.
- The strongest new evidence is that planning before synthesis, specifically
  clause-level split synthesis with per-part duration fitting, fixes the hardest
  long-tail swallow case better than repeated monolithic retries.
- Some strict Chinese term misses are true homophones or near-homophones in ASR.
  They should be kept visible in the listening pack rather than hidden by the
  aggregate ASR score.
- The next engineering step should promote split planning from a manual rescue
  into an automatic long-segment strategy with a join-quality check.

Listening pack:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_listening_pack/index.html
```

The pack contains the final selected audio, teacher audio, representative
candidate families, the successful `seg10686` split_mix rescue, and the failed
`seg10584` split experiment. The manifest was validated for missing audio links.

## Automated Split Planning Tooling

Implemented after the canary24 result:

```text
tools/lora_split_plan.py
tools/lora_split_assemble.py
```

`lora_split_plan.py` converts high-risk whole-sample feature rows into
clause-level feature rows. It preserves the original sample ID as
`source_sample_id`, assigns part IDs like `seg10686_p1`, allocates per-part
target durations, and writes both a JSONL feature file and a JSON plan.

Example:

```text
python tools/lora_split_plan.py \
  --features data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary_24.features.jsonl \
  --sample-ids seg10686 \
  --output-features data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_auto_split_seg10686.features.jsonl \
  --output-plan data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_auto_split_seg10686.plan.json \
  --pause-sec 0.18 \
  --max-part-sec 16 \
  --max-part-chars 170 \
  --max-parts 4
```

`lora_split_assemble.py` takes the split plan plus one or more part reports,
inserts silence between parts, concatenates the wavs, and emits a normal
inference report row that can be passed to ASR and candidate gate. Multiple
`--parts-report` values are supported; later reports override earlier rows with
the same part sample ID. This is what makes the successful `seg10686` mixed
candidate reproducible: p1 from `emotext`, p2 from `light_vector`.

Example:

```text
python tools/lora_split_assemble.py \
  --plan data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_auto_split_seg10686.plan.json \
  --parts-report data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_rescue_split_seg10686_parts_gatefit/inference_report.json \
  --parts-report data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_rescue_split_seg10686_p2_light_gatefit/inference_report.json \
  --output-dir data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_auto_split_seg10686_mix_assembled \
  --output-report data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_auto_split_seg10686_mix_assembled/inference_report.json \
  --variant split_auto_mix_gatefit \
  --sample-ids seg10686
```

Validation:

```text
auto split_mix duration: 31.730884s
manual split_mix duration: 31.730884s
auto/manual SHA256: C1D9F97BC50B90EF7B5907E9FCE9425E911A064E3963637F44BE9D2E736D7641
join_quality: pass
```

The candidate gate now also understands `join_quality`. A split candidate with
`join_quality.pass=false` is rejected by default unless
`--allow-failed-join-quality` is explicitly set.

Current caveat:

- `--max-part-sec` is still a planning pressure, not a hard guarantee. The
  planner prioritizes semantic boundaries over cutting inside a clause. If a
  future full-episode canary shows long subparts still swallowing tails, add a
  hard split mode for over-budget clauses.

Next gate:

- Promote split planning into an automatic path for long or high-risk segments.
- Add a join-quality check for split audio, including pause length, local RMS,
  and ASR continuity around the join.
- Keep strict Chinese term misses visible, but separate likely ASR homophones
  from audible pronunciation failures through human listening labels.
- Move the canary24-selected route into service integration behind an opt-in
  setting only after the automatic split path reproduces the manual `seg10686`
  rescue.

## One-Command Canary Candidate Loop

Implemented:

```text
tools/lora_canary_candidate_loop.py
```

This is the offline orchestration entry point for the closed-loop canary path.
It runs split planning, candidate synthesis or report reuse, duration fit, ASR
term rescoring, prosody merge/eval, candidate gate, listening pack generation,
and a machine-readable summary.

Dry-run command used to reproduce canary24 from existing artifacts:

```powershell
$base='data\training\lora_runs\pilot-episode146-code1500-r4-step80'
python tools\lora_canary_candidate_loop.py `
  --features "$base\canary_24.features.jsonl" `
  --run-dir "$base\canary24_loop_dryrun_r23" `
  --adapter "$base\adapter_step60.pt" `
  --skip-inference `
  --existing-run-root "$base" `
  --voice-prompt-relpath voices/test_ref.wav
```

Candidate families:

```text
flat
light_vector
emotext_a05
emotext_a05_08
rescue_r23_emotext
split_emotext
split_light_vector
split_mix
```

`rescue_r23_emotext` is intentionally included even though it was not in the
first fixed-family sketch. The canary24 success set depends on this duration
rate rescue for `seg10578`: without it, the dry-run selected max drift regresses
to 2676ms. With it, the loop reproduces the accepted canary24 gate result.

canary24 dry-run result:

```text
run dir:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_loop_dryrun_r23

selected count: 24
mean abs drift: 24.426ms
max abs drift: 223.084ms
mean ASR similarity: 0.98554
min ASR similarity: 0.96154
mean critical-term recall: 0.92368
min critical-term recall: 0.5
mean F0 range: 5.662 semitones
max fit tempo delta: 0.07857
join-quality failures: 0
```

`seg10686` regression:

```text
selected candidate: split_mix
duration: 31.730884s
drift: +30.884ms
ASR similarity: 0.97959
critical-term recall: 1.0
join_quality: pass
SHA256: C1D9F97BC50B90EF7B5907E9FCE9425E911A064E3963637F44BE9D2E736D7641
```

Primary outputs:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_loop_dryrun_r23/loop_summary.json
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_loop_dryrun_r23/candidate_gate_report.json
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary24_loop_dryrun_r23/listening_pack/index.html
```

Important dry-run caveat:

- `split_light_vector` is part of the full-run candidate family. The dry-run
  can only reuse already existing split part reports, so canary24 dry-run
  reproduces `split_emotext` and `split_mix` for the historical `seg10686`
  rescue but does not fabricate missing split-light artifacts for other long
  samples.

Two-sample full-generation smoke:

```powershell
$base='data\training\lora_runs\pilot-episode146-code1500-r4-step80'
python tools\lora_canary_candidate_loop.py `
  --features "$base\canary_24.features.jsonl" `
  --run-dir "$base\canary2_full_smoke_maxsamples" `
  --adapter "$base\adapter_step60.pt" `
  --voice-prompt-relpath voices/test_ref.wav `
  --sample-ids seg10578,seg10686
```

Result:

```text
selected count: 2
selected choices:
  seg10578 -> split_emotext
  seg10686 -> split_mix
mean abs drift: 22.698ms
max abs drift: 27.710ms
mean ASR similarity: 0.98036
min ASR similarity: 0.97959
critical-term recall: 1.0
selected join-quality failures: 0
```

The full smoke exposed and fixed a real orchestration bug: split part inference
was not passing `--max-samples`, so the underlying inference tool defaulted to
3 rows and silently omitted `seg10578_p2` when two long samples produced four
split parts. The loop now passes `--max-samples 100000` to every inference call.

The full smoke also produced one rejected split candidate:

```text
split_light_vector / seg10686 -> join_quality.pass=false, reason high_total_drift
```

This is expected and useful: failed join-quality candidates remain visible in
`loop_summary.json` but do not enter selected.

Next loop expansion:

- Run a full canary on 80-120 high-risk samples, with actual split part
  inference for all planned long samples.
- Keep `join_quality.pass=false` as a hard reject unless explicitly overridden.
- Do not wire the main TTS path until the expanded offline loop produces stable
  summary JSON and listening packs.

## Canary84 Expansion Protocol

Current local pilot feature inventory:

```text
data/training/lora_features/pilot-episode146-code1500
total rows: 84
train: 76
val: 4
test: 4
target duration range: 4.6s - 31.7s
split-planned rows: 29
split part rows: 58
```

The local data cannot produce a true 120-sample canary yet; canary84 is the
full available pilot set. A 120-sample run requires expanding the feature cache
first.

Selection command:

```powershell
$base='data\training\lora_runs\pilot-episode146-code1500-r4-step80'
python tools\lora_canary_select.py `
  --features-dir data\training\lora_features\pilot-episode146-code1500 `
  --output "$base\canary_84.features.jsonl" `
  --count 84 `
  --sample-ids-output "$base\canary_84.sample_ids.txt"
```

Split plan command:

```powershell
python tools\lora_split_plan.py `
  --features "$base\canary_84.features.jsonl" `
  --output-features "$base\canary84_plan\split.features.jsonl" `
  --output-plan "$base\canary84_plan\split.plan.json" `
  --min-target-sec 18 `
  --min-text-chars 180 `
  --max-part-sec 16 `
  --max-part-chars 170 `
  --max-parts 4 `
  --pause-sec 0.18 `
  --duration-token-rate-policy piecewise `
  --duration-token-rate 23.4375
```

Loop scalability updates:

```text
--sample-ids-file       read comma-separated or newline-separated sample IDs
--candidate-families    run a subset such as split_emotext,split_light_vector,split_mix
--resume                reuse existing raw/fit/ASR/prosody/split reports
```

Rescue queue helper:

```text
tools/lora_canary_rescue_ids.py
```

It reads one or more `loop_summary.json` files and writes a sample ID file for
the next rescue pass. By default it includes `no_selection_samples`,
`low_asr_samples`, and `low_critical_term_samples`.

Listening pack filtering:

- `tools/lora_build_listening_pack.py` supports `--only-report-samples`.
- The canary loop now uses it by default, so sharded listening packs show teacher
  audio only for samples that also have candidate rows. This avoids pages full
  of teacher-only rows when a shard is built from the full canary feature file.

The loop now passes `--max-samples 100000` to inference, preventing silent
truncation when split features contain more rows than the inference tool's
default.

`--resume` is coverage-aware. A cached report is reused only when it covers the
requested sample IDs and, for assembled reports, all expected audio paths still
exist. This prevents accidentally reusing a smaller dry-run report for a larger
canary expansion.

The candidate gate never selects a candidate with failed `join_quality`, even in
fallback mode. If every candidate for a sample fails join quality, the loop emits
`no_selection_samples` in `loop_summary.json` instead of copying an unsafe audio
file into `selected/`.

First canary84 shard:

```text
sample id file:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split6.sample_ids.txt

sample ids:
seg10663
seg10611
seg10569
seg10686
seg10602
seg10642
```

Split-only full-generation command:

```powershell
$base='data\training\lora_runs\pilot-episode146-code1500-r4-step80'
python tools\lora_canary_candidate_loop.py `
  --features "$base\canary_84.features.jsonl" `
  --run-dir "$base\canary84_split6_full_smoke" `
  --adapter "$base\adapter_step60.pt" `
  --voice-prompt-relpath voices/test_ref.wav `
  --sample-ids-file "$base\canary84_split6.sample_ids.txt" `
  --candidate-families split_emotext,split_light_vector,split_mix `
  --resume
```

Result:

```text
selected count: 6
mean abs drift: 12.827ms
max abs drift: 21.769ms
mean ASR similarity: 0.98336
min ASR similarity: 0.96377
mean critical-term recall: 0.94286
min critical-term recall: 0.71429
mean F0 range: 5.858 semitones
selected join-quality failures: 0
```

Selected candidates:

```text
seg10569 -> split_light_vector, drift 13.243ms, ASR 0.96377, fallback
seg10602 -> split_emotext,      drift  0.317ms, ASR 0.96727, fallback
seg10611 -> split_light_vector, drift 21.769ms, ASR 1.00000
seg10642 -> split_mix,          drift 11.519ms, ASR 0.98955
seg10663 -> split_mix,          drift 20.272ms, ASR 1.00000
seg10686 -> split_mix,          drift  9.841ms, ASR 0.97959
```

Rejected join-quality candidates remain visible:

```text
split_emotext / seg10569 -> high_total_drift
split_light_vector / seg10686 -> high_total_drift
split_mix / seg10569 -> high_total_drift
```

Listening pack:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split6_full_smoke/listening_pack/index.html
```

Interpretation:

- Split planning is robust on this high-risk shard for timing: selected max
  drift is far below 300ms.
- ASR is the next pressure point. Two selected rows are fallback choices because
  ASR is just under the 0.97 hard gate, despite excellent timing.
- The gate is correctly rejecting failed join/drift candidates and still
  finding a usable split candidate for each sample.

Second canary84 split shard:

```text
sample id file:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_shards/split29_shard02.sample_ids.txt

sample ids:
seg10679
seg10574
seg10513
seg10518
seg10633
seg10566
```

Split-only result after the no-selection gate fix:

```text
selected count: 5
mean abs drift: 8.000ms
max abs drift: 14.785ms
mean ASR similarity: 0.97045
min ASR similarity: 0.90547
mean critical-term recall: 0.75000
min critical-term recall: 0.25000
mean F0 range: 5.495 semitones
selected join-quality failures: 0
no-selection samples: seg10566
```

Automatically generated rescue queue:

```powershell
$base='data\training\lora_runs\pilot-episode146-code1500-r4-step80'
python tools\lora_canary_rescue_ids.py `
  --summary "$base\canary84_split29_shard02\loop_summary.json" `
  --output "$base\canary84_shards\split29_shard02_rescue.sample_ids.txt"
```

Output:

```text
seg10518
seg10566
seg10679
```

`seg10566` is the first clear counterexample to "split first is always better".
All split candidates failed `join_quality` because the assembled audio drifted by
about 4.2s, so the gate correctly refused to select any split output. A focused
base rescue run selected `rescue_r23_emotext` instead:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_seg10566_base_rescue

selected:
rescue_r23_emotext

metrics:
abs drift: 4.490ms
ASR similarity: 1.00000
critical-term recall: 1.00000
Latin surface recall: 1.00000
F0 range: 4.938 semitones
fit tempo delta: 0.02609
```

Interpretation:

- Split planning is strong for most long/high-risk rows, but it is not a safe
  universal default.
- The full candidate loop should include `rescue_r23_emotext` alongside split
  candidates, then let the gate choose between split and base rescue.
- Split-only shard runs are useful as diagnostics, but formal canary acceptance
  must use the full candidate set or an explicit rescue pass for no-selection
  samples.

Third canary84 split shard:

```text
sample id file:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_shards/split29_shard03.sample_ids.txt

sample ids:
seg10670
seg10629
seg10678
seg10638
seg10568
seg10586
```

Split-only result:

```text
selected count: 5
mean abs drift: 12.902ms
max abs drift: 47.279ms
mean ASR similarity: 0.98295
min ASR similarity: 0.95973
mean critical-term recall: 0.91875
min critical-term recall: 0.80000
mean F0 range: 5.716 semitones
selected join-quality failures: 0
no-selection samples: seg10638
```

Automatically generated rescue queue:

```text
seg10629
seg10638
seg10678
```

Base rescue result on that queue:

```text
selected count: 3
mean abs drift: 12.933ms
max abs drift: 17.687ms
mean ASR similarity: 0.93691
min ASR similarity: 0.85714
selected candidate family: light_vector for all 3 samples
```

Interpretation:

- Base rescue solved the no-selection row `seg10638`:
  `light_vector` reached drift 11.247ms, ASR 0.98837, term recall 1.0, and F0
  range 5.104 semitones.
- Base rescue was worse than split for `seg10629` and did not improve
  `seg10678`; those should stay on the split-selected path unless human
  listening says otherwise.
- `rescue_r23_emotext` is not a universal rescue: on this queue it had mean
  drift 1599.6ms and mean ASR 0.95152.

Combined shard03 gate:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard03_combined_gate

selected count: 6
mean abs drift: 12.627ms
max abs drift: 47.279ms
mean ASR similarity: 0.98385
min ASR similarity: 0.95973
mean critical-term recall: 0.93500
min critical-term recall: 0.80000
mean F0 range: 5.614 semitones
selected join-quality failures: 0
```

Selected decisions:

```text
seg10568 -> split_emotext
seg10586 -> split_emotext
seg10629 -> split_light_vector, fallback low ASR 0.95973
seg10638 -> light_vector
seg10670 -> split_light_vector
seg10678 -> split_light_vector, fallback low ASR 0.96522
```

Important implementation note:

- When manually merging reports across run directories, Docker-side prosody
  commands must use forward-slash paths. A first hand-written combined run used
  Windows backslashes and produced a candidate gate without F0 metrics; that
  result was discarded and replaced by the valid combined gate above.

Next expansion order:

1. Finish split-only generation for the remaining split-planned canary84 shards.
2. Run a full all-candidate pass for no-selection and low-ASR split rows,
   starting with `rescue_r23_emotext`.
3. Review low-ASR and low-term rows in the listening pack before changing
   thresholds.
4. Run all-candidate generation for the non-split short/medium samples.
5. Merge split and non-split reports into one canary84 candidate gate.
6. Only after canary84 selected metrics and listening quality are stable, start
   the main TTS opt-in integration.

## Canary84 Shard01-Shard05 Notes

First canary84 split shard after spoken-text reporting fix:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard01_combined_gate_auto

selected count: 6
mean abs drift: 14.316ms
max abs drift: 21.769ms
mean ASR similarity: 0.98399
min ASR similarity: 0.96377
mean critical-term recall: 0.94286
min critical-term recall: 0.71429
mean F0 range: 5.629 semitones
selected join-quality failures: 0

listening pack:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard01_combined_gate_auto/listening_pack/index.html
```

Selected decisions:

```text
seg10569 -> split_light_vector, fallback tier best_within_drift
seg10602 -> light_vector
seg10611 -> split_light_vector
seg10642 -> split_mix
seg10663 -> split_mix
seg10686 -> split_mix
```

Shard01 interpretation:

- `seg10686` now reproduces the desired split path automatically:
  `split_mix`, drift 9.841ms, ASR 0.97959, critical-term recall 1.0.
- Base rescue helped `seg10602`: the combined gate moved it from split ASR
  0.96727 to base `light_vector` ASR 0.97101 with 9.252ms drift.
- Base rescue did not help `seg10569`; it lowered semantic quality. The final
  fallback policy keeps the within-drift split candidate instead: drift
  13.243ms, ASR 0.96377, critical-term recall 0.71429.
- `seg10569` remains the shard01 human-listening risk because strict term
  scoring misses `日志条目` / `日志`.
- `--spoken-text-policy figure-digits` now records rewrite counts only for the
  requested sample set. Shard01 correctly reports zero rewrites.

Second canary84 split shard after base rescue:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard02_combined_gate_auto

selected count: 6
mean abs drift: 6.009ms
max abs drift: 13.424ms
mean ASR similarity: 0.98339
min ASR similarity: 0.94527
mean critical-term recall: 0.91667
min critical-term recall: 0.50000
mean F0 range: 5.581 semitones
selected join-quality failures: 0

listening pack:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard02_combined_gate_auto/listening_pack/index.html
```

Selected decisions:

```text
seg10513 -> split_light_vector
seg10518 -> light_vector, fallback tier best_within_drift
seg10566 -> emotext_a05
seg10574 -> split_light_vector
seg10633 -> split_light_vector, fallback tier best_within_drift
seg10679 -> light_vector
```

Shard02 interpretation:

- Base rescue fixed the split no-selection row `seg10566`: `emotext_a05`,
  drift 13.424ms, ASR 1.0, critical-term recall 1.0.
- Base rescue also moved `seg10679` to a strict base `light_vector` candidate:
  drift 4.127ms, ASR 1.0, critical-term recall 1.0.
- `seg10518` improved from split ASR 0.90547 to base `light_vector` ASR
  0.94527, but it still misses numeric terms `5` and `6`. This remains a
  human-listening risk and likely needs a richer spoken-text / numeric-term
  policy rather than more retrying.

Fourth canary84 split shard:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard04_combined_gate_auto

selected count: 6
mean abs drift: 12.381ms
max abs drift: 32.109ms
mean ASR similarity: 0.97888
min ASR similarity: 0.95000
mean critical-term recall: 0.91667
min critical-term recall: 0.66667
mean F0 range: 5.407 semitones
selected join-quality failures: 0

listening pack:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard04_combined_gate_auto/listening_pack/index.html
```

Selected decisions:

```text
seg10526 -> split_mix
seg10576 -> split_mix, fallback low ASR / low term recall
seg10578 -> split_light_vector
seg10587 -> split_mix
seg10598 -> split_light_vector
seg10630 -> rescue_r23_emotext
```

Interpretation:

- Shard04 is temporally strong, with every selected output under 33ms drift.
- The remaining risk is semantic, not timing: `seg10576` needs human listening
  because the strict term scorer misses `日志条目` / `日志`.
- The combined gate correctly chose a base rescue candidate for `seg10630`.

Fifth canary84 split shard before spoken-text planning:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard05

selected count: 5
mean abs drift: 13.378ms
max abs drift: 28.118ms
mean ASR similarity: 0.98700
min ASR similarity: 0.95918
mean critical-term recall: 1.00000
mean F0 range: 5.834 semitones
selected join-quality failures: 0
```

The only weak row was `seg10634`. All retry-style base rescue candidates kept
the same `图2` failure mode: ASR read the phrase as a near homophone such as
`图二` / `图离`, while the Latin/surface term scorer expected the literal digit
`2`. The best base rescue candidate had low drift but still missed the digit:

```text
seg10634 rescue_r23_emotext:
abs drift: 79.002ms
ASR similarity: 0.94898
critical-term recall: 0.66667
missing term: 2
```

This is a planning problem rather than a sampling problem. The added
`figure-digits` spoken-text policy preserves the display text while feeding TTS
a pronounceable text form:

```text
display text: 图2
spoken text: 图二
policy: figure-digits
```

After regenerating `seg10634` with spoken text and recombining shard05:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard05_combined_gate_spoken_auto

selected count: 5
mean abs drift: 49.641ms
max abs drift: 200.952ms
mean ASR similarity: 0.99313
min ASR similarity: 0.98446
mean critical-term recall: 1.00000
min critical-term recall: 1.00000
mean F0 range: 5.782 semitones
selected join-quality failures: 0

listening pack:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_shard05_combined_gate_spoken_auto/listening_pack/index.html
```

Selected decisions:

```text
seg10520 -> split_emotext
seg10573 -> split_mix
seg10588 -> split_light_vector
seg10634 -> spoken_fig2_split_mix, fallback tier soft_fit_delta
seg10669 -> split_emotext
```

`seg10634` no longer fails ASR or term recall:

```text
candidate: spoken_fig2_split_mix
abs drift: 200.952ms
ASR similarity: 0.98980
critical-term recall: 1.00000
F0 range: 4.849 semitones
join quality: pass
fit tempo delta: 0.08609
```

Gate policy change:

- A candidate whose only rejection reason is `high_fit_delta` may now be used as
  a `soft_fit_delta` fallback when no strict candidate exists.
- `bad_join_quality` and `high_drift` remain hard exclusions from fallback
  selection.
- If no strict or soft-fit candidate exists, fallback ranking is semantic-first:
  ASR and critical-term recall are ranked before drift. This prevents a low-drift
  but semantically worse rescue from replacing a better split candidate.
- This lets the gate prefer a semantically correct candidate with 200ms drift
  over a 19ms-drift candidate that fails ASR, while still blocking multi-second
  drift regressions.

Tooling added for this path:

- `tools/lora_spoken_text.py` adds the initial spoken-text normalization pass.
- `tools/lora_canary_candidate_loop.py --spoken-text-policy figure-digits`
  writes `plan/spoken.features.jsonl` and records rewrite metadata for the
  requested sample set.
- `tools/lora_build_listening_pack.py` now exposes display text and actual TTS
  input separately in the HTML/manifest when they differ.
- `tools/lora_candidate_gate.py` now records `fallback_tier` in each selected
  decision.

Split29 selected aggregate:

```text
run:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_selected_combined_auto

selected count: 29
mean abs drift: 17.938ms
max abs drift: 200.952ms
mean ASR similarity: 0.98436
min ASR similarity: 0.94527
mean critical-term recall: 0.93983
min critical-term recall: 0.50000
mean F0 range: 5.596 semitones
selected join-quality failures: 0
no-selection samples: none

listening pack:
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_split29_selected_combined_auto/listening_pack/index.html
```

Aggregate risk queue for human listening:

```text
low ASR:
seg10518, seg10569, seg10576, seg10629, seg10678

low critical-term recall:
seg10518: missing 5, 6
seg10569: missing 日志条目, 日志
seg10576: missing 日志条目, 日志
```

Current conclusion:

- The split/rescue/spoken-text path is now temporally excellent on all 29
  split-planned canary84 samples: mean drift is under 20ms and max drift is
  about 201ms.
- Remaining defects are semantic / transcript scoring issues. They are not
  likely to be solved by blind retry.
- Next engineering focus should be deterministic pre-TTS planning for numeric
  terms and stricter human-listening override tracking for ASR near-homophones.

## Phase Gates

Phase 0: silver smoke

- Build a small silver manifest.
- Confirm audio paths, text, target durations, and split files are valid.
- Confirm the local environment reports missing trainer pieces honestly.
- No quality claims.

Phase 1: gold seed set

- Collect 2-5 hours of human-approved Chinese technical dubbing.
- Include long and short segments, code-switching terms, and natural pauses.
- Keep a fixed holdout set with manual listening labels.

Phase 2: local LoRA smoke on RTX 5080 16GB

- Batch size 1; gradient checkpointing for code lengths near 1500.
- Train only GPT LoRA weights for 20-100 smoke steps.
- Gate: train loss decreases, adapter can be loaded, inference works, and
  duration/ASR selection beats pure baseline.

Phase 3: cloud pilot

- Move to one A100/H100 once the gold set and preprocessing are validated.
- Train 1-2k steps.
- Gate by ASR similarity, critical-term recall, duration p95 drift, and human
  preference against the non-LoRA baseline.

## Known Blockers

- The preprocessing/training/inference path now works locally, but it still
  needs hardening, tests, and integration into the main service.
- Current installed package has no trainer modules.
- `peft` is not installed in the current ml-service container.
- The current database mostly contains synthetic TTS audio, not gold human
  target audio.

## First Quality Hypothesis

The first LoRA should not try to learn a new voice or vocoder. It should learn
domain-specific Chinese technical pacing and duration-conditioned semantic token
generation, especially for 15-45 second lecture segments where current
engineering controls either drift or swallow content.
