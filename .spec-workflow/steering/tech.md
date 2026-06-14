# HoloDub — Technology Architecture

## Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Control Plane | Go 1.25+ (Gin + GORM) | High concurrency, single binary deploy, Redis-backed job queue |
| ML Inference | Python 3.10+ (FastAPI + PyTorch) | Ecosystem: Faster-Whisper, IndexTTS2, Demucs, Pyannote all Python-native |
| Database | PostgreSQL | JSONB for flexible metadata (judge scores, dubbing plans, rework history) |
| Queue | Redis | Stage leases, retry queues, future fair scheduler |
| Frontend | Vue 3 + Vite + Tailwind | Lightweight SPA, embedded in Go binary via embed.FS |
| Deployment | Docker Compose | 5 containers: api, worker, ml-service, postgres, redis |
| TTS Engine | IndexTTS2 (FP16) | Duration-aware zero-shot TTS, RTX 5080 validated |
| ASR | Faster-Whisper large-v3 | Word-level timestamps for semantic splitting |
| LLM Provider | Alibaba DashScope / OpenAI-compatible | qwen-turbo/plus/max, kimi-k2.5, deepseek-v3 |

## Architecture Diagram

```
User (Vue SPA)
    │
    ▼
┌──────────┐    ┌──────────┐    ┌───────────┐
│  API      │───▶│  Redis   │◀───│  Worker    │
│  (Gin)    │    │  Queue   │    │  (Pipeline)│
└──────────┘    └──────────┘    └─────┬──────┘
    │                                 │
    ▼                                 ▼
┌──────────┐                   ┌───────────┐
│PostgreSQL│                   │ ML Service│
│ (GORM)   │                   │ (FastAPI) │
└──────────┘                   └───────────┘
```

## Key Design Decisions

### 1. Two-language split (Go + Python)
Go handles orchestration (pipeline state machine, job queue, API), Python handles ML inference (ASR, TTS, VAD). Communication via HTTP (ml-service REST API). Rationale: Go's concurrency model is superior for pipeline orchestration; Python's ML ecosystem is irreplaceable.

### 2. Pipeline as explicit state machine
7 stages: media → separate → asr_smart → segment_review → translate → tts_duration → merge. Each stage is a discrete queue task with lease-based mutual exclusion. The state machine is in `internal/pipeline/pipeline.go::runPipeline()`.

### 3. Episode → Chapter → Segment hierarchy
Long videos (>20min) are automatically chapterized (LLM-driven semantic boundary detection with DP fallback). Each chapter becomes an independent Job that runs in parallel. Episode-level stages (media, separate, ASR, glossary) run once for the whole video.

### 4. LLM function calling over prompt+JSON parse
All LLM interactions use strict tool calling (OpenAI-compatible) for structured output: translation plans, judge verdicts, chapter boundaries, glossary extraction. Fallback to prompt+JSON parse exists for degraded providers.

### 5. SegmentAgent ReAct pattern (OPT-201)
The TTS retry loop (180+ lines of inline logic) was refactored into a pure-function state machine: `Decide(state, obs, cfg) → Decision`. Fully unit-testable. Ensemble (multi-model fan-out) and hard drift caps layered on top.

### 6. Closed-loop rework engine (OPT-407)
Three-level Judge (Segment/Chapter/Episode) feeds into a decision table that triggers automatic retry, ensemble escalation, glossary broadcast, or human escalation. Cost ceiling and oscillation detection prevent infinite loops.

## Critical Environment Variables

| Variable | Purpose |
|----------|---------|
| `DASHSCOPE_API_KEY` | LLM provider (qwen, kimi models) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | Alternative LLM provider |
| `DATABASE_URL` | PostgreSQL connection |
| `REDIS_URL` | Redis connection |
| `ML_SERVICE_URL` | Python ML service endpoint |
| `HF_TOKEN` | HuggingFace (for Pyannote diarization) |
| `SEGMENT_AGENT_ENABLED` | Feature flag for OPT-201 |
| `REWORK_ENGINE_LEVEL` | none/segment/chapter/episode |
| `CHAPTERIZE_LLM_DRIVEN` | LLM vs DP chapterization |