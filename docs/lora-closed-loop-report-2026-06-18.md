# HoloDub LoRA Closed-Loop Report - episode 146 full118

- **Generated**: 2026-06-19
- **Scope**: IndexTTS2 LoRA candidate loop, split planning, spoken-text planning, listening overrides, main-flow shadow contract
- **Episode set**: `data/training/lora_corpus/smoke-episode146/all.jsonl`
- **Current golden offline baseline**: `episode146_full118_combined_v1`
- **Previous canary baseline**: `canary84_full84_hybrid_user_issues_v1`
- **Reference exemplar for report shape**: `docs/opt-405/bench-baseline-2026-05-11/report.md`

## Executive Decision

The LoRA route remains viable, but the problem is no longer just temporal drift.
The current system needs four separate gates before production opt-in:

1. temporal fit: duration drift and fit tempo delta;
2. semantic integrity: ASR similarity, term recall, and tail completeness;
3. acoustic naturalness: F0 range, silence ratio, and join quality;
4. topic and structure preservation: pre-TTS rewrite must not reorder,
   over-compress, or alter the local explanation.

`episode146_full118_combined_v1` is the current episode-level offline baseline.
It combines the canary84 selected set with split-first and duration-rate rescue
paths for the 34 feature-overlength rows that were missing from the first
episode shadow pass:

```text
selected count: 118
mean abs drift: 33.54ms
max abs drift: 298.73ms
mean ASR similarity: 0.9924
min ASR similarity: 0.9625
mean F0 range: 5.80 semitones
min critical-term recall: 0.875
join-quality failures: 0
listening overrides: 1
risk queue entries: 40
```

Do not treat this as final production baseline yet. It is the strongest
objective baseline so far, but it still requires human listening review of the
risk queue before the main TTS flow is allowed to replace production audio with
candidate-selected audio.

## Episode Full118 Combined Baseline

On 2026-06-19 the work moved from canary-only evaluation into an episode-level
offline baseline. The first shadow pass showed 34 accepted rows missing from
the canary84 feature cache because they exceeded the `max_code_tokens=1500`
limit. These were recovered through split-first generation and targeted
duration-rate rescue instead of raising the feature limit globally.

Current episode 146 selected coverage:

```text
corpus accepted rows: 118
selected rows: 118
no-selection rows: 0
manifest-level rejected rows: 62
coverage of corpus accepted rows: 100.00%
```

