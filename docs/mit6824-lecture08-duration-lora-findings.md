# MIT 6.824 Lecture 8 Duration LoRA Findings

This note records the current Lecture 8 duration/prosody investigation so the
project does not drift back into accepting tempo-fit artifacts as progress.

## Current Verdict

Lecture 8 is blocked again. The first audible full-episode review exposed
dragged speech, which means the previous "low final drift" evidence was not
sufficient. The project must not keep running the full episode with tempo-fit
as the main repair mechanism.

The next required step is to reproduce the Lecture-7-style LoRA loop on the
Lecture 8 high-fit holdout: gold-standard source filtering, generated-audio
holdout selection, 5% fit as a hard gate, ASR/text-completeness gates, split
join quality, and a compact listening pack before any new full-episode shadow.

The user-reported symptom is valid: selected audio can have low final drift
while still sounding dragged because duration fit corrected too much of the raw
model error. Final drift alone is not an acceptance metric.

Previous optimistic holdout repair:

```text
data/training/lora_runs/mit6824-lec08-v8-feasible-cps56-durationpos-r4-step160/
  lecture08_highfit_12of12_tailfade_textrepair_v1
```

- Selected count: `12 / 12`
- Mean absolute drift: `66.48 ms`
- Max absolute drift: `278.41 ms`
- Mean ASR similarity: `0.9915`
- Min ASR similarity: `0.9756`
- Join quality failures: `0`
- Max selected fit tempo delta: `0.0422`
- Critical term recall: `1.0`
- Latin surface recall: `1.0`
- Listening pack:
  `data/training/lora_runs/mit6824-lec08-v8-feasible-cps56-durationpos-r4-step160/lecture08_highfit_12of12_tailfade_textrepair_v1/listening_pack/index.html`

This result is not evidence for widening fit. It is evidence that the correct
path is Lecture-7-style data selection plus pre-TTS speech-rate feasibility,
text compression, split planning, and boundary repair. Because the later
episode review still sounded dragged, this artifact is now treated as a
comparison baseline, not an acceptance gate.

Current post-review evidence:

- v9 training completed but did not improve the fixed high-fit holdout.
- v9 primary loop selected `2 / 12`.
- v9 primary + micro6 rescue selected `4 / 12`.
- v9 plus text repair projected only `5 / 12`.
- v10 repeated the clean v7 goldmatch recipe without holdout leakage. The
  training pack passed audit, but generated-audio holdout regressed:
  primary selected `1 / 12`, micro6 selected `0 / 12`.
- The strict non-holdout rebuild from completed episode180 is not new training
  evidence. It produced `90` clean rows (`0.6581 h`), but all `90 / 90` source
  ids already exist in the v7 goldmatch reference corpus, and the split audit
  still fails because val/test rows are insufficient.
- The live episode183 partial run has started producing new source ids, but
  the current partial candidate pack is still only `17` runtime-clean no-ASR
  rows (`0.1101 h`). It is not training-ready until final-mix ASR, fit audit,
  human review, and enough volume pass.
- The remaining failures are split-boundary repair or text-compression/training
  evidence, not permission to relax fit.
- The shadow profile must use the best current Lecture-8 goldmatch adapter as a
  controlled baseline until a newer adapter beats it on generated audio.

## Episode 182 Stop Decision

The first full Lecture 8 v8 shadow run was cancelled and must not be treated as
an acceptance candidate:

```text
episode_id=182
jobs=353..357
run=mit6824-lec08-zookeeper-prod-v3-fit005-tailrepair-v8adapter-shadow
evidence=data/validation/mit6824_acceptance/lec-08/episode182_failure_evidence.json
```

The adapter under test was correct (`adapter_step160.pt` from v8), but the main
flow did not yet enforce the same gate semantics as the offline holdout:

- `seg16090`: a 7.34 s / 35 character short dense segment stayed on the default
  retry path, ended with `no_more_attempts`, and shipped a `380 ms` overrun.
