# LoRA Main-Flow Opt-In Rollout

Current golden baseline:

- `episode147_v3h_raw_stable_tailfix_human_approved`
- Review pack: `data/training/lora_runs/main_flow_shadow_episode147_final/human_review_pack_v3h_raw_stable_tailfix_exact`
- Quality baseline: `data/training/lora_runs/main_flow_shadow_episode147_final/quality_baseline.json`

Adapter training, naming, promotion, and rollback are governed by
[LoRA adapter training SOP](lora-adapter-training-sop.md). This rollout doc
must not redefine or relax that contract.

## Stage 1: Shadow Regression

Purpose: verify provenance and thresholds without changing audio output.

Required env:

```text
LORA_CANDIDATE_LOOP_ENABLED=true
LORA_CANDIDATE_EXECUTION_ENABLED=false
LORA_SPLIT_PLANNING_ENABLED=true
LORA_CANDIDATE_MIN_TARGET_SEC=18
LORA_CANDIDATE_MIN_TEXT_CHARS=180
LORA_CANDIDATE_DENSE_MIN_TARGET_SEC=9
LORA_CANDIDATE_DENSE_MIN_CHARS_PER_SEC=4.9
LORA_CANDIDATE_NEAR_LONG_MIN_TARGET_SEC=16
LORA_CANDIDATE_NEAR_LONG_MIN_TEXT_CHARS=70
LORA_CANDIDATE_SHORT_DENSE_MIN_TARGET_SEC=6
LORA_CANDIDATE_SHORT_DENSE_MAX_TARGET_SEC=9
LORA_CANDIDATE_SHORT_DENSE_MIN_CHARS_PER_SEC=5.5
LORA_CANDIDATE_SHORT_TRANSITION_MIN_TARGET_SEC=5
LORA_CANDIDATE_SHORT_TRANSITION_MAX_TARGET_SEC=10
LORA_CANDIDATE_SHORT_TRANSITION_MIN_CHARS_PER_SEC=5.0
LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_TARGET_SEC=6
LORA_CANDIDATE_SHORT_TERM_NUMBER_MAX_TARGET_SEC=9
LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_CHARS_PER_SEC=4.8
LORA_CANDIDATE_MAX_PER_SEGMENT=2
```

Audit an episode through the API:

```powershell
python tools/lora_main_flow_shadow_audit.py `
  --episode-id 147 `
  --api-base http://127.0.0.1:8080 `
  --output-dir data/training/lora_runs/main_flow_shadow_episode147_final/main_flow_shadow_audit_v3h `
  --require-completed `
  --data-root data