Episode full118 artifacts:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/combined_summary.json
data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/candidate_gate_report.json
data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/risk_queue.json
data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/human_review_report.json
data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/listening_pack/index.html
data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/risk_listening_pack/index.html
```

## Main Flow Shadow Refactor

Main-flow integration has also started, but it is deliberately opt-in,
versioned, and shadow-only:

1. `LORA_CANDIDATE_LOOP_ENABLED=false` remains the default.
2. `LORA_SPLIT_PLANNING_ENABLED=false` remains the default.
3. Short segments keep the existing TTS path unchanged.
4. Eligible long/high-risk segments now receive a versioned `lora_candidate`
   shadow plan in `dubbing_meta`.
5. The segment meta now records `tts_lora_candidate` provenance with candidate
   families, generation defaults, split policy, gate policy, hard-failure
   reasons, fallback policy, and selected family.
6. The ML TTS adapter emits a diagnostic string with policy version and audio
   selection mode; it does not execute extra candidates or apply tempo rescue.
7. The selected production audio remains `default_audio` until the ASR, term,
   prosody, and join-quality gates are available as a production service.

This contract is now codified in `internal/pipeline/lora_candidate_strategy.go`
rather than only described in experiment notes. The strategy emits
`schema_version=2`, the `episode146_full145_episode_assembly_v7` quality
baseline, the risk-queue ordering, and fixed regression anchors for `seg10569`,
`seg10686`, and `seg10518`. The production family list deliberately excludes
`split_light_vector_10`; that 10% tempo-rescue path is comparison-only and must
not enter current production selected output. Any future experiment that studies
it must be a separately named research policy with its own acceptance gate. The
ML adapter diagnostics also echo schema, baseline, split state, join-quality
requirement, drift gate, and tempo gate so a shadow episode can be audited
without opening every segment row by hand.

The first production-facing milestone is therefore not "use LoRA candidates in
the final audio". It is "run a real episode with the flags enabled and prove
that every eligible segment records the correct shadow provenance while audio
remains byte-path compatible with the default TTS execution".

2026-06-19 main-flow shadow status:

- `cmd/lora-shadow-clone` cloned episode 146 into episode 147 with all chapters
  re-enqueued through the normal `tts_duration -> merge -> episode merge`
  lifecycle.
- `docker-compose.lora-shadow.yml` enables only the shadow flags:
  `LORA_CANDIDATE_LOOP_ENABLED=true` and `LORA_SPLIT_PLANNING_ENABLED=true`.
  Candidate execution still remains disabled and selected audio remains
  `default_audio`.
- `tools/lora_main_flow_shadow_audit.py` is the objective contract checker for
  this run. It verifies episode completion, final media existence, final video
  drift, missing audio, per-segment duration drift distribution, and every
  `tts_lora_candidate` provenance field.
- The Python TTS adapter now parses `lora_candidate` into a structured
  `_LoraCandidatePolicy`. `should_execute_candidates` is true only when the
  policy is enabled, `candidate_execution_enabled=true`, and
  `audio_selection_mode=candidate_audio`. The current shadow run therefore
  cannot accidentally replace audio; the parser exists to make the later
  opt-in candidate executor attach to one stable interface.
- Episode 147 has now completed the real main-flow shadow run:
  180/180 segments synthesized, 107 long/high-risk eligible rows recorded
  LoRA/split provenance, 6/6 chapters completed, missing audio `0`, and
  shadow contract issues `0`.
- The first final audit failed for the right reason: final video drift was
  about `+954ms`. The root cause was not the shadow LoRA contract but the
  merge path trusting stream-copy chapter video durations after keyframe-aligned
  slicing. The fix adds explicit duration locks in chapter muxing and episode
  concat (`MuxVideoWithDuration`, `ClampMuxedVideoDuration`,
  `chapterMergeTargetDurationMs`, and `episodeMergeTargetDurationMs`).
- After applying the duration lock to the generated episode 147 final, the
  formal audit passes with final video drift `52ms` against a `100ms` gate.
  Review pack:
  `data/training/lora_runs/main_flow_shadow_episode147_final/listening_pack/index.html`.
- `tools/lora_main_flow_review_pack.py` now builds a focused human review pack
  from the live API data. For episode 147 it generated 23 final-mix context
  clips covering explicit risk segments, low segment-judge scores, retry
  verdicts, and chapter-judge `top_3_weakest_segments`.

Current episode-level objective audit:

- Run: `episode146_full145_episode_assembly_v7`
- Audit report:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full145_episode_assembly_v7/objective_audit/objective_audit.md`
- Objective review pack:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full145_episode_assembly_v7/objective_audit/objective_review.html`
- Hard objective gates: pass. Final audio drift `0ms`, final video drift
  `43ms`, LoRA mean drift `29.64ms`, LoRA max drift `298.73ms`, mean ASR
  `0.9917`, min ASR `0.9625`, join-quality failures `0`.
- Review gates: still open. The audit requires listening review for 35
  existing-TTS fallback rows, 14 rows with >200ms clipping, and 1 row with
  tempo fit above 8%.
- The objective review pack now includes 50 final-mix context clips under
  `objective_audit/context_audio/`, so the operator can compare each isolated
  segment against the same audio inside the completed episode timeline.
- Shadow projection: 107 duration-eligible segments would receive the
  opt-in shadow candidate policy; projected candidate execution remains
  disabled and projected audio selection remains `default_audio`.

## Leaderboard

| Rank | Run | Count | Mean Drift | Max Drift | Mean ASR | Min ASR | Mean F0 | Join Fail | Status | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| 1 | `episode146_full118_combined_v1` | 118 | 33.54ms | 298.73ms | 0.9924 | 0.9625 | 5.80 | 0 | episode offline baseline | Covers all accepted rows; risk queue has 40 entries for focused listening. |
| 2 | `canary84_full84_hybrid_user_issues_v1` | 84 | 38.67ms | 298.73ms | 0.9937 | 0.9625 | 5.82 | 0 | canary baseline | Fixes several user-reported pronunciation/tail cases; still has open listening risks. |
| 3 | `canary84_full84_regen_v2_user_review` | 84 | 39.16ms | 298.73ms | 0.9931 | 0.9625 | 5.83 | 0 | previous golden | Stable reproducibility target; includes known manual listening issues. |
| 4 | `canary84_user_issues_technical_v2_loop_v1` | 7 | 11.68ms | 21.13ms | 0.9723 | 0.8770 | 5.16 | 0 selected | targeted rescue | Useful for specific user issues, but not safe as a blanket replacement. |

## User Listening Issue Regression

| Sample | User issue | Current selected path | Current status | Next action |
|---|---|---|---|---|
| `seg10509` | Word boundary in "previous log" sounded unnatural. | `original_selected` | open | Try a narrower spoken-text rewrite such as "previous log entry"; do not use the current technical-v2 result because it changed ASR surface form. |
| `seg10511` | "5 hao term" sounded like "5 ri term". | `technical_v2 / split_light_vector` | fixed candidate | Keep as regression anchor; verify in every compact listening pack. |
| `seg10518` | Missing pause between the number list and the next sentence. | `original_selected / split29 light_vector` | partially open | Build a pause-preserving candidate instead of relying on blind retry; technical-v2 is rejected due low ASR and tail truncation. |
| `seg10544` | Section number `5.4.1` missed a dot. | `technical_v2 / light_vector` | likely improved, needs listening confirmation | Add an alternate section-number form if user still hears dot loss. |
| `seg10602` | Polyphone "chang" was read as "zhang". | `original_selected` | open | Use pronunciation hinting or a more local rewrite; current technical-v2 hurts nearby `RPC/RAP` recognition. |
| `seg10633` | Sentence tail was swallowed. | `technical_v2 / split_mix` | fixed candidate | Tail completeness guard must remain a hard constraint. |
| `seg10634` | Dash after the second figure reference affected pronunciation. | `technical_v2 / split_light_vector` | fixed candidate | Keep figure/dash spoken-text planning and join-quality guard. |
| `seg10569` | Human preferred shard01 selected over 10% tempo rescue. | `shard01_selected` via override | fixed by human override | Keep `split_light_vector_10` only as a comparison row, not selected. |

## What Changed

The latest loop added three improvements that should stay in the offline
workflow:

1. `technical-v2` spoken-text planning for targeted numeric, figure, section,
   dash, and polyphone cases.
2. Tail completeness scoring in `lora_candidate_gate.py`, with
   `tail_truncation` treated as a hard reject for normal overrides.
3. Manual listening issues are now first-class risk-queue items, so the gate can
   surface problems that ASR and term recall fail to capture.

The important lesson is that deterministic planning is helping more than retry
volume. The best fixes came from changing the TTS input form before synthesis,
then letting the existing candidate gate choose between original and planned
variants.

## Topic And Structure Drift

This is now a separate quality dimension. The earlier documentation mixed
topic quality, rewrite quality, TTS quality, and experiment chronology, which
made the plan feel like it was written in the order we discovered things.
Going forward, topic and structure must be measured and reported like the
chapter benchmark reports boundary/title/topic:

| Dimension | Question | Failure mode | Gate |
|---|---|---|---|
| Topic continuity | Does the candidate stay on the same local concept? | A rewrite moves from Raft log replication into a neighboring explanation. | Reject or force review. |
| Structure preservation | Does it keep the same explanation order? | The rewritten text compresses or reorders "cause -> example -> conclusion". | Reject for production; allow only in review packs. |
| Term anchoring | Are technical terms preserved in the same role? | `RPC`, `Raft`, log entry, or figure references are changed or hidden. | Hard reject if term role changes. |
| Pause intent | Are sentence and clause boundaries preserved? | Number list and next sentence merge acoustically. | Force split or pause-planned candidate. |

The practical implementation should happen before TTS:

1. assign each segment a compact `topic_context` from neighboring segments;
2. when spoken text differs from display text, emit a rewrite rationale and
   preserve a one-to-one mapping of critical phrases;
3. run a lightweight topic/structure judge against source text and spoken text;
4. write scores into the candidate report next to ASR, drift, F0, and join
   quality;
5. include worst topic/structure cases in the listening pack, just like worst
   boundary cases in the chapter benchmark.

This prevents "sounds good but says the wrong thing" and also prevents the
documentation itself from drifting into experiment diary form.

## Current Risk Queue

`episode146_full118_combined_v1/risk_queue.json` contains 40 entries. The
compact risk listening pack expands these into selected/runner-up/teacher rows
for review.

Highest-priority rows:

1. `seg10569`: listening override, fallback tier, low F0, Latin surface risk,
   regression anchor.
2. `seg10658`: only selected row with fit tempo delta above 8%; keep marked as
   a listening decision point.
3. `seg10558`: fallback tier and ASR similarity below 0.985.
4. `seg10644`: fallback tier and ASR similarity below 0.985.
5. `seg10646`: fallback tier, ASR similarity below 0.985, Latin surface risk
   around `Raft`.
6. `seg10587`, `seg10616`, `seg10617`, `seg10685`: low F0 rows.
7. `seg10508`, `seg10544`, `seg10602`, `seg10621`, `seg10628`, `seg10633`:
   ASR similarity below 0.985 or user-reported regression anchors.

Manual issue anchors from the previous canary review are still valid listening
targets, but the full118 risk queue is now the source of priority order.

## Technical-v3 Focused Experiment

Run:

```text
canary84_user_issues_technical_v3_loop_v1
```

Purpose: test narrower spoken-text rewrites for the three still-unstable rows
without carrying over the broader `technical-v2` failures.

Rules added:

- `previous log index/term field` -> `previous log-entry's index/term field`
  spoken form;