- `seg16096`: candidate generation stopped with
  `needs_text_compression_speech_rate`, but the outer loop counted it against
  candidate budget, skipped later candidate execution, and accepted default
  `clip_overflow` with `4101 ms` overrun.
- `seg16097`: the same text-compression signal was ignored and default
  `clip_overflow` shipped with `4230 ms` overrun.

Main-flow fixes now locked by tests:

- Short dense retry escalation: after default non-convergence, 5-12 s segments
  with enough text density can enter LoRA split/candidate planning instead of
  exhausting default retry.
- Text-compression planning signals do not consume candidate attempt budget.
- If candidate runtime still requires text compression and the candidate/default
  audio would exceed the hard drift cap, the segment fails loudly instead of
  entering the final mix.
- `SEGMENT_AGENT_MAX_FIT_DELTA=0.05` is now a main-flow gate, not just an
  offline report. The SegmentAgent writes `final_implied_fit_delta` to
  `segments.meta.tts_agent`, rejects final accepts above 5%, and re-checks the
  ML runtime `selected_candidate` so a selected LoRA candidate with
  `high_fit_delta` cannot bypass the outer retry loop.
- `tools/mit6824_fit_audit.py` now checks both selected candidate tempo fit
  and final accepted `tts_agent.final_implied_fit_delta`, including fallback
  audio and nested `tts_lora_candidate.runtime` payloads. A low final drift
  with hidden tempo pressure is therefore a failed acceptance artifact, not a
  review-pack surprise.

## Locked Evidence

Current acceptance artifact:

```text
data/validation/mit6824_acceptance/lec-08/fit_audit.json
```

Fit audit summary:

- Status: `fail`
- Recommendation:
  `repair_fit_policy_and_start_duration_prosody_training_feasibility`
- Selected candidate audio above 5% fit delta: `43 / 145`
- Candidate raw fit above 8%: `202 / 399`
- Candidate raw fit above 20%: `140 / 399`
- Systemic duration/prosody signal: `true`

The current training decision is:

```text
data/validation/mit6824_training_decision.json
```

It allows a controlled lecture-local duration/prosody experiment, but still
forbids promoted per-lecture adapters by default.

## What Lecture 7 Actually Proved

Lecture 7/v25 sounded better to the user than the first Lecture 8 run, but it
must not be treated as a productized fit-policy baseline. After adding the fit
audit, v25 also fails the new acceptance gate:

- Selected candidate audio above 5% fit delta: `66 / 159`
- Candidate raw fit above 8%: `316 / 535`
- Candidate raw fit above 20%: `156 / 535`

The useful Lesson 7 evidence is therefore not "8% tempo fit is acceptable".
The useful evidence is the process:

- use a fixed set of listening anchors
- keep ASR, term recall, drift, tail, and join quality as hard gates
- use split candidates for long dense clauses
- carry provenance through the main flow
- let fit audit block acceptance even if final drift looks good

For Lecture 8, the concrete goldmatch standard is the v7 split-aware training
run:

```text
data/training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200
```

It is not promoted to production, but it is the current training-process
reference because its high-fit holdout best-of had `10 / 12` strict pass,
`0` selected samples above 5% fit, and no selected join failures. Any newer
Lecture 8 adapter must beat this generated-audio holdout, not merely show a
better training loss.

Lecture 8 is different. It has many long and ultra-long Zookeeper/Raft-style
segments where the model can produce semantically good ASR but the raw duration
is far from target. Tempo fitting then hides the timing error and creates
dragged speech.

## Experiments So Far

`v3 splitpart`

- Manifest: 557 rows, 2.345 h logical audio
- Result: raw duration worsened on holdout
- Split loop selected `1 / 8`
- Default split/mix remained several seconds off target and join quality failed
  most samples
- Decision: reject

`v4 real-longweighted`