```

Expected result:

- `shadow_contract_pass=true`
- Long/high-risk segments carry `tts_lora_candidate`
- `candidate_execution_enabled=false`
- `audio_selection_mode=default_audio`
- Short segments do not change audio path

## Stage 2: Opt-In Fallback Smoke

Purpose: verify the production path can request candidate execution and safely fall back
when the ML candidate backend is not yet available.

Required env:

```text
LORA_CANDIDATE_LOOP_ENABLED=true
LORA_CANDIDATE_EXECUTION_ENABLED=true
LORA_SPLIT_PLANNING_ENABLED=true
LORA_CANDIDATE_MIN_TARGET_SEC=18
LORA_CANDIDATE_MIN_TEXT_CHARS=180
LORA_CANDIDATE_DENSE_MIN_TARGET_SEC=9
LORA_CANDIDATE_DENSE_MIN_CHARS_PER_SEC=4.9
LORA_CANDIDATE_NEAR_LONG_MIN_TARGET_SEC=16
LORA_CANDIDATE_NEAR_LONG_MIN_TEXT_CHARS=70
LORA_CANDIDATE_SHORT_DENSE_MIN_TARGET_SEC=6
LORA_CANDIDATE_SHORT_DENSE_MAX_TARGET_SEC=9
LORA_CANDIDATE_SHORT_DENSE_MIN_CHARS_PER_SEC=5.5
LORA_CANDIDATE_SHORT_TRANSITION_MIN_TARGET_SEC=5
LORA_CANDIDATE_SHORT_TRANSITION_MAX_TARGET_SEC=10
LORA_CANDIDATE_SHORT_TRANSITION_MIN_CHARS_PER_SEC=5.0
LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_TARGET_SEC=6
LORA_CANDIDATE_SHORT_TERM_NUMBER_MAX_TARGET_SEC=9
LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_CHARS_PER_SEC=4.8
LORA_CANDIDATE_MAX_PER_SEGMENT=2
ML_LORA_CANDIDATE_EXECUTION_ENABLED=false
INDEXTTS2_LORA_ADAPTER_PATH=
```

Expected result for eligible segments in this stage:

- `tts_lora_candidate.phase=main_flow_opt_in`
- `candidate_execution_enabled=true`
- `tts_lora_candidate.runtime.fallback_reason=ml_lora_candidate_execution_disabled`
- final selected family remains `default`
- final audio output remains the same default TTS path

This stage is intentionally a safety smoke. It proves Go can request candidate
audio without changing output when the ML-side double gate is still closed.

## Stage 3: Real Candidate Audio

Only after Stage 2 passes, enable the ML-side double gate:

```text
ML_TTS_BACKEND=indextts2
INDEXTTS2_INLINE=true
ML_ASR_BACKEND=faster_whisper
ML_LORA_CANDIDATE_EXECUTION_ENABLED=true
INDEXTTS2_LORA_ADAPTER_PATH=training/lora_runs/pilot-episode146-code1500-r4-step80/adapter_step80.pt
```

Expected behavior:

- ML service generates `default`, `split_light_vector`, and `split_mix`
  candidates for eligible long/high-risk segments.
- LoRA is loaded once but disabled by default; only candidate contexts enable
  the adapter, so default and short-segment paths are not polluted.
- `split_mix` may use internal `split_emotext` parts, but production selected
  families stay limited to `default`, `split_light_vector`, and `split_mix`.
- `candidate_result` is returned from `/tts/run` and persisted into
  `tts_lora_candidate.runtime`.
- If ML returns no `candidate_result` or candidate execution errors, Go retries
  once with candidate execution disabled and records the fallback reason.

Hard gates stay unchanged:

- high drift, low ASR, low term recall, bad join quality, and tail truncation
  cannot be overridden by normal listening override
- accepted low-ASR cases such as `seg10835` must remain explicit regression
  anchors, not generalized threshold relaxations
- `split_light_vector_10` remains comparison-only and must not appear in
  production candidate families

### Stage 3 Direct ML Smoke Evidence

Direct ML `/tts/run` smoke, using the step80 adapter and canary regression
anchor `seg10686`:

```text
data/training/lora_runs/main_flow_candidate_smoke/direct_ml_candidate_smoke_seg10686_v5.wav
data/training/lora_runs/main_flow_candidate_smoke/direct_ml_candidate_smoke_seg10686_v5_summary.json
```

Observed result:

- selected family: `split_mix`
- selected split strategy: `automatic`
- audio selection mode: `candidate_audio`
- fallback reason: empty
- selected duration: `31683 ms`
- target duration: `31700 ms`
- selected drift: `-16.958 ms`
- selected ASR similarity: `0.9931972789`
- selected join quality: pass
- default candidate rejected for `low_asr`, `high_drift`, `high_fit_delta`,
  and `low_term_recall`
- selected candidate fit delta: `0.0`
- selected part families: `split_light_vector`, `split_emotext`,
  `split_emotext`

This proves the ML-side Stage 3 path can generate and gate real candidate
audio. It is still a single-segment smoke, not an episode-level promotion.

Implementation detail locked by this smoke: ML candidate requests must use the
same generation surface as the offline canary loop: duration embedding enabled,
piecewise duration token rates, `max_text_tokens_per_segment=600`, and no
adapter-side second text chunking inside an already planned split part.

### Stage 3 Go Pipeline Smoke Evidence

Go API/worker `tts_duration` smoke, using the same `seg10686` target text and
the ML-side Stage 3 gate:

```text
data/training/lora_runs/main_flow_candidate_smoke/go_pipeline_smoke_seg10686_v2_summary.json
jobs/183/tts/vp0/segment-0001.agent_attempt00.wav
```

Observed result:

- segment status: `synthesized`
- selected family: `split_mix`
- selected split strategy: `automatic`
- audio selection mode: `candidate_audio`
- fallback reason: empty
- candidate count: `3`
- split parts: `3`
- target duration: `31700 ms`
- output duration: `31683 ms`
- drift: `-17 ms`

This proves the opt-in main-flow path can persist candidate runtime provenance
from Go into segment metadata while using candidate audio, not only direct ML
smoke calls.

### Stage 3 Go Pipeline Batch Smoke

Use the reproducible smoke tool for short/long mixed main-flow checks:

```powershell
python tools/lora_go_pipeline_smoke.py `
  --sample-ids seg10511,seg10518,seg10686 `
  --output data/training/lora_runs/main_flow_candidate_smoke/go_pipeline_smoke_short_long_v1_summary.json `
  --timeout-sec 2400 `
  --poll-sec 10
