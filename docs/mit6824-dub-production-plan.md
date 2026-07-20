# MIT 6.824 Dub Production Plan

This document is the course-level operating contract for finishing the MIT
6.824 dubbing project. The LoRA/candidate work exists to support this delivery
goal; it is not an independent research loop.

## Scope

Current delivery target:

```text
MIT 6.824 2020 lectures 8-20
```

Current local progress, verified by the course status artifacts: lecture 8 is
blocked before another full-episode run. The latest audible review exposed
dragged speech, so the active goal is to rebuild the Lecture-8 LoRA/candidate
path from the fixed high-fit holdout before resuming episode-scale shadow work.
The previous lecture 8 attempts (`episode_id=180`, the cancelled
`episode_id=181`, rejected `episode_id=182`, and paused `episode_id=183`) are
retained only as failed comparison points because they exposed audible tempo
stretching, hard-cap clipping, ignored text-compression signals, insufficient
main-flow fit enforcement, or weak adapter generalization.
Lectures 9-20 are not started yet. Lecture 9 must not start until lecture 8 is
repaired and accepted or an explicit user decision changes the course order.

Lecture 7 remains a regression fixture for LoRA candidate execution because it
contains known hard cases and human-reviewed anchors. It should not keep
absorbing optimization work after the main-flow candidate chain is proven.

The course tracker is generated with:

```powershell
python tools/mit6824_course_tracker.py `
  --lecture-start 8 `
  --lecture-end 20 `
  --output-json data/validation/mit6824_course_tracker.json `
  --output-md data/validation/mit6824_course_tracker.md
```

The tracker is authoritative for local source availability and DB episode
state.

## Delivery Loop

Every new lecture should move through the same loop:

```text
source video -> episode creation -> main-flow dub -> final mix -> final-mix ASR
-> risk queue -> human review pack -> human acceptance -> archive
```

Required artifacts per accepted lecture:

- final video
- final audio
- final-mix ASR report
- fit audit
- risk queue
- human review pack
- adapter/profile provenance
- notes for any accepted risk anchors

`fit_audit.status = pass` is a content gate, not a cosmetic report. A lecture
with selected audio above the 5% fit warning threshold is blocked even when
final drift and ASR look acceptable, because audible tempo stretching is a
delivery failure.

Passing one lecture does not promote a new model by itself. Repeated behavior
across multiple lectures is what changes model or policy decisions.

## Long-Run Liveness

`tts_duration` can legitimately run longer than the default stage lease when a
chapter contains many long candidate-enabled segments. A missing lease alone is
not proof that the worker is dead.

Use this order before any manual recovery:

- check the latest synthesized segment timestamp
- check job heartbeat
- check worker logs for recent `agent_decision` or judge output
- check the ready/delayed Redis queues for an existing same job/stage task
- check whether the current frontier segment is being retried or retranslated

Only requeue a stage when the worker process has been stopped or there is clear
evidence that no goroutine is still processing the stage. The recovery helper is
intentionally conservative:

```powershell
python tools/mit6824_resume_stalled_stage.py --job-id <id> --stage tts_duration
```

It refuses expired-lease recovery by default. Use
`--allow-expired-lease-requeue --execute` only after confirming the old worker is
not still processing that stage.

## Lecture 8 Rerun

Lecture 8 must follow the same standard that made lecture 7 useful as a
regression fixture: LoRA candidate execution, split planning, 5% production fit
gate, final-mix ASR, fit audit, risk queue, and human review pack. Do not use
8% or 10% tempo rescue as a selected production path.

Current rerun status:

```text
name: blocked_before_next_full_episode_shadow
episode_id: 183
root_job_id: 358
adapter under review: training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200/adapter_latest.pt
comparison baselines: lecture08_highfit_12of12_tailfade_textrepair_v1, holdout_highfit_bestof_review_v1
```

Episode 182 failed because short dense and 40s-class segments could still escape
with hard-cap drift or clipped tails after candidate planning signaled text
compression. Episode 183 then showed that merely adding repair signals and a v9
training pass was still not enough: v9 selected only `2 / 12` on the primary
high-fit holdout and `4 / 12` after micro6 rescue. That means the next move is
not another whole-episode run. It is to repair the holdout and training set until
the generated-audio gate beats the Lecture-8 v7 goldmatch reference.

The current profile therefore requires all of the following before the next
episode shadow:

- `SEGMENT_AGENT_REJECT_HARD_CAP_ESCAPE=true`
- `SEGMENT_AGENT_MAX_FIT_DELTA=0.05`
- real LoRA candidate execution enabled in both Go and ML service
- retry escalation from failed default TTS into the split/candidate path
- ML rescue split strategies enabled in the main-flow `split_policy`
- `LORA_SHADOW_ADAPTER_PATH` pinned to the adapter being tested, so UI/runtime
  provenance and ML inference agree