- number-list sentence boundary -> explicit "then/afterward" spoken bridge;
- duration polyphone case -> "operation takes longer" wording, without editing
  nearby `RPC/RAP` surface terms.

Result:

```text
selected count: 3
mean abs drift: 17.96ms
max abs drift: 24.22ms
mean ASR similarity: 0.9659
min ASR similarity: 0.9158
join-quality failures: 0 selected
```

Per-sample outcome:

| Sample | technical-v3 outcome | Decision |
|---|---|---|
| `seg10509` | Still heard as a `ri-zhi` near-form in ASR and has low F0 (`flat_f0`). | Reject for auto-selection. |
| `seg10518` | Still low ASR and tail truncation. | Reject; pause issue needs a different mechanism. |
| `seg10602` | ASR improves from current hybrid `0.9745` to `0.9819`; wording avoids the `chang/zhang` risk. | Keep as listening candidate only; automatic gate still prefers current hybrid because drift and F0 are better. |

The full combine run:

```text
canary84_full84_hybrid_user_issues_v2_v3candidate
```

did not replace any selected row. Full canary84 metrics stayed identical to the
current hybrid run, which is the correct conservative outcome.

Focused listening pack:

```text
data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_user_issues_technical_v3_compare/listening_pack/index.html
```

## Reproduction Commands