```

Observed result:

```text
data/training/lora_runs/main_flow_candidate_smoke/go_pipeline_smoke_short_long_v1_summary.json
jobs/185/tts/vp0/segment-0001.agent_attempt05.wav
jobs/185/tts/vp0/segment-0002.agent_attempt00.wav
jobs/185/tts/vp0/segment-0003.agent_attempt00.wav
```

- `seg10511`: below threshold, default short-segment path, output `7523 ms`
  vs target `7520 ms`, drift `+3 ms`
- `seg10518`: eligible long/high-risk path, selected `split_mix`, output
  `24085 ms` vs target `24100 ms`, drift `-15 ms`, split parts `2`
- `seg10686`: regression anchor, selected `split_mix`, output `31683 ms`
  vs target `31700 ms`, drift `-17 ms`, split parts `3`
- validation issues: `0`

This proves the production opt-in path can mix short default segments and long
candidate segments in one real `tts_duration` run. It is still a smoke-level
check; episode-level promotion requires a full shadow run against the golden
episode147 baseline and full-audio ASR comparison.

## Stage 4: Episode147 Opt-In Shadow

After Stage 3 batch smoke passes, clone the golden episode into a new opt-in
shadow episode and run real `tts_duration` for all cloned chapters.

The clone tool must remove historical candidate provenance before enqueueing.
It also records source lineage on every cloned segment:

```text
meta.shadow_source_job_id
meta.shadow_source_segment_id
```

Active run:

```text
source episode: 147
shadow episode: 176
run dir: data/training/lora_runs/main_flow_candidate_episode147_v24
clone report: data/training/lora_runs/main_flow_candidate_episode147_v24/clone_report.json
chapter jobs: 324, 325, 326, 327, 328, 329
segments: 180
```

Retired shadow iterations:

```text
v1 / episode 153 / jobs 186-191
  stopped after early candidate execution exposed old-code issues:
  several long segments hit candidate attempt limits or all-candidate fallback.

v2 / episode 154 / jobs 192-197
  stopped after source10700 reproduced candidate_attempt_limit_reached before
  the micro-clause split rescue was present in the container image.

v3 / episode 155 / jobs 198-203
  stopped after source10697 showed semantically correct split candidates
  rejected only for high drift / high fit delta. This led to the
  micro_clause_tight_pause rescue. v3 also ran before Go main-flow
  spoken-text parity rules were rebuilt into the worker.

v4 / episode 156 / jobs 204-209
  stopped after confirming source10694, source10695, and source10700 improved,
  but source10697 still reached candidate_attempt_limit_reached. Candidate
  details showed micro_clause_tight_pause + split_mix had ASR/term pass but
  fit_delta=0.0846, above the current production 0.05 gate. This led to
  concat-only rescue strategies that keep 0.18s planning targets but assemble
  with 0.04s or 0.0s pause. The old 0.08 marker is historical severity only,
  not the production acceptance line.

v5 / episode 157 / jobs 210-215
  stopped after discovering that concat-only rescue plans were incorrectly
  de-duplicated against micro_clause_tight_pause. The run had only synthesized
  one segment, so it is not a valid quality experiment. The fix makes split
  plan identity include planning_pause_sec and per-part target_sec, with a
  regression test proving micro_clause_tight_concat remains distinct from
  micro_clause_tight_pause.

