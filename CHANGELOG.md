# Changelog

All notable changes to HoloDub are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once we cut a tagged release.

## [Unreleased]

### Added

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