Targeted user-issue regeneration:

```powershell
$base = 'data\training\lora_runs\pilot-episode146-code1500-r4-step80'
python tools\lora_canary_candidate_loop.py `
  --features "$base\canary_84.features.jsonl" `
  --run-dir "$base\canary84_user_issues_technical_v2_loop_v1" `
  --adapter "$base\adapter_step80.pt" `
  --voice-prompt-relpath voices/test_ref.wav `
  --sample-ids seg10509,seg10511,seg10518,seg10544,seg10602,seg10633,seg10634 `
  --candidate-families flat,light_vector,emotext_a05,split_light_vector,split_mix `
  --spoken-text-policy technical-v2 `
  --min-tail-recall 0.75 `
  --resume
```

Episode full118 combine:

```powershell
$base = 'data\training\lora_runs\pilot-episode146-code1500-r4-step80'
python tools\lora_canary_combine_gate.py `
  --features data\training\lora_corpus\smoke-episode146\all.jsonl `
  --run-dir "$base\episode146_full118_combined_v1" `
  --candidate canary84="$base\canary84_full84_hybrid_user_issues_v1\candidate_gate_report.json" `
  --candidate overlength_first="$base\episode146_overlength34_split_first_v1\candidate_gate_report_regated.json" `
  --candidate overlength_rate23="$base\episode146_overlength17_rate23_rescue_v1\candidate_gate_report_regated.json" `
  --candidate overlength_rate21p5="$base\episode146_overlength8_rate21p5_rescue_v1\candidate_gate_report_regated.json" `
  --candidate overlength_fixedsplit="$base\episode146_overlength4_fixedsplit_rate21p5_v1\candidate_gate_report_regated.json" `
  --candidate seg10646_raft_spacing="$base\episode146_seg10646_raft_spacing_rate21p5_v1\candidate_gate_report_regated.json" `
  --candidate seg10533_allfamilies="$base\episode146_seg10533_allfamilies_rate23_v2\candidate_gate_report.json" `
  --title "HoloDub Episode146 Full118 Combined v1"