v6 / episode 158 / jobs 216-221
  stopped at 12/180 synthesized segments after proving the important first
  chapter fixes in the real main-flow path. Notable passes included source10694
  spoken-text parity, source10695/source10696 split_mix, source10697
  micro_clause_tight_concat split_mix, source10700 micro_clause_six_part
  split_light_vector, and source10707 split_light_vector. It was retired because
  full-episode throughput was too slow before strong-gate candidate generation
  short-circuiting was present.

v7 / episode 159 / jobs 222-227
  stopped before any segment was persisted. It was launched while orphaned
  ML requests from the retired v6 run were still writing late candidate files
  under jobs/216. The worker now has a runTTSDuration cancel gate that checks
  cancel_requested after acquiring a TTS concurrency slot and before starting
  a segment ML call, reducing future cancel spillover.

v8 / episode 160 / jobs 228-233
  stopped at 4/180 synthesized segments. The first four candidate outputs were
  healthy, including source10697 via micro_clause_tight_concat split_mix, but
  source10704 exposed an outer-retry throughput bug: after the first full
  candidate search returned all_candidates_rejected, the same segment could
  spend a second full LoRA candidate budget on the retranslated attempt because
  the prior candidate count had not been updated inside the live agent loop.
  The worker now counts all_candidates_rejected locally before the next synth
  call, so the existing LORA_CANDIDATE_MAX_PER_SEGMENT cap is enforced within
  a single segment run.

v9 / episode 161 / jobs 234-239
  stopped at 5/180 synthesized segments after exposing a separate full-episode
  blocker in the non-candidate agent path: hard-cap escape branches were
  logged as accepted but returned an error, which could prevent a complete
  shadow episode from being assembled. The agent now returns marked output for
  hard-cap escapes and writes the final decision into segment meta under
  tts_agent, so high-drift segments remain visible to objective gates and
  human review without aborting the entire chapter.

v10 / episode 162 / jobs 240-245
  stopped at 12/180 synthesized segments. It proved the hard-cap marked-output
  path and risk_queue reporting work in the episode monitor, and it kept the
  known candidate anchors healthy. It was retired because source10698 exposed
  a dense mid-segment gap: the segment was below the original 18s/180-char
  eligibility thresholds, but had 73 Chinese non-whitespace chars in a 12.58s
  slot and the default retry loop ended with a +2364ms hard-cap drift. This
  led to candidate policy v3 and the dense_mid_segment trigger used by v11.

v11 / episode 163 / jobs 246-251
  stopped at 8/180 synthesized segments. The Go-side dense_mid_segment trigger
  correctly marked source10698 as candidate-eligible, but ML-side split
  planning still used only the legacy 18s/180-char predicate, so dense-mid
  requests generated default-only candidate attempts instead of split_light_vector
  / split_mix. v12 fixes ML policy parsing and forces split planning when
  eligibility.dense_eligible=true.

v12 / episode 164 / jobs 252-257
  stopped at 10/180 synthesized segments. It proved ML-side dense split works:
  source10692 changed from default fallback to split_mix automatic with +22ms
  drift. However source10698 measured 5.01 chars/sec, just below the 5.5 dense
  floor, so it stayed on the default path and landed at -480ms drift. That is
  within the 500ms hard gate but above the desired <300ms quality target, so
  v13 lowers the dense floor to 5.0 chars/sec.

v13 / episode 165 / jobs 258-263
  stopped at 14/180 synthesized segments. The 5.0 dense trigger did route
  source10698 into split planning, but two defects still let the segment fall
  back into the ordinary retry loop: (1) ASR similarity treated "slot 13" vs
  "slot thirteen" and a common 槽位/曹魏 Whisper homophone as a low-ASR hard
  failure; (2) the agent counted generic TTS retry attempts against the LoRA
  candidate budget, so max_per_segment=2 effectively allowed only one real
  candidate execution after an all_candidates_rejected result. The fallback
  retry finally accepted source10698 at +1493ms with hard_cap_exceeded=true,
  so v13 is retired.