- Manifest: 336 rows, 2.629 h logical audio
- Accepted features: 222 train rows, many very long rows rejected by code-token
  limits
- Result: improved some 20-30 s raw samples but 40 s raw samples still failed
- Split loop selected `0 / 8`
- Shorter split parts still selected `0 / 8`; one near-pass (`seg16008`) was
  correctly rejected by high fit delta
- Decision: reject

`v5 stop-BCE`

- Added explicit stop-position BCE loss
- Result: stabilized one prior generation crash, but biased several long
  samples toward early stop
- Raw probe examples:
  - `seg15916`: target 39.78 s, generated 28.60 s, fit 28.1%
  - `seg15965`: target 29.66 s, generated 25.05 s, fit 15.5%
  - `seg15992`: target 41.82 s, generated 30.41 s, fit 27.3%
  - `seg16030`: target 26.16 s, generated 25.22 s, fit 3.6%
- Decision: reject current objective; stop-BCE alone is not sufficient

`v6 duration-position loss`

- Added a duration-position objective over teacher-forced stop logits.
- Training surrogate improved:
  - validation duration-position loss: `0.3578 -> 0.1471`
  - validation mel loss: `5.1022 -> 4.0753`
- Generated high-fit holdout did not pass:
  - `step40`: `3 / 12` within 5%, mean fit `11.45%`, max fit `20.49%`
  - `step160`: `3 / 12` within 5%, mean fit `12.47%`, max fit `24.71%`
  - `step80` and `step120` hit CUDA generation assertions before producing
    complete reports
- Comparison pack:
  `data/training/lora_runs/mit6824-lec08-local-lora-v6-real-longweighted-durationpos-r4-step160-targetdur/holdout_compare_v6_steps/listening_pack/index.html`
- Decision: reject. A lower validation loss is not evidence of production
  quality unless generated-duration holdout passes.

`v7 Lecture-7-goldmatch split12 parts`

- Corpus:
  `data/training/lora_corpus/mit6824-lec08-v7-goldmatch-split12-parts`
  - base rows from the Lecture 7-aligned gold/silver filters
  - split-part expansion with `max_part_sec=12`, `max_part_chars=140`,
    `pause_sec=0.18`
  - `712` manifest rows, `671` accepted features, `2.394 h` logical audio
- Training:
  `data/training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200`
  - validation duration-position loss: `0.0875 -> 0.0564`
  - validation mel loss: `5.2697 -> 4.1479`
- Duration holdout improved versus the old step80 adapter:
  - old step80 split12: `2 / 12` within 5%, mean fit ratio `8.13%`
  - v7 split12 + bounded pause padding: `10 / 12` within 5%,
    mean fit ratio `2.27%`
  - v7 best-of over split12/split8/padding: `12 / 12` within 5%,
    mean abs drift `186.70 ms`, max abs drift `464.67 ms`
- But the objective gate still fails:
  - best-of ASR mean `0.9683`, min `0.9143`
  - multi-candidate gate selected only `6 / 12`
  - strict selected rows: mean drift `87.96 ms`, max drift `278.41 ms`,
    mean ASR `0.9849`, min ASR `0.9756`
  - remaining failures are hard failures, not preference calls:
    `low_asr` or `bad_join_quality`
- Part-level ASR located the main failure below assembly:
  - split12 part ASR mean `0.9701`, min `0.8387`
  - multiple parts drop sentence tails under tight target durations
  - high text density correlates with failure; many bad parts are around
    `6.0-6.5` Chinese chars/sec
- Review pack:
  `data/training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200/holdout_highfit_bestof_review_v1/listening_pack/index.html`
- Decision: reject for full episode. v7 proves split-aware training and
  pause-budgeting can reduce dragged tempo, but it also exposes a more serious
  failure: under tight timing, the model may omit content. The next iteration
  must make text completeness and speech-rate feasibility first-class gates,
  not merely improve duration loss.

`v8 feasible-only cps<=5.6 duration-position`

