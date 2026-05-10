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