v14 / episode 166 / jobs 264-269
  stopped at 26/180 synthesized segments. Chapter 1 was healthy, including
  source10698 via split_mix at -14ms drift, but source10720 exposed another
  ASR-surface false rejection: Whisper variants around 宕机/当机, 他/它, and
  em dash punctuation lowered candidate ASR enough to reject viable split
  candidates, then the ordinary retry path accepted a default attempt. Because
  this made the run no longer a clean candidate-path regression, v14 is retired
  and replaced by v15 after the ASR normalization and review-queue fixes below.

v15 / episode 167 / jobs 270-275
  stopped after source10737 exposed a short dense-text gap in the 12s dense
  trigger. The segment was 9.86s with 54 non-whitespace chars (5.48 chars/sec),
  stayed below the candidate thresholds, and the default retry loop accepted a
  hard-cap overrun at +1123ms. A direct ML dense smoke on the same text proved
  the candidate route is viable: split_light_vector automatic selected at
  +7.8ms drift, ASR similarity 1.0, and join_quality pass. v16 lowers the
  dense trigger target floor to 9s while leaving the hard gate thresholds
  unchanged.

v16 / episode 168 / jobs 276-281
  stopped after source10693 exposed a near-long dense gap. The segment was
  17.14s with 74/76 non-whitespace chars depending on source vs generated
  target wording, just below the 18s long threshold and below the 5.0 cps dense
  threshold. Because it did not enter candidate execution, the default retry
  loop accepted a +1287ms hard-cap overrun. A direct ML dense smoke on the same
  v16 target text selected split_mix automatic at -8.8ms drift, ASR similarity
  1.0, critical term recall 1.0, and join_quality pass. v17 adds a narrow
  near_long_dense trigger: target_sec >= 16 and text_chars >= 70.

v17 / episode 169 / jobs 282-287
  stopped before any segment was synthesized. The Go policy emitted
  eligibility.near_long_eligible, but ML-side split planning still only
  forced split candidates for dense_eligible. v18 fixes ML policy parsing and
  treats near_long_eligible like dense_eligible for split planning, with Python
  regression coverage.

v18 / episode 170 / jobs 288-293
  stopped at 18/180 synthesized segments after proving source10693 near-long
  split planning in the real path, but source10694 exposed a main-flow
  spoken-text parity regression: the Go TTS input converted "5号任期" into
  "第五任期" even though the human-approved anchor requires an audible "五号",
  and a direct dense candidate smoke could still read the same surface as
  "五日任期". v19 changes the deterministic spoken-text rewrite to
  "5号任期" -> "五 号任期" in both Go and offline LoRA tooling. Hard gates and
  candidate thresholds remain unchanged.

v19 / episode 171 / jobs 294-299
  stopped at 58/180 synthesized segments after source10751 exposed a
  non-candidate best-audio restore issue: the default retry loop kept the
  final attempt at -592ms even though an earlier saved attempt was about
  +302ms. The root cause was the interaction between asymmetric hard caps
  (0.3s over / 0.7s under) and best-restore skip rules. v20 rebuilds the
  worker with same-text best-audio restoration, transparent least-bad hard-cap
  restore metadata, hard-drift monitor coverage for default audio, and a
  shadow-only under-drift cap of 0.5s.

v20 / episode 172 / jobs 300-305
  stopped at 45/180 synthesized segments. At chapter 2, source10733 exposed a
  narrow dense-trigger gap:
  72 speakable chars in a 14.55s slot (4.95 cps) missed the previous 5.0 cps
  dense floor and stayed on the default path at -488ms drift. A direct ML dense
  smoke on the same spoken text selected split_light_vector automatic at
  +18ms drift, ASR similarity 1.0, and join_quality pass. Candidate policy v6
  lowers only the dense_mid_segment cps floor from 5.0 to 4.9; hard gates,
  ASR/term/join requirements, and the 10% tempo-rescue ban remain unchanged.

v21 / episode 173 / jobs 306-311
  stopped at 45/180 synthesized segments. It proved the v6 dense 4.9 fix for
  source10733, but exposed short default-path soft drift risks at source10715
  and source10738. Direct ML smokes showed both are better served by the
  split candidate path, leading to the narrow v8 short_dense_segment and
  short_transition_question triggers. Hard gates and tempo limits remain
  unchanged.