- Training data was derived from the v7 feature cache but filtered to remove
  split parts whose required text density exceeded `5.6` non-space chars/sec:
  - train rows: `611 -> 495`
  - val rows: `26 -> 23`
  - test rows: `34`
- Training:
  `data/training/lora_runs/mit6824-lec08-v8-feasible-cps56-durationpos-r4-step160`
  - validation duration-position loss: `0.0952 -> 0.1075`
  - validation mel loss: `5.3039 -> 4.1855`
  - validation mel-top1: `0.0758 -> 0.1420`
- Generated split12 assembly improved ASR versus v7 split12 but still failed:
  - v8 split12 ASR mean `0.9788`, min `0.9375`
  - v8 split12 padded ASR mean `0.9782`, min `0.9375`
- After fixing the candidate gate so `low_asr` is always hard failure, the
  combined v7/v8 strict report selected only `8 / 12`:
  - mean drift `94.70 ms`
  - max drift `278.41 ms`
  - mean ASR `0.9872`
  - min ASR `0.9756`
  - join failures `0`
- Remaining hard failures:
  - `seg15916`: join quality failure even when duration is exact
  - `seg15976`: low ASR on the only join-passing candidates
  - `seg15990`: low ASR, previously incorrectly allowed by fallback
  - `seg16040`: low ASR, and v8 also fails drift/join
- Strict review pack:
  `data/training/lora_runs/mit6824-lec08-v8-feasible-cps56-durationpos-r4-step160/strict_v7_v8_listening_pack/index.html`
- Repair queue:
  `data/training/lora_runs/mit6824-lec08-v8-feasible-cps56-durationpos-r4-step160/lecture08_repair_queue_v1.json`
  - `seg15916`: join-boundary repair
  - `seg15976`, `seg15990`, `seg16040`: text compression before TTS
- Decision: reject for full episode. Filtering impossible-density parts helps
  expose the failure mode, but does not by itself produce a promotable adapter.
  The next repair has to happen before TTS: text compression/retranslation and
  boundary repair for high-density parts, then LoRA/candidate generation.

`v8 high-fit repair pass`

- Run:
  `data/training/lora_runs/mit6824-lec08-v8-feasible-cps56-durationpos-r4-step160/lecture08_highfit_12of12_tailfade_textrepair_v1`
- The remaining four failures were repaired without relaxing the 5% fit gate:
  - `seg15916`: join-boundary tail fade on split8 assembly
  - `seg15976`: text compression before TTS
  - `seg15990`: manual speech-rate-feasible rewrite plus forced split after
    `旧时间戳`
  - `seg16040`: text compression before TTS
- Promoted repair metrics:
  - `seg15916`: ASR `1.0`, drift `0.045 ms`, fit `0`
  - `seg15976`: ASR `1.0`, drift `12.97 ms`, fit `0.0422`
  - `seg15990`: ASR `1.0`, drift `17.10 ms`, fit `0.0284`
  - `seg16040`: ASR `1.0`, drift `10.07 ms`, fit `0.0316`
- Combined strict selected metrics:
  - count `12`
  - mean absolute drift `66.48 ms`
  - max absolute drift `278.41 ms`
  - mean ASR `0.9915`
  - min ASR `0.9756`
  - join failures `0`
  - max fit delta `0.0422`
- Decision: promote this as the high-fit holdout repair baseline for the next
  Lecture 8 full-episode shadow. Do not promote it as delivery acceptance until
  final-mix ASR and the human review pack pass.

## Gold-Standard Data Lesson

The Lecture 7-aligned corpus filter is:

- judge score >= `0.8`
- final-mix ASR similarity >= `0.97`
- final drift <= `500 ms` and drift ratio <= `5%`
- selected fit tempo delta <= `5%`
- max text chars <= `220`

Using this same filter:

- Lecture 7 candidate gold corpus:
  `data/training/lora_corpus/mit6824-lec07-v25-gold-candidate-v3`
  - `105` rows, `0.6567 h`
