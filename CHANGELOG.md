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