v22 / episode 174 / jobs 312-317
  stopped at 68/180 synthesized segments. Aggregate drift remained healthy,
  but source10746 exposed a split-planner risk: hard fallback splitting can
  cut inside critical terms such as 日志条目, forcing rescue strategies to
  recover ASR/term quality. v23 adds critical-term-aware hard split boundaries
  in both ML service and offline split tooling, with regression tests. This is
  planner-only and does not relax term recall or any other gate.

v23 / episode 175 / jobs 318-323
  stopped at 15/180 synthesized segments. Candidate outputs were healthy, but
  source10694 stayed on the default path because its 7.52s / about 5.05 cps
  shape was below the short_dense_segment 5.5 cps floor. It entered the review
  queue at -380ms. A direct ML smoke on the exact spoken text selected
  split_light_vector automatic at +12.75ms, ASR 1.0, critical term recall 1.0,
  join_quality pass, and fit_delta 0.0462, while the default candidate was
  3726ms with ASR 0.627. v24 adds only the narrow
  short_term_number_anchor trigger for number + 号任期 surfaces; it does not
  lower the general short_dense threshold.
```

Current candidate deltas:

```text
ASR gate normalization:
  Arabic 1-2 digit tokens now normalize to their spoken Chinese number form
  (13 -> 十三) for ASR similarity
  common Whisper homophones 曹魏/曹卫 normalize to 槽位
  common Whisper surface variants 当机 normalize to 宕机 and 他/她 normalize
  to 它 where this only affects similarity scoring
  en dash / em dash punctuation is ignored during ASR similarity scoring
  hard term recall still remains strict; this only prevents false low-ASR
  rejection for near-surface ASR variants

Candidate retry budget:
  agent attempt-limit now uses prior real LoRA candidate executions only
  ordinary retranslation attempts no longer consume max_per_segment
  skipped attempt-limit runtime preserves previous_candidate_runtime so
  candidate rejection diagnostics survive the final fallback attempt

source10698 direct dense smoke:
  selected split_light_vector automatic
  drift +11.5ms
  ASR similarity 0.99145
  join_quality pass
  report:
    data/training/lora_runs/main_flow_candidate_episode147_v14_smoke/
      seg10698_direct_smoke_dense_summary.json

ML candidate strategies:
  automatic_clause_duration_plan
  balanced_four_part
  micro_clause_six_part
  micro_clause_tight_pause
  micro_clause_tight_concat
  micro_clause_zero_concat
  semantic_two_part

Candidate generation short-circuit:
  default strong pass skips split planning
  split_light_vector strong pass skips split_mix for that plan
  rescue search still stops on the existing first hard gate pass
  no hard gate threshold is relaxed

Agent retry budget:
  all_candidates_rejected counts against the live segment candidate budget
  before the next synth attempt
  candidate_execution_not_available does not consume this budget
  short/default paths and hard gates remain unchanged

Dense-mid eligibility:
  evidence: v10 source10698 was below the 18s/180-char thresholds but carried
  73 Chinese non-whitespace chars in a 12.58s slot, repeatedly failed the
  default retry loop, and landed in risk_queue with +2364ms hard-cap drift
  additional evidence: v15 source10737 was below the 12s dense floor but had
  54 non-whitespace chars in a 9.86s slot; default accepted +1123ms hard-cap
  drift, while direct split_light_vector dense smoke selected +7.8ms / ASR 1.0
  / join_quality pass
  direct smoke report:
    data/training/lora_runs/main_flow_candidate_episode147_v15_smoke/
      seg10737_direct_smoke_dense_summary.json
  additional evidence: v20 source10733 measured 72 speakable chars in a
  14.55s slot (4.95 cps); default accepted -488ms, while direct
  split_light_vector dense smoke selected +18ms / ASR 1.0 / join_quality pass
  direct smoke report:
    data/training/lora_runs/main_flow_candidate_episode147_v20/direct_smoke/
      source10733_dense_smoke.json
  candidate policy v6 uses an explicit dense_mid_segment trigger:
  target_sec >= 9 and chars_per_sec >= 4.9
  the original 18s/180-char thresholds remain unchanged and the trigger is
  visible in meta.eligibility.reasons
  ML split planning now also treats eligibility.dense_eligible=true as a split
  trigger, so dense-mid requests generate split_light_vector and split_mix
  candidates instead of default-only attempts