- Lecture 8 candidate silver corpus:
  `data/training/lora_corpus/mit6824-lec08-lowfit-silver-candidate-v3`
  - `92` rows, `0.6675 h`

This is enough for controlled duration/prosody smoke experiments, not enough
for promotion as a course adapter. The strict Lecture 8 expansion
`mit6824-lec08-strict-fit005-silver-v2` has `105` rows / `0.7827 h`, but it
relaxes the judge filter and should not replace the gold-aligned v3 filter as
the main acceptance-like dataset.

Preprocessing reveals a model-context constraint: the effective GPT code-token
limit is `1811`, so clean 40s+ whole utterances are mostly rejected or clipped.
That means Lecture 8's 40s failures should be repaired through split planning
and split-aware validation, not by expecting a single raw 40s LoRA generation
to become reliable.

## Operational Rules From This Failure

- No selected audio above 5% fit delta may pass lecture acceptance.
- Candidate gate default max fit delta is `0.05`.
- `low_asr`, high drift, high fit delta, low term recall, tail truncation, and
  bad join quality are hard failures. They cannot enter selected through
  automatic fallback.
- No full Lecture 8 episode run should start from adapters that fail the
  high-fit holdout.
- Raw long segments should be forced through split/candidate planning; raw
  40 s generation is not trusted.
- ASR pass is not enough when raw duration is wrong.
- 10% tempo rescue remains comparison-only and is not production selectable.
- Training checkpoints must be selected by generated-duration holdout, not by
  final step or validation loss alone.

## Next Training Direction

The next experiment should change the objective, not just resample the same
data:

- keep a fixed high-fit holdout from Lecture 8
- evaluate generated duration during validation, not only training loss
- penalize early and late stop behavior against target duration
- reject or rewrite split parts whose target speech density exceeds the safe
  range before synthesis
- use ASR/tail completeness at part level before assembling split candidates
- prefer text compression or source-window borrowing over forcing the model to
  satisfy impossible timing with omitted words
- prefer real boundary-aligned audio over synthetic split cuts
- promote only if holdout selected count, fit delta, ASR, tail completeness, and
  join quality pass together

The minimum gate before any full Lecture 8 retry:

- selected holdout pass count covers all high-risk anchors
- max selected fit delta <= 0.05
- no join quality failures
- no tail truncation
- ASR similarity stays within the existing floor
- listening pack confirms no dragged vowels on selected long samples

This gate is no longer considered satisfied by
`lecture08_highfit_12of12_tailfade_textrepair_v1`. The later full-episode
review exposed dragged speech, so that artifact is now only a comparison
baseline. No new full Lecture 8 run should start until the fixed high-fit
holdout beats the v7/v8 reference without relying on high fit.

## Episode 183 Main-Flow Gate Fix

Episode 182 proved that the holdout result was not enough: the adapter and
candidate policy can be good while the main flow still ships bad audio through
escape paths. Three regression anchors were recorded in
`data/validation/mit6824_acceptance/lec-08/episode182_failure_evidence.json`:

- `seg16090`: short dense retry did not escalate into LoRA/candidate planning
  before a hard-cap overrun was accepted.
- `seg16096` and `seg16097`: candidate planning correctly signaled
  `needs_text_compression_speech_rate`, but the outer loop treated that as a
  normal candidate failure and eventually shipped clipped hard-cap output.

The main-flow fix for episode 183 is therefore:

- short dense non-converging retries can opt into LoRA candidate/split planning
  below the old long-segment threshold
- text-compression planning signals do not consume candidate attempt budget
- a hard-cap accept with active or previous text-compression requirement is
  rejected instead of shipped
- course validation runs set `SEGMENT_AGENT_REJECT_HARD_CAP_ESCAPE=true`, so
  any remaining hard-cap escape is a failed segment and enters repair/training
  rather than silently passing