Failed shadow audio may enter only the repair queue, not the training set.
Training material must follow the Lecture-7-style goldmatch rule: human-approved
or promoted clean audio only, fixed holdout first, no sample leakage, and no
selected sample above 5% fit.

## Starting Lecture 8 From Scratch

Lecture 8 is the first expected new production lecture after the active lecture
7 candidate regression finishes. Do not start it on the same local GPU while a
full candidate shadow run is still active.

Preview the lecture 8 start payload from the tracker:

```powershell
python tools/mit6824_start_lecture.py --lecture 8
```

Start it only after the active lecture 7 candidate regression has finished:

```powershell
python tools/mit6824_start_lecture.py --lecture 8 --execute
```

The start tool refuses to execute while an active `lora-candidate-episode147-v25`
run is still present. It uses this normal API payload with a DATA_ROOT-relative
input path:

For unattended handoff from the active lecture 7 regression to lecture 8, use
the course auto-advance wrapper. It waits for the v25 finalizer status to become
complete, refreshes candidate coverage, freezes the lecture 7 regression suite,
waits for the active lecture 7 episode to finish, optionally rebuilds and
restarts the Go `api`/`worker` services so spoken-text and orchestration changes
are loaded, optionally restarts the ML service so newly changed
candidate/provenance code is loaded, refreshes the course tracker, and only then
calls the safe start tool:

```powershell
python tools/mit6824_auto_advance.py `
  --lecture 8 `
  --wait-finalize-status data/training/lora_runs/main_flow_candidate_episode147_v25/auto_finalize_status.json `
  --watch `
  --execute `
  --allow-regression-open-issues `
  --regression-open-issue-note lecture7_fixture_open_issues_do_not_block_lecture8_delivery `
  --rebuild-go-services-before-start `
  --restart-ml-service-before-start `
  --poll-sec 300