Near-long eligibility:
  evidence: v16 source10693 was a 17.14s near-long segment with 74/76 target
  chars and cps below the dense_mid_segment floor, so it did not request LoRA
  candidates and default retry shipped a +1287ms hard-cap overrun
  direct smoke report:
    data/training/lora_runs/main_flow_candidate_episode147_v16_smoke/
      seg10693_direct_smoke_dense_summary.json
  candidate policy v5 adds an explicit near_long_dense trigger:
  target_sec >= 16 and text_chars >= 70
  episode147 evidence scope is narrow: this predicate hits 9 source segments
  in the 16-18s band, including the failed source10693
  hard gates are unchanged: drift <= 500ms, ASR >= 0.97, term recall >= 0.8,
  join_quality pass, and fit_delta <= 0.05
  ML split planning now also treats eligibility.near_long_eligible=true as a
  split trigger, matching the Go policy metadata and preventing default-only
  near-long candidate attempts

Short transition question eligibility:
  evidence: v20 and v21 both reproduced source10738 as a short default-path
  soft risk: target 6.5s, default output 6.188s, drift -312ms. This is below
  the 500ms hard gate but above the 300ms review line. The text is a unique
  question-to-continuation bridge in episode147: "还有什么疑问吗？好的，我们就继续..."
  direct smoke report:
    data/training/lora_runs/main_flow_candidate_episode147_v21/direct_smoke/
      source10738_short_dense_smoke.json
  direct smoke result:
    split_light_vector automatic +2ms / ASR 1.0 / join_quality pass
    split_mix automatic +21ms / ASR 1.0 / join_quality pass
    both candidates carry `flat_f0` as a soft review risk
  candidate policy v8 includes a narrow short_transition_question trigger:
  target_sec between 5 and 10, chars_per_sec >= 5.0, text contains a question
  cue such as 疑问/问题 before the question mark and a continuation cue such as
  好的 + 继续/接着/进入 after it
  hard gates are unchanged; this trigger only decides whether split candidates
  are generated

Short dense eligibility:
  evidence: v21 source10715 reproduced a second short default-path soft risk:
  target 7.1s, final default attempt 6.768s, drift -332ms after 3 attempts.
  The text is a short dense technical clause with an English term and a spoken
  tail: "Append Entries RPC，对吧？然后呢，哦，"
  direct smoke report:
    data/training/lora_runs/main_flow_candidate_episode147_v21/direct_smoke/
      source10715_short_dense_smoke.json
  direct smoke result:
    split_light_vector automatic +3.7ms / ASR 0.9863 / latin recall 1.0 /
    join_quality pass
  episode147 scope at threshold 6-9s and chars_per_sec >= 5.5 is 8 segments,
  including source10715 and source10738; the wider 5.0 cps trigger would hit
  14-17 short segments and is too broad for this evidence
  candidate policy v8 adds short_dense_segment:
  target_sec between 6 and 9 and chars_per_sec >= 5.5
  hard gates are unchanged; candidate output must still pass drift, ASR, term,
  join_quality, and fit_delta gates

Short term-number eligibility:
  evidence: v23 source10694 was a 7.52s segment containing the preserved
  "五 号任期" spoken surface. The v8 default path accepted -380ms, above the
  300ms review line. Direct ML smoke on the same spoken text selected
  split_light_vector automatic at +12.75ms / ASR 1.0 / critical term recall
  1.0 / join_quality pass / fit_delta 0.0462. The default candidate for the
  same text was rejected at -3793ms and ASR 0.627.
  direct smoke report:
    data/training/lora_runs/main_flow_candidate_episode147_v23/direct_smoke/
      source10694_dense_smoke_summary.json
  candidate policy v9 adds short_term_number_anchor:
  target_sec between 6 and 9, chars_per_sec >= 4.8, and a digit/fullwidth
  digit/Chinese number immediately before "号任期"
  this is deliberately narrower than lowering short_dense_segment from 5.5 cps
  to 5.0 cps; hard gates and the 5% production fit limit remain unchanged