```

## Artifacts

- Episode full118 summary:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/combined_summary.json`
- Episode full118 report:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/candidate_gate_report.json`
- Episode full118 listening pack:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/listening_pack/index.html`
- Episode full118 risk listening pack:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full118_combined_v1/risk_listening_pack/index.html`
- User issue comparison pack:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_user_issues_hybrid_compare/listening_pack/index.html`
- Episode v7 objective review pack:
  `data/training/lora_runs/pilot-episode146-code1500-r4-step80/episode146_full145_episode_assembly_v7/objective_audit/objective_review.html`
- Main-flow shadow audit tool:
  `tools/lora_main_flow_shadow_audit.py`
- Main-flow shadow review pack tool:
  `tools/lora_main_flow_review_pack.py`
- Main-flow shadow human acceptance recording tool:
  `tools/lora_record_human_acceptance.py`
- Main-flow shadow goal completion audit tool:
  `tools/lora_goal_completion_audit.py`
- Main-flow shadow episode 147 final audit:
  `data/training/lora_runs/main_flow_shadow_episode147_final/main_flow_shadow_audit.md`
- Main-flow shadow episode 147 goal completion audit:
  `data/training/lora_runs/main_flow_shadow_episode147_final/goal_completion_audit.json`
- Main-flow shadow episode 147 listening pack:
  `data/training/lora_runs/main_flow_shadow_episode147_final/listening_pack/index.html`
- Main-flow shadow episode 147 focused clip manifest:
  `data/training/lora_runs/main_flow_shadow_episode147_final/listening_pack/review_manifest.json`
- Main-flow shadow episode 147 human acceptance checklist:
  `data/training/lora_runs/main_flow_shadow_episode147_final/listening_pack/human_acceptance_checklist.md`
- Main-flow shadow episode 147 human review notes template:
  `data/training/lora_runs/main_flow_shadow_episode147_final/listening_pack/human_review_notes.template.json`

## Next Engineering Steps

1. Human-review the 50-entry v7 objective review queue in final-mix context,
   prioritizing fallback, tempo-over-8-percent, clipping, low-F0, low-ASR, and
   Latin-surface-risk rows.
2. Human-review the episode 147 main-flow shadow pack. Treat it as validation
   of full-pipeline provenance, merge stability, and duration locking, not as
   approval of production candidate-audio replacement.
3. Add topic/structure judge output to offline reports before any candidate is
   allowed to replace production audio.
4. Keep `seg10518` as the pause-boundary regression anchor; the forced split
   boundary mechanism should be tested before it becomes a generic rule.
5. Promote actual LoRA candidate execution only after ASR, term, prosody, and
   join-quality gates are available as a production service and short-row
   behavior remains unchanged under opt-in flags.