If the same anchors still fail after these gates, the failure should be used as
gold/silver training evidence for a Lecture 8 v9 adapter or objective change.
It should not be hidden by increasing tempo fit beyond 5%.

Live episode 183 status after the fit-gate patch was loaded:

- synthesized segments: `9`
- candidate-audio selections: `9`
- mean absolute drift: `10.444 ms`
- max absolute drift: `14 ms`
- hard-cap accepts: `0`
- final implied fit delta above `0.05`: `0`
- inflight text/candidate repair rows: `seg16302`, `seg16314`, `seg16315`

This proves only that the patched gate is active on the accepted subset. It
does not accept Lecture 8. The whole episode still must complete, run final-mix
ASR, pass `fit_audit.status = pass`, build a risk queue, and pass human review.

## v9/v10 Training Decision

The Lecture 7 result was useful because it established a disciplined process,
not because a permissive tempo fit was acceptable. The reusable process is:

- choose data with the Lecture-7-style low-fit gold/silver filter
- hold out hard anchors before training
- select checkpoints by generated-audio holdout, not loss alone
- reject high fit, clipped tails, low ASR, low term recall, and bad joins as
  hard failures
- keep listening packs as final human evidence, not as a way to waive hard
  duration failures

Lecture 8 satisfies the trigger for a controlled duration/prosody LoRA
experiment, but the experiments run so far do not produce a promotable adapter.
The rejected v9 path was:

```text
base feature cache:
data/training/lora_features/mit6824-lec08-v8-feasible-cps56-split12-parts

repair adjunct manifest:
data/training/lora_corpus/mit6824-lec08-v9-promoted-repair-v1/manifest.json
```

The repair adjunct currently contains `4` promoted rows, `0.024 h`, max target
speech density `5.394191` chars/sec, max fit delta `0.04216047`, and no missing
audio. It is not a standalone gold dataset and must be appended to the v8
feasible cache only after ML-container preprocessing. Failed episode 182/183
shadow audio remains training-ineligible until repaired, regenerated under the
5% fit gate, and accepted by ASR/join/tail/human review.

The v9 result:

- validation duration-position loss regressed to `0.1578`
- primary holdout selected `2 / 12`
- primary + micro6 selected `4 / 12`
- text repair projected only `5 / 12`
- decision: reject for full episode

The follow-up v10 result:

```text
data/training/lora_runs/mit6824-lec08-v10-goldmatch-v7clean-r4-step240
```

- training-pack audit passed on the clean v7 goldmatch corpus:
  `712` manifest rows, `671` feature rows, `2.394 h`, no holdout leakage,
  max fit delta `0`
- the promoted repair pack audit failed as training data:
  `4` rows, `0.024 h`, no val/test, and `4` holdout-leakage rows
- best validation duration-position checkpoint was step `200`, but generated
  holdout still regressed
- primary holdout selected `1 / 12`
- micro6 holdout selected `0 / 12`
- decision artifact:
  `data/training/lora_runs/mit6824-lec08-v10-goldmatch-v7clean-r4-step240/v10_training_decision.json`

Current decision: keep the v7 goldmatch adapter as the controlled Lecture 8
reference. Do not run another full Lecture 8 shadow until a newer adapter or
planner beats v7/v8 on the fixed high-fit holdout.

The next training sequence must change data, not merely add steps:

1. Keep repaired high-fit holdout out of training when it is used for
   evaluation.
2. Collect additional non-holdout Lecture 8 audio that passes the Lecture-7
   low-fit/high-ASR/join/tail gates.
3. Prefer human-approved or final-mix accepted aligned audio; failed shadow
   audio is training-ineligible until repaired and revalidated.
4. Add text-planning and join-boundary evidence as labels/rewrite artifacts,
   not as permission to stretch audio.
5. Start the next adapter only after `mit6824_lora_training_pack_audit.py`
   passes, then select checkpoints by generated-audio holdout.