Hard-cap provenance:
  hard-cap escape branches return marked output instead of aborting the
  segment
  segment meta records tts_agent.final_reason, final_drift_ms,
  hard_cap_exceeded, hard_cap_direction, and restored_from_best
  objective gates and review packs must treat hard_cap_exceeded=true as a
  high-priority risk, not as a silent pass
  v20 adds transparent least-bad restore metadata for the case where the
  current final attempt and the saved best attempt both violate directional
  caps: restored_best_hard_cap_exceeded and
  restored_best_hard_cap_direction show that the selected best audio is still
  a risk item, not a clean pass

Automation:
  background monitor writes episode176_monitor_snapshot.json and
  episode176_monitor_history.jsonl
  background autofinalize writes auto_finalize_status.json and, after episode
  completion, runs final mix ASR plus the focused human review pack
  monitor pending-row eligibility mirrors the v9 dense/near-long/short-dense/
  short-transition/short-term-number triggers for review visibility; hard validation still uses
  actual synthesized plan/runtime plus drift, fallback, join, ASR, and term
  gates

Review risk queue:
  monitor now reports review_drift_exceeds_soft_limit for synthesized segments
  above the 300ms soft listening threshold even when they remain below the
  500ms hard gate
  finalize includes monitor risk_queue segment ids in the focused review pack,
  so default-path soft drifts and candidate fallbacks are not silently skipped

Rescue ordering follow-up:
  v15 observed source10697 again passing only after
  micro_clause_tight_concat, while micro_clause_tight_pause consumed a full
  unsuccessful rescue round first
  v16 prioritizes concat rescues before pause-only rescues in the rebuilt ML
  service while leaving hard gate thresholds unchanged

Main-flow spoken-text parity:
  Go tests lock pronunciation-oriented rewrites for the known user-reported
  episode147 anchors, including term-number wording, decimal section names,
  list punctuation before node wording, figure-dash cleanup, previous-log
  phrasing, and the chang2/zhang3 ambiguity anchor.
  The term-number anchor now preserves the "号" semantics instead of turning it
  into an ordinal: "5号任期" is sent to TTS as "五 号任期", with subtitles kept
  unchanged. The same rule is mirrored in tools/lora_spoken_text.py so offline
  canary packs and main-flow shadow runs use the same TTS input.
```

Monitor progress:

```powershell
python tools/lora_episode_candidate_monitor.py `
  --episode-id 176 `
  --source-episode-id 147 `
  --backfill-lineage `
  --output data/training/lora_runs/main_flow_candidate_episode147_v24/episode176_monitor_snapshot.json
```

The monitor validates, while the run is in progress:

- eligible synthesized segments use `candidate_audio`
- candidate fallback reasons are surfaced
- candidate drift stays within `500 ms`
- soft-review drift above `300 ms` is listed in `risk_queue`
- focus source segments `10693/10694/10695/10696/10697/10698/10700/10701/10702/10712/10715/10720/10725/10728/10733/10738/10746/10751/10786/10794/10800/10801/10808/10833/10835/10866`
  remain traceable through `shadow_source_segment_id`

When all chapter jobs complete, run a second lineage backfill to guard against
in-flight meta races, then build final ASR/objective reports and a focused
human review pack before comparing against the golden baseline.

Finalize after completion:

```powershell
python tools/lora_episode_candidate_finalize.py `
  --source-episode-id 147 `
  --shadow-episode-id 176 `
  --run-dir data/training/lora_runs/main_flow_candidate_episode147_v24
```

The finalize command is safe to run early. If the episode is incomplete, it
only writes:

```text
data/training/lora_runs/main_flow_candidate_episode147_v24/finalize_summary.json
data/training/lora_runs/main_flow_candidate_episode147_v24/episode_candidate_monitor_final.json
```

Once complete, it additionally produces:

```text
data/training/lora_runs/main_flow_candidate_episode147_v24/final_mix_asr/final_mix_asr_report.json
data/training/lora_runs/main_flow_candidate_episode147_v24/human_review_pack/index.html
```
