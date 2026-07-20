# Configuration Reference

This file is the human-readable companion to [`.env.example`](../.env.example). Treat `.env.example` as the source of truth for exact defaults; use this guide to choose the right mode and understand which switches matter for a real HoloDub deployment.

## Common Modes

| Mode                  | Purpose                                                                        | Key Settings                                                                                                                            |
| --------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| Smoke run             | Prove the stack, UI, queue, DB, and merge path work without external services. | `TRANSLATION_PROVIDER=mock`, `ML_SEPARATOR_BACKEND=ffmpeg_stub`, `ML_ASR_BACKEND=mock`, `ML_VAD_BACKEND=none`, `ML_TTS_BACKEND=silence` |
| Real translation only | Keep ASR/TTS mocked, but call a real LLM-compatible translation endpoint.      | `TRANSLATION_PROVIDER=openai_compatible`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`                                           |
| Real ML pipeline      | Use model-backed ASR, optional VAD/diarization, and IndexTTS2.                 | `ML_PYTHON_EXTRAS=real`, `ML_ASR_BACKEND=faster_whisper`, `ML_TTS_BACKEND=indextts2`, `INDEXTTS2_INLINE=true`                           |
| Production-like run   | Run real services with auth, metrics, and deployment hygiene.                  | `APP_ENV=production`, `API_AUTH_TOKEN`, `ENABLE_METRICS=true`, tuned Redis/Postgres/storage settings                                    |

## Service And Storage

| Variable                                                  | Notes                                                                                                             |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `APP_NAME`, `APP_ENV`                                     | Application identity and environment mode. `APP_ENV=production` requires `API_AUTH_TOKEN`.                        |
| `HTTP_ADDR`                                               | Go API bind address, default `:8080`.                                                                             |
| `DATA_ROOT`                                               | Shared media/model/artifact root mounted into API, worker, and ML containers. Docker Compose defaults to `/data`. |
| `DATABASE_DRIVER`, `DATABASE_DSN`                         | PostgreSQL connection used by the Go control plane.                                                               |
| `REDIS_ADDR`, `REDIS_PASSWORD`, `REDIS_DB`                | Redis queue and delayed/dead-letter queue storage.                                                                |
| `QUEUE_KEY`, `DELAYED_QUEUE_KEY`, `DEAD_LETTER_QUEUE_KEY` | Queue names. Keep defaults unless running several isolated HoloDub environments on one Redis.                     |
| `STAGE_TIMEOUT_SECONDS`, `STAGE_LEASE_TTL_SECONDS`        | Stage timeout and lease windows. Long videos or cold model loads may need larger values.                          |
| `AUTO_MIGRATE_ON_START`                                   | Convenient locally. In production, only one service instance should run migrations.                               |

## Security And Observability

| Variable                                             | Notes                                                                                                                    |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `API_AUTH_TOKEN`                                     | Required when `APP_ENV=production`; strongly recommended for any exposed deployment.                                     |
| `TRUSTED_PROXIES`                                    | Comma-separated proxy list, for example `127.0.0.1,10.0.0.0/8`, so client IPs can be read safely from forwarded headers. |
| `REQUEST_RATE_LIMIT_RPS`, `REQUEST_RATE_LIMIT_BURST` | API rate limiting. The job detail UI issues several requests per load, so local defaults are intentionally forgiving.    |
| `ENABLE_METRICS`                                     | Enables Prometheus metrics. See [observability](observability/README.md).                                                |
| `NOTIFICATION_TIMEOUT_SECONDS`                       | Timeout for outbound notification hooks.                                                                                 |
| `MODEL_MANIFEST_PATH`                                | Model manifest used to describe available model assets.                                                                  |

## Translation

| Variable                 | Notes                                                                                        |
| ------------------------ | -------------------------------------------------------------------------------------------- |
| `TRANSLATION_PROVIDER`   | `mock` for smoke tests, `openai_compatible` for real translation.                            |
| `OPENAI_BASE_URL`        | OpenAI-compatible `/v1` endpoint, including hosted providers, gateways, or self-hosted APIs. |
| `OPENAI_API_KEY`         | Credential for the compatible endpoint.                                                      |
| `OPENAI_MODEL`           | Main translation model.                                                                      |
| `OPENAI_TEMPERATURE`     | Lower values are usually better for stable, length-aware dubbing translation.                |
| `OPENAI_TIMEOUT_SECONDS` | Per-call timeout for translation requests.                                                   |

## Duration And Drift Control

HoloDub measures TTS duration against the original segment window. When the generated audio drifts too far, it can ask a retranslation model to produce a tighter line before retrying TTS.

| Variable                             | Notes                                                                                        |
| ------------------------------------ | -------------------------------------------------------------------------------------------- |
| `RETRANSLATION_ENABLED`              | Enables the drift-driven retry loop.                                                         |
| `RETRANSLATION_DRIFT_THRESHOLD`      | Relative drift threshold, for example `0.06` means 6 percent.                                |
| `RETRANSLATION_ABS_MAX_DRIFT_SEC`    | Absolute drift cap. The effective threshold is the stricter of relative and absolute limits. |
| `RETRANSLATION_MIN_DRIFT_THRESHOLD`  | Floor for the relative threshold so long segments do not become impossibly strict.           |
| `RETRANSLATION_MAX_ATTEMPTS`         | Maximum retries for manual or operator-triggered segment retry paths.                        |
| `RETRANSLATION_INITIAL_MAX_ATTEMPTS` | Higher retry budget for the initial pipeline.                                                |
| `RETRANSLATION_MODEL`                | Model used for the retranslation agent; it reuses the compatible API credentials.            |
| `SEGMENT_AGENT_MAX_FIT_DELTA`        | Main-flow fit audit cap; accepted audio above this implied tempo delta is rejected.          |
| `VOICE_PROFILE_RATE_ALPHA`           | EMA weight for voice-profile speaking-rate calibration.                                      |

## ML Backends

| Variable               | Notes                                                                                                                     |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `ML_SERVICE_URL`       | Go worker endpoint for the Python ML service.                                                                             |
| `ML_SEPARATOR_BACKEND` | `ffmpeg_stub` for smoke mode, `demucs` for real vocal/BGM separation.                                                     |
| `ML_ASR_BACKEND`       | `mock` for smoke mode, `faster_whisper` for real ASR.                                                                     |
| `ML_VAD_BACKEND`       | `none` or `pyannote`. Pyannote requires `PYANNOTE_AUTH_TOKEN`.                                                            |
| `ML_TTS_BACKEND`       | `silence` for smoke mode, `indextts2` for real zero-shot TTS.                                                             |
| `ML_PYTHON_EXTRAS`     | Empty for the lightweight image; `real` installs model-heavy Python dependencies. Rebuild `ml-service` after changing it. |
| `GPU_CONCURRENCY`      | ML service GPU concurrency. Use `1` on tight VRAM; `2` can improve TTS throughput on larger GPUs.                         |
| `TTS_CONCURRENCY`      | Worker-side parallel TTS request count.                                                                                   |
| `FASTER_WHISPER_MODEL` | Whisper model name for real ASR.                                                                                          |
| `PYANNOTE_PIPELINE`    | Pyannote diarization/VAD pipeline.                                                                                        |

## IndexTTS2

HoloDub supports three IndexTTS2 integration modes.

### Inline Mode

Recommended for a single local GPU box:

```env
ML_PYTHON_EXTRAS=real
ML_TTS_BACKEND=indextts2
INDEXTTS2_INLINE=true
INDEXTTS2_USE_EMO_TEXT=true
INDEXTTS2_DEFAULT_VOICE_RELPATH=voices/default_speaker.wav
```

| Variable                          | Notes                                                                                                                                |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `INDEXTTS2_MODEL_DIR`             | Local checkpoint directory. Leave empty to download on first run.                                                                    |
| `INDEXTTS2_ATTN_BACKEND`          | Empty/`sdpa` by default; `sage` or `flash` can be used when the image and GPU support them.                                          |
| `INDEXTTS2_USE_EMO_TEXT`          | Lets the adapter infer emotion from translated text.                                                                                 |
| `INDEXTTS2_DEFAULT_VOICE_RELPATH` | Fallback reference audio when no speaker-specific voice profile is bound. A Chinese reference clip usually improves Chinese prosody. |

### HTTP Mode

Use this when IndexTTS2 runs as a separate service:

```env
ML_TTS_BACKEND=indextts2
INDEXTTS2_ENDPOINT=http://your-indextts2-service:8000/tts
INDEXTTS2_API_KEY=
INDEXTTS2_MODEL=
```

### Command Mode

Use this for a local script adapter:

```env
ML_TTS_BACKEND=indextts2
INDEXTTS2_COMMAND=python run_tts.py --text "{text}" --duration "{duration}" --output "{output}"
```

## Segment Review

After `asr_smart`, the review agent can propose safe merges and optionally require a human gate before translation.

| Variable                                   | Notes                                                                   |
| ------------------------------------------ | ----------------------------------------------------------------------- |
| `SEGMENT_REVIEW_ENABLED`                   | Enables the review stage.                                               |
| `SEGMENT_REVIEW_USE_TOOLS`                 | Uses strict tool/function calling when the provider supports it.        |
| `SEGMENT_REVIEW_MODEL`                     | Review model. Falls back to `RETRANSLATION_MODEL`, then `OPENAI_MODEL`. |
| `SEGMENT_REVIEW_REQUIRE_HUMAN`             | When true, jobs pause in `awaiting_review` for manual confirmation.     |
| `SEGMENT_REVIEW_AUTO_APPLY`                | Auto-applies high-confidence safe merges.                               |
| `SEGMENT_REVIEW_AUTO_APPLY_MIN_CONFIDENCE` | Confidence threshold for automatic merge application.                   |
| `SEGMENT_REVIEW_MAX_MERGE_GAP_MS`          | Maximum gap between segments considered for safe merge.                 |
| `SEGMENT_REVIEW_MAX_MERGED_DURATION_MS`    | Maximum duration after merge.                                           |
| `SEGMENT_REVIEW_MAX_MERGE_SEGMENTS`        | Maximum number of source segments in one merge proposal.                |

## Chapter, Episode, And Judge Features

The current pipeline includes longer-form media controls:

- `CHAPTERIZE_ENABLED`: split long episodes into chapter jobs.
- `CHAPTERIZE_LLM_DRIVEN`: use the glossary/chapterization LLM path instead of the deterministic DP fallback.
- `CHAPTER_REVIEW_LLM_ENABLED`: generate bilingual chapter titles and cut summaries.
- `CHAPTER_JUDGE_MODEL`: asynchronous chapter-level quality judge.
- `EPISODE_JUDGE_MODEL`: asynchronous episode-level quality judge.
- `EPISODE_MERGE_ENABLED`: merge completed chapters into episode-level output.
- `REWORK_ENGINE_LEVEL`: closed-loop rework dispatch level; default `none` keeps rework observe-only.

## Audio Normalization

HoloDub can normalize loudness with ffmpeg `loudnorm`:

- `LOUDNORM_TARGET_I`: integrated loudness target.
- `LOUDNORM_TARGET_TP`: true-peak target.
- `LOUDNORM_TARGET_LRA`: loudness range target.
- `LOUDNORM_CHAPTER_ENABLED`: normalize chapter output before muxing.
- `LOUDNORM_MASTER_ENABLED`: normalize the final episode master.

## Frontend Development

```bash
cd ui
npm install
npm run dev
```

Build the embedded UI:

```bash
cd ui
npm run build
```

The build output is embedded from `internal/ui/static` by the Go binary.