```

Regression freeze is required by default. Use `--no-require-regression-freeze`
only for an explicit manual recovery run, never for normal production handoff.
If the lecture 7 regression completes but still has documented fixture-quality
issues, `--allow-regression-open-issues` may lock it as
`locked_with_open_issues`. That state does **not** mean `frozen=true` or that the
audio is accepted as a golden baseline; it only records the open issues and
allows course delivery to move to lecture 8 after the end-to-end candidate chain
has been exercised. Incomplete runs, failed candidate-coverage refreshes,
pending eligible coverage after completion, and unknown or structural validation
issues still block auto-advance. Validation issues that are simply the recorded
form of candidate fallback or candidate-enabled `default_audio` are kept as
open issues, not treated as a golden pass.
The active-run check happens before any service rebuild or restart, so a
still-merging lecture 7 episode cannot be interrupted by repeated lecture 8
auto-advance polling.

Without `--execute`, the same command path is a dry run and will only print the
payload that would be submitted.

```powershell
$body = @{
  name = "mit6824-lec08-zookeeper-prod-v1"
  input_relpath = "source_videos/mit-6.824-2020/lec-08 - Zookeeper.mkv"
  source_language = "en"
  target_language = "zh-CN"
  auto_start = $true
  max_retries = 0
  config = @{
    course = "mit6824-2020"
    lecture = 8
    production_profile = "mit6824-course-prod-v1"
    lora_candidate_opt_in = $true
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/jobs" `
  -ContentType "application/json" `
  -Body $body
```

Expected first-run artifacts should live under the created episode/job output
paths. The external course tracker remains:

```text
data/validation/mit6824_course_tracker.json
data/validation/mit6824_course_tracker.md
```

After any lecture run changes state, refresh the full course status:

```powershell
python tools/mit6824_status_refresh.py
```

This is the canonical read-only refresh. It updates the course tracker,
acceptance status, delivery index, candidate coverage, training decision, batch
review, course-advance dry run, and goal status together. Human actions such as
accepting a lecture or recording blockers should use `--refresh-status`, which
now calls this full refresh path rather than only rebuilding the acceptance
table.

For a single read-only snapshot of the whole course objective, use:

```powershell
python tools/mit6824_goal_status.py
```

It writes:

```text
data/validation/mit6824_goal_status.json
data/validation/mit6824_goal_status.md
```

The snapshot combines lecture 7 regression progress, auto-advance state,
lecture finalizer state, lectures 8-20 acceptance status, and the current
training recommendation.

The active regression section also reports a health classification:

- `running_recent`: a running job has produced a synthesized segment recently.
- `running_slow`: no recent synthesized segment, but a job is still running and
  the delay is still inside the slow threshold.
- `stale_needs_diagnosis`: a running job has not produced a synthesized segment
  past the stale threshold.
- `no_running_job`: segments remain, but no active job is running.

The goal status next action must reflect that health. `running_recent` remains
`wait_for_v25_candidate_regression`, `running_slow` becomes
`monitor_v25_candidate_regression_slow`, and `stale_needs_diagnosis`,
`no_running_job`, or `error` become explicit
`diagnose_v25_candidate_regression_*` actions. This prevents a stalled
regression from looking like healthy waiting in the handoff reports.
The delivery index must treat these diagnosis actions as a hard start blocker
for lecture 8 and show `inspect_v25_candidate_regression`, not a start command.

To refresh the tracker, acceptance status, training decision, candidate coverage,
course advance dry-run, and goal status in one read-only command, use:

```powershell
python tools/mit6824_status_refresh.py
```

It writes `data/validation/mit6824_status_refresh.json` and appends
`data/validation/mit6824_status_refresh_history.jsonl`. With `--watch`, it can
keep the status artifacts current during long v25 or lecture runs. This command
does not start jobs or finalize lectures.

The refresh also writes watcher health snapshots:

```text
data/validation/mit6824_watchdog_status.json
data/validation/mit6824_watchdog_status.md
data/validation/mit6824_handoff_report.json
data/validation/mit6824_handoff_report.md
```

For an explicit watcher check, use:

```powershell
python tools/mit6824_watchdog.py --restart-status-watch
```

The watchdog expects the v25 watcher, v25 auto-finalizer, lecture 8
auto-advance watcher, lecture 8 finalizer watcher, and read-only status refresh
watcher. It can restart only the read-only status watcher. Production-changing
watchers are reported with restart commands but are not auto-repaired.

The handoff report is the first file to read after returning to the project. It
combines goal status, delivery index, watchdog status, auto-advance state, and
finalizer state into one next action plus recovery commands. It remains
read-only. It also estimates v25 progress from
`mit6824_status_refresh_history.jsonl` when enough recent segment-count deltas
exist; otherwise it explicitly reports that no recent estimate is available. It
can be regenerated directly with:

```powershell
python tools/mit6824_handoff_report.py
```

Candidate coverage is written to:

```text
data/validation/mit6824_candidate_coverage.json
data/validation/mit6824_candidate_coverage.md
```

It checks that synthesized segments meeting the long/high-risk candidate policy
actually have candidate runtime evidence. Incomplete eligible rows are reported
as pending, while synthesized eligible rows without candidate runtime are hard
coverage issues.

If an incomplete eligible row already has a runtime `fallback_reason`, coverage
must surface it as `eligible_pending_fallback`. This remains a warning while
the row is still in flight, but it becomes a regression-freeze blocker if the
segment finishes as default/fallback audio. Treat these rows as candidate-chain
repair targets, not as a reason to loosen production gates.

The human-facing delivery index is written to:

```text
data/validation/mit6824_delivery_index.json
data/validation/mit6824_delivery_index.md
```

It turns the lower-level tracker and acceptance status into one action table:
which lecture is next, which lectures are queued, which review packs are ready
for listening, and the exact accept command for lectures awaiting human
approval.

The 2-3 lecture batch review is written to:

```text
data/validation/mit6824_batch_review.json
data/validation/mit6824_batch_review.md
```

It connects delivery evidence to the training decision rule. Before at least
two reviewed lectures exist, it reports `not_due`. Once a batch is available,
it summarizes repeated failure families, the default repair path, and whether a
course/domain adapter feasibility review is justified. Per-lecture adapter
training remains disallowed.

For the course-level next action, use:

```powershell
python tools/mit6824_course_advance.py --refresh
```

By default this is also read-only. It reports whether the course should wait for
the lecture 7 regression fixture, start the next not-started lecture, wait for
an in-progress lecture, run a finalizer, or wait for human acceptance. With
`--execute`, it can run the selected start/finalize action. It does not skip
ahead to lecture N+1 while lecture N is still awaiting human acceptance unless
`--allow-unaccepted-previous` is explicitly supplied.

For a long-running safe course watcher after the current dedicated lecture 8
handoff is no longer needed:

```powershell
python tools/mit6824_course_advance.py `
  --refresh `
  --watch `
  --execute `
  --spawn-finalizer `
  --poll-sec 300
```

The watcher stops at `waiting_for_human_acceptance`, `all_lectures_accepted`, or
manual-triage states. It also checks existing finalizer status files and avoids
spawning duplicate finalizer watchers for the same lecture.

Acceptance artifacts are checked under:

```text
data/validation/mit6824_acceptance/lec-XX/
```

Each accepted lecture should either place artifacts there directly or provide
an `acceptance.json` with explicit paths for:

```text
final_video
final_audio
final_mix_asr_report
fit_audit
risk_queue
human_review_pack
adapter_profile_provenance
```

For lecture-level automatic finalization, use:

```powershell
python tools/mit6824_finalize_lecture.py `
  --lecture 8 `
  --refresh-tracker `
  --watch `
  --execute `
  --poll-sec 300
```

The finalizer waits for the lecture episode to exist and complete, then runs
final-mix ASR, runs `fit_audit`, builds the human review pack, writes
`adapter_profile_provenance.json`, creates `risk_queue.json`, and writes
`acceptance.json` for human acceptance. After the manifest is written, it runs
`tools/mit6824_status_refresh.py` so acceptance status, delivery index,
training decision, and batch review are updated for the reviewer. A refresh
failure is recorded in the finalizer status as a warning without discarding the
completed acceptance artifacts.

`adapter_profile_provenance.json` must identify both the configured course
profile and the observed runtime behavior. For candidate-enabled lectures it
records the start config plus segment-runtime counts: total/synthesized
segments, candidate runtime segments, candidate-audio selections, fallback
segments, selected-family distribution, audio-selection distribution, and any
adapter metadata that the ML runtime exposed. This makes each lecture's
adapter/profile usage auditable without relying on memory or log spelunking.

`risk_queue.json` is a first-class review artifact, not a placeholder. It is
built from final-mix ASR `flagged_rows` / `worst_rows`, the review pack
manifest, and LoRA candidate runtime risks such as candidate fallback or
candidate-enabled segments that had to use `default_audio`. It carries segment
IDs, labels, similarity, flags, expected/ASR text, selected candidates,
candidate fallback reasons, and priority buckets.

After listening to the review pack and accepting the lecture, mark it accepted:

```powershell
python tools/mit6824_accept_lecture.py `
  --lecture 8 `
  --reviewer user `
  --notes "accepted after human review" `
  --refresh-status `
  --auto-advance-next
```

The accept tool refuses to mark a lecture accepted unless all required
artifacts exist: final video, final audio, final-mix ASR report, risk queue,
human review pack, and adapter/profile provenance. Accepted risk anchors can be
recorded with repeated `--risk-anchor` flags. With `--auto-advance-next`, a
successful acceptance runs the safe course advance action once: it refreshes
tracker/acceptance inputs for the action, starts the next lecture only if
previous-acceptance and source gates pass, and spawns that lecture's finalizer
watcher. With `--refresh-status`, the accept tool runs the full course status
refresh after that advance attempt so the delivery index and training decision
reflect the latest state.

If human review finds blockers, do not accept the lecture. Record the blockers
instead:

```powershell
python tools/mit6824_record_review_blockers.py `
  --lecture 8 `
  --reviewer user `
  --notes "needs repair after human review" `
  --blocker "describe blocker" `
  --refresh-status
```

Blocked lectures are reported as `review_blocked`; course advance will not
start the next lecture until the blockers are repaired and the lecture is
finalized/reviewed again. Blocker text also feeds the training decision and
2-3 lecture batch review as evidence, without implying per-episode training.
With `--refresh-status`, the blocker tool runs the full course status refresh
immediately after writing the blocker decision.

## Stop Rules

Stop iterating on a lecture and move to the next lecture when:

- final video exists and plays
- final-mix ASR has no large missing-text or wrong-order regions
- high-priority risk samples are manually accepted or repaired
- no known tail truncation, bad join, or large drift regression remains
- accepted exceptions are documented as anchors instead of hidden threshold
  relaxations

Do not keep optimizing a lecture because a small number of low-risk samples
could sound marginally better. Add them to the regression pool only if they
represent a repeatable failure family.

The course delivery target has priority over indefinite regression polishing:
after lecture 7 v25 proves the candidate chain, lecture 7 becomes a fixture and
the next broad proof must come from lecture 8.

## Training Decision Rule

Do not train a separate adapter per lecture. Per-lecture adapters overfit,
confuse asset management, and do not prove product quality.

Training is a course/profile decision, not an episode habit. A single episode
can contribute accepted data or reveal a failure family, but it should not
create its own adapter unless the product intentionally needs a new voice/domain
profile.

The normal production path is therefore:

```text
run the next lecture with the current course profile
-> review final ASR/risk queue/listening pack
-> repair rules or candidate policy for isolated issues
-> collect accepted aligned data and issue families
-> decide after multiple reviewed lectures whether a course adapter is needed
```

Use the existing adapter/profile unless one of these is true:

- the same failure family repeats across multiple lectures
- the current adapter fails canary gates
- the current adapter fails full-episode shadow plus human review in a way that
  cannot be repaired by rules, split planning, or candidate selection
- new human-approved aligned audio materially changes the dataset
- candidate policy, split planning, spoken-text normalization, and gate tuning
  cannot reach the target behavior

A single failed production lecture normally triggers repair and rerun, not a
new adapter. Per-lecture training is allowed only if the product intentionally
needs a new voice/domain profile; it is not the default path for MIT 6.824.

Prefer these fixes before training:

- single term, number, section, or mixed Latin issue:
  spoken-text rule, glossary, or normalization
- single long-clause truncation:
  split planning, candidate gate, or rescue policy
- single bad join:
  split boundary or join-quality policy
- complete high-ASR split candidates that are consistently too slow:
  shorten/rephrase target text using candidate text-compression feedback; do
  not relax tempo-fit gates or train an adapter from a single instance

When training is justified, train a course/domain-scoped adapter such as a MIT
6.824 distributed-systems profile. Use fixed train/validation/holdout splits,
and never let a sample enter both training data and canary or episode holdout.

Training readiness requires all of the following:

- at least two reviewed production lectures, unless the current adapter fails a
  fixed canary/full-episode gate outright
- the same failure family repeating across at least two lectures
- at least three concrete issue instances in that family
- at least 30 minutes of measured human-approved aligned final audio as an
  engineering floor
- a fixed held-out canary and episode set with no training overlap

Non-training examples:

- `5号` read as `5日`: normalization/spoken-text rule
- `5.4.1` losing a dot: section-number normalization
- `Raft` or `CRAQ` pronunciation ambiguity: glossary / mixed Latin handling
- one sentence with no pause: split boundary or pause policy
- one tail truncation: candidate rescue / join / duration policy

Data expectations:

- engineering floor: tens of minutes of human-approved aligned audio
- promotion target: 1-3 hours of gold audio
- stable profile target: 2-5 hours of gold audio

## Lecture 7 Regression Role

Lecture 7 candidate runs are allowed only for:

- confirming ML-service internal candidate execution
- verifying known anchors do not regress
- validating episode-level final-mix ASR and review-pack generation
- checking that short/default paths remain unchanged

After the active v25 run finishes, only deterministic fixes should trigger a
targeted rerun. The next broad quality proof must come from new lectures,
starting with lecture 8.

Build the lecture 7 regression manifest while v25 is running:

```powershell
python tools/mit6824_regression_suite.py
```

This writes:

```text
data/validation/mit6824_regression/lecture7_v25_regression_suite.json
data/validation/mit6824_regression/lecture7_v25_regression_suite.md
```

After v25 completes and the finalizer has produced ASR/review artifacts, freeze
the suite:

```powershell
python tools/mit6824_regression_suite.py --freeze
```

Freeze refuses incomplete runs, fallback rows, high drift, hard-cap failures,
validation issues, or missing/failed anchors. Low segment-judge rows remain
review risks by default; use `--fail-low-judge` only when the suite should treat
them as hard blockers.

## Batch Review Cadence

After every 2-3 newly processed lectures:

- summarize blocker categories
- separate rule issues from model issues
- decide whether the current adapter remains sufficient
- add recurring cases to canary/regression fixtures
- update the course tracker and acceptance notes

Generate the training decision report from the current acceptance status:

```powershell
python tools/mit6824_training_decision.py `
  --acceptance-status data/validation/mit6824_acceptance_status.json `
  --output-json data/validation/mit6824_training_decision.json `
  --output-md data/validation/mit6824_training_decision.md
```

The training decision report uses each lecture's `risk_queue.json` when it is
present, so ASR `flagged_rows` / `worst_rows` are not double-counted. The ASR
report is still read for gate-level failures. This keeps the 2-3 lecture review
focused on repeated failure families instead of inflated duplicate counts.

The report also measures accepted lectures' `final_audio` duration and writes a
`gold_audio` summary. Even if repeated failure families are present, the
recommendation must remain `do_not_train_collect_human_approved_audio` until the
measured human-approved aligned audio reaches the configured floor.

The default recommendation must remain `do_not_train_*` until repeated failure
families appear across multiple reviewed lectures and the gold-audio floor is
met. Per-lecture training is not a valid recommendation.

This cadence keeps the project moving toward completed videos while still
testing whether the engineering loop generalizes beyond the lecture 7 fixture.
