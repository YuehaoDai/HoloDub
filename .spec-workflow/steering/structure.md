# HoloDub — Codebase Structure

## Directory Layout

```
HoloDub/
├── cmd/                        # Entry points
│   ├── api/main.go             # REST API server (Gin)
│   ├── worker/main.go          # Pipeline worker (Redis consumer)
│   ├── chapterize-bench/       # Offline chapterize benchmark CLI
│   └── migrate-output/         # One-shot output layout migration tool
│
├── internal/                   # Private Go packages
│   ├── pipeline/               # Core pipeline orchestration
│   │   ├── pipeline.go         # Service struct, stage dispatch, runPipeline
│   │   ├── stage_media.go      # Media extraction, audio probe
│   │   ├── stage_asr.go        # ASR smart split orchestration
│   │   ├── stage_translate.go  # LLM translation (two-pass: seed → full)
│   │   ├── stage_tts.go        # TTS synthesis + judge hook
│   │   ├── stage_tts_agent.go  # SegmentAgent adapter (OPT-201)
│   │   ├── stage_merge.go      # FFmpeg audio/video merge
│   │   ├── stage_chapterize.go # Chapter boundary detection + fan-out
│   │   ├── stage_glossary_extract.go  # Episode-level glossary extraction
│   │   ├── stage_episode_merge.go     # N-chapter concat
│   │   ├── stage_episode_judge.go     # Episode-level judge dispatch
│   │   ├── stage_episode_glossary_broadcast.go  # Cross-chapter glossary propagation
│   │   ├── judge_backfill.go   # Worker-startup judge gap filler
│   │   └── tts_stuck_backfill.go  # TTS-stuck segment recovery
│   │
│   ├── agents/                 # SegmentAgent ReAct (OPT-201)
│   │   ├── segment_agent.go    # Decide() pure function + Run() loop
│   │   ├── dubbing_tools.go    # DubbingTools interface (Synthesize/Retranslate/Judge)
│   │   ├── types.go            # State, Observation, Decision, Config
│   │   └── split.go            # SplitSourceText segment splitting
│   │
│   ├── rework/                 # Closed-loop rework engine (OPT-407)
│   │   ├── engine.go           # Engine: MaybeReworkSegment/Chapter/Episode
│   │   ├── decision.go         # Decide() → Action decision table
│   │   ├── convergence.go      # Cost accumulation, oscillation detection
│   │   └── types.go            # Action, ReworkAttempt, Level enums
│   │
│   ├── llm/                    # LLM client + operations
│   │   ├── client.go           # doChat, doChatTool, streaming, retry logic
│   │   ├── judge.go            # Segment-level judge (OPT-002)
│   │   ├── chapter_judge.go    # Chapter-level judge (OPT-409)
│   │   ├── episode_judge.go    # Episode-level judge (OPT-406)
│   │   ├── glossary.go         # Episode glossary extraction (OPT-402)
│   │   ├── dubbing_plan.go     # Structured dubbing plan (OPT-204)
│   │   ├── ensemble.go         # Multi-model ensemble (OPT-202)
│   │   ├── pricing.go          # LLM cost calculation
│   │   └── bench.go            # Offline benchmark tool call
│   │
│   ├── chapterize/             # Chapter boundary algorithms
│   │   ├── algo.go             # DP optimal cuts (Pass 1+2)
│   │   ├── llm_apply.go        # LLM plan validation + snap + hard-constraint
│   │   └── llm_review.go       # LLM chapter boundary review (Pass 3)
│   │
│   ├── models/                 # GORM data models
│   │   └── models.go           # Job, Segment, Episode, VoiceProfile, enums
│   │
│   ├── store/                  # Database access layer
│   │   └── store.go            # GORM CRUD + transaction helpers
│   │
│   ├── config/                 # Configuration loading
│   │   └── config.go           # Env parsing, defaults, .env loading
│   │
│   ├── observability/          # Prometheus metrics
│   │   └── metrics.go          # Counters: LLM tokens, cost, drift, decisions
│   │
│   ├── ml/                     # ML service HTTP client
│   │   └── client.go           # ASR, TTS, VAD REST calls
│   │
│   ├── queue/                  # Redis job queue
│   │   └── queue.go            # Enqueue, Dequeue, stage leases
│   │
│   ├── media/                  # FFmpeg helpers
│   │   └── ffmpeg.go           # Slice, loudnorm, concat, probe
│   │
│   ├── http/                   # REST API routes
│   │   ├── router.go           # Gin router setup
│   │   └── router_*.go         # Per-resource route groups
│   │
│   ├── webhook/                # Webhook notifications
│   └── storage/                # File system helpers
│
├── ml_service/                 # Python ML inference service
│   ├── app/
│   │   ├── main.py             # FastAPI app entry
│   │   ├── config.py           # Python-side config
│   │   ├── models.py           # Pydantic request/response models
│   │   ├── runtime.py          # GPU/VRAM management
│   │   ├── routes/
│   │   │   ├── asr.py          # /asr/smart_split endpoint
│   │   │   ├── tts.py          # /tts/synthesize endpoint
│   │   │   ├── media.py        # Audio extraction
│   │   │   ├── health.py       # Health/readiness checks
│   │   │   └── admin.py        # Admin endpoints
│   │   ├── adapters/
│   │   │   ├── asr.py          # Faster-Whisper adapter + segment_words()
│   │   │   ├── tts.py          # IndexTTS2 adapter + prosody control
│   │   │   ├── vad.py          # Pyannote diarization (stub)
│   │   │   └── media.py        # FFmpeg audio extraction
│   │   ├── services/
│   │   │   ├── model_registry.py  # Model download/cache
│   │   │   ├── model_manifest.py  # Model metadata
│   │   │   └── gpu_guard.py       # GPU resource guard
│   │   └── storage.py          # File storage helpers
│   └── tests/
│
├── ui/                         # Vue 3 frontend
│   ├── src/
│   │   ├── api.ts              # API client + TypeScript types
│   │   ├── components/
│   │   │   ├── JobDetail.vue   # Job pipeline progress
│   │   │   ├── SegmentTable.vue  # Segment list with judge scores
│   │   │   ├── SegmentReview.vue # Merge/split review UI
│   │   │   ├── EpisodeDetail.vue # Episode-level dashboard
│   │   │   └── ...
│   │   └── composables/        # Vue composables (polling, etc.)
│   └── ...
│
├── migrations/                 # PostgreSQL migrations
│   ├── 001_*.sql through 012_*.sql
│
├── data/                       # Runtime data (gitignored)
│   ├── jobs/                   # Per-job: ASR JSON, TTS WAVs, merge outputs
│   ├── episodes/               # Per-episode: chapters, final output
│   └── hf-cache/               # HuggingFace model cache
│
├── tests/quality/              # Quality regression tests
├── docs/                       # Documentation
│   ├── roadmap/                # (ARCHIVED) optimization roadmap
│   └── production/             # Scale roadmap
├── docker/                     # Docker build files
├── scripts/                    # Utility scripts
├── .taskmaster/                # TaskMaster project management
└── .spec-workflow/             # SpecWorkflow structured development
```

## Key Patterns

### Stage State Machine
Each pipeline stage is a discrete function called from `runPipeline()`:
```go
func (s *Service) runPipeline(ctx context.Context, job *models.Job) error {
    switch job.CurrentStage {
    case StageMedia: return s.runMedia(ctx, job)
    case StageASRSmart: return s.runASRSmart(ctx, job)
    // ...
    }
}
```

### Async Judge Pattern
Judge calls run in detached goroutines after stage completion:
```go
go func() {
    ctx := context.Background()  // detached — survives worker restart
    s.maybeJudgeSegmentAsync(ctx, job, seg)
}()
```

### Feature Flag Pattern
New behavior gated behind env vars, old path preserved:
```go
if s.cfg.SegmentAgentEnabled {
    return s.runSegmentAgentV2WithHint(...)
}
return s.processOneTTSSegment(...)  // legacy path
```

### Partial UPDATE Pattern
Concurrent writers (judge + merge + rework) use partial UPDATE to avoid collisions:
```go
db.Model(&ep).Where("id = ?", id).Updates(map[string]interface{}{
    "episode_judge_score": score,
    "episode_judge_meta":  metaJSON,
})  // does NOT touch status/output/glossary
```